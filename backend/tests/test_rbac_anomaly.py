import asyncio
import json
import re
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import database
from app.access_control.bootstrap import ensure_repository_security
from app.ai import agent_runtime, rbac_anomaly
from app.ai.gateway import ai_gateway
from app.ai.provider import LLMProvider, ProviderResult
from app.config import settings
from app.secret_store import encrypt_secret


def _iso(days_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


_RATIONALE_BY_TYPE = {
    "DORMANT_GRANT": "This role has been dormant a long time -- consider revoking it.",
    "ROLE_ACTIVITY_MISMATCH": "This viewer has real commit history -- reconcile their role.",
}


class FakeRbacProvider(LLMProvider):
    provider_name = "OPENROUTER"

    async def complete(self, **kwargs) -> ProviderResult:
        # Each finding now gets its own unique deduplication_key rather than
        # sharing one dict slot per anomaly TYPE (two findings of the same
        # type used to silently overwrite each other's explanation) -- this
        # fake reads those real per-finding keys back out of the prompt
        # instead of hardcoding them, so it stays correct regardless of how
        # many findings of each type are detected.
        prompt = kwargs["messages"][-1]["content"]
        actions = []
        for key, anomaly_type in re.findall(r"- \[(.+?)\] \((.+?)\)", prompt):
            actions.append({
                "title": "Member", "rationale": _RATIONALE_BY_TYPE.get(anomaly_type, "Reviewed the flagged access pattern."),
                "action_type": key, "risk_level": "MEDIUM",
            })
        content = json.dumps({
            "answer": "Reviewed the flagged access patterns.", "evidence": [], "confidence": 0.7,
            "insufficient_evidence": False,
            "recommended_actions": actions,
            "warnings": [],
        })
        return ProviderResult(content, input_tokens=55, output_tokens=25, reasoning_tokens=0)


class RbacAnomalyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "rbac_anomaly.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("rbac-owner@example.com")
        self.member = database.get_or_create_user("rbac-member@example.com")
        database.save_user_ai_settings(self.owner["user_id"], encrypt_secret("sk-or-test-rbac"), settings.openrouter_model)

        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_RBAC" (ROW_ID INTEGER PRIMARY KEY, "TEAM" TEXT)')
        conn.execute('INSERT INTO "QUEUE_BOARD_RBAC" (ROW_ID, "TEAM") VALUES (1, "MI")')
        conn.commit()
        conn.close()
        registered = database.register_dataset("QUEUE_BOARD_RBAC", self.owner["user_id"], "rbac.xlsx", 1, 1)
        database.add_dataset_member("QUEUE_BOARD_RBAC", self.owner["user_id"], self.member["email"], "editor")
        self.repository_id = registered["repository_id"]
        self.table_id = "QUEUE_BOARD_RBAC"
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (self.repository_id,)
        ).fetchone()[0]
        conn.close()
        self._original_provider = ai_gateway.providers.get("OPENROUTER")

    def tearDown(self):
        if self._original_provider is not None:
            ai_gateway.providers["OPENROUTER"] = self._original_provider
        else:
            ai_gateway.providers.pop("OPENROUTER", None)
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _backdate_grant(self, user_id, days_ago):
        conn = database._get_connection()
        try:
            conn.execute(
                "UPDATE SECURITY_USER_ROLE_ASSIGNMENTS SET CREATED_AT=? WHERE ORGANIZATION_ID=? AND USER_ID=? AND SCOPE_TYPE='REPOSITORY' AND SCOPE_ID=?",
                (_iso(days_ago), self.organization_id, user_id, self.repository_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _insert_device(self, user_id, trust_status, first_seen_ago_hours, last_seen_ago_hours=0.0, machine_suffix=""):
        conn = database._get_connection()
        try:
            conn.execute(
                "INSERT INTO DEVICE_FINGERPRINTS VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    f"FGP_{uuid.uuid4().hex[:16].upper()}", user_id, None, "203.0.113.10", None,
                    f"MACHINE-{uuid.uuid4().hex[:8]}{machine_suffix}", "test-agent", trust_status,
                    (datetime.now(timezone.utc) - timedelta(hours=first_seen_ago_hours)).isoformat(),
                    (datetime.now(timezone.utc) - timedelta(hours=last_seen_ago_hours)).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    async def test_dormant_grant_and_role_mismatch_are_detected_and_explained(self):
        # A second, distinct user is a documented viewer but has real commit
        # history -- only possible if their role changed after they committed,
        # which is exactly the kind of drift this check exists to catch.
        contributor = database.get_or_create_user("rbac-contributor@example.com")
        database.add_dataset_member(self.table_id, self.owner["user_id"], contributor["email"], "editor")
        from app.repositories.commit_store import commit_semantic_delta
        from app.repositories.merge_store import branch_context
        copy = database.create_working_copy(self.table_id, contributor["user_id"], contributor["email"])
        snapshot = database.get_table_snapshot(copy["table_id"])
        sheet = snapshot["semantic"]["sheets"][0]
        column = next(c for c in sheet["columns"] if c["name"] == "TEAM")
        context = branch_context(copy["branch_id"])
        commit_semantic_delta(
            table_id=copy["table_id"], repository_id=context["repository_id"], branch_id=copy["branch_id"],
            expected_head_commit_id=snapshot["head_commit_id"], base_version=snapshot["version"],
            changes=[{"operation_type": "CELL_VALUE_UPDATE", "sheet_id": sheet["sheet_id"],
                      "row_id": sheet["rows"][0]["row_id"], "column_id": column["column_id"], "new_value": "RCB"}],
            user_id=contributor["user_id"], user_email=contributor["email"], message="Set value",
        )
        conn = database._get_connection()
        try:
            conn.execute("UPDATE REPOSITORY_MEMBERS SET ROLE='viewer' WHERE REPOSITORY_ID=? AND USER_ID=?", (self.repository_id, contributor["user_id"]))
            ensure_repository_security(conn, self.repository_id, database._utcnow())
            conn.commit()
        finally:
            conn.close()
        # Backdate the member's grant LAST -- ensure_repository_security()
        # above resyncs EVERY member's assignment row (including the
        # member's) whenever it runs, so backdating any earlier would be
        # silently overwritten back to "now".
        self._backdate_grant(self.member["user_id"], days_ago=30)

        ai_gateway.providers["OPENROUTER"] = FakeRbacProvider()
        result = await rbac_anomaly.scan_rbac_anomalies(self.organization_id, self.owner["user_id"], self.repository_id)

        types_found = {finding["anomaly_type"] for finding in result["findings"]}
        self.assertIn("DORMANT_GRANT", types_found)
        self.assertIn("ROLE_ACTIVITY_MISMATCH", types_found)
        dormant = next(f for f in result["findings"] if f["anomaly_type"] == "DORMANT_GRANT")
        self.assertEqual(self.member["user_id"], dormant["subject_user_id"])
        self.assertIn("revoking", dormant["explanation"].lower())

        run = agent_runtime.get_run(self.organization_id, self.owner["user_id"], result["agent_run_id"], can_audit=True)
        self.assertEqual("COMPLETED", run["run"]["status"])

        stored = rbac_anomaly.list_findings(self.repository_id)
        self.assertEqual(len(result["findings"]), len(stored))
        self.assertTrue(all(item["status"] == "OPEN" for item in stored))

    async def test_recently_granted_role_with_no_activity_is_not_yet_flagged(self):
        # Default fixture: member was just added, well within the grace period.
        ai_gateway.providers["OPENROUTER"] = FakeRbacProvider()
        result = await rbac_anomaly.scan_rbac_anomalies(self.organization_id, self.owner["user_id"], self.repository_id)
        self.assertFalse(any(f["anomaly_type"] == "DORMANT_GRANT" for f in result["findings"]))

    async def test_untrusted_device_recently_active_is_flagged(self):
        self._insert_device(self.member["user_id"], "UNKNOWN", first_seen_ago_hours=2, last_seen_ago_hours=1)
        ai_gateway.providers["OPENROUTER"] = FakeRbacProvider()

        result = await rbac_anomaly.scan_rbac_anomalies(self.organization_id, self.owner["user_id"], self.repository_id)

        self.assertTrue(any(f["anomaly_type"] == "UNTRUSTED_DEVICE_ACTIVE" for f in result["findings"]))

    async def test_trusted_device_is_never_flagged(self):
        self._insert_device(self.member["user_id"], "TRUSTED", first_seen_ago_hours=2, last_seen_ago_hours=1)
        ai_gateway.providers["OPENROUTER"] = FakeRbacProvider()

        result = await rbac_anomaly.scan_rbac_anomalies(self.organization_id, self.owner["user_id"], self.repository_id)

        self.assertFalse(any(f["anomaly_type"] == "UNTRUSTED_DEVICE_ACTIVE" for f in result["findings"]))

    async def test_multiple_new_devices_in_a_short_window_is_a_burst(self):
        for suffix in ("A", "B", "C"):
            self._insert_device(self.member["user_id"], "TRUSTED", first_seen_ago_hours=1, machine_suffix=suffix)
        ai_gateway.providers["OPENROUTER"] = FakeRbacProvider()

        result = await rbac_anomaly.scan_rbac_anomalies(self.organization_id, self.owner["user_id"], self.repository_id)

        self.assertTrue(any(f["anomaly_type"] == "MULTI_DEVICE_BURST" for f in result["findings"]))

    async def test_rescanning_an_unchanged_condition_does_not_duplicate_findings(self):
        self._backdate_grant(self.member["user_id"], days_ago=30)
        ai_gateway.providers["OPENROUTER"] = FakeRbacProvider()

        await rbac_anomaly.scan_rbac_anomalies(self.organization_id, self.owner["user_id"], self.repository_id)
        await rbac_anomaly.scan_rbac_anomalies(self.organization_id, self.owner["user_id"], self.repository_id)

        dormant = [f for f in rbac_anomaly.list_findings(self.repository_id) if f["anomaly_type"] == "DORMANT_GRANT"]
        self.assertEqual(1, len(dormant))

    async def test_dismissing_a_finding_records_a_precedent_and_updates_status(self):
        self._backdate_grant(self.member["user_id"], days_ago=30)
        ai_gateway.providers["OPENROUTER"] = FakeRbacProvider()
        result = await rbac_anomaly.scan_rbac_anomalies(self.organization_id, self.owner["user_id"], self.repository_id)
        finding_id = next(f["finding_id"] for f in result["findings"] if f["anomaly_type"] == "DORMANT_GRANT")

        decision = rbac_anomaly.resolve_finding(
            self.repository_id, finding_id, self.owner["user_id"], "DISMISSED", "Known contractor on planned leave.",
        )
        self.assertEqual("DISMISSED", decision["status"])

        stored = next(f for f in rbac_anomaly.list_findings(self.repository_id) if f["finding_id"] == finding_id)
        self.assertEqual("DISMISSED", stored["status"])

        conn = database._get_connection()
        try:
            precedent_count = conn.execute(
                "SELECT COUNT(*) FROM RBAC_ANOMALY_KNOWLEDGE WHERE SOURCE='HISTORICAL' AND ANOMALY_TYPE='DORMANT_GRANT'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(1, precedent_count)

    async def test_start_rbac_anomaly_scan_completes_in_the_background(self):
        self._backdate_grant(self.member["user_id"], days_ago=30)
        ai_gateway.providers["OPENROUTER"] = FakeRbacProvider()

        started = rbac_anomaly.start_rbac_anomaly_scan(self.organization_id, self.owner["user_id"], self.repository_id)
        self.assertEqual("RUNNING", started["status"])

        for _ in range(50):
            await asyncio.sleep(0.02)
            run = agent_runtime.get_run(self.organization_id, self.owner["user_id"], started["agent_run_id"], can_audit=True)
            if run["run"]["status"] != "RUNNING":
                break
        self.assertEqual("COMPLETED", run["run"]["status"])


if __name__ == "__main__":
    unittest.main()
