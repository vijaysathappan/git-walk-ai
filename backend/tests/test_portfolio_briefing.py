import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from app import database
from app.ai import agent_runtime, portfolio_briefing
from app.ai.gateway import ai_gateway
from app.ai.provider import LLMProvider, ProviderResult
from app.config import settings
from app.euc.dependency.services import DependencyService
from app.euc.intelligence import IntelligenceService
from app.euc.service import analyze_euc, ingest_euc
from app.secret_store import encrypt_secret


def _xlsx_bytes(formulas):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet.append(["SCORE", "CALC"])
    for index, formula in enumerate(formulas, start=1):
        sheet.append([index * 10, formula])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class FakeBriefingProvider(LLMProvider):
    provider_name = "OPENROUTER"

    def __init__(self, answer: str = "One repository carries most of the portfolio's critical risk right now."):
        self.answer = answer
        self.calls = 0

    async def complete(self, **kwargs) -> ProviderResult:
        self.calls += 1
        content = json.dumps({
            "answer": self.answer, "evidence": [], "confidence": 0.75,
            "insufficient_evidence": False, "recommended_actions": [], "warnings": [],
        })
        return ProviderResult(content, input_tokens=50, output_tokens=20, reasoning_tokens=0)


class PortfolioBriefingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "portfolio_briefing.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("briefing-owner@example.com")
        database.save_user_ai_settings(self.owner["user_id"], encrypt_secret("sk-or-test-briefing"), settings.openrouter_model)

        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_BRIEFING" (ROW_ID INTEGER PRIMARY KEY, "SCORE" INTEGER, "CALC" INTEGER)')
        conn.executemany(
            'INSERT INTO "QUEUE_BOARD_BRIEFING" (ROW_ID, "SCORE", "CALC") VALUES (?, ?, ?)',
            [(i, i * 10, 0) for i in range(1, 4)],
        )
        conn.commit()
        conn.close()
        registered = database.register_dataset("QUEUE_BOARD_BRIEFING", self.owner["user_id"], "briefing.xlsx", 3, 2)
        self.repository_id = registered["repository_id"]
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (self.repository_id,)
        ).fetchone()[0]
        conn.close()

        asset = ingest_euc(self.repository_id, "briefing.xlsx", _xlsx_bytes(["=SCORE*2", "=NOTASHEET!Z99", "=SCORE*2"]), self.owner["user_id"])
        analyze_euc(asset["euc_id"], self.owner["user_id"])
        DependencyService().build(asset["euc_id"], self.owner["user_id"])
        IntelligenceService().build(asset["euc_id"], self.owner["user_id"])

        self._original_provider = ai_gateway.providers.get("OPENROUTER")

    def tearDown(self):
        if self._original_provider is not None:
            ai_gateway.providers["OPENROUTER"] = self._original_provider
        else:
            ai_gateway.providers.pop("OPENROUTER", None)
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def test_generate_portfolio_briefing_grounds_the_narrative_in_real_numbers(self):
        ai_gateway.providers["OPENROUTER"] = FakeBriefingProvider()

        result = await portfolio_briefing.generate_portfolio_briefing(self.organization_id, self.owner["user_id"])

        self.assertEqual(FakeBriefingProvider().answer, result["narrative"])
        self.assertGreaterEqual(result["summary"]["total_assets"], 1)
        self.assertTrue(any("risk score" in observation for observation in result["observations"]))

        run = agent_runtime.get_run(self.organization_id, self.owner["user_id"], result["agent_run_id"], can_audit=True)
        self.assertEqual("COMPLETED", run["run"]["status"])
        self.assertEqual(["PLAN", "GATHER", "EXPLAIN"], [step["step_type"] for step in run["steps"]])

        latest = portfolio_briefing.get_latest_briefing(self.organization_id)
        self.assertEqual(result["briefing_id"], latest["briefing_id"])

    async def test_briefing_history_accumulates_across_runs(self):
        ai_gateway.providers["OPENROUTER"] = FakeBriefingProvider()
        await portfolio_briefing.generate_portfolio_briefing(self.organization_id, self.owner["user_id"])
        second = await portfolio_briefing.generate_portfolio_briefing(self.organization_id, self.owner["user_id"])

        conn = database._get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM PORTFOLIO_BRIEFINGS WHERE ORGANIZATION_ID=?", (self.organization_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(2, count)
        self.assertEqual(second["briefing_id"], portfolio_briefing.get_latest_briefing(self.organization_id)["briefing_id"])


if __name__ == "__main__":
    unittest.main()
