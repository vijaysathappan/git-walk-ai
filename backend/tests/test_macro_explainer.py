"""Phase 5 USP1: AI Macro Explainer -- grounded, cached, agent-audited
plain-English explanation of a macro, following the exact same pattern
test_formula_explainer.py already validates for formulas (fake provider
swapped into ai_gateway, PLAN->INVESTIGATE->EXPLAIN step recording,
durable cache keyed by source hash)."""

import asyncio
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from vba_fixture_builder import build_xlsm  # noqa: E402

from app import database  # noqa: E402
from app.ai import agent_runtime  # noqa: E402
from app.ai.gateway import ai_gateway  # noqa: E402
from app.ai.provider import LLMProvider, ProviderResult  # noqa: E402
from app.config import settings  # noqa: E402
from app.macros import macro_explainer, run_service  # noqa: E402
from app.secret_store import encrypt_secret  # noqa: E402

MACRO_SOURCE = (
    "Public Sub Recalc()\n"
    "    Dim i As Long\n"
    "    For i = 2 To 4\n"
    "        Cells(i, 3).Value = Cells(i, 1).Value * Cells(i, 2).Value\n"
    "    Next i\n"
    "End Sub\n"
)


class FakeMacroProvider(LLMProvider):
    provider_name = "OPENROUTER"

    def __init__(self):
        self.calls = 0

    async def complete(self, **kwargs) -> ProviderResult:
        self.calls += 1
        content = json.dumps({
            "answer": "This macro recalculates the total column from quantity times price.",
            "evidence": [], "confidence": 0.8, "insufficient_evidence": False,
            "recommended_actions": [
                {"title": "What it loops over", "rationale": "Walks rows 2 through 4.", "action_type": "STEP", "risk_level": "LOW"},
                {"title": "What it computes", "rationale": "Sets the total column to quantity times price for each row.", "action_type": "STEP", "risk_level": "LOW"},
            ],
            "warnings": [],
        })
        return ProviderResult(content, input_tokens=50, output_tokens=20, reasoning_tokens=0)


class MacroExplainerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "macro_explainer.db"
        database.initialize_product_schema()

        self.owner = database.get_or_create_user(f"macroexplain_{uuid.uuid4().hex[:8]}@example.com")
        database.save_user_ai_settings(self.owner["user_id"], encrypt_secret("sk-or-test-macro"), settings.openrouter_model)

        self.table_id = "QUEUE_BOARD_MACROEXPLAIN"
        conn = database._get_connection()
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{self.table_id}"')
            conn.execute(f'CREATE TABLE "{self.table_id}" (ROW_ID INTEGER PRIMARY KEY AUTOINCREMENT, QTY INTEGER, PRICE INTEGER, TOTAL INTEGER)')
            conn.commit()
        finally:
            conn.close()
        registered = database.register_dataset(self.table_id, self.owner["user_id"], "explain.xlsx", 0, 3)
        self.repository_id = registered["repository_id"]
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (self.repository_id,)
        ).fetchone()[0]
        conn.close()

        xlsm_bytes = build_xlsm([["Qty", "Price", "Total"]], "Module1", MACRO_SOURCE)
        run_service.register_source(self.table_id, "explain.xlsm", xlsm_bytes, self.owner["user_id"])
        extraction = run_service.extract(self.table_id, self.owner["user_id"])
        self.assertEqual("COMPLETED", extraction["status"])
        listing = run_service.list_macros(self.table_id, self.owner["user_id"])
        self.macro_id = listing["macros"][0]["macro_id"]

        self._original_provider = ai_gateway.providers.get("OPENROUTER")

    def tearDown(self):
        if self._original_provider is not None:
            ai_gateway.providers["OPENROUTER"] = self._original_provider
        else:
            ai_gateway.providers.pop("OPENROUTER", None)
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_structural_facts_are_parsed_deterministically(self):
        from app.macros.parser.statement_parser import parse_sub
        sub_ast = parse_sub(MACRO_SOURCE)
        facts = macro_explainer.structural_facts(sub_ast)
        self.assertEqual(1, len(facts["loops"]))
        self.assertEqual([1, 2, 3], facts["columns_referenced"])
        self.assertFalse(facts["creates_new_sheet"])
        self.assertFalse(facts["uses_dictionary"])

    async def test_explain_macro_grounds_the_explanation_and_caches_it(self):
        ai_gateway.providers["OPENROUTER"] = FakeMacroProvider()

        result = await macro_explainer.explain_macro(self.owner["user_id"], self.table_id, self.macro_id)

        self.assertFalse(result["cache_hit"])
        self.assertEqual(2, len(result["steps"]))
        self.assertIn("recalculates", result["summary"])
        self.assertGreater(result["confidence"], 0)

        run = agent_runtime.get_run(self.organization_id, self.owner["user_id"], result["agent_run_id"], can_audit=True)
        self.assertEqual("COMPLETED", run["run"]["status"])
        self.assertEqual(["PLAN", "INVESTIGATE", "EXPLAIN"], [step["step_type"] for step in run["steps"]])

        provider = ai_gateway.providers["OPENROUTER"]
        second = await macro_explainer.explain_macro(self.owner["user_id"], self.table_id, self.macro_id)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(1, provider.calls)

    async def test_start_macro_explanation_runs_in_the_background_and_completes(self):
        ai_gateway.providers["OPENROUTER"] = FakeMacroProvider()

        started = macro_explainer.start_macro_explanation(self.owner["user_id"], self.table_id, self.macro_id)
        self.assertEqual("RUNNING", started["status"])

        run = None
        for _ in range(50):
            await asyncio.sleep(0.02)
            run = agent_runtime.get_run(self.organization_id, self.owner["user_id"], started["agent_run_id"], can_audit=True)
            if run["run"]["status"] != "RUNNING":
                break
        self.assertEqual("COMPLETED", run["run"]["status"])

        cached = run_service.get_cached_macro_explanation(self.table_id, self.macro_id, self.owner["user_id"])
        self.assertIsNotNone(cached)

    async def test_organization_id_is_resolved_from_the_repository_not_the_caller(self):
        # Regression test: the acting user's "primary" organization can
        # differ from the org the repository actually belongs to (a user
        # in more than one org) -- the explainer must always check
        # ai.agent.run against the REPOSITORY's own org, never the
        # caller's primary org, or a correctly-scoped role grant in the
        # right org gets silently ignored.
        conn = database._get_connection()
        try:
            other_org_id = "ORG_OTHER_" + uuid.uuid4().hex[:8].upper()
            now = database._utcnow()
            conn.execute(
                "INSERT INTO ORGANIZATIONS (ORGANIZATION_ID, NAME, SLUG, CREATED_BY, CREATED_AT, UPDATED_AT) VALUES (?,?,?,?,?,?)",
                (other_org_id, "Other Org", f"other-org-{uuid.uuid4().hex[:8]}", self.owner["user_id"], now, now),
            )
            # An earlier JOINED_AT than the repository's own org membership
            # (created during setUp) so primary_organization() picks THIS
            # org, not the repository's -- the exact ambiguity being tested.
            conn.execute(
                "INSERT INTO ORGANIZATION_MEMBERS (ORGANIZATION_ID, USER_ID, STATUS, JOINED_AT, UPDATED_AT) VALUES (?,?,?,?,?)",
                (other_org_id, self.owner["user_id"], "ACTIVE", "2000-01-01T00:00:00+00:00", now),
            )
            conn.commit()
        finally:
            conn.close()
        from app.access_control.service import primary_organization
        # Confirms the fixture actually creates the ambiguity being tested:
        # this user's "primary" org must NOT be the repository's own org.
        self.assertNotEqual(self.organization_id, primary_organization(self.owner["user_id"]))

        ai_gateway.providers["OPENROUTER"] = FakeMacroProvider()
        result = await macro_explainer.explain_macro(self.owner["user_id"], self.table_id, self.macro_id)
        self.assertFalse(result["cache_hit"])

    async def test_viewer_without_ai_agent_run_permission_is_denied(self):
        # ai.agent.run is granted to editor/owner but not viewer -- the
        # explainer must not be reachable by someone who can merely view.
        viewer = database.get_or_create_user(f"macroexplain_viewer_{uuid.uuid4().hex[:8]}@example.com")
        database.add_dataset_member(self.table_id, self.owner["user_id"], viewer["email"], "viewer")
        with self.assertRaises(PermissionError):
            await macro_explainer.explain_macro(viewer["user_id"], self.table_id, self.macro_id)


if __name__ == "__main__":
    unittest.main()
