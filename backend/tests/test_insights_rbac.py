import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database
from app.ai import insights_agent
from app.ai.gateway import ai_gateway
from app.ai.provider import LLMProvider, ProviderResult
from app.config import settings
from app.secret_store import encrypt_secret


class DenyAllProvider(LLMProvider):
    """Never actually reached by a denied call -- if this fires, the test
    asserting denial-before-generation has already failed."""
    provider_name = "OPENROUTER"

    async def complete(self, **kwargs) -> ProviderResult:
        raise AssertionError("the LLM must never be called for a caller without ai.agent.run")


class InsightsRbacTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "insights_rbac.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("insights-rbac-owner@example.com")
        database.save_user_ai_settings(self.owner["user_id"], encrypt_secret("sk-or-test-insights-rbac"), settings.openrouter_model)

        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_RBAC_INSIGHTS" (ROW_ID INTEGER PRIMARY KEY, "REGION" TEXT, "AMOUNT" REAL)')
        conn.executemany(
            'INSERT INTO "QUEUE_BOARD_RBAC_INSIGHTS" (ROW_ID, "REGION", "AMOUNT") VALUES (?, ?, ?)',
            [(1, "West", 100.0), (2, "East", 50.0)],
        )
        conn.commit()
        conn.close()
        registered = database.register_dataset("QUEUE_BOARD_RBAC_INSIGHTS", self.owner["user_id"], "rbac_insights.xlsx", 2, 2)
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (registered["repository_id"],)
        ).fetchone()[0]
        conn.close()
        self.repository_id = registered["repository_id"]
        self.branch_id = registered["main_branch_id"]
        self.table_id = "QUEUE_BOARD_RBAC_INSIGHTS"
        self._original_provider = ai_gateway.providers.get("OPENROUTER")
        ai_gateway.providers["OPENROUTER"] = DenyAllProvider()

    def tearDown(self):
        if self._original_provider is not None:
            ai_gateway.providers["OPENROUTER"] = self._original_provider
        else:
            ai_gateway.providers.pop("OPENROUTER", None)
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def test_a_user_with_no_role_in_this_organization_is_denied(self):
        outsider = database.get_or_create_user("insights-rbac-outsider@example.com")
        with self.assertRaises(PermissionError):
            insights_agent.start_data_insight(
                self.organization_id, outsider["user_id"], self.repository_id, self.table_id,
                self.branch_id, "How many sales happened in each region?",
            )

    async def test_the_repository_owner_is_allowed_to_launch_a_run(self):
        started = insights_agent.start_data_insight(
            self.organization_id, self.owner["user_id"], self.repository_id, self.table_id,
            self.branch_id, "How many sales happened in each region?",
        )
        self.assertEqual("RUNNING", started["status"])
        self.assertIn("agent_run_id", started)


if __name__ == "__main__":
    unittest.main()
