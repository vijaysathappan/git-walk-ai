import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database
from app.euc.dependency.services import DependencyService
from app.euc.intelligence import IntelligenceService
from app.euc.branch_comparison import snapshot_branch_for_comparison
from app.euc.portfolio import portfolio_risk_overview
from app.repositories.commit_store import commit_semantic_delta
from app.repositories.merge_store import branch_context
from app.services.merge_service import MergeActor, merge_service


class ContinuousAssuranceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "continuous_assurance.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("ca-owner@example.com")
        self.dependency_service = DependencyService()
        self.intelligence_service = IntelligenceService()
        self.maker = MergeActor(self.owner["user_id"], self.owner["email"])

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _make_repository(self, table_id):
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute(f'CREATE TABLE "{table_id}" (ROW_ID INTEGER PRIMARY KEY, "SCORE" INTEGER, "CALC" INTEGER)')
        conn.executemany(
            f'INSERT INTO "{table_id}" (ROW_ID, "SCORE", "CALC") VALUES (?, ?, ?)',
            [(i, i * 10, 0) for i in range(1, 6)],
        )
        conn.commit()
        conn.close()
        return database.register_dataset(table_id, self.owner["user_id"], f"{table_id.lower()}.xlsx", 5, 3)

    def _set_formulas(self, branch_id, table_id, formulas):
        snapshot = database.get_table_snapshot(table_id)
        sheet = snapshot["semantic"]["sheets"][0]
        rows = sorted(sheet["rows"], key=lambda item: item["position"])
        column = next(item for item in sheet["columns"] if item["name"] == "CALC")
        context = branch_context(branch_id)
        changes = [
            {"operation_type": "CELL_FORMULA_UPDATE", "sheet_id": sheet["sheet_id"],
             "row_id": row["row_id"], "column_id": column["column_id"], "new_formula": formula}
            for row, formula in zip(rows, formulas)
        ]
        return commit_semantic_delta(
            table_id=table_id, repository_id=context["repository_id"], branch_id=branch_id,
            expected_head_commit_id=snapshot["head_commit_id"], base_version=snapshot["version"],
            changes=changes, user_id=self.owner["user_id"], user_email=self.owner["email"],
            message="Set CALC formulas",
        )

    def test_merge_that_introduces_a_broken_reference_notifies_the_owner(self):
        registered = self._make_repository("QUEUE_BOARD_CA_TRACKED")
        repository_id = registered["repository_id"]
        main_branch_id = registered["main_branch_id"]
        table_id = "QUEUE_BOARD_CA_TRACKED"

        # Clean baseline on main: consistent formulas, no broken references.
        self._set_formulas(main_branch_id, table_id, ["=SCORE*2"] * 5)

        # Opt the repository into EUC governance with a fully-scored
        # baseline (Stage 2.1 -> 2.3) against main, before any merge happens.
        snapshot_branch_for_comparison(repository_id, main_branch_id, "main", table_id, self.owner["user_id"])

        # Branch, introduce a broken reference, and merge it into main.
        copy = database.create_working_copy(table_id, self.owner["user_id"], self.owner["email"])
        self._set_formulas(copy["branch_id"], copy["table_id"], [
            "=SCORE*2", "=SCORE*2", "=NOTASHEET!Z99", "=SCORE*2", "=SCORE*2",
        ])
        request = merge_service.create_request(
            source_branch_id=copy["branch_id"], target_branch_id=main_branch_id,
            title="Introduce broken reference", description="test", actor=self.maker,
        )
        merge_service.review(
            merge_request_id=request["merge_request_id"], decision="APPROVED",
            comment="self-review", actor=self.maker,
        )
        merge_service.merge(request["merge_request_id"], self.maker)

        notifications = [
            item for item in database.list_notifications(self.owner["user_id"])
            if item["type"] == "EUC_RISK_DRIFT"
        ]
        self.assertEqual(1, len(notifications))
        self.assertEqual(repository_id, notifications[0]["resource_id"])
        self.assertIn("up from", notifications[0]["body"])

        # Continuous Assurance re-scored the repository into a fresh,
        # content-addressed asset (Stage 2.1 never mutates a prior snapshot
        # in place) — the Portfolio Risk Command Center should immediately
        # reflect that new asset as SCORED with real findings, proving the
        # two initiatives compose correctly end to end.
        portfolio = portfolio_risk_overview(self.owner["user_id"])
        entry = next(item for item in portfolio["assets"] if item["repository_id"] == repository_id)
        self.assertEqual("SCORED", entry["status"])
        severe = entry["finding_severities"].get("HIGH", 0) + entry["finding_severities"].get("CRITICAL", 0)
        self.assertGreaterEqual(severe, 1)

    def test_merge_on_a_repository_never_opted_into_euc_governance_is_a_silent_noop(self):
        registered = self._make_repository("QUEUE_BOARD_CA_UNTRACKED")
        repository_id = registered["repository_id"]
        main_branch_id = registered["main_branch_id"]
        table_id = "QUEUE_BOARD_CA_UNTRACKED"

        copy = database.create_working_copy(table_id, self.owner["user_id"], self.owner["email"])
        self._set_formulas(copy["branch_id"], copy["table_id"], ["=SCORE*2"] * 5)
        request = merge_service.create_request(
            source_branch_id=copy["branch_id"], target_branch_id=main_branch_id,
            title="First formulas", description="test", actor=self.maker,
        )
        merge_service.review(
            merge_request_id=request["merge_request_id"], decision="APPROVED",
            comment="self-review", actor=self.maker,
        )
        merge_service.merge(request["merge_request_id"], self.maker)

        notifications = [
            item for item in database.list_notifications(self.owner["user_id"])
            if item["type"] == "EUC_RISK_DRIFT"
        ]
        self.assertEqual(0, len(notifications))


if __name__ == "__main__":
    unittest.main()
