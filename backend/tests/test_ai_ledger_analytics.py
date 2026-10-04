import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database
from app.ai import commit_review, ledger_analytics
from app.ai.gateway import ai_gateway
from app.ai.provider import AIProviderError, LLMProvider, ProviderResult
from app.ai.merge_agent import analyze_merge_request
from app.config import settings
from app.repositories.commit_store import commit_semantic_delta
from app.repositories.merge_store import branch_context
from app.secret_store import encrypt_secret
from app.services.merge_service import MergeActor, merge_service


class FakeSuccessProvider(LLMProvider):
    provider_name = "OPENROUTER"

    async def complete(self, **kwargs) -> ProviderResult:
        content = json.dumps({
            "answer": "Recommendation.", "evidence": [], "confidence": 0.8, "insufficient_evidence": False,
            "recommended_actions": [{"title": "Decision", "rationale": "Looks fine.", "action_type": "KEEP_MAIN", "risk_level": "LOW"}],
            "warnings": [],
        })
        return ProviderResult(content, input_tokens=120, output_tokens=45, reasoning_tokens=0)


class FakeCommitSuccessProvider(LLMProvider):
    provider_name = "OPENROUTER"

    async def complete(self, **kwargs) -> ProviderResult:
        content = json.dumps({
            "answer": "Review.", "evidence": [], "confidence": 0.75, "insufficient_evidence": False,
            "recommended_actions": [{"title": "Author summary", "rationale": "Low risk.", "action_type": "LOOKS_GOOD", "risk_level": "LOW"}],
            "warnings": [],
        })
        return ProviderResult(content, input_tokens=80, output_tokens=30, reasoning_tokens=0)


class FailingProvider(LLMProvider):
    provider_name = "OPENROUTER"

    async def complete(self, **kwargs) -> ProviderResult:
        raise AIProviderError("SIMULATED_FAILURE", "Simulated provider outage", transient=False)


class AILedgerAnalyticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "ledger.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("ledger-owner@example.com")
        database.save_user_ai_settings(self.owner["user_id"], encrypt_secret("sk-or-test-ledger"), settings.openrouter_model)

        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_LEDGER" (ROW_ID INTEGER PRIMARY KEY, "TEAM" TEXT, "SCORE" INTEGER)')
        conn.executemany(
            'INSERT INTO "QUEUE_BOARD_LEDGER" (ROW_ID, "TEAM", "SCORE") VALUES (?, ?, ?)',
            [(1, "KKR", 10), (2, "CSK", 20)],
        )
        conn.commit()
        conn.close()
        registered = database.register_dataset("QUEUE_BOARD_LEDGER", self.owner["user_id"], "ledger.xlsx", 2, 2)
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (registered["repository_id"],)
        ).fetchone()[0]
        conn.close()
        self.main_branch_id = registered["main_branch_id"]
        self.table_id = "QUEUE_BOARD_LEDGER"
        self.actor = MergeActor(self.owner["user_id"], self.owner["email"])
        self._original_provider = ai_gateway.providers.get("OPENROUTER")

    def tearDown(self):
        if self._original_provider is not None:
            ai_gateway.providers["OPENROUTER"] = self._original_provider
        else:
            ai_gateway.providers.pop("OPENROUTER", None)
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _commit_cell(self, branch_id, table_id, column_name, value):
        snapshot = database.get_table_snapshot(table_id)
        sheet = snapshot["semantic"]["sheets"][0]
        row = sheet["rows"][0]
        column = next(item for item in sheet["columns"] if item["name"] == column_name)
        context = branch_context(branch_id)
        return commit_semantic_delta(
            table_id=table_id, repository_id=context["repository_id"], branch_id=branch_id,
            expected_head_commit_id=snapshot["head_commit_id"], base_version=snapshot["version"],
            changes=[{"operation_type": "CELL_VALUE_UPDATE", "sheet_id": sheet["sheet_id"], "row_id": row["row_id"], "column_id": column["column_id"], "new_value": value}],
            user_id=self.owner["user_id"], user_email=self.owner["email"], message=f"Set {column_name}",
        )

    def _conflicted_merge_request(self, title, branch_value, main_value):
        copy = database.create_working_copy(self.table_id, self.owner["user_id"], self.owner["email"])
        self._commit_cell(copy["branch_id"], copy["table_id"], "TEAM", branch_value)
        self._commit_cell(self.main_branch_id, self.table_id, "TEAM", main_value)
        return merge_service.create_request(
            source_branch_id=copy["branch_id"], target_branch_id=self.main_branch_id,
            title=title, description=None, actor=self.actor,
        )

    async def test_leaderboard_run_ledger_receipt_and_budget_reflect_real_activity(self):
        # One successful MERGE_CONFLICT_AGENT run.
        ai_gateway.providers["OPENROUTER"] = FakeSuccessProvider()
        request_a = self._conflicted_merge_request("Resolve conflict A", "SRH", "MI")
        success_result = await analyze_merge_request(self.organization_id, self.owner["user_id"], request_a["merge_request_id"], self.actor)
        self.assertEqual("COMPLETED", success_result["status"])

        # One successful COMMIT_REVIEW_AGENT run, on a clean personal branch commit.
        ai_gateway.providers["OPENROUTER"] = FakeCommitSuccessProvider()
        copy = database.create_working_copy(self.table_id, self.owner["user_id"], self.owner["email"])
        commit_result = self._commit_cell(copy["branch_id"], copy["table_id"], "SCORE", "99")
        review = await commit_review.review_commit(self.organization_id, self.owner["user_id"], commit_result["commit_id"])
        self.assertEqual("LOOKS_GOOD", review["recommendation"])

        # One FAILED MERGE_CONFLICT_AGENT run against a second conflicted request.
        ai_gateway.providers["OPENROUTER"] = FailingProvider()
        request_b = self._conflicted_merge_request("Resolve conflict B", "GT", "RCB")
        with self.assertRaises(AIProviderError):
            await analyze_merge_request(self.organization_id, self.owner["user_id"], request_b["merge_request_id"], self.actor)

        # --- agent_leaderboard ---
        leaderboard = {item["agent_key"]: item for item in ledger_analytics.agent_leaderboard(self.organization_id, self.owner["user_id"])}
        self.assertIn("MERGE_CONFLICT_AGENT", leaderboard)
        self.assertIn("COMMIT_REVIEW_AGENT", leaderboard)
        merge_agent_stats = leaderboard["MERGE_CONFLICT_AGENT"]
        self.assertEqual(1, merge_agent_stats["succeeded"])
        self.assertEqual(1, merge_agent_stats["failed"])
        self.assertEqual(2, merge_agent_stats["requests"])
        self.assertAlmostEqual(0.5, merge_agent_stats["success_rate"])
        self.assertGreater(merge_agent_stats["reference_cost_usd"], 0)
        self.assertEqual("SIMULATED_FAILURE", merge_agent_stats["last_error_code"])

        commit_agent_stats = leaderboard["COMMIT_REVIEW_AGENT"]
        self.assertEqual(1, commit_agent_stats["succeeded"])
        self.assertEqual(0, commit_agent_stats["failed"])
        self.assertEqual(1.0, commit_agent_stats["success_rate"])

        # --- run_ledger + run_receipt ---
        failed_runs = ledger_analytics.run_ledger(
            self.organization_id, self.owner["user_id"], agent_key="MERGE_CONFLICT_AGENT", status="FAILED",
        )
        self.assertEqual(1, len(failed_runs["items"]))
        failed_run = failed_runs["items"][0]
        self.assertEqual(1, failed_run["failed_requests"])

        receipt = ledger_analytics.run_receipt(self.organization_id, self.owner["user_id"], failed_run["agent_run_id"])
        self.assertGreaterEqual(len(receipt["steps"]), 1)
        self.assertEqual(1, len(receipt["requests"]))
        self.assertEqual("FAILED", receipt["requests"][0]["status"])

        with self.assertRaises(KeyError):
            ledger_analytics.run_receipt(self.organization_id, self.owner["user_id"], "AIAR_DOES_NOT_EXIST")

        # --- daily_trend / model_breakdown ---
        trend = ledger_analytics.daily_trend(self.organization_id, self.owner["user_id"])
        self.assertGreaterEqual(len(trend), 1)
        total_trend_requests = sum(item["requests"] for item in trend)
        self.assertEqual(3, total_trend_requests)  # 2 merge-agent calls + 1 commit-review call

        models = ledger_analytics.model_breakdown(self.organization_id, self.owner["user_id"])
        self.assertGreaterEqual(len(models), 1)

        # --- budget_status ---
        budget = ledger_analytics.budget_status(self.organization_id, self.owner["user_id"])
        self.assertGreater(budget["today_usage"], 0)
        self.assertFalse(budget["agent_runs_subject_to_quota"])
        self.assertIn("alerts", budget)
        self.assertGreaterEqual(budget["reference_savings_30d_usd"], 0)


if __name__ == "__main__":
    unittest.main()
