import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import database
from app.access_control.service import assign_role
from app.ai.gateway import ai_gateway
from app.ai.provider import LLMProvider, ProviderResult
from app.config import settings
from app.repositories.merge_store import branch_context, get_reviewer_request
from app.repositories.commit_store import commit_semantic_delta
from app.secret_store import encrypt_secret
from app.services import reviewer_request_service
from app.services.merge_service import MergeActor, merge_service


class FakeAssessmentProvider(LLMProvider):
    provider_name = "OPENROUTER"

    def __init__(self, recommendation: str = "APPROVE"):
        self.recommendation = recommendation
        self.calls = 0

    async def complete(self, **kwargs) -> ProviderResult:
        self.calls += 1
        content = json.dumps({
            "answer": "Overall risk assessment for this merge request.",
            "evidence": [], "confidence": 0.85, "insufficient_evidence": False,
            "recommended_actions": [{
                "title": "Owner decision", "rationale": "Only low-risk cell edits with no open conflicts.",
                "action_type": self.recommendation, "risk_level": "LOW",
            }],
            "warnings": [],
        })
        return ProviderResult(content, input_tokens=60, output_tokens=25, reasoning_tokens=0)


class ReviewerRequestServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "reviewer_request.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("rr-owner@example.com")
        self.editor = database.get_or_create_user("rr-editor@example.com")
        database.save_user_ai_settings(self.owner["user_id"], encrypt_secret("sk-or-test-rr"), settings.openrouter_model)

        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_RR" (ROW_ID INTEGER PRIMARY KEY, "TEAM" TEXT, "SCORE" INTEGER)')
        conn.executemany(
            'INSERT INTO "QUEUE_BOARD_RR" (ROW_ID, "TEAM", "SCORE") VALUES (?, ?, ?)',
            [(1, "KKR", 10), (2, "CSK", 20)],
        )
        conn.commit()
        conn.close()
        registered = database.register_dataset("QUEUE_BOARD_RR", self.owner["user_id"], "rr.xlsx", 2, 2)
        database.add_dataset_member("QUEUE_BOARD_RR", self.owner["user_id"], self.editor["email"], "editor")
        self.repository_id = registered["repository_id"]
        self.main_branch_id = registered["main_branch_id"]
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (self.repository_id,)
        ).fetchone()[0]
        conn.close()
        # Being a dataset "editor" grants repository visibility only -- the
        # org-scoped RBAC system that gates merge_request.review/approve is
        # separate and needs its own explicit role assignment, same as any
        # real reviewer would need before being asked to review anything.
        assign_role(self.organization_id, "CHECKER", "REPOSITORY", self.repository_id, self.owner["user_id"], user_id=self.editor["user_id"])
        self.copy = database.create_working_copy("QUEUE_BOARD_RR", self.owner["user_id"], self.owner["email"])
        self.owner_actor = MergeActor(self.owner["user_id"], self.owner["email"])
        self.editor_actor = MergeActor(self.editor["user_id"], self.editor["email"])
        self._original_provider = ai_gateway.providers.get("OPENROUTER")

    def tearDown(self):
        if self._original_provider is not None:
            ai_gateway.providers["OPENROUTER"] = self._original_provider
        else:
            ai_gateway.providers.pop("OPENROUTER", None)
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

    def _clean_request(self, actor):
        self._commit_cell(self.copy["branch_id"], self.copy["table_id"], "TEAM", "SRH", actor)
        return merge_service.create_request(
            source_branch_id=self.copy["branch_id"], target_branch_id=self.main_branch_id,
            title="Update team", description=None, actor=actor,
        )

    def _expire_request(self, request_id):
        conn = database._get_connection()
        try:
            past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            conn.execute("UPDATE MERGE_REVIEWER_REQUESTS SET EXPIRES_AT=? WHERE REQUEST_ID=?", (past, request_id))
            conn.commit()
        finally:
            conn.close()

    async def test_request_reviewer_notifies_and_records_a_pending_row(self):
        request = self._clean_request(self.owner_actor)
        created = reviewer_request_service.request_reviewer(
            request["merge_request_id"], self.editor["user_id"], self.owner_actor,
        )
        self.assertEqual("PENDING", created["status"])
        self.assertEqual(self.editor["user_id"], created["reviewer_user_id"])

        notifications = database.list_notifications(self.editor["user_id"])
        self.assertTrue(any(item["type"] == "REVIEWER_REQUESTED" for item in notifications))

    async def test_a_second_pending_request_to_the_same_reviewer_is_rejected(self):
        request = self._clean_request(self.owner_actor)
        reviewer_request_service.request_reviewer(request["merge_request_id"], self.editor["user_id"], self.owner_actor)
        with self.assertRaises(ValueError):
            reviewer_request_service.request_reviewer(request["merge_request_id"], self.editor["user_id"], self.owner_actor)

    async def test_only_the_assigned_reviewer_can_respond(self):
        request = self._clean_request(self.owner_actor)
        created = reviewer_request_service.request_reviewer(request["merge_request_id"], self.editor["user_id"], self.owner_actor)
        with self.assertRaises(PermissionError):
            reviewer_request_service.respond(created["request_id"], "REJECTED", "not my call", self.owner_actor)

    async def test_reviewer_response_reuses_the_real_review_path_and_notifies_the_requester(self):
        request = self._clean_request(self.owner_actor)
        created = reviewer_request_service.request_reviewer(request["merge_request_id"], self.editor["user_id"], self.owner_actor)

        result = reviewer_request_service.respond(created["request_id"], "REJECTED", "found an issue", self.editor_actor)
        self.assertEqual("REJECTED", result["status"])

        stored = get_reviewer_request(created["request_id"])
        self.assertEqual("RESPONDED", stored["status"])
        self.assertEqual("REJECTED", stored["decision"])

        notifications = database.list_notifications(self.owner["user_id"])
        self.assertTrue(any(item["type"] == "REVIEWER_RESPONDED" for item in notifications))

        # Already responded -- a second response must be rejected outright.
        with self.assertRaises(ValueError):
            reviewer_request_service.respond(created["request_id"], "APPROVED", "changed my mind", self.editor_actor)

    async def test_ai_fallback_is_refused_before_the_sla_window_elapses(self):
        request = self._clean_request(self.owner_actor)
        created = reviewer_request_service.request_reviewer(request["merge_request_id"], self.editor["user_id"], self.owner_actor)
        with self.assertRaises(ValueError):
            await reviewer_request_service.use_ai_fallback(created["request_id"], self.owner_actor)

    async def test_ai_fallback_is_refused_to_anyone_other_than_the_requester(self):
        request = self._clean_request(self.owner_actor)
        created = reviewer_request_service.request_reviewer(request["merge_request_id"], self.editor["user_id"], self.owner_actor)
        self._expire_request(created["request_id"])
        with self.assertRaises(PermissionError):
            await reviewer_request_service.use_ai_fallback(created["request_id"], self.editor_actor)

    async def test_ai_fallback_applies_the_ai_recommendation_through_the_real_review_path(self):
        ai_gateway.providers["OPENROUTER"] = FakeAssessmentProvider("APPROVE")
        request = self._clean_request(self.owner_actor)
        created = reviewer_request_service.request_reviewer(request["merge_request_id"], self.editor["user_id"], self.owner_actor)
        self._expire_request(created["request_id"])

        result = await reviewer_request_service.use_ai_fallback(created["request_id"], self.owner_actor)
        self.assertEqual("APPROVED", result["status"])

        stored = get_reviewer_request(created["request_id"])
        self.assertEqual("AI_FALLBACK", stored["status"])
        self.assertEqual("APPROVED", stored["decision"])

    async def test_ai_fallback_refuses_to_force_a_decision_when_ai_is_not_confident(self):
        ai_gateway.providers["OPENROUTER"] = FakeAssessmentProvider("HOLD_FOR_REVIEW")
        request = self._clean_request(self.owner_actor)
        created = reviewer_request_service.request_reviewer(request["merge_request_id"], self.editor["user_id"], self.owner_actor)
        self._expire_request(created["request_id"])

        with self.assertRaises(ValueError):
            await reviewer_request_service.use_ai_fallback(created["request_id"], self.owner_actor)
        # Must still be PENDING -- a HOLD_FOR_REVIEW recommendation must never
        # silently mark the request as resolved.
        stored = get_reviewer_request(created["request_id"])
        self.assertEqual("PENDING", stored["status"])


if __name__ == "__main__":
    unittest.main()
