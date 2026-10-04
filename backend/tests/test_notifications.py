import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import database
from app.ai import euc_narrative
from app.ai.gateway import ai_gateway
from app.ai.merge_agent import assess_merge_request
from app.ai.provider import LLMProvider, ProviderResult
from app.config import settings
from app.repositories.commit_store import commit_semantic_delta
from app.repositories.merge_store import branch_context
from app.secret_store import encrypt_secret
from app.services.merge_service import MergeActor, merge_service


class FakeHighRiskProvider(LLMProvider):
    provider_name = "OPENROUTER"

    async def complete(self, **kwargs) -> ProviderResult:
        content = json.dumps({
            "answer": "This merge is risky.", "evidence": [], "confidence": 0.9, "insufficient_evidence": False,
            "recommended_actions": [{"title": "Owner decision", "rationale": "High blast radius changes.", "action_type": "REJECT", "risk_level": "HIGH"}],
            "warnings": [],
        })
        return ProviderResult(content, input_tokens=90, output_tokens=40, reasoning_tokens=0)


class FakeRiskDriftProvider(LLMProvider):
    provider_name = "OPENROUTER"

    async def complete(self, **kwargs) -> ProviderResult:
        content = json.dumps({
            "answer": "Introduces a formula pattern break.", "evidence": [], "confidence": 0.8,
            "insufficient_evidence": False, "recommended_actions": [], "warnings": [],
        })
        return ProviderResult(content, input_tokens=80, output_tokens=35, reasoning_tokens=0)


class NotificationCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "notifications.db"
        database.initialize_product_schema()
        self.user = database.get_or_create_user("ntf-user@example.com")

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_create_list_and_mark_read(self):
        first = database.create_notification(self.user["user_id"], "TEST_TYPE", "Title one", "Body one")
        database.create_notification(self.user["user_id"], "TEST_TYPE", "Title two", "Body two")

        all_items = database.list_notifications(self.user["user_id"])
        self.assertEqual(2, len(all_items))
        unread = database.list_notifications(self.user["user_id"], unread_only=True)
        self.assertEqual(2, len(unread))

        database.mark_notification_read(first["notification_id"], self.user["user_id"])
        unread_after = database.list_notifications(self.user["user_id"], unread_only=True)
        self.assertEqual(1, len(unread_after))

        with self.assertRaises(KeyError):
            database.mark_notification_read("NTF_DOES_NOT_EXIST", self.user["user_id"])

    def test_mark_all_read_is_scoped_to_caller(self):
        other = database.get_or_create_user("other-user@example.com")
        database.create_notification(self.user["user_id"], "TEST_TYPE", "Mine")
        database.create_notification(other["user_id"], "TEST_TYPE", "Not mine")

        marked = database.mark_all_notifications_read(self.user["user_id"])

        self.assertEqual(1, marked)
        self.assertEqual(0, len(database.list_notifications(self.user["user_id"], unread_only=True)))
        self.assertEqual(1, len(database.list_notifications(other["user_id"], unread_only=True)))

    def test_disabled_preference_suppresses_the_notification(self):
        prefs = database.get_notification_preferences(self.user["user_id"])
        self.assertTrue(prefs["DEVICE_BLOCKED"])  # default: enabled with no row

        database.set_notification_preference(self.user["user_id"], "DEVICE_BLOCKED", False)
        self.assertFalse(database.get_notification_preferences(self.user["user_id"])["DEVICE_BLOCKED"])

        result = database.create_notification(self.user["user_id"], "DEVICE_BLOCKED", "Should be suppressed")
        self.assertIsNone(result)
        self.assertEqual(0, len(database.list_notifications(self.user["user_id"])))

        # Other types are unaffected by a single type's opt-out.
        other_result = database.create_notification(self.user["user_id"], "MERGE_RISK_FLAGGED", "Should still arrive")
        self.assertIsNotNone(other_result)
        self.assertEqual(1, len(database.list_notifications(self.user["user_id"])))

    def test_device_blocked_notifies_the_devices_owner(self):
        device = database.record_device_fingerprint(
            self.user["user_id"], session_id=None, ip_address="203.0.113.9",
            user_agent="pytest", machine_id="LAPTOP-1",
        )

        database.set_device_trust_status(device["fingerprint_id"], "BLOCKED")

        notifications = database.list_notifications(self.user["user_id"])
        self.assertTrue(any(item["type"] == "DEVICE_BLOCKED" for item in notifications))

    def test_trusting_a_device_does_not_notify(self):
        device = database.record_device_fingerprint(
            self.user["user_id"], session_id=None, ip_address="203.0.113.9",
            user_agent="pytest", machine_id="LAPTOP-2",
        )

        database.set_device_trust_status(device["fingerprint_id"], "TRUSTED")

        notifications = database.list_notifications(self.user["user_id"])
        self.assertFalse(any(item["type"] == "DEVICE_BLOCKED" for item in notifications))


class MergeAndEucNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "notifications_ai.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("ntf-owner@example.com")
        self.editor = database.get_or_create_user("ntf-editor@example.com")
        database.save_user_ai_settings(self.editor["user_id"], encrypt_secret("sk-or-test-ntf"), settings.openrouter_model)
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_NTF" (ROW_ID INTEGER PRIMARY KEY, "TEAM" TEXT, "SCORE" INTEGER)')
        conn.executemany(
            'INSERT INTO "QUEUE_BOARD_NTF" (ROW_ID, "TEAM", "SCORE") VALUES (?, ?, ?)',
            [(i, f"TEAM{i}", 50) for i in range(1, 11)],
        )
        conn.commit()
        conn.close()
        registered = database.register_dataset("QUEUE_BOARD_NTF", self.owner["user_id"], "ntf.xlsx", 1, 1)
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (registered["repository_id"],)
        ).fetchone()[0]
        conn.close()
        # Give the editor membership so they can create a merge request against the owner's repo.
        conn = database._get_connection()
        conn.execute(
            "INSERT INTO REPOSITORY_MEMBERS (REPOSITORY_ID, USER_ID, ROLE, GRANTED_BY, CREATED_AT, UPDATED_AT) VALUES (?,?,?,?,?,?)",
            (registered["repository_id"], self.editor["user_id"], "editor", self.owner["user_id"], database._utcnow(), database._utcnow()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO ORGANIZATION_MEMBERS (ORGANIZATION_ID,USER_ID,STATUS,JOINED_AT,UPDATED_AT) VALUES (?,?,'ACTIVE',?,?)",
            (self.organization_id, self.editor["user_id"], database._utcnow(), database._utcnow()),
        )
        conn.commit()
        conn.close()
        from app.access_control.bootstrap import initialize_access_control
        conn = database._get_connection()
        initialize_access_control(conn, database._utcnow())
        # ai.agent.run is checked at ORGANIZATION scope (AI usage/quota is
        # org-wide) — grant the editor org-admin too so they can run their
        # own risk assessment in this test, distinct from the repository
        # owner who should receive the notification.
        conn.execute(
            "INSERT OR IGNORE INTO SECURITY_USER_ROLE_ASSIGNMENTS (ASSIGNMENT_ID,ORGANIZATION_ID,USER_ID,ROLE_ID,SCOPE_TYPE,SCOPE_ID,GRANTED_BY,CREATED_AT) VALUES (?,?,?,?, 'ORGANIZATION',?,?,?)",
            (f"URA_TEST_{self.editor['user_id']}", self.organization_id, self.editor["user_id"], "ROL_ORG_ADMIN",
             self.organization_id, self.owner["user_id"], database._utcnow()),
        )
        conn.commit()
        conn.close()
        self.repository_id = registered["repository_id"]
        self.main_branch_id = registered["main_branch_id"]
        self.table_id = "QUEUE_BOARD_NTF"
        self.actor = MergeActor(self.editor["user_id"], self.editor["email"])
        self._original_provider = ai_gateway.providers.get("OPENROUTER")

    def tearDown(self):
        if self._original_provider is not None:
            ai_gateway.providers["OPENROUTER"] = self._original_provider
        else:
            ai_gateway.providers.pop("OPENROUTER", None)
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _commit_cell(self, branch_id, table_id, value):
        snapshot = database.get_table_snapshot(table_id)
        sheet = snapshot["semantic"]["sheets"][0]
        row = sheet["rows"][0]
        column = next(item for item in sheet["columns"] if item["name"] == "TEAM")
        context = branch_context(branch_id)
        return commit_semantic_delta(
            table_id=table_id, repository_id=context["repository_id"], branch_id=branch_id,
            expected_head_commit_id=snapshot["head_commit_id"], base_version=snapshot["version"],
            changes=[{"operation_type": "CELL_VALUE_UPDATE", "sheet_id": sheet["sheet_id"], "row_id": row["row_id"], "column_id": column["column_id"], "new_value": value}],
            user_id=self.editor["user_id"], user_email=self.editor["email"], message="Set TEAM",
        )

    async def test_high_risk_merge_assessment_notifies_the_owner(self):
        # The deterministic engine's exact score for a given change set is
        # already covered by test_merge_risk_engine.py — what this test
        # verifies is the notification side-effect once assess_merge_request
        # concludes HIGH/CRITICAL, so the baseline is forced deterministically
        # rather than reverse-engineering a real change set that clears the
        # HIGH threshold (score >= 65) through the full three-way-merge path.
        from app.euc.intelligence.scoring.models import ScoreComponent, ScoreResult
        forced_score = ScoreResult("MERGE_RISK", (ScoreComponent("CONFLICTS", 70, 1.0, "forced for test", {}),))

        ai_gateway.providers["OPENROUTER"] = FakeHighRiskProvider()
        copy = database.create_working_copy(self.table_id, self.editor["user_id"], self.editor["email"])
        self._commit_cell(copy["branch_id"], copy["table_id"], "SRH")
        request = merge_service.create_request(
            source_branch_id=copy["branch_id"], target_branch_id=self.main_branch_id,
            title="Risky change", description=None, actor=self.actor,
        )

        with patch("app.ai.merge_agent.evaluate_merge_risk", return_value=forced_score):
            assessment = await assess_merge_request(self.organization_id, self.editor["user_id"], request["merge_request_id"], self.actor)

        self.assertEqual("HIGH", assessment["risk_level"])
        owner_notifications = database.list_notifications(self.owner["user_id"])
        self.assertTrue(any(item["type"] == "MERGE_RISK_FLAGGED" for item in owner_notifications))
        flagged = next(item for item in owner_notifications if item["type"] == "MERGE_RISK_FLAGGED")
        self.assertEqual("MERGE_REQUEST", flagged["resource_type"])
        self.assertEqual(request["merge_request_id"], flagged["resource_id"])

    async def test_low_risk_merge_assessment_does_not_notify(self):
        ai_gateway.providers["OPENROUTER"] = FakeHighRiskProvider()
        copy = database.create_working_copy(self.table_id, self.editor["user_id"], self.editor["email"])
        self._commit_cell(copy["branch_id"], copy["table_id"], "SRH")
        request = merge_service.create_request(
            source_branch_id=copy["branch_id"], target_branch_id=self.main_branch_id,
            title="Trivial change", description=None, actor=self.actor,
        )

        assessment = await assess_merge_request(self.organization_id, self.editor["user_id"], request["merge_request_id"], self.actor)

        self.assertEqual("LOW", assessment["risk_level"])
        owner_notifications = database.list_notifications(self.owner["user_id"])
        self.assertFalse(any(item["type"] == "MERGE_RISK_FLAGGED" for item in owner_notifications))


if __name__ == "__main__":
    unittest.main()
