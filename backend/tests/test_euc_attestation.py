import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import database
from app.euc import attestation
from app.euc.branch_comparison import snapshot_branch_for_comparison
from app.euc.continuous_assurance import rescore_repository_after_merge
from app.repositories.commit_store import commit_semantic_delta
from app.repositories.merge_store import branch_context
from app.services.merge_service import MergeActor, merge_service


class EucAttestationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "attestation.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("attest-owner@example.com")
        self.editor = database.get_or_create_user("attest-editor@example.com")

        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_ATT" (ROW_ID INTEGER PRIMARY KEY, "SCORE" INTEGER, "CALC" INTEGER)')
        conn.executemany(
            'INSERT INTO "QUEUE_BOARD_ATT" (ROW_ID, "SCORE", "CALC") VALUES (?, ?, ?)',
            [(i, i * 10, 0) for i in range(1, 4)],
        )
        conn.commit()
        conn.close()
        registered = database.register_dataset("QUEUE_BOARD_ATT", self.owner["user_id"], "attest.xlsx", 3, 2)
        database.add_dataset_member("QUEUE_BOARD_ATT", self.owner["user_id"], self.editor["email"], "editor")
        self.repository_id = registered["repository_id"]
        self.main_branch_id = registered["main_branch_id"]
        self.table_id = "QUEUE_BOARD_ATT"

        self._set_formulas(self.main_branch_id, self.table_id, ["=SCORE*2", "=SCORE*2", "=SCORE*2"])
        baseline = snapshot_branch_for_comparison(
            self.repository_id, self.main_branch_id, "main", self.table_id, self.owner["user_id"],
        )
        self.euc_id = baseline["euc_id"]

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

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

    def test_only_the_repository_owner_can_submit(self):
        with self.assertRaises(PermissionError):
            attestation.submit_attestation(self.euc_id, self.editor["user_id"], "I have reviewed this workbook.")

    def test_short_statements_are_rejected(self):
        with self.assertRaises(ValueError):
            attestation.submit_attestation(self.euc_id, self.owner["user_id"], "ok")

    def test_submitting_snapshots_the_current_findings_state_and_records_an_audit_event(self):
        result = attestation.submit_attestation(
            self.euc_id, self.owner["user_id"],
            "I have reviewed this EUC's open findings; residual risk is acceptable for this quarter.",
        )

        self.assertEqual(self.euc_id, result["euc_id"])
        self.assertEqual(self.owner["user_id"], result["submitted_by"])
        self.assertIsNotNone(result["submitted_at"])

        history = attestation.list_attestations(self.euc_id, self.owner["user_id"])
        self.assertEqual(1, len(history))
        self.assertEqual(result["attestation_id"], history[0]["attestation_id"])

    def test_a_fresh_asset_with_no_attestation_is_not_immediately_overdue(self):
        status = attestation.get_attestation_status(self.euc_id, self.owner["user_id"])

        self.assertFalse(status["is_overdue"])
        self.assertIsNone(status["latest"])
        self.assertTrue(status["can_attest"])

    def test_an_attestation_older_than_the_interval_is_overdue(self):
        attestation.submit_attestation(self.euc_id, self.owner["user_id"], "Reviewed and accepted for this cycle.")
        conn = database._get_connection()
        try:
            stale = (datetime.now(timezone.utc) - timedelta(days=attestation.ATTESTATION_INTERVAL_DAYS + 1)).isoformat()
            conn.execute("UPDATE EUC_ATTESTATIONS SET SUBMITTED_AT=? WHERE EUC_ID=?", (stale, self.euc_id))
            conn.commit()
        finally:
            conn.close()

        status = attestation.get_attestation_status(self.euc_id, self.owner["user_id"])
        self.assertTrue(status["is_overdue"])

    def test_attestation_history_survives_a_re_ingested_asset(self):
        """EUC_ASSETS are content-addressed -- a merge creates a brand new
        EUC_ID. Attestation history must stay attached to the repository,
        not silently orphan itself on the old, now-superseded asset."""
        attestation.submit_attestation(self.euc_id, self.owner["user_id"], "Reviewed the original snapshot.")

        copy = database.create_working_copy(self.table_id, self.owner["user_id"], self.owner["email"])
        self._set_formulas(copy["branch_id"], copy["table_id"], ["=SCORE*3", "=SCORE*3", "=SCORE*3"])
        request = merge_service.create_request(
            source_branch_id=copy["branch_id"], target_branch_id=self.main_branch_id,
            title="Change multiplier", description="test", actor=MergeActor(self.owner["user_id"], self.owner["email"]),
        )
        merge_service.review(
            merge_request_id=request["merge_request_id"], decision="APPROVED",
            comment="self-review", actor=MergeActor(self.owner["user_id"], self.owner["email"]),
        )
        merge_service.merge(request["merge_request_id"], MergeActor(self.owner["user_id"], self.owner["email"]))

        conn = database._get_connection()
        try:
            new_euc_id = conn.execute(
                "SELECT EUC_ID FROM EUC_ASSETS WHERE REPOSITORY_ID=? ORDER BY UPDATED_AT DESC LIMIT 1",
                (self.repository_id,),
            ).fetchone()["EUC_ID"]
        finally:
            conn.close()
        self.assertNotEqual(self.euc_id, new_euc_id, "the merge should have produced a new content-addressed asset")

        status = attestation.get_attestation_status(new_euc_id, self.owner["user_id"])
        self.assertIsNotNone(status["latest"], "attestation history must carry over to the new asset via the repository")
        self.assertFalse(status["is_overdue"])

    def test_continuous_assurance_notifies_the_owner_when_attestation_is_overdue(self):
        attestation.submit_attestation(self.euc_id, self.owner["user_id"], "Initial attestation for this workbook.")
        conn = database._get_connection()
        try:
            stale = (datetime.now(timezone.utc) - timedelta(days=attestation.ATTESTATION_INTERVAL_DAYS + 1)).isoformat()
            conn.execute("UPDATE EUC_ATTESTATIONS SET SUBMITTED_AT=? WHERE REPOSITORY_ID=?", (stale, self.repository_id))
            conn.commit()
        finally:
            conn.close()

        copy = database.create_working_copy(self.table_id, self.owner["user_id"], self.owner["email"])
        self._set_formulas(copy["branch_id"], copy["table_id"], ["=SCORE*4", "=SCORE*4", "=SCORE*4"])
        request = merge_service.create_request(
            source_branch_id=copy["branch_id"], target_branch_id=self.main_branch_id,
            title="Change multiplier again", description="test", actor=MergeActor(self.owner["user_id"], self.owner["email"]),
        )
        merge_service.review(
            merge_request_id=request["merge_request_id"], decision="APPROVED",
            comment="self-review", actor=MergeActor(self.owner["user_id"], self.owner["email"]),
        )
        merge_service.merge(request["merge_request_id"], MergeActor(self.owner["user_id"], self.owner["email"]))

        notifications = [
            item for item in database.list_notifications(self.owner["user_id"])
            if item["type"] == "EUC_ATTESTATION_OVERDUE"
        ]
        self.assertEqual(1, len(notifications))
        self.assertEqual(self.repository_id, notifications[0]["resource_id"])

    def test_portfolio_overview_ranks_overdue_first(self):
        overview = attestation.portfolio_attestation_overview(self.owner["user_id"])
        entry = next(item for item in overview["items"] if item["repository_id"] == self.repository_id)

        self.assertFalse(entry["is_overdue"])
        self.assertIsNone(entry["last_submitted_at"])
        self.assertEqual(1, overview["summary"]["never_attested_count"])

        attestation.submit_attestation(self.euc_id, self.owner["user_id"], "Reviewed and accepted.")
        overview_after = attestation.portfolio_attestation_overview(self.owner["user_id"])
        entry_after = next(item for item in overview_after["items"] if item["repository_id"] == self.repository_id)
        self.assertIsNotNone(entry_after["last_submitted_at"])
        self.assertEqual(0, overview_after["summary"]["never_attested_count"])


if __name__ == "__main__":
    unittest.main()
