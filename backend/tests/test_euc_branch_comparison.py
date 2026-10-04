import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database
from app.ai import euc_narrative
from app.ai.gateway import ai_gateway
from app.ai.provider import LLMProvider, ProviderResult
from app.config import settings
from app.euc import branch_comparison
from app.excel.identity import semantic_snapshot
from app.repositories.commit_store import commit_semantic_delta
from app.repositories.merge_store import branch_context
from app.secret_store import encrypt_secret


class FakeRiskDriftProvider(LLMProvider):
    provider_name = "OPENROUTER"

    def __init__(self):
        self.calls = 0

    async def complete(self, **kwargs) -> ProviderResult:
        self.calls += 1
        content = json.dumps({
            "answer": "This branch introduces a formula pattern break that deviates from the rest of the column.",
            "evidence": [], "confidence": 0.82, "insufficient_evidence": False,
            "recommended_actions": [], "warnings": [],
        })
        return ProviderResult(content, input_tokens=80, output_tokens=35, reasoning_tokens=0)


class DiffFindingsUnitTests(unittest.TestCase):
    def test_introduced_and_resolved_are_classified_by_stable_key(self):
        main_findings = [
            {"finding_id": "F1", "rule_id": "FORMULA_PATTERN_BREAK", "sheet_id": "S1", "cell_address": "C3", "node_id": None},
            {"finding_id": "F2", "rule_id": "BROKEN_REFERENCE", "sheet_id": "S1", "cell_address": None, "node_id": "N1"},
        ]
        branch_findings = [
            {"finding_id": "F1B", "rule_id": "FORMULA_PATTERN_BREAK", "sheet_id": "S1", "cell_address": "C3", "node_id": None},
            {"finding_id": "F3", "rule_id": "FORMULA_PATTERN_BREAK", "sheet_id": "S1", "cell_address": "D9", "node_id": None},
        ]
        diff = branch_comparison.diff_findings(main_findings, branch_findings)
        self.assertEqual(1, len(diff["introduced"]))
        self.assertEqual("F3", diff["introduced"][0]["finding_id"])
        self.assertEqual(1, len(diff["resolved"]))
        self.assertEqual("F2", diff["resolved"][0]["finding_id"])


class BranchComparisonIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "euc_branch_comparison.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("radar-owner@example.com")
        database.save_user_ai_settings(self.owner["user_id"], encrypt_secret("sk-or-test-radar"), settings.openrouter_model)

        conn = sqlite3.connect(database.DB_PATH)
        conn.execute(
            'CREATE TABLE "QUEUE_BOARD_RADAR" (ROW_ID INTEGER PRIMARY KEY, "SCORE" INTEGER, "CALC" INTEGER)'
        )
        conn.executemany(
            'INSERT INTO "QUEUE_BOARD_RADAR" (ROW_ID, "SCORE", "CALC") VALUES (?, ?, ?)',
            [(i, i * 10, 0) for i in range(1, 6)],
        )
        conn.commit()
        conn.close()
        registered = database.register_dataset("QUEUE_BOARD_RADAR", self.owner["user_id"], "radar.xlsx", 5, 3)
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (registered["repository_id"],)
        ).fetchone()[0]
        conn.close()
        self.repository_id = registered["repository_id"]
        self.main_branch_id = registered["main_branch_id"]
        self.table_id = "QUEUE_BOARD_RADAR"

        # Establish a consistent copied-down formula on main across all 5 rows.
        self._set_formulas(self.main_branch_id, self.table_id, ["=SCORE*2"] * 5)

        self.copy = database.create_working_copy(self.table_id, self.owner["user_id"], self.owner["email"])
        # Break the pattern in the middle row (row 3 of 5) on the branch only —
        # DependencyGraphBuilder._pattern_breaks() only flags a differing
        # formula when its immediate neighbors above and below both match
        # each other, so the break must sit strictly between two identical
        # neighbors, not at either end of the column.
        self._set_formulas(self.copy["branch_id"], self.copy["table_id"], [
            "=SCORE*2", "=SCORE*2", "=SCORE*3+7", "=SCORE*2", "=SCORE*2",
        ])

        self._original_provider = ai_gateway.providers.get("OPENROUTER")

    def tearDown(self):
        if self._original_provider is not None:
            ai_gateway.providers["OPENROUTER"] = self._original_provider
        else:
            ai_gateway.providers.pop("OPENROUTER", None)
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _set_formulas(self, branch_id, table_id, formulas):
        snapshot = database.get_table_snapshot(table_id)
        sheet = snapshot["semantic"]["sheets"][0]
        rows = sorted(sheet["rows"], key=lambda item: item["position"])
        column = next(item for item in sheet["columns"] if item["name"] == "CALC")
        context = branch_context(branch_id)
        changes = [
            {"operation_type": "CELL_FORMULA_UPDATE", "sheet_id": sheet["sheet_id"],
             "row_id": row["row_id"], "column_id": column["column_id"], "new_formula": formula}
            for row, formula in zip(rows, formulas)
        ]
        return commit_semantic_delta(
            table_id=table_id, repository_id=context["repository_id"], branch_id=branch_id,
            expected_head_commit_id=snapshot["head_commit_id"], base_version=snapshot["version"],
            changes=changes, user_id=self.owner["user_id"], user_email=self.owner["email"],
            message="Set CALC formulas",
        )

    def test_resolve_cell_identity_maps_excel_address_to_stable_ids(self):
        conn = database._get_connection()
        try:
            branch_state = semantic_snapshot(conn, self.copy["table_id"])
        finally:
            conn.close()
        sheet = branch_state["sheets"][0]
        column = next(item for item in sheet["columns"] if item["name"] == "CALC")
        rows = sorted(sheet["rows"], key=lambda item: item["position"])

        # Row 3 (the pattern-broken row) is the 3rd data row -> Excel row 4
        # (row 1 is the header written by _write_branch_xlsx).
        identity = branch_comparison.resolve_cell_identity(branch_state, sheet["sheet_id"], "B4")
        self.assertIsNotNone(identity)
        self.assertEqual(rows[2]["row_id"], identity["row_id"])
        self.assertEqual(column["column_id"], identity["column_id"])

        self.assertIsNone(branch_comparison.resolve_cell_identity(branch_state, sheet["sheet_id"], "B1"))
        self.assertIsNone(branch_comparison.resolve_cell_identity(branch_state, "NOT_A_SHEET", "B4"))

    async def test_compare_branch_inventory_attributes_introduced_pattern_break(self):
        ai_gateway.providers["OPENROUTER"] = FakeRiskDriftProvider()
        result = await euc_narrative.compare_branch_inventory(
            self.organization_id, self.owner["user_id"], self.repository_id, self.copy["branch_id"],
        )

        introduced_rules = {item["rule_id"] for item in result["findings_introduced"]}
        self.assertIn("FORMULA_PATTERN_BREAK", introduced_rules)

        pattern_break = next(item for item in result["findings_introduced"] if item["rule_id"] == "FORMULA_PATTERN_BREAK")
        attribution = next((item for item in result["attribution"] if item["finding_id"] == pattern_break["finding_id"]), None)
        self.assertIsNotNone(attribution, "The introduced pattern-break finding should be attributed to a commit.")
        self.assertEqual(self.owner["email"], attribution["author_email"])
        self.assertIsNotNone(attribution["commit_id"])

        self.assertTrue(result["summary"])
        stored = euc_narrative.get_latest_comparison(self.copy["branch_id"])
        self.assertIsNotNone(stored)
        self.assertEqual(result["comparison_id"], stored["comparison_id"])
        self.assertEqual(len(result["findings_introduced"]), len(stored["findings_introduced"]))

    async def test_recomparison_upserts_a_new_row_not_a_duplicate(self):
        ai_gateway.providers["OPENROUTER"] = FakeRiskDriftProvider()
        first = await euc_narrative.compare_branch_inventory(
            self.organization_id, self.owner["user_id"], self.repository_id, self.copy["branch_id"],
        )
        second = await euc_narrative.compare_branch_inventory(
            self.organization_id, self.owner["user_id"], self.repository_id, self.copy["branch_id"],
        )
        self.assertNotEqual(first["comparison_id"], second["comparison_id"])
        latest = euc_narrative.get_latest_comparison(self.copy["branch_id"])
        self.assertEqual(second["comparison_id"], latest["comparison_id"])


if __name__ == "__main__":
    unittest.main()
