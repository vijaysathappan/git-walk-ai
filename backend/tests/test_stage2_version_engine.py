import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database
from app.excel.diff_engine import apply_deltas, semantic_diff
from app.repositories.commit_store import (
    BranchHeadChangedError,
    branch_metrics,
    commit_semantic_delta,
    get_cell_history,
    get_commit,
    reconstruct_branch,
)


class SemanticDiffTests(unittest.TestCase):
    def test_insert_shift_is_not_reported_as_mass_row_movement(self):
        before = {
            "sheets": [{
                "sheet_id": "SHEET_A", "name": "Data", "position": 0,
                "columns": [{"column_id": "COL_A", "name": "AMOUNT", "position": 0}],
                "rows": [
                    {"row_id": "ROW_A", "position": 0, "values": {"COL_A": 10}, "formulas": {}, "styles": {}, "comments": {}},
                    {"row_id": "ROW_B", "position": 1, "values": {"COL_A": 20}, "formulas": {}, "styles": {}, "comments": {}},
                ],
            }]
        }
        after = copy.deepcopy(before)
        after["sheets"][0]["rows"].insert(
            0,
            {"row_id": "ROW_NEW", "position": 0, "values": {"COL_A": 5}, "formulas": {}, "styles": {}, "comments": {}},
        )
        after["sheets"][0]["rows"][1]["position"] = 1
        after["sheets"][0]["rows"][2]["position"] = 2

        changes = semantic_diff(before, after)

        self.assertEqual(["ROW_INSERT"], [item["operation_type"] for item in changes])
        self.assertEqual(after, apply_deltas(before, changes))

    def test_formula_and_explicit_reorder_are_semantic_operations(self):
        before = {
            "sheets": [{
                "sheet_id": "SHEET_A", "name": "Data", "position": 0,
                "columns": [
                    {"column_id": "COL_A", "name": "A", "position": 0},
                    {"column_id": "COL_B", "name": "B", "position": 1},
                ],
                "rows": [
                    {"row_id": "ROW_A", "position": 0, "values": {"COL_A": 10, "COL_B": 20}, "formulas": {}, "styles": {}, "comments": {}},
                    {"row_id": "ROW_B", "position": 1, "values": {"COL_A": 30, "COL_B": 40}, "formulas": {}, "styles": {}, "comments": {}},
                ],
            }]
        }
        after = copy.deepcopy(before)
        after["sheets"][0]["rows"] = [after["sheets"][0]["rows"][1], after["sheets"][0]["rows"][0]]
        after["sheets"][0]["rows"][0]["position"] = 0
        after["sheets"][0]["rows"][1]["position"] = 1
        after["sheets"][0]["rows"][1]["formulas"]["COL_B"] = "=A2*2"

        operations = {item["operation_type"] for item in semantic_diff(before, after)}

        self.assertIn("ROW_MOVE", operations)
        self.assertIn("CELL_FORMULA_UPDATE", operations)
        self.assertNotIn("ROW_INSERT", operations)
        self.assertNotIn("ROW_DELETE", operations)

    def test_a_brand_new_sheet_diff_includes_its_columns_and_rows_not_just_sheet_create(self):
        # Regression test: a sheet present in `after` but absent from
        # `before` must diff its full column/row contents too, not just
        # emit a bare SHEET_CREATE -- this is exactly what silently
        # dropped a Virtual Run macro's new sheet during merge (merge()
        # recomputes its change list via semantic_diff(main, merged_state)
        # rather than reusing the branch's own commit's change list).
        before = {"sheets": [{
            "sheet_id": "SHEET_A", "name": "Data", "position": 0,
            "columns": [{"column_id": "COL_A", "name": "AMOUNT", "position": 0}],
            "rows": [{"row_id": "ROW_A", "position": 0, "values": {"COL_A": 10}, "formulas": {}, "styles": {}, "comments": {}}],
        }]}
        after = copy.deepcopy(before)
        after["sheets"].append({
            "sheet_id": "SHEET_B", "name": "Summary", "position": 1,
            "columns": [
                {"column_id": "COL_X", "name": "CATEGORY", "position": 0, "data_type": "TEXT"},
                {"column_id": "COL_Y", "name": "TOTAL", "position": 1, "data_type": "TEXT"},
            ],
            "rows": [
                {"row_id": "ROW_X", "position": 0, "values": {"COL_X": "Food", "COL_Y": 15}, "formulas": {}, "styles": {}, "comments": {}},
                {"row_id": "ROW_Y", "position": 1, "values": {"COL_X": "Travel", "COL_Y": 20}, "formulas": {}, "styles": {}, "comments": {}},
            ],
        })

        changes = semantic_diff(before, after)
        operations = [item["operation_type"] for item in changes]

        self.assertEqual(1, operations.count("SHEET_CREATE"))
        self.assertEqual(2, operations.count("COLUMN_INSERT"))
        self.assertEqual(2, operations.count("ROW_INSERT"))
        new_sheet_row_inserts = [c for c in changes if c["operation_type"] == "ROW_INSERT" and c["sheet_id"] == "SHEET_B"]
        self.assertEqual({"COL_X": "Food", "COL_Y": 15}, new_sheet_row_inserts[0]["new_value"])
        self.assertEqual({"COL_X": "Travel", "COL_Y": 20}, new_sheet_row_inserts[1]["new_value"])

        # Replaying the (now-complete) change list must reproduce `after` exactly.
        self.assertEqual(after, apply_deltas(before, changes))


class Stage2CommitTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "stage2.db"
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute(
            'CREATE TABLE "QUEUE_BOARD_STAGE2" ('
            'ROW_ID INTEGER PRIMARY KEY, "TEAM" TEXT, "SCORE" INTEGER)'
        )
        conn.executemany(
            'INSERT INTO "QUEUE_BOARD_STAGE2" (ROW_ID, "TEAM", "SCORE") VALUES (?, ?, ?)',
            [(1, "KKR", 10), (2, "CSK", 20)],
        )
        conn.commit()
        conn.close()
        database.initialize_product_schema()
        self.user = database.get_or_create_user("stage2@example.com")
        database.register_dataset(
            "QUEUE_BOARD_STAGE2", self.user["user_id"], "stage2.xlsx", 2, 2
        )
        self.copy = database.create_working_copy(
            "QUEUE_BOARD_STAGE2", self.user["user_id"], self.user["email"]
        )

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_atomic_delta_commit_reconstruction_concurrency_and_lineage(self):
        snapshot = database.get_table_snapshot(self.copy["table_id"])
        sheet = snapshot["semantic"]["sheets"][0]
        row = sheet["rows"][0]
        moved_row = sheet["rows"][1]
        team = next(column for column in sheet["columns"] if column["name"] == "TEAM")
        score = next(column for column in sheet["columns"] if column["name"] == "SCORE")
        changes = [
            {
                "operation_type": "CELL_VALUE_UPDATE", "sheet_id": sheet["sheet_id"],
                "row_id": row["row_id"], "column_id": team["column_id"], "new_value": "SRH",
            },
            {
                "operation_type": "CELL_FORMULA_UPDATE", "sheet_id": sheet["sheet_id"],
                "row_id": row["row_id"], "column_id": score["column_id"], "new_formula": "=10+5",
            },
            {
                "operation_type": "ROW_MOVE", "sheet_id": sheet["sheet_id"],
                "row_id": moved_row["row_id"], "previous_row_position": 1, "new_row_position": 0,
            },
        ]

        result = commit_semantic_delta(
            table_id=self.copy["table_id"], repository_id=self.copy["repository_id"],
            branch_id=self.copy["branch_id"], expected_head_commit_id=snapshot["head_commit_id"],
            base_version=snapshot["version"], changes=changes,
            user_id=self.user["user_id"], user_email=self.user["email"], message="Stage 2 delta",
        )

        self.assertEqual("SRH", database.read_cell(self.copy["table_id"], "TEAM", 1))
        self.assertEqual("KKR", database.read_cell("QUEUE_BOARD_STAGE2", "TEAM", 1))
        self.assertEqual(3, result["change_count"])
        commit = get_commit(result["commit_id"])
        self.assertEqual(3, len(commit["changes"]))
        rebuilt = reconstruct_branch(self.copy["branch_id"])
        self.assertEqual(moved_row["row_id"], rebuilt["sheets"][0]["rows"][0]["row_id"])
        rebuilt_row = next(item for item in rebuilt["sheets"][0]["rows"] if item["row_id"] == row["row_id"])
        self.assertEqual("SRH", rebuilt_row["values"][team["column_id"]])
        self.assertEqual("=10+5", rebuilt_row["formulas"][score["column_id"]])
        history = get_cell_history(
            self.copy["branch_id"], sheet["sheet_id"], row["row_id"], team["column_id"]
        )
        self.assertEqual(result["commit_id"], history[0]["commit_id"])
        self.assertEqual(1, branch_metrics(self.copy["branch_id"])["commits"])

        with self.assertRaises(BranchHeadChangedError):
            commit_semantic_delta(
                table_id=self.copy["table_id"], repository_id=self.copy["repository_id"],
                branch_id=self.copy["branch_id"], expected_head_commit_id=snapshot["head_commit_id"],
                base_version=snapshot["version"], changes=changes,
                user_id=self.user["user_id"], user_email=self.user["email"], message="Stale delta",
            )


if __name__ == "__main__":
    unittest.main()
