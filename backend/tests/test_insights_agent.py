import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database
from app.ai import agent_runtime, insights_agent
from app.ai.gateway import ai_gateway
from app.ai.provider import LLMProvider, ProviderResult
from app.config import settings
from app.secret_store import encrypt_secret


def _grounded(answer, actions):
    return json.dumps({
        "answer": answer, "evidence": [], "confidence": 0.8, "insufficient_evidence": False,
        "recommended_actions": actions, "warnings": [],
    })


class TwoStepFakeProvider(LLMProvider):
    """First call answers the query-planning prompt (group by REGION, sum
    AMOUNT, chart BAR); second call answers the narrative prompt."""
    provider_name = "OPENROUTER"

    def __init__(self):
        self.calls = 0

    async def complete(self, **kwargs) -> ProviderResult:
        self.calls += 1
        if self.calls == 1:
            content = _grounded(
                "I'll sum the amount by region.",
                [
                    {"title": "REGION", "rationale": "groups sales by region", "action_type": "GROUP_BY", "risk_level": "LOW"},
                    {"title": "SUM", "rationale": "AMOUNT", "action_type": "AGGREGATION", "risk_level": "LOW"},
                    {"title": "BAR", "rationale": "best fit for comparing regions", "action_type": "CHART_TYPE", "risk_level": "LOW"},
                ],
            )
        else:
            content = _grounded(
                "West leads total sales at 150.",
                [
                    {"title": "West leads", "rationale": "West totals 150, the highest of any region.", "action_type": "BULLET", "risk_level": "LOW"},
                    {"title": "Review North", "rationale": "North is far behind at 10 and may need attention.", "action_type": "ACTION", "risk_level": "LOW"},
                ],
            )
        return ProviderResult(content, input_tokens=80, output_tokens=40, reasoning_tokens=0)


class RepositoryActivityFakeProvider(LLMProvider):
    """Answers a question about the repository's OWN governance/activity
    (e.g. "what percentage of merges succeed") rather than its data table --
    picks the REPOSITORY_ACTIVITY domain and two real, allow-listed metrics."""
    provider_name = "OPENROUTER"

    def __init__(self):
        self.calls = 0

    async def complete(self, **kwargs) -> ProviderResult:
        self.calls += 1
        if self.calls == 1:
            content = _grounded(
                "I'll report the merge success rate and conflict rate.",
                [
                    {"title": "REPOSITORY_ACTIVITY", "rationale": "the question is about governance, not the data table", "action_type": "DOMAIN", "risk_level": "LOW"},
                    {"title": "merge_success_rate", "rationale": "directly answers the question", "action_type": "METRIC", "risk_level": "LOW"},
                    {"title": "conflict_rate", "rationale": "useful related context", "action_type": "METRIC", "risk_level": "LOW"},
                    {"title": "BAR", "rationale": "compares two percentages side by side", "action_type": "CHART_TYPE", "risk_level": "LOW"},
                ],
            )
        else:
            content = _grounded(
                "Merges succeed most of the time, with few conflicts.",
                [
                    {"title": "High success rate", "rationale": "The merge success rate is well above the conflict rate.", "action_type": "BULLET", "risk_level": "LOW"},
                ],
            )
        return ProviderResult(content, input_tokens=70, output_tokens=35, reasoning_tokens=0)


class InvalidThenValidFakeProvider(LLMProvider):
    """First query-planning call proposes an unknown column (must be
    rejected and repaired); second call (the retry) is valid; third call
    answers the narrative prompt."""
    provider_name = "OPENROUTER"

    def __init__(self):
        self.calls = 0

    async def complete(self, **kwargs) -> ProviderResult:
        self.calls += 1
        if self.calls == 1:
            content = _grounded("Bad plan.", [
                {"title": "NOT_A_REAL_COLUMN", "rationale": "groups by an invented column", "action_type": "GROUP_BY", "risk_level": "LOW"},
            ])
        elif self.calls == 2:
            content = _grounded("Corrected plan.", [
                {"title": "REGION", "rationale": "groups sales by region", "action_type": "GROUP_BY", "risk_level": "LOW"},
                {"title": "COUNT", "rationale": "none", "action_type": "AGGREGATION", "risk_level": "LOW"},
                {"title": "BAR", "rationale": "best fit for counts by category", "action_type": "CHART_TYPE", "risk_level": "LOW"},
            ])
        else:
            content = _grounded("Three regions are represented.", [
                {"title": "Even split", "rationale": "Each region has at least one sale.", "action_type": "BULLET", "risk_level": "LOW"},
            ])
        return ProviderResult(content, input_tokens=80, output_tokens=40, reasoning_tokens=0)


class InsightsAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "insights_agent.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("insights-owner@example.com")
        database.save_user_ai_settings(self.owner["user_id"], encrypt_secret("sk-or-test-insights"), settings.openrouter_model)

        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_INSIGHTS" (ROW_ID INTEGER PRIMARY KEY, "REGION" TEXT, "AMOUNT" REAL)')
        conn.executemany(
            'INSERT INTO "QUEUE_BOARD_INSIGHTS" (ROW_ID, "REGION", "AMOUNT") VALUES (?, ?, ?)',
            [(1, "West", 100.0), (2, "West", 50.0), (3, "East", 75.0), (4, "East", 25.0), (5, "North", 10.0)],
        )
        conn.commit()
        conn.close()
        registered = database.register_dataset("QUEUE_BOARD_INSIGHTS", self.owner["user_id"], "insights.xlsx", 5, 2)
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (registered["repository_id"],)
        ).fetchone()[0]
        conn.close()
        self.repository_id = registered["repository_id"]
        self.branch_id = registered["main_branch_id"]
        self.table_id = "QUEUE_BOARD_INSIGHTS"
        self._original_provider = ai_gateway.providers.get("OPENROUTER")

    def tearDown(self):
        if self._original_provider is not None:
            ai_gateway.providers["OPENROUTER"] = self._original_provider
        else:
            ai_gateway.providers.pop("OPENROUTER", None)
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def _await_completion(self, run_id):
        for _ in range(100):
            await asyncio.sleep(0.02)
            run = agent_runtime.get_run(self.organization_id, self.owner["user_id"], run_id, can_audit=True)
            if run["run"]["status"] != "RUNNING":
                return run
        raise AssertionError("agent run never completed")

    async def test_full_run_produces_a_grounded_chart_ready_result(self):
        ai_gateway.providers["OPENROUTER"] = TwoStepFakeProvider()

        started = insights_agent.start_data_insight(
            self.organization_id, self.owner["user_id"], self.repository_id, self.table_id,
            self.branch_id, "Which region has the highest total sales amount?",
        )
        self.assertEqual("RUNNING", started["status"])
        run = await self._await_completion(started["agent_run_id"])
        self.assertEqual("COMPLETED", run["run"]["status"])
        self.assertEqual(["PLAN", "QUERY_PLANNED", "EXECUTED", "NARRATED"], [s["step_type"] for s in run["steps"]])

        insight_result_id = run["result"]["insight_result_id"]
        result = insights_agent.get_result_by_id(insight_result_id)
        self.assertEqual("BAR", result["chart_type"])
        by_region = {row["group_key"]: row["value"] for row in result["result_data"]}
        self.assertEqual({"West": 150.0, "East": 100.0, "North": 10.0}, by_region)
        self.assertIn("West leads", result["headline"])
        self.assertTrue(result["bullets"])
        self.assertGreater(result["confidence"], 0)

    async def test_identical_question_on_unchanged_data_is_served_from_cache(self):
        ai_gateway.providers["OPENROUTER"] = TwoStepFakeProvider()
        provider = ai_gateway.providers["OPENROUTER"]
        question = "Which region has the highest total sales amount?"

        first = insights_agent.start_data_insight(
            self.organization_id, self.owner["user_id"], self.repository_id, self.table_id, self.branch_id, question,
        )
        await self._await_completion(first["agent_run_id"])
        self.assertEqual(2, provider.calls)

        second = insights_agent.start_data_insight(
            self.organization_id, self.owner["user_id"], self.repository_id, self.table_id, self.branch_id, question,
        )
        self.assertEqual("COMPLETED", second["status"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(2, provider.calls, "a cache hit must not spend a new LLM call")

    async def test_an_invalid_query_plan_is_repaired_then_falls_through_to_a_valid_one(self):
        ai_gateway.providers["OPENROUTER"] = InvalidThenValidFakeProvider()

        started = insights_agent.start_data_insight(
            self.organization_id, self.owner["user_id"], self.repository_id, self.table_id,
            self.branch_id, "How many sales happened in each region?",
        )
        run = await self._await_completion(started["agent_run_id"])
        self.assertEqual("COMPLETED", run["run"]["status"])
        result = insights_agent.get_result_by_id(run["result"]["insight_result_id"])
        self.assertEqual("REGION", result["query_spec"]["group_by"])
        self.assertEqual("COUNT", result["query_spec"]["aggregation"])

    async def test_a_governance_question_answers_from_repository_activity_not_the_data_table(self):
        ai_gateway.providers["OPENROUTER"] = RepositoryActivityFakeProvider()

        started = insights_agent.start_data_insight(
            self.organization_id, self.owner["user_id"], self.repository_id, self.table_id,
            self.branch_id, "What percentage of our merge requests succeed?",
        )
        run = await self._await_completion(started["agent_run_id"])
        self.assertEqual("COMPLETED", run["run"]["status"])
        result = insights_agent.get_result_by_id(run["result"]["insight_result_id"])

        self.assertEqual("REPOSITORY_ACTIVITY", result["query_spec"]["domain"])
        self.assertEqual(["merge_success_rate", "conflict_rate"], result["query_spec"]["metrics"])
        by_metric = {row["group_key"]: row["value"] for row in result["result_data"]}
        self.assertIn("Merge success rate", by_metric)
        self.assertIn("Conflict rate", by_metric)
        # No merges were ever created in this fixture -- governance_store's
        # own (unmodified) rule is that an empty merge history is 100%
        # success / 0% conflict, never a divide-by-zero or invented number.
        self.assertEqual(100.0, by_metric["Merge success rate"])
        self.assertEqual(0.0, by_metric["Conflict rate"])
        self.assertIsNone(result["forecast"], "a point-in-time governance summary has no time series to forecast")


if __name__ == "__main__":
    unittest.main()
