import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database
from app.ai import merge_agent
from app.ai.gateway import ai_gateway
from app.ai.provider import LLMProvider, ProviderResult
from app.ai.service import ai_service
from app.config import settings
from app.repositories.merge_store import branch_context
from app.repositories.commit_store import commit_semantic_delta, get_commit
from app.secret_store import encrypt_secret
from app.services.merge_service import MergeActor, merge_service


class FakeAssessmentProvider(LLMProvider):
    provider_name = "OPENROUTER"

    def __init__(self, recommendation: str = "APPROVE", risk_level: str = "LOW"):
        self.recommendation = recommendation
        self.risk_level = risk_level
        self.calls = 0

    async def complete(self, **kwargs) -> ProviderResult:
        self.calls += 1
        content = json.dumps({
            "answer": "Overall risk assessment for this merge request.",
            "evidence": [], "confidence": 0.85, "insufficient_evidence": False,
            "recommended_actions": [{
                "title": "Owner decision", "rationale": "Only low-risk cell edits with no open conflicts.",
                "action_type": self.recommendation, "risk_level": self.risk_level,
            }],
            "warnings": [],
        })
        return ProviderResult(content, input_tokens=60, output_tokens=25, reasoning_tokens=0)


class FakeFieldExplanationProvider(LLMProvider):
    provider_name = "OPENROUTER"

    def __init__(self, cleared_column: str, explanation: str, recommendation: str = "HOLD_FOR_REVIEW", risk_level: str = "MEDIUM"):
        self.cleared_column = cleared_column
        self.explanation = explanation
        self.recommendation = recommendation
        self.risk_level = risk_level
        self.calls = 0
        self.last_messages = None

    async def complete(self, **kwargs) -> ProviderResult:
        self.calls += 1
        self.last_messages = kwargs.get("messages")
        content = json.dumps({
            "answer": "Assessment with per-field explanations.",
            "evidence": [], "confidence": 0.7, "insufficient_evidence": False,
            "recommended_actions": [
                {"title": self.cleared_column, "rationale": self.explanation, "action_type": "FIELD_EXPLANATION", "risk_level": "LOW"},
                {"title": "Owner decision", "rationale": f"{self.cleared_column} was cleared; review before approving.",
                 "action_type": self.recommendation, "risk_level": self.risk_level},
            ],
            "warnings": [],
        })
        return ProviderResult(content, input_tokens=90, output_tokens=40, reasoning_tokens=0)


class ThinkTagWrappedProvider(LLMProvider):
    provider_name = "OPENROUTER"

    async def complete(self, **kwargs) -> ProviderResult:
        payload = json.dumps({
            "answer": "Approve; low risk.", "evidence": [], "confidence": 0.8,
            "insufficient_evidence": False,
            "recommended_actions": [{"title": "Owner decision", "rationale": "No conflicts.", "action_type": "APPROVE", "risk_level": "LOW"}],
            "warnings": [],
        })
        content = f"<think>Reasoning about the merge before answering.</think>{payload}"
        return ProviderResult(content, input_tokens=70, output_tokens=30, reasoning_tokens=15)


class FakeMergeAdvisorProvider(LLMProvider):
    provider_name = "OPENROUTER"

    def __init__(self, resolution_type: str = "ACCEPT_BRANCH", custom_value: str | None = None):
        self.resolution_type = resolution_type
        self.custom_value = custom_value
        self.calls = 0

    async def complete(self, **kwargs) -> ProviderResult:
        self.calls += 1
        rationale = "Branch made the only intentional change; safe to accept."
        if self.resolution_type == "CUSTOM":
            rationale = f"Neither side is fully correct. CUSTOM_VALUE: {self.custom_value}"
        content = json.dumps({
            "answer": "Recommend a resolution for this conflict.",
            "evidence": [],
            "confidence": 0.9,
            "insufficient_evidence": False,
            "recommended_actions": [{
                "title": "Apply recommended resolution", "rationale": rationale,
                "action_type": self.resolution_type, "risk_level": "LOW",
            }],
            "warnings": [],
        })
        return ProviderResult(content, input_tokens=80, output_tokens=30, reasoning_tokens=5)


class MergeConflictAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "merge_agent.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("agent-owner@example.com")
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_AGENT" (ROW_ID INTEGER PRIMARY KEY, "TEAM" TEXT, "SCORE" INTEGER)')
        conn.executemany(
            'INSERT INTO "QUEUE_BOARD_AGENT" (ROW_ID, "TEAM", "SCORE") VALUES (?, ?, ?)',
            [(1, "KKR", 10), (2, "CSK", 20)],
        )
        conn.commit()
        conn.close()
        database.save_user_ai_settings(self.owner["user_id"], encrypt_secret("sk-or-test-merge-agent"), settings.openrouter_model)
        registered = database.register_dataset("QUEUE_BOARD_AGENT", self.owner["user_id"], "agent.xlsx", 2, 2)
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (registered["repository_id"],)
        ).fetchone()[0]
        conn.close()
        self.main_branch_id = registered["main_branch_id"]
        self.copy = database.create_working_copy("QUEUE_BOARD_AGENT", self.owner["user_id"], self.owner["email"])
        self.actor = MergeActor(self.owner["user_id"], self.owner["email"])
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

    def _conflicted_request(self):
        self._commit_cell(self.copy["branch_id"], self.copy["table_id"], "TEAM", "SRH", self.actor)
        self._commit_cell(self.main_branch_id, "QUEUE_BOARD_AGENT", "TEAM", "MI", self.actor)
        return merge_service.create_request(
            source_branch_id=self.copy["branch_id"], target_branch_id=self.main_branch_id,
            title="Resolve team conflict", description=None, actor=self.actor,
        )

    def _clean_request(self):
        self._commit_cell(self.copy["branch_id"], self.copy["table_id"], "TEAM", "SRH", self.actor)
        return merge_service.create_request(
            source_branch_id=self.copy["branch_id"], target_branch_id=self.main_branch_id,
            title="Correct team name", description=None, actor=self.actor,
        )

    def _request_with_cleared_field(self):
        self._commit_cell(self.copy["branch_id"], self.copy["table_id"], "TEAM", "", self.actor)
        return merge_service.create_request(
            source_branch_id=self.copy["branch_id"], target_branch_id=self.main_branch_id,
            title="Clear team assignment", description=None, actor=self.actor,
        )

    async def test_analyze_proposes_a_suggestion_for_each_open_conflict(self):
        ai_gateway.providers["OPENROUTER"] = FakeMergeAdvisorProvider("ACCEPT_BRANCH")
        request = self._conflicted_request()

        result = await merge_agent.analyze_merge_request(
            self.organization_id, self.owner["user_id"], request["merge_request_id"], self.actor,
        )

        self.assertEqual("COMPLETED", result["status"])
        self.assertEqual(1, len(result["suggestions"]))
        suggestion = result["suggestions"][0]
        self.assertEqual("ACCEPT_BRANCH", suggestion["resolution_type"])
        self.assertGreater(suggestion["confidence"], 0)
        stored = merge_agent.list_suggestions(request["merge_request_id"])
        self.assertEqual(1, len(stored))
        self.assertEqual("PROPOSED", stored[0]["status"])

    async def test_second_analyze_call_skips_already_proposed_conflicts(self):
        ai_gateway.providers["OPENROUTER"] = FakeMergeAdvisorProvider("ACCEPT_BRANCH")
        request = self._conflicted_request()

        first = await merge_agent.analyze_merge_request(
            self.organization_id, self.owner["user_id"], request["merge_request_id"], self.actor,
        )
        second = await merge_agent.analyze_merge_request(
            self.organization_id, self.owner["user_id"], request["merge_request_id"], self.actor,
        )

        self.assertEqual(1, len(first["suggestions"]))
        self.assertEqual(0, len(second["suggestions"]))
        self.assertEqual(1, len(merge_agent.list_suggestions(request["merge_request_id"])))

    async def test_apply_ai_suggestion_uses_the_same_resolve_path_as_a_manual_resolution(self):
        ai_gateway.providers["OPENROUTER"] = FakeMergeAdvisorProvider("ACCEPT_BRANCH")
        request = self._conflicted_request()
        conflict_id = request["conflicts"][0]["conflict_id"]

        await merge_agent.analyze_merge_request(
            self.organization_id, self.owner["user_id"], request["merge_request_id"], self.actor,
        )
        prepared = merge_agent.prepare_apply_action(
            self.organization_id, self.owner["user_id"], request["merge_request_id"], conflict_id, self.actor,
        )
        self.assertEqual("PENDING_CONFIRMATION", prepared["status"])
        self.assertEqual("APPLY_MERGE_RESOLUTION", prepared["action_type"])

        confirmed = await ai_service.confirm_action(self.organization_id, self.owner["user_id"], prepared["action_id"])

        self.assertEqual("EXECUTED", confirmed["status"])
        updated_request = merge_service.get_request(request["merge_request_id"], self.actor)
        resolved_conflict = next(item for item in updated_request["conflicts"] if item["conflict_id"] == conflict_id)
        self.assertEqual("RESOLVED", resolved_conflict["status"])
        self.assertEqual("ACCEPT_BRANCH", resolved_conflict["resolution_type"])
        self.assertEqual(self.owner["user_id"], resolved_conflict["resolved_by"])
        applied_suggestion = next(
            item for item in merge_agent.list_suggestions(request["merge_request_id"])
            if item["conflict_id"] == conflict_id
        )
        self.assertEqual("APPLIED", applied_suggestion["status"])

        # Completing the merge request proves the AI-applied resolution is
        # indistinguishable from a manual one to the rest of the pipeline.
        merge_service.review(
            merge_request_id=request["merge_request_id"], decision="APPROVED",
            comment="AI-assisted resolution reviewed", actor=self.actor,
        )
        merged = merge_service.merge(request["merge_request_id"], self.actor)
        self.assertEqual("SRH", database.read_cell("QUEUE_BOARD_AGENT", "TEAM", 1))
        self.assertEqual("MERGE", get_commit(merged["commit_id"])["status"])

    async def test_custom_resolution_without_parseable_value_downgrades_to_manual_review(self):
        ai_gateway.providers["OPENROUTER"] = FakeMergeAdvisorProvider("CUSTOM", custom_value=None)
        # Force an unparsable rationale (no CUSTOM_VALUE line) by using a provider
        # whose rationale omits the marker entirely.
        class NoMarkerProvider(FakeMergeAdvisorProvider):
            async def complete(self, **kwargs) -> ProviderResult:
                self.calls += 1
                content = json.dumps({
                    "answer": "A custom fix is needed but details are unclear.",
                    "evidence": [], "confidence": 0.4, "insufficient_evidence": False,
                    "recommended_actions": [{
                        "title": "Needs a custom fix", "rationale": "This needs a bespoke value with no marker.",
                        "action_type": "CUSTOM", "risk_level": "MEDIUM",
                    }],
                    "warnings": [],
                })
                return ProviderResult(content, input_tokens=50, output_tokens=20, reasoning_tokens=0)
        ai_gateway.providers["OPENROUTER"] = NoMarkerProvider()
        request = self._conflicted_request()

        result = await merge_agent.analyze_merge_request(
            self.organization_id, self.owner["user_id"], request["merge_request_id"], self.actor,
        )

        self.assertEqual("MANUAL_REVIEW", result["suggestions"][0]["resolution_type"])
        self.assertIsNone(result["suggestions"][0]["custom_value"])

    async def test_record_resolution_outcome_runs_for_a_manual_resolution_too(self):
        request = self._conflicted_request()
        conflict_id = request["conflicts"][0]["conflict_id"]

        before = database._get_connection()
        try:
            before_count = before.execute("SELECT COUNT(*) FROM MERGE_RESOLUTION_KNOWLEDGE WHERE SOURCE='HISTORICAL'").fetchone()[0]
        finally:
            before.close()

        merge_service.resolve_conflict(
            merge_request_id=request["merge_request_id"], conflict_id=conflict_id,
            resolution_type="ACCEPT_BRANCH", custom_value=None, actor=self.actor,
        )

        after = database._get_connection()
        try:
            after_count = after.execute("SELECT COUNT(*) FROM MERGE_RESOLUTION_KNOWLEDGE WHERE SOURCE='HISTORICAL'").fetchone()[0]
        finally:
            after.close()
        self.assertEqual(before_count + 1, after_count)

    async def test_assessment_runs_for_a_clean_merge_request_with_no_open_conflicts(self):
        ai_gateway.providers["OPENROUTER"] = FakeAssessmentProvider("APPROVE", "LOW")
        request = self._clean_request()
        self.assertEqual(0, len(request["conflicts"]))

        result = await merge_agent.assess_merge_request(
            self.organization_id, self.owner["user_id"], request["merge_request_id"], self.actor,
        )

        self.assertEqual("APPROVE", result["recommendation"])
        self.assertEqual("LOW", result["risk_level"])
        self.assertGreater(result["confidence"], 0)
        stored = merge_agent.get_latest_assessment(request["merge_request_id"])
        self.assertIsNotNone(stored)
        self.assertEqual("APPROVE", stored["recommendation"])

    async def test_get_latest_assessment_returns_none_before_any_run(self):
        request = self._clean_request()
        self.assertIsNone(merge_agent.get_latest_assessment(request["merge_request_id"]))

    async def test_ai_reported_risk_level_is_ignored_in_favor_of_deterministic_score(self):
        # The fake provider claims HIGH risk, but the deterministic engine is
        # authoritative: one trivial cell edit with no conflicts/clearing/
        # formulas/structure/anomalies scores LOW regardless of what the
        # (free, sometimes unreliable) model says.
        ai_gateway.providers["OPENROUTER"] = FakeAssessmentProvider("HOLD_FOR_REVIEW", "HIGH")
        request = self._clean_request()

        result = await merge_agent.assess_merge_request(
            self.organization_id, self.owner["user_id"], request["merge_request_id"], self.actor,
        )

        self.assertEqual("LOW", result["risk_level"])
        self.assertGreater(len(result["risk_breakdown"]), 0)
        # The AI is still allowed to escalate the recommendation even when risk is LOW.
        self.assertEqual("HOLD_FOR_REVIEW", result["recommendation"])
        self.assertEqual("HOLD_FOR_REVIEW", result["ai_recommendation"])

    async def test_ai_cannot_downgrade_recommendation_below_deterministic_baseline(self):
        # An open conflict forces a HOLD_FOR_REVIEW baseline regardless of
        # risk score; even if the model says APPROVE, the stored
        # recommendation must not be laxer than the baseline.
        ai_gateway.providers["OPENROUTER"] = FakeAssessmentProvider("APPROVE", "LOW")
        request = self._conflicted_request()

        result = await merge_agent.assess_merge_request(
            self.organization_id, self.owner["user_id"], request["merge_request_id"], self.actor,
        )

        self.assertEqual("APPROVE", result["ai_recommendation"])
        self.assertNotEqual("APPROVE", result["recommendation"])
        self.assertIn(result["recommendation"], {"HOLD_FOR_REVIEW", "REJECT"})

    async def test_assessment_considers_open_conflicts_when_present(self):
        provider = FakeAssessmentProvider("HOLD_FOR_REVIEW", "HIGH")
        ai_gateway.providers["OPENROUTER"] = provider
        request = self._conflicted_request()

        result = await merge_agent.assess_merge_request(
            self.organization_id, self.owner["user_id"], request["merge_request_id"], self.actor,
        )

        self.assertIn(result["recommendation"], {"HOLD_FOR_REVIEW", "REJECT", "APPROVE"})
        self.assertEqual(1, provider.calls)

    async def test_assessment_investigates_and_explains_a_cleared_field_by_name(self):
        provider = FakeFieldExplanationProvider("TEAM", "The team assignment was intentionally cleared pending reassignment.")
        ai_gateway.providers["OPENROUTER"] = provider
        request = self._request_with_cleared_field()

        result = await merge_agent.assess_merge_request(
            self.organization_id, self.owner["user_id"], request["merge_request_id"], self.actor,
        )

        self.assertEqual(1, len(result["field_investigations"]))
        field = result["field_investigations"][0]
        self.assertEqual("TEAM", field["column_name"])
        self.assertEqual("KKR", field["previous_value"])
        self.assertEqual(self.owner["email"], field["changed_by"])
        self.assertEqual("The team assignment was intentionally cleared pending reassignment.", field["explanation"])
        self.assertEqual("HOLD_FOR_REVIEW", result["recommendation"])

        stored = merge_agent.get_latest_assessment(request["merge_request_id"])
        self.assertEqual(1, len(stored["field_investigations"]))
        self.assertEqual("TEAM", stored["field_investigations"][0]["column_name"])

    async def test_assessment_without_cleared_fields_investigates_nothing(self):
        ai_gateway.providers["OPENROUTER"] = FakeAssessmentProvider("APPROVE", "LOW")
        request = self._clean_request()

        result = await merge_agent.assess_merge_request(
            self.organization_id, self.owner["user_id"], request["merge_request_id"], self.actor,
        )

        self.assertEqual([], result["field_investigations"])


class ReasoningModelOutputParsingTests(unittest.TestCase):
    def test_parse_strips_think_tags_before_validating_json(self):
        from app.ai.gateway import StructuredOutputService
        wrapped = "<think>internal reasoning</think>{\"answer\": \"ok\", \"evidence\": [], \"confidence\": 0.5, \"insufficient_evidence\": false, \"recommended_actions\": [], \"warnings\": []}"
        parsed = StructuredOutputService.parse(wrapped)
        self.assertEqual("ok", parsed.answer)

    def test_safe_fallback_recovers_valid_json_instead_of_dumping_raw_text(self):
        from app.ai.gateway import StructuredOutputService
        wrapped = "<think>internal reasoning</think>{\"answer\": \"Approve; low risk.\", \"evidence\": [], \"confidence\": 0.8, \"insufficient_evidence\": false, \"recommended_actions\": [{\"title\": \"Owner decision\", \"rationale\": \"No conflicts.\", \"action_type\": \"APPROVE\", \"risk_level\": \"LOW\"}], \"warnings\": []}"
        recovered = StructuredOutputService.safe_fallback(wrapped)
        self.assertEqual("Approve; low risk.", recovered.answer)
        self.assertEqual(1, len(recovered.recommended_actions))
        self.assertEqual("APPROVE", recovered.recommended_actions[0].action_type)
        self.assertNotIn("unstructured answer", " ".join(recovered.warnings))

    def test_safe_fallback_still_falls_back_on_genuinely_unstructured_text(self):
        from app.ai.gateway import StructuredOutputService
        recovered = StructuredOutputService.safe_fallback("This is just prose with no JSON at all.")
        self.assertEqual("This is just prose with no JSON at all.", recovered.answer)
        self.assertIn("unstructured answer", " ".join(recovered.warnings))


if __name__ == "__main__":
    unittest.main()
