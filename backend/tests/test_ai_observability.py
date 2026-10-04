"""AI-specific observability: AI_REQUEST_ID <-> ambient request_id/trace_id
correlation in structured logs, and grounding_confidence /
insufficient_evidence metrics surfacing through the same
GET /api/v1/observability/metrics response every other metric uses."""

import json
import logging
import tempfile
import unittest
from pathlib import Path

from app import database
from app.ai.gateway import AIGateway
from app.ai.provider import LLMProvider, ProviderResult
from app.ai.service import AIService
from app.observability import new_request_context
from app.repositories.governance_store import operational_metrics
from app.secret_store import encrypt_secret


class ScriptedProvider(LLMProvider):
    provider_name = "OPENROUTER"

    def __init__(self, response_text: str):
        self.response_text = response_text

    async def complete(self, **kwargs) -> ProviderResult:
        return ProviderResult(self.response_text, input_tokens=60, output_tokens=20, reasoning_tokens=0)


class AIObservabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "ai_observability.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("observability-owner@example.com")
        import sqlite3
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_OBS" (ROW_ID INTEGER PRIMARY KEY, VALUE TEXT)')
        conn.execute('INSERT INTO "QUEUE_BOARD_OBS" VALUES (1, "seed")')
        conn.commit(); conn.close()
        self.repository = database.register_dataset("QUEUE_BOARD_OBS", self.owner["user_id"], "obs.xlsx", 1, 1)
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (self.repository["repository_id"],)
        ).fetchone()[0]
        conn.close()
        database.save_user_ai_settings(self.owner["user_id"], encrypt_secret("sk-or-obs-test"), "nvidia/nemotron-3-super-120b-a12b:free")
        conn = database._get_connection()
        try:
            AIService._ensure_settings(conn, self.organization_id, self.owner["user_id"])
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _valid_response(self, evidence_id: str) -> str:
        return json.dumps({
            "answer": "Grounded answer.",
            "evidence": [{"type": "REPOSITORY", "id": evidence_id}],
            "confidence": 0.9, "insufficient_evidence": False,
            "recommended_actions": [], "warnings": [],
        })

    async def test_ai_request_id_is_correlated_with_ambient_request_and_trace_id(self):
        """An operator following an X-Request-ID from an access log must be
        able to find the exact AI_REQUEST_ID it produced, and vice versa —
        both must appear in the SAME structured log line."""
        context = new_request_context()
        provider = ScriptedProvider(self._valid_response(self.repository["repository_id"]))
        gateway = AIGateway({"OPENROUTER": provider})
        with self.assertLogs("gitwalk", level="INFO") as captured:
            result = await gateway.generate(
                organization_id=self.organization_id, user_id=self.owner["user_id"],
                feature="ENTERPRISE_COPILOT", question="What is the repository status?",
                evidence=[{"type": "REPOSITORY", "id": self.repository["repository_id"]}],
                context_hash="test-hash-1",
            )
        # Parse the JSON payload directly out of each captured record,
        # rather than the formatted "LEVEL:logger:message" output string.
        payloads = [json.loads(record.getMessage()) for record in captured.records]
        matching = [p for p in payloads if p.get("ai_request_id") == result["ai_request_id"]]
        self.assertTrue(matching, "no log line correlated the ai_request_id at all")
        self.assertTrue(any(p["request_id"] == context.request_id for p in matching))
        self.assertTrue(any(p["trace_id"] == context.trace_id for p in matching))

    async def test_grounding_confidence_and_insufficient_evidence_metrics_are_recorded(self):
        provider = ScriptedProvider(self._valid_response(self.repository["repository_id"]))
        gateway = AIGateway({"OPENROUTER": provider})
        await gateway.generate(
            organization_id=self.organization_id, user_id=self.owner["user_id"],
            feature="ENTERPRISE_COPILOT", question="What is the repository status?",
            evidence=[{"type": "REPOSITORY", "id": self.repository["repository_id"]}],
            context_hash="test-hash-2",
        )
        conn = database._get_connection()
        try:
            names = {row[0] for row in conn.execute("SELECT DISTINCT METRIC_NAME FROM OPERATION_METRICS")}
        finally:
            conn.close()
        self.assertIn("ai_grounding_confidence", names)
        self.assertIn("ai_insufficient_evidence", names)

    async def test_ai_metrics_surface_through_the_observability_metrics_endpoint(self):
        """Confirms the metrics are not just written but actually exposed —
        GET /api/v1/observability/metrics groups OPERATION_METRICS by name
        generically, so recording under these two names is sufficient; this
        test proves that end-to-end rather than assuming it."""
        provider = ScriptedProvider(self._valid_response(self.repository["repository_id"]))
        gateway = AIGateway({"OPENROUTER": provider})
        await gateway.generate(
            organization_id=self.organization_id, user_id=self.owner["user_id"],
            feature="ENTERPRISE_COPILOT", question="What is the repository status?",
            evidence=[{"type": "REPOSITORY", "id": self.repository["repository_id"]}],
            context_hash="test-hash-3",
        )
        metrics = operational_metrics(hours=24)["metrics"]
        self.assertIn("ai_grounding_confidence", metrics)
        self.assertIn("ai_insufficient_evidence", metrics)
        self.assertGreater(metrics["ai_grounding_confidence"]["average"], 0)

    async def test_insufficient_evidence_metric_reflects_a_true_low_grounding_case(self):
        """The metric must track real gateway behavior, not just always
        emit 0 — force insufficient_evidence via zero evidence and confirm
        the recorded metric value is 1.0 for that request."""
        provider = ScriptedProvider(json.dumps({
            "answer": "No evidence available.", "evidence": [], "confidence": 0.0,
            "insufficient_evidence": True, "recommended_actions": [], "warnings": [],
        }))
        gateway = AIGateway({"OPENROUTER": provider})
        await gateway.generate(
            organization_id=self.organization_id, user_id=self.owner["user_id"],
            feature="ENTERPRISE_COPILOT", question="What changed in a repository with no evidence?",
            evidence=[], context_hash="test-hash-4",
        )
        conn = database._get_connection()
        try:
            row = conn.execute(
                "SELECT METRIC_VALUE FROM OPERATION_METRICS WHERE METRIC_NAME='ai_insufficient_evidence' ORDER BY CREATED_AT DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(1.0, row[0])


if __name__ == "__main__":
    unittest.main()
