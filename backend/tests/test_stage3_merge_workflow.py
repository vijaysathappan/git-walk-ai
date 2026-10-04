import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database
from app.excel.merge_engine import three_way_merge
from app.excel.validation import validate_workbook
from app.repositories.commit_store import commit_semantic_delta, get_commit
from app.repositories.merge_store import branch_context
from app.services.merge_service import MergeActor, merge_service


class MergeEngineTests(unittest.TestCase):
    def test_same_cell_divergence_is_a_typed_conflict(self):
        base = {
            "sheets": [{"sheet_id": "SHEET_A", "name": "Data", "position": 0,
                "columns": [{"column_id": "COL_A", "name": "AMOUNT", "position": 0, "data_type": "INTEGER"}],
                "rows": [{"row_id": "ROW_A", "position": 0, "values": {"COL_A": 100}, "formulas": {}, "styles": {}, "comments": {}}]}]
        }
        main = copy.deepcopy(base)
        branch = copy.deepcopy(base)
        main["sheets"][0]["rows"][0]["values"]["COL_A"] = 120
        branch["sheets"][0]["rows"][0]["values"]["COL_A"] = 140

        result = three_way_merge(base, main, branch)

        self.assertEqual(1, len(result["conflicts"]))
        self.assertEqual("CELL_VALUE_CONFLICT", result["conflicts"][0]["conflict_type"])

    def test_formula_to_static_is_a_blocking_validation_error(self):
        base = {
            "sheets": [{"sheet_id": "SHEET_A", "name": "Data", "position": 0,
                "columns": [{"column_id": "COL_A", "name": "TOTAL", "position": 0, "data_type": "INTEGER"}],
                "rows": [{"row_id": "ROW_A", "position": 0, "values": {"COL_A": 100}, "formulas": {"COL_A": "=50+50"}, "styles": {}, "comments": {}}]}]
        }
        candidate = copy.deepcopy(base)
        candidate["sheets"][0]["rows"][0]["formulas"] = {}

        result = validate_workbook(candidate, base)

        self.assertEqual("FAILED", result["status"])
        self.assertIn("FORMULA_TO_STATIC", {item["rule_code"] for item in result["results"]})


class Stage3WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "stage3.db"
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute(
            'CREATE TABLE "QUEUE_BOARD_STAGE3" ('
            'ROW_ID INTEGER PRIMARY KEY, "TEAM" TEXT, "SCORE" INTEGER)'
        )
        conn.executemany(
            'INSERT INTO "QUEUE_BOARD_STAGE3" (ROW_ID, "TEAM", "SCORE") VALUES (?, ?, ?)',
            [(1, "KKR", 10), (2, "CSK", 20)],
        )
        conn.commit()
        conn.close()
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("maker@example.com")
        self.reviewer = database.get_or_create_user("checker@example.com")
        registered = database.register_dataset(
            "QUEUE_BOARD_STAGE3", self.owner["user_id"], "stage3.xlsx", 2, 2
        )
        database.add_dataset_member(
            "QUEUE_BOARD_STAGE3", self.owner["user_id"], self.reviewer["email"], "editor"
        )
        self.main_branch_id = registered["main_branch_id"]
        self.copy = database.create_working_copy(
            "QUEUE_BOARD_STAGE3", self.owner["user_id"], self.owner["email"]
        )
        self.maker = MergeActor(self.owner["user_id"], self.owner["email"])
        self.checker = MergeActor(self.reviewer["user_id"], self.reviewer["email"])

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _commit_cell(self, branch_id, table_id, column_name, value, actor):
        snapshot = database.get_table_snapshot(table_id)
        sheet = snapshot["semantic"]["sheets"][0]
        row = sheet["rows"][0]
        column = next(item for item in sheet["columns"] if item["name"] == column_name)
        context = branch_context(branch_id)
        return commit_semantic_delta(
            table_id=table_id, repository_id=context["repository_id"], branch_id=branch_id,
            expected_head_commit_id=snapshot["head_commit_id"], base_version=snapshot["version"],
            changes=[{"operation_type": "CELL_VALUE_UPDATE", "sheet_id": sheet["sheet_id"], "row_id": row["row_id"], "column_id": column["column_id"], "new_value": value}],
            user_id=actor.user_id, user_email=actor.email, message=f"Set {column_name}",
        )

    def test_clean_request_requires_owner_and_creates_two_parent_merge(self):
        self._commit_cell(
            self.copy["branch_id"], self.copy["table_id"], "TEAM", "SRH", self.maker
        )
        request = merge_service.create_request(
            source_branch_id=self.copy["branch_id"], target_branch_id=self.main_branch_id,
            title="Update team", description="Reviewed cricket correction", actor=self.maker,
        )
        self.assertEqual("PASSED", request["validation_status"])
        with self.assertRaises(PermissionError):
            merge_service.review(
                merge_request_id=request["merge_request_id"], decision="APPROVED",
                comment="editor review", actor=self.checker,
            )
        approved = merge_service.review(
            merge_request_id=request["merge_request_id"], decision="APPROVED",
            comment="Owner reviewed protected main", actor=self.maker,
        )
        self.assertEqual("APPROVED", approved["status"])

        merged = merge_service.merge(request["merge_request_id"], self.maker)

        self.assertEqual("SRH", database.read_cell("QUEUE_BOARD_STAGE3", "TEAM", 1))
        commit = get_commit(merged["commit_id"])
        self.assertEqual("MERGE", commit["status"])
        self.assertEqual(2, len(commit["parents"]))
        self.assertEqual("MERGED", branch_context(self.copy["branch_id"])["status"])
        with self.assertRaises(PermissionError):
            database.validate_working_copy(
                table_id=self.copy["table_id"], user_id=self.owner["user_id"],
                repository_id=self.copy["repository_id"], branch_id=self.copy["branch_id"],
                working_copy_id=self.copy["working_copy_id"], base_commit_id=self.copy["base_commit_id"],
                issued_at=self.copy["issued_at"], signature=self.copy["signature"],
            )
        reverted = merge_service.revert_commit(merged["commit_id"], self.maker)
        self.assertEqual("REVERT", get_commit(reverted["commit_id"])["status"])
        self.assertEqual(merged["commit_id"], get_commit(reverted["commit_id"])["reverts_commit_id"])
        self.assertEqual("KKR", database.read_cell("QUEUE_BOARD_STAGE3", "TEAM", 1))
        replacement = database.create_working_copy(
            "QUEUE_BOARD_STAGE3", self.owner["user_id"], self.owner["email"]
        )
        self.assertNotEqual(self.copy["branch_id"], replacement["branch_id"])

    def test_conflict_is_persisted_resolved_and_merged(self):
        self._commit_cell(
            self.copy["branch_id"], self.copy["table_id"], "TEAM", "SRH", self.maker
        )
        self._commit_cell(
            self.main_branch_id, "QUEUE_BOARD_STAGE3", "TEAM", "MI", self.maker
        )
        request = merge_service.create_request(
            source_branch_id=self.copy["branch_id"], target_branch_id=self.main_branch_id,
            title="Resolve team conflict", description=None, actor=self.maker,
        )
        self.assertEqual("CONFLICTED", request["status"])
        conflict = next(
            item for item in request["conflicts"]
            if item["conflict_type"] == "CELL_VALUE_CONFLICT"
        )
        resolved = merge_service.resolve_conflict(
            merge_request_id=request["merge_request_id"], conflict_id=conflict["conflict_id"],
            resolution_type="ACCEPT_BRANCH", custom_value=None, actor=self.maker,
        )
        self.assertEqual("RESOLVED", resolved["conflict_status"])
        merge_service.review(
            merge_request_id=request["merge_request_id"], decision="APPROVED",
            comment="Conflict resolution checked by owner", actor=self.maker,
        )

        merge_service.merge(request["merge_request_id"], self.maker)

        self.assertEqual("SRH", database.read_cell("QUEUE_BOARD_STAGE3", "TEAM", 1))

    def test_sync_main_preserves_non_conflicting_branch_work(self):
        divergence = merge_service.branch_divergence(
            self.copy["branch_id"], self.main_branch_id, self.maker
        )
        self.assertEqual(self.copy["branch_name"], divergence["source_branch_name"])
        self.assertEqual("main", divergence["target_branch_name"])

        self._commit_cell(
            self.copy["branch_id"], self.copy["table_id"], "TEAM", "SRH", self.maker
        )
        self._commit_cell(
            self.main_branch_id, "QUEUE_BOARD_STAGE3", "SCORE", 99, self.maker
        )

        result = merge_service.sync_branch(self.copy["branch_id"], self.maker)

        self.assertEqual("SYNCED", result["status"])
        self.assertEqual("SRH", database.read_cell(self.copy["table_id"], "TEAM", 1))
        self.assertEqual(99, database.read_cell(self.copy["table_id"], "SCORE", 1))
        self.assertEqual(2, len(get_commit(result["commit_id"])["parents"]))

    def test_commit_discards_noop_cell_updates(self):
        snapshot = database.get_table_snapshot(self.copy["table_id"])
        sheet = snapshot["semantic"]["sheets"][0]
        row = sheet["rows"][0]
        columns = {item["name"]: item for item in sheet["columns"]}

        result = commit_semantic_delta(
            table_id=self.copy["table_id"],
            repository_id=self.copy["repository_id"],
            branch_id=self.copy["branch_id"],
            expected_head_commit_id=snapshot["head_commit_id"],
            base_version=snapshot["version"],
            changes=[
                {
                    "operation_type": "CELL_VALUE_UPDATE",
                    "sheet_id": sheet["sheet_id"],
                    "row_id": row["row_id"],
                    "column_id": columns["TEAM"]["column_id"],
                    "new_value": "KKR",
                },
                {
                    "operation_type": "CELL_VALUE_UPDATE",
                    "sheet_id": sheet["sheet_id"],
                    "row_id": row["row_id"],
                    "column_id": columns["SCORE"]["column_id"],
                    "new_value": 15,
                },
            ],
            user_id=self.owner["user_id"],
            user_email=self.owner["email"],
            message="Keep only the effective score change",
        )

        self.assertEqual(1, result["change_count"])
        changes = get_commit(result["commit_id"])["changes"]
        self.assertEqual(1, len(changes))
        self.assertEqual(columns["SCORE"]["column_id"], changes[0]["column_id"])


if __name__ == "__main__":
    unittest.main()
