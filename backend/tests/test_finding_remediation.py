import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database
from app.ai import finding_remediation
from app.ai.gateway import ai_gateway
from app.ai.provider import LLMProvider, ProviderResult
from app.ai.service import ai_service
from app.config import settings
from app.euc.branch_comparison import snapshot_branch_for_comparison
from app.euc.intelligence import IntelligenceService
from app.repositories.commit_store import commit_semantic_delta
from app.repositories.merge_store import branch_context
from app.secret_store import encrypt_secret


class FakeRemediationProvider(LLMProvider):
    provider_name = "OPENROUTER"

    def __init__(self, status: str = "REMEDIATION_PLANNED"):
        self.status = status
        self.calls = 0

    async def complete(self, **kwargs) -> ProviderResult:
        self.calls += 1
        content = json.dumps({
            "answer": "This broken reference should be repointed at the correct sheet.",
            "evidence": [], "confidence": 0.77, "insufficient_evidence": False,
            "recommended_actions": [{
                "title": "Recommended lifecycle status",
                "rationale": "The formula references a sheet that does not exist; schedule a fix with the sheet owner.",
                "action_type": self.status, "risk_level": "HIGH",
            }],
            "warnings": [],
        })
        return ProviderResult(content, input_tokens=70, output_tokens=30, reasoning_tokens=0)


class UnsafeStatusProvider(LLMProvider):
    """A model that tries to recommend RESOLVED directly -- not an allowed
    agent recommendation, since only a human may claim a fix is verified."""
    provider_name = "OPENROUTER"

    async def complete(self, **kwargs) -> ProviderResult:
        content = json.dumps({
            "answer": "This is already fixed.",
            "evidence": [], "confidence": 0.9, "insufficient_evidence": False,
            "recommended_actions": [{
                "title": "Resolved", "rationale": "Already fine.", "action_type": "RESOLVED", "risk_level": "LOW",
            }],
            "warnings": [],
        })
        return ProviderResult(content, input_tokens=40, output_tokens=15, reasoning_tokens=0)


class FindingRemediationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "finding_remediation.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("remediation-owner@example.com")
        database.save_user_ai_settings(self.owner["user_id"], encrypt_secret("sk-or-test-remediation"), settings.openrouter_model)

        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_REMEDIATION" (ROW_ID INTEGER PRIMARY KEY, "SCORE" INTEGER, "CALC" INTEGER)')
        conn.executemany(
            'INSERT INTO "QUEUE_BOARD_REMEDIATION" (ROW_ID, "SCORE", "CALC") VALUES (?, ?, ?)',
            [(i, i * 10, 0) for i in range(1, 4)],
        )
        conn.commit()
        conn.close()
        registered = database.register_dataset("QUEUE_BOARD_REMEDIATION", self.owner["user_id"], "remediation.xlsx", 3, 2)
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (registered["repository_id"],)
        ).fetchone()[0]
        conn.close()
        self.repository_id = registered["repository_id"]
        self.main_branch_id = registered["main_branch_id"]
        self.table_id = "QUEUE_BOARD_REMEDIATION"

        self._set_formulas(self.main_branch_id, self.table_id, ["=NOTASHEET!Z1", "=NOTASHEET!Z2", "=NOTASHEET!Z3"])
        baseline = snapshot_branch_for_comparison(
            self.repository_id, self.main_branch_id, "main", self.table_id, self.owner["user_id"],
        )
        self.euc_id = baseline["euc_id"]
        self.finding_id = next(f["finding_id"] for f in baseline["findings"] if f["severity"] in ("HIGH", "CRITICAL"))

        self.intelligence_service = IntelligenceService()
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

    async def test_propose_remediation_stores_a_recommendation(self):
        ai_gateway.providers["OPENROUTER"] = FakeRemediationProvider("REMEDIATION_PLANNED")

        result = await finding_remediation.propose_remediation(
            self.organization_id, self.owner["user_id"], self.euc_id, self.finding_id,
        )

        self.assertEqual("PROPOSED", result["status"])
        self.assertEqual("REMEDIATION_PLANNED", result["recommended_status"])
        self.assertGreater(result["confidence"], 0)
        self.assertIn("sheet", result["reason"].lower())
        stored = finding_remediation.list_remediations(self.euc_id, self.finding_id)
        self.assertEqual(1, len(stored))
        self.assertEqual("PROPOSED", stored[0]["status"])

    async def test_agent_cannot_recommend_resolved_directly(self):
        ai_gateway.providers["OPENROUTER"] = UnsafeStatusProvider()

        result = await finding_remediation.propose_remediation(
            self.organization_id, self.owner["user_id"], self.euc_id, self.finding_id,
        )

        self.assertNotEqual("RESOLVED", result["recommended_status"])
        self.assertEqual("ACKNOWLEDGED", result["recommended_status"])

    async def test_apply_remediation_uses_the_same_update_finding_path_as_a_manual_change(self):
        ai_gateway.providers["OPENROUTER"] = FakeRemediationProvider("REMEDIATION_PLANNED")
        await finding_remediation.propose_remediation(
            self.organization_id, self.owner["user_id"], self.euc_id, self.finding_id,
        )

        prepared = finding_remediation.prepare_apply_action(
            self.organization_id, self.owner["user_id"], self.euc_id, self.finding_id,
        )
        self.assertEqual("PENDING_CONFIRMATION", prepared["status"])
        self.assertEqual("APPLY_FINDING_REMEDIATION", prepared["action_type"])

        confirmed = await ai_service.confirm_action(self.organization_id, self.owner["user_id"], prepared["action_id"])
        self.assertEqual("EXECUTED", confirmed["status"])

        finding = self.intelligence_service.finding_detail(self.euc_id, self.owner["user_id"], self.finding_id)
        self.assertEqual("REMEDIATION_PLANNED", finding["status"])
        self.assertEqual(1, len(finding["actions"]))
        self.assertIn("sheet", finding["actions"][0]["reason"].lower())

        remediation = finding_remediation.list_remediations(self.euc_id, self.finding_id)[0]
        self.assertEqual("APPLIED", remediation["status"])
        self.assertIsNotNone(remediation["applied_at"])

    async def test_applying_an_accepted_risk_recommendation_sets_an_expiry(self):
        ai_gateway.providers["OPENROUTER"] = FakeRemediationProvider("ACCEPTED_RISK")
        await finding_remediation.propose_remediation(
            self.organization_id, self.owner["user_id"], self.euc_id, self.finding_id,
        )
        prepared = finding_remediation.prepare_apply_action(
            self.organization_id, self.owner["user_id"], self.euc_id, self.finding_id,
        )
        await ai_service.confirm_action(self.organization_id, self.owner["user_id"], prepared["action_id"])

        finding = self.intelligence_service.finding_detail(self.euc_id, self.owner["user_id"], self.finding_id)
        self.assertEqual("ACCEPTED_RISK", finding["status"])
        self.assertIsNotNone(finding["accepted_until"])

    async def test_prepare_apply_without_a_proposal_raises(self):
        with self.assertRaises(KeyError):
            finding_remediation.prepare_apply_action(
                self.organization_id, self.owner["user_id"], self.euc_id, self.finding_id,
            )


if __name__ == "__main__":
    unittest.main()
