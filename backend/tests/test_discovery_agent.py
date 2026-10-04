import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database
from app.ai import agent_runtime, discovery_agent
from app.ai.gateway import ai_gateway
from app.ai.provider import LLMProvider, ProviderResult
from app.config import settings
from app.repositories.commit_store import commit_semantic_delta
from app.repositories.merge_store import branch_context
from app.secret_store import encrypt_secret
from app.services.merge_service import MergeActor, merge_service


class FakeDiscoveryProvider(LLMProvider):
    provider_name = "OPENROUTER"

    async def complete(self, **kwargs) -> ProviderResult:
        content = json.dumps({
            "answer": "The top pick is online right now and has the lightest review queue of the two experts.",
            "evidence": [], "confidence": 0.7, "insufficient_evidence": False,
            "recommended_actions": [], "warnings": [],
        })
        return ProviderResult(content, input_tokens=45, output_tokens=20, reasoning_tokens=0)


class DiscoveryAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "discovery.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("discovery-owner@example.com")
        self.member = database.get_or_create_user("discovery-member@example.com")
        database.save_user_ai_settings(self.owner["user_id"], encrypt_secret("sk-or-test-discovery"), settings.openrouter_model)

        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_DISCOVERY" (ROW_ID INTEGER PRIMARY KEY, "TEAM" TEXT)')
        conn.execute('INSERT INTO "QUEUE_BOARD_DISCOVERY" (ROW_ID, "TEAM") VALUES (1, "MI")')
        conn.commit()
        conn.close()
        registered = database.register_dataset("QUEUE_BOARD_DISCOVERY", self.owner["user_id"], "discovery.xlsx", 1, 1)
        database.add_dataset_member("QUEUE_BOARD_DISCOVERY", self.owner["user_id"], self.member["email"], "editor")
        self.repository_id = registered["repository_id"]
        self.table_id = "QUEUE_BOARD_DISCOVERY"
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (self.repository_id,)
        ).fetchone()[0]
        conn.close()
        self.owner_actor = MergeActor(self.owner["user_id"], self.owner["email"])
        self.member_actor = MergeActor(self.member["user_id"], self.member["email"])
        self._original_provider = ai_gateway.providers.get("OPENROUTER")

    def tearDown(self):
        if self._original_provider is not None:
            ai_gateway.providers["OPENROUTER"] = self._original_provider
        else:
            ai_gateway.providers.pop("OPENROUTER", None)
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _commit(self, actor, value):
        copy = database.create_working_copy(self.table_id, actor.user_id, actor.email)
        snapshot = database.get_table_snapshot(copy["table_id"])
        sheet = snapshot["semantic"]["sheets"][0]
        column = next(item for item in sheet["columns"] if item["name"] == "TEAM")
        context = branch_context(copy["branch_id"])
        return commit_semantic_delta(
            table_id=copy["table_id"], repository_id=context["repository_id"], branch_id=copy["branch_id"],
            expected_head_commit_id=snapshot["head_commit_id"], base_version=snapshot["version"],
            changes=[{"operation_type": "CELL_VALUE_UPDATE", "sheet_id": sheet["sheet_id"],
                      "row_id": sheet["rows"][0]["row_id"], "column_id": column["column_id"], "new_value": value}],
            user_id=actor.user_id, user_email=actor.email, message=f"Set {value}",
        ), copy["branch_id"]

    def test_no_expertise_yet_returns_no_candidates_without_erroring(self):
        result = discovery_agent._rank_candidates("team", {"sheets": [], "experts": []}, [])
        self.assertEqual(([], False), result)

    async def test_ranks_the_online_lightly_loaded_expert_above_the_offline_backlogged_one(self):
        self._commit(self.owner_actor, "CSK")
        self._commit(self.member_actor, "RCB")

        # Member is buried in a stale open review; owner is online right now.
        member_copy = database.create_working_copy(self.table_id, self.member["user_id"], self.member["email"])
        main_branch = database.get_repository(self.table_id, self.owner["user_id"])["default_branch_id"]
        merge_service.create_request(
            source_branch_id=member_copy["branch_id"], target_branch_id=main_branch,
            title="Stale review", description="", actor=self.member_actor,
        )
        database.touch_dataset_presence(self.table_id, self.owner["user_id"], "client-1", "excel", "viewing")

        ai_gateway.providers["OPENROUTER"] = FakeDiscoveryProvider()

        result = await discovery_agent.discover_who_to_ask(
            self.organization_id, self.owner["user_id"], self.repository_id, "team roster",
        )

        self.assertIsNotNone(result["recommended_user_id"])
        self.assertEqual(self.owner["user_id"], result["recommended_user_id"])
        self.assertGreaterEqual(len(result["candidates"]), 1)
        self.assertIn("online", result["rationale"].lower())

        run = agent_runtime.get_run(self.organization_id, self.owner["user_id"], result["agent_run_id"], can_audit=True)
        self.assertEqual("COMPLETED", run["run"]["status"])
        self.assertEqual(["PLAN", "RANK", "EXPLAIN"], [step["step_type"] for step in run["steps"]])

    async def test_start_discovery_completes_in_the_background(self):
        self._commit(self.owner_actor, "CSK")
        ai_gateway.providers["OPENROUTER"] = FakeDiscoveryProvider()

        started = discovery_agent.start_discovery(self.organization_id, self.owner["user_id"], self.repository_id, "team")
        self.assertEqual("RUNNING", started["status"])

        for _ in range(50):
            await asyncio.sleep(0.02)
            run = agent_runtime.get_run(self.organization_id, self.owner["user_id"], started["agent_run_id"], can_audit=True)
            if run["run"]["status"] != "RUNNING":
                break
        self.assertEqual("COMPLETED", run["run"]["status"])


if __name__ == "__main__":
    unittest.main()
