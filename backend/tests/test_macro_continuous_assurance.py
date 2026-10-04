"""Phase 5 USP4: Continuous Macro Assurance -- re-checks every runnable
macro's referenced columns/sheets against a branch's current schema
(mirrors app.euc.continuous_assurance's opt-in, notify-on-flip-only
pattern), and the merge_service hook that fires it after every merge."""

import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from vba_fixture_builder import build_xlsm  # noqa: E402

from app import database  # noqa: E402
from app.macros import continuous_assurance, run_service  # noqa: E402
from app.repositories.commit_store import commit_semantic_delta  # noqa: E402
from app.repositories.merge_store import branch_context  # noqa: E402
from app.services.merge_service import MergeActor, merge_service  # noqa: E402

RUNNABLE_MACRO = (
    "Public Sub Recalc()\n"
    "    Dim i As Long\n"
    "    For i = 2 To 4\n"
    "        Cells(i, 3).Value = Cells(i, 1).Value * Cells(i, 2).Value\n"
    "    Next i\n"
    "End Sub\n"
)


class MacroContinuousAssuranceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "macro_assurance.db"
        database.initialize_product_schema()

        self.owner = database.get_or_create_user(f"owner_{uuid.uuid4().hex[:8]}@example.com")
        self.table_id = "QUEUE_BOARD_MACROASSURE"
        conn = database._get_connection()
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{self.table_id}"')
            conn.execute(f'CREATE TABLE "{self.table_id}" (ROW_ID INTEGER PRIMARY KEY AUTOINCREMENT, QTY INTEGER, PRICE INTEGER, TOTAL INTEGER)')
            conn.executemany(
                f'INSERT INTO "{self.table_id}" (QTY, PRICE, TOTAL) VALUES (?, ?, ?)',
                [(2, 10, None), (3, 5, None), (1, 4, None)],
            )
            conn.commit()
        finally:
            conn.close()
        registered = database.register_dataset(self.table_id, self.owner["user_id"], "assure.xlsx", 3, 3)
        self.repository_id = registered["repository_id"]
        self.main_branch_id = registered["main_branch_id"]

        xlsm_bytes = build_xlsm([["Qty", "Price", "Total"]], "Module1", RUNNABLE_MACRO)
        run_service.register_source(self.table_id, "assure.xlsm", xlsm_bytes, self.owner["user_id"])
        run_service.extract(self.table_id, self.owner["user_id"])

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _delete_total_column(self):
        context = branch_context(self.main_branch_id)
        snapshot = database.get_table_snapshot(self.table_id)
        sheet = snapshot["semantic"]["sheets"][0]
        total_column = next(c for c in sheet["columns"] if c["name"] == "TOTAL")
        commit_semantic_delta(
            table_id=self.table_id, repository_id=context["repository_id"], branch_id=self.main_branch_id,
            expected_head_commit_id=context["head_commit_id"], base_version=snapshot["version"],
            changes=[{"operation_type": "COLUMN_DELETE", "sheet_id": sheet["sheet_id"], "column_id": total_column["column_id"]}],
            user_id=self.owner["user_id"], user_email=self.owner["email"], message="Remove Total column",
        )

    def test_no_runnable_macros_returns_none(self):
        conn = database._get_connection()
        try:
            conn.execute("UPDATE MACRO_DEFINITIONS SET STATIC_RISK='BLOCKED_UNSUPPORTED' WHERE REPOSITORY_ID=?", (self.repository_id,))
            conn.commit()
        finally:
            conn.close()
        result = continuous_assurance.rescore_macros_after_merge(self.repository_id, self.table_id, self.main_branch_id, self.owner["user_id"])
        self.assertIsNone(result)

    def test_macro_referencing_only_existing_columns_is_ok(self):
        result = continuous_assurance.rescore_macros_after_merge(self.repository_id, self.table_id, self.main_branch_id, self.owner["user_id"])
        self.assertEqual(1, result["checked_count"])
        self.assertEqual("OK", result["results"][0]["status"])
        self.assertEqual(0, result["newly_stale_count"])

        listing = run_service.list_macros(self.table_id, self.owner["user_id"])
        self.assertIsNone(listing["macros"][0]["assurance"])

    def test_removing_a_referenced_column_flags_stale_and_notifies_once(self):
        self._delete_total_column()

        result = continuous_assurance.rescore_macros_after_merge(self.repository_id, self.table_id, self.main_branch_id, self.owner["user_id"])
        self.assertEqual("STALE", result["results"][0]["status"])
        self.assertEqual(1, result["newly_stale_count"])
        self.assertIn("column 3", result["results"][0]["reasons"][0])

        listing = run_service.list_macros(self.table_id, self.owner["user_id"])
        assurance = listing["macros"][0]["assurance"]
        self.assertEqual("STALE", assurance["status"])

        notifications = database.list_notifications(self.owner["user_id"])
        stale_notes = [n for n in notifications if n["type"] == "MACRO_ASSURANCE_STALE"]
        self.assertEqual(1, len(stale_notes))

        # Re-checking again while STILL stale must NOT notify a second time.
        result2 = continuous_assurance.rescore_macros_after_merge(self.repository_id, self.table_id, self.main_branch_id, self.owner["user_id"])
        self.assertEqual(0, result2["newly_stale_count"])
        notifications_after = database.list_notifications(self.owner["user_id"])
        stale_notes_after = [n for n in notifications_after if n["type"] == "MACRO_ASSURANCE_STALE"]
        self.assertEqual(1, len(stale_notes_after))

    def test_merge_service_hook_fires_the_check_and_notifies_the_owner(self):
        # Integration test for the actual merge_service.merge() wiring --
        # the detection logic itself is already covered directly above.
        maker = MergeActor(self.owner["user_id"], self.owner["email"])
        copy = database.create_working_copy(self.table_id, self.owner["user_id"], self.owner["email"])
        snapshot = database.get_table_snapshot(copy["table_id"])
        sheet = snapshot["semantic"]["sheets"][0]
        total_column = next(c for c in sheet["columns"] if c["name"] == "TOTAL")
        context = branch_context(copy["branch_id"])
        commit_semantic_delta(
            table_id=copy["table_id"], repository_id=context["repository_id"], branch_id=copy["branch_id"],
            expected_head_commit_id=context["head_commit_id"], base_version=snapshot["version"],
            changes=[{"operation_type": "COLUMN_DELETE", "sheet_id": sheet["sheet_id"], "column_id": total_column["column_id"]}],
            user_id=self.owner["user_id"], user_email=self.owner["email"], message="Remove Total column on a branch",
        )
        request = merge_service.create_request(
            source_branch_id=copy["branch_id"], target_branch_id=self.main_branch_id,
            title="Remove Total column", description="test", actor=maker,
        )
        merge_service.review(merge_request_id=request["merge_request_id"], decision="APPROVED", comment="self-review", actor=maker)
        merge_service.merge(request["merge_request_id"], maker)

        listing = run_service.list_macros(self.table_id, self.owner["user_id"])
        self.assertEqual("STALE", listing["macros"][0]["assurance"]["status"])
        notifications = [n for n in database.list_notifications(self.owner["user_id"]) if n["type"] == "MACRO_ASSURANCE_STALE"]
        self.assertEqual(1, len(notifications))


if __name__ == "__main__":
    unittest.main()
