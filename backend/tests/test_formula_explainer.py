import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database
from app.ai import agent_runtime, formula_explainer
from app.ai.formula_knowledge import extract_cell_references, extract_functions
from app.ai.gateway import ai_gateway
from app.ai.provider import LLMProvider, ProviderResult
from app.api.repositories import _discover_formula
from app.config import settings
from app.repositories.commit_store import commit_semantic_delta
from app.repositories.merge_store import branch_context
from app.secret_store import encrypt_secret


class DiscoverFormulaTests(unittest.TestCase):
    """`_discover_formula` is a pure function -- no DB fixture needed -- so
    the store's "leave sheet/cell/formula blank" path is covered directly
    against constructed branch-state shapes."""

    def test_finds_the_first_formula_in_position_order(self):
        branch_state = {
            "sheets": [{
                "name": "Data", "sheet_id": "SHEET_1",
                "columns": [
                    {"column_id": "COL_A", "position": 0, "name": "PRICE"},
                    {"column_id": "COL_B", "position": 1, "name": "DOUBLED"},
                ],
                "rows": [
                    {"row_id": "ROW_1", "position": 0, "formulas": {}},
                    {"row_id": "ROW_2", "position": 1, "formulas": {"COL_B": "=A3*2"}},
                ],
            }],
        }
        result = _discover_formula(branch_state)
        self.assertEqual(("Data", "B3", "=A3*2"), result)

    def test_skips_sheets_with_no_formulas_and_checks_the_next_one(self):
        branch_state = {
            "sheets": [
                {"name": "Empty", "sheet_id": "SHEET_1", "columns": [{"column_id": "COL_A", "position": 0}],
                 "rows": [{"row_id": "ROW_1", "position": 0, "formulas": {}}]},
                {"name": "HasFormula", "sheet_id": "SHEET_2", "columns": [{"column_id": "COL_A", "position": 0}],
                 "rows": [{"row_id": "ROW_1", "position": 0, "formulas": {"COL_A": "=SUM(1,2)"}}]},
            ],
        }
        result = _discover_formula(branch_state)
        self.assertEqual(("HasFormula", "A2", "=SUM(1,2)"), result)

    def test_returns_none_when_the_branch_has_no_formulas_anywhere(self):
        branch_state = {"sheets": [{"name": "Data", "sheet_id": "SHEET_1", "columns": [], "rows": [{"row_id": "ROW_1", "position": 0, "formulas": {}}]}]}
        self.assertIsNone(_discover_formula(branch_state))


class FakeFormulaProvider(LLMProvider):
    provider_name = "OPENROUTER"

    def __init__(self):
        self.calls = 0

    async def complete(self, **kwargs) -> ProviderResult:
        self.calls += 1
        content = json.dumps({
            "answer": "This formula doubles the item's list price.",
            "evidence": [], "confidence": 0.82, "insufficient_evidence": False,
            "recommended_actions": [
                {"title": "What A2 represents", "rationale": "A2 holds the item's list price.", "action_type": "STEP", "risk_level": "LOW"},
                {"title": "How the result is computed", "rationale": "The formula multiplies A2 by 2.", "action_type": "STEP", "risk_level": "LOW"},
            ],
            "warnings": [],
        })
        return ProviderResult(content, input_tokens=60, output_tokens=25, reasoning_tokens=0)


class FormulaExplainerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "formula_explainer.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("formula-owner@example.com")
        database.save_user_ai_settings(self.owner["user_id"], encrypt_secret("sk-or-test-formula"), settings.openrouter_model)

        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_FORMULA" (ROW_ID INTEGER PRIMARY KEY, "PRICE" REAL, "DOUBLED" REAL)')
        conn.executemany(
            'INSERT INTO "QUEUE_BOARD_FORMULA" (ROW_ID, "PRICE", "DOUBLED") VALUES (?, ?, ?)',
            [(1, 10.0, 0.0), (2, 20.0, 0.0)],
        )
        conn.commit()
        conn.close()
        registered = database.register_dataset("QUEUE_BOARD_FORMULA", self.owner["user_id"], "formula.xlsx", 2, 2)
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (registered["repository_id"],)
        ).fetchone()[0]
        conn.close()
        self.repository_id = registered["repository_id"]
        self.branch_id = registered["main_branch_id"]
        self.table_id = "QUEUE_BOARD_FORMULA"

        snapshot = database.get_table_snapshot(self.table_id)
        self.sheet = snapshot["semantic"]["sheets"][0]
        self.price_column = next(c for c in self.sheet["columns"] if c["name"] == "PRICE")
        self.doubled_column = next(c for c in self.sheet["columns"] if c["name"] == "DOUBLED")
        self.first_row = sorted(self.sheet["rows"], key=lambda r: r["position"])[0]
        self.sheet_id = self.sheet["sheet_id"]
        self.row_id = self.first_row["row_id"]
        self.column_id = self.doubled_column["column_id"]
        self.formula = "=A2*2"

        context = branch_context(self.branch_id)
        commit_semantic_delta(
            table_id=self.table_id, repository_id=context["repository_id"], branch_id=self.branch_id,
            expected_head_commit_id=snapshot["head_commit_id"], base_version=snapshot["version"],
            changes=[{
                "operation_type": "CELL_FORMULA_UPDATE", "sheet_id": self.sheet_id,
                "row_id": self.row_id, "column_id": self.column_id, "new_formula": self.formula,
            }],
            user_id=self.owner["user_id"], user_email=self.owner["email"], message="Add doubling formula",
        )
        self._original_provider = ai_gateway.providers.get("OPENROUTER")

    def tearDown(self):
        if self._original_provider is not None:
            ai_gateway.providers["OPENROUTER"] = self._original_provider
        else:
            ai_gateway.providers.pop("OPENROUTER", None)
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_extract_functions_and_references_are_deterministic(self):
        self.assertEqual(["VLOOKUP"], extract_functions("=VLOOKUP(A1,B:C,2,FALSE)"))
        self.assertIn("A1", extract_cell_references("=VLOOKUP(A1,B:C,2,FALSE)"))
        self.assertEqual([], extract_functions("=A2*2"))
        self.assertIn("A2", extract_cell_references("=A2*2"))

    async def test_explain_formula_grounds_the_explanation_and_caches_it(self):
        ai_gateway.providers["OPENROUTER"] = FakeFormulaProvider()

        result = await formula_explainer.explain_formula(
            self.organization_id, self.owner["user_id"], self.repository_id, self.branch_id,
            self.sheet_id, self.row_id, self.column_id, self.formula, cell_address="B2",
        )

        self.assertFalse(result["cache_hit"])
        self.assertEqual(2, len(result["steps"]))
        self.assertIn("doubles", result["summary"])
        self.assertGreater(result["confidence"], 0)

        run = agent_runtime.get_run(self.organization_id, self.owner["user_id"], result["agent_run_id"], can_audit=True)
        self.assertEqual("COMPLETED", run["run"]["status"])
        self.assertEqual(3, len(run["steps"]))
        self.assertEqual(["PLAN", "INVESTIGATE", "EXPLAIN"], [step["step_type"] for step in run["steps"]])

        # A second call with the identical formula must be served from the
        # durable cache -- no second provider call at all.
        provider = ai_gateway.providers["OPENROUTER"]
        second = await formula_explainer.explain_formula(
            self.organization_id, self.owner["user_id"], self.repository_id, self.branch_id,
            self.sheet_id, self.row_id, self.column_id, self.formula, cell_address="B2",
        )
        self.assertTrue(second["cache_hit"])
        self.assertEqual(1, provider.calls)

    async def test_a_changed_formula_busts_the_cache_and_re_explains(self):
        ai_gateway.providers["OPENROUTER"] = FakeFormulaProvider()
        await formula_explainer.explain_formula(
            self.organization_id, self.owner["user_id"], self.repository_id, self.branch_id,
            self.sheet_id, self.row_id, self.column_id, self.formula,
        )
        provider = ai_gateway.providers["OPENROUTER"]

        result = await formula_explainer.explain_formula(
            self.organization_id, self.owner["user_id"], self.repository_id, self.branch_id,
            self.sheet_id, self.row_id, self.column_id, "=A2*3",
        )
        self.assertFalse(result["cache_hit"])
        self.assertEqual(2, provider.calls)

    async def test_start_formula_explanation_runs_in_the_background_and_completes(self):
        ai_gateway.providers["OPENROUTER"] = FakeFormulaProvider()

        started = formula_explainer.start_formula_explanation(
            self.organization_id, self.owner["user_id"], self.repository_id, self.branch_id,
            self.sheet_id, self.row_id, self.column_id, self.formula,
        )
        self.assertEqual("RUNNING", started["status"])
        self.assertIn("agent_run_id", started)

        for _ in range(50):
            await asyncio.sleep(0.02)
            run = agent_runtime.get_run(self.organization_id, self.owner["user_id"], started["agent_run_id"], can_audit=True)
            if run["run"]["status"] != "RUNNING":
                break
        self.assertEqual("COMPLETED", run["run"]["status"])

        cached = formula_explainer.get_cached_explanation(
            self.branch_id, self.sheet_id, self.row_id, self.column_id,
            formula_explainer._hash(self.formula),
        )
        self.assertIsNotNone(cached)

    async def test_missing_euc_analysis_does_not_block_the_explanation(self):
        # No EUC asset was ever registered for this repository -- the
        # dependency-graph enrichment must be gracefully absent, not fatal.
        ai_gateway.providers["OPENROUTER"] = FakeFormulaProvider()
        result = await formula_explainer.explain_formula(
            self.organization_id, self.owner["user_id"], self.repository_id, self.branch_id,
            self.sheet_id, self.row_id, self.column_id, self.formula, cell_address="B2",
        )
        self.assertFalse(result["cache_hit"])
        self.assertIsNotNone(result["summary"])


if __name__ == "__main__":
    unittest.main()
