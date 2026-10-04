import io
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from openpyxl import Workbook

from app import database
from app.observability import (
    new_request_context,
    record_audit_event,
    record_metric,
)
from app.repositories.commit_store import commit_semantic_delta
from app.repositories.governance_store import (
    cell_traceability,
    list_audit_events,
    operational_metrics,
    repository_insights,
    verify_audit_integrity,
    workbook_blame,
)
from app.repositories.merge_store import branch_context
from app.security_uploads import UnsafeWorkbookError, inspect_xlsx


class Stage4GovernanceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "stage4.db"
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute(
            'CREATE TABLE "QUEUE_BOARD_STAGE4" ('
            'ROW_ID INTEGER PRIMARY KEY, "PAYMENT_ID" TEXT, "AMOUNT" INTEGER)'
        )
        conn.execute(
            'INSERT INTO "QUEUE_BOARD_STAGE4" VALUES (1, "PAY-100", 125000)'
        )
        conn.commit()
        conn.close()
        database.initialize_product_schema()
        self.user = database.get_or_create_user("auditor@example.com")
        self.registered = database.register_dataset(
            "QUEUE_BOARD_STAGE4", self.user["user_id"], "payments.xlsx", 1, 2
        )
        new_request_context("REQ_TEST_STAGE4", "TRC_TEST_STAGE4")

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_audit_ledger_is_hash_chained_and_append_only(self):
        first = record_audit_event(
            "REPOSITORY_CREATED", actor_user_id=self.user["user_id"],
            repository_id=self.registered["repository_id"], payload={"name": "Payments"},
        )
        second = record_audit_event(
            "WORKBOOK_VALIDATED", actor_user_id="USR_SYSTEM", actor_type="SYSTEM",
            repository_id=self.registered["repository_id"], payload={"rows": 1},
        )

        integrity = verify_audit_integrity()
        events = list_audit_events(repository_id=self.registered["repository_id"])

        self.assertTrue(integrity["valid"])
        self.assertEqual(2, integrity["event_count"])
        self.assertEqual([second, first], [item["event_id"] for item in events])
        conn = sqlite3.connect(database.DB_PATH)
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE AUDIT_EVENTS SET STATUS='ALTERED' WHERE EVENT_ID=?", (first,))
        conn.close()

    def test_metrics_are_separate_and_aggregated(self):
        record_metric("commit_latency", 10, "ms")
        record_metric("commit_latency", 30, "ms", status="FAILED")

        result = operational_metrics(24)

        self.assertEqual(20, result["metrics"]["commit_latency"]["average"])
        self.assertEqual(1, result["metrics"]["commit_latency"]["failures"])

    def test_upload_inspection_accepts_xlsx_and_blocks_unsafe_zip_paths(self):
        stream = io.BytesIO()
        workbook = Workbook()
        workbook.active.append(["PAYMENT_ID", "AMOUNT"])
        workbook.active.append(["PAY-100", 125000])
        workbook.save(stream)

        profile = inspect_xlsx(
            stream.getvalue(), "payments.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertGreater(profile["zip_entries"], 3)

        malicious = io.BytesIO()
        with zipfile.ZipFile(malicious, "w") as archive:
            for name in ("[Content_Types].xml", "xl/workbook.xml", "_rels/.rels", "../escape"):
                archive.writestr(name, "x")
        with self.assertRaises(UnsafeWorkbookError):
            inspect_xlsx(malicious.getvalue(), "unsafe.xlsx", "application/zip")

    def test_workbook_blame_and_traceability_follow_stable_cell_identity(self):
        working_copy = database.create_working_copy(
            "QUEUE_BOARD_STAGE4", self.user["user_id"], self.user["email"]
        )
        snapshot = database.get_table_snapshot(working_copy["table_id"])
        sheet = snapshot["semantic"]["sheets"][0]
        row = sheet["rows"][0]
        amount = next(item for item in sheet["columns"] if item["name"] == "AMOUNT")
        context = branch_context(working_copy["branch_id"])
        result = commit_semantic_delta(
            table_id=working_copy["table_id"], repository_id=context["repository_id"],
            branch_id=working_copy["branch_id"], expected_head_commit_id=context["head_commit_id"],
            base_version=snapshot["version"],
            changes=[{"operation_type": "CELL_VALUE_UPDATE", "sheet_id": sheet["sheet_id"],
                      "row_id": row["row_id"], "column_id": amount["column_id"],
                      "new_value": 128500}],
            user_id=self.user["user_id"], user_email=self.user["email"],
            message="Correct settlement amount",
        )

        blame = workbook_blame(working_copy["branch_id"])
        cell = next(item for item in blame["cells"] if item["column_id"] == amount["column_id"])
        trace = cell_traceability(
            working_copy["branch_id"], sheet["sheet_id"], row["row_id"], amount["column_id"]
        )

        self.assertEqual(128500, cell["value"])
        self.assertEqual(result["commit_id"], cell["last_commit_id"])
        self.assertEqual(self.user["email"], cell["last_author_email"])
        self.assertEqual(1, len(trace["history"]))
        insights = repository_insights(context["repository_id"])
        self.assertEqual(1, insights["version_control"]["commits"])
        self.assertEqual(1, insights["version_control"]["cell_changes"])
        # No macro source was ever registered for this repository -- the
        # governance block must default cleanly, not error or omit itself.
        self.assertEqual({
            "has_macros": False, "total_macros": 0, "runnable": 0,
            "blocked_external": 0, "blocked_unsupported": 0,
            "sql_lane": 0, "interpreted_lane": 0,
            "runs_last_30d": 0, "most_run_macro": None, "last_run_at": None,
        }, insights["macro_governance"])


if __name__ == "__main__":
    unittest.main()

