import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database
from app.ai.catalog import NVIDIA_FREE_MODEL_DEFAULTS, free_nvidia_chat_models
from app.ai.gateway import AIGateway
from app.ai.provider import AIProviderError, LLMProvider, ProviderResult
from app.ai.service import AIService
from app.config import settings
from app.secret_store import encrypt_secret


class FakeProvider(LLMProvider):
    provider_name = "OPENROUTER"

    def __init__(self, evidence_id: str, invalid_first: bool = False, plain_output: bool = False):
        self.evidence_id = evidence_id
        self.invalid_first = invalid_first
        self.plain_output = plain_output
        self.calls = 0
        self.last_model = None
        self.last_response_schema = None

    async def complete(self, **kwargs) -> ProviderResult:
        self.calls += 1
        self.last_model = kwargs.get("model")
        self.last_response_schema = kwargs.get("response_schema")
        if self.invalid_first and self.calls == 1:
            return ProviderResult("not-json", 12, 3)
        if self.plain_output:
            return ProviderResult("A cautious advisory answer without structured citations.", 20, 8)
        content = json.dumps({
            "answer": "The repository evidence supports a controlled review.",
            "evidence": [
                {"type": "REPOSITORY", "id": self.evidence_id},
                {"type": "REPOSITORY", "id": "INVENTED_REFERENCE"},
            ],
            "confidence": 0.99,
            "insufficient_evidence": False,
            "recommended_actions": [{
                "title": "Review evidence", "rationale": "Keep policy and deterministic evidence authoritative.",
                "action_type": "REVIEW", "risk_level": "LOW",
            }],
            "warnings": [],
        })
        return ProviderResult(content, input_tokens=120, output_tokens=40, reasoning_tokens=10)


class Stage5AIPlatformTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "stage5.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("ai.owner@example.com")
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_AI" (ROW_ID INTEGER PRIMARY KEY, VALUE TEXT)')
        conn.execute('INSERT INTO "QUEUE_BOARD_AI" VALUES (1, "seed")')
        conn.commit(); conn.close()
        self.repository = database.register_dataset("QUEUE_BOARD_AI", self.owner["user_id"], "ai-governance.xlsx", 1, 1)
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (self.repository["repository_id"],)
        ).fetchone()[0]
        conn.close()
        database.save_user_ai_settings(self.owner["user_id"], encrypt_secret("sk-or-test-stage5"), settings.openrouter_model)

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def service(self, invalid_first: bool = False, plain_output: bool = False):
        provider = FakeProvider(self.repository["repository_id"], invalid_first, plain_output)
        return AIService(AIGateway({"OPENROUTER": provider})), provider

    async def test_grounded_chat_repairs_output_filters_citations_and_records_usage(self):
        service, provider = self.service(invalid_first=True)
        result = await service.chat(self.organization_id, self.owner["user_id"], {
            "question": "Explain the repository control posture with evidence",
            "repository_id": self.repository["repository_id"],
            "resource_type": "REPOSITORY", "resource_id": self.repository["repository_id"],
        })
        self.assertEqual(2, provider.calls)
        self.assertIsNotNone(provider.last_response_schema)
        self.assertEqual([self.repository["repository_id"]], [item["id"] for item in result["evidence"]])
        self.assertTrue(any("unverified" in warning for warning in result["warnings"]))
        self.assertGreater(result["confidence"], 0)
        self.assertGreaterEqual(result["latency_ms"], 0)
        self.assertEqual(170, sum(result["usage"].values()))
        conversation = service.conversations(self.organization_id, self.owner["user_id"], result["conversation_id"])
        self.assertEqual(["USER", "ASSISTANT"], [message["role"] for message in conversation["messages"]])
        usage = service.usage(self.organization_id, self.owner["user_id"])
        self.assertEqual(170, usage["tokens"]["total"])
        self.assertEqual(1, usage["requests"])

    async def test_cross_tenant_repository_is_denied_before_provider(self):
        outsider = database.get_or_create_user("ai.outsider@example.com")
        database.save_user_ai_settings(outsider["user_id"], encrypt_secret("sk-or-test-outsider"), settings.openrouter_model)
        service, provider = self.service()
        with self.assertRaises(PermissionError):
            await service.chat(self.organization_id, outsider["user_id"], {
                "question": "Read a repository I cannot access", "repository_id": self.repository["repository_id"],
            })
        self.assertEqual(0, provider.calls)

    async def test_selected_free_nvidia_model_is_used_and_paid_model_is_blocked(self):
        service, provider = self.service()
        selected = NVIDIA_FREE_MODEL_DEFAULTS[1]
        result = await service.chat(self.organization_id, self.owner["user_id"], {
            "question": "Use the selected model for this grounded response",
            "model": selected,
        })
        self.assertEqual(selected, provider.last_model)
        self.assertEqual(selected, result["model"])
        self.assertIsNone(provider.last_response_schema)

        with self.assertRaises(AIProviderError) as raised:
            await service.chat(self.organization_id, self.owner["user_id"], {
                "question": "A paid route must never reach the provider",
                "model": "openai/gpt-4.1-mini",
            })
        self.assertEqual("AI_MODEL_NOT_ALLOWED", raised.exception.code)
        self.assertEqual(1, provider.calls)

    def test_catalog_keeps_only_zero_price_nvidia_text_models(self):
        payload = {"data": [
            {"id": NVIDIA_FREE_MODEL_DEFAULTS[0], "pricing": {"prompt": "0", "completion": "0"},
             "architecture": {"output_modalities": ["text"]}},
            {"id": "nvidia/paid-model", "pricing": {"prompt": "0.1", "completion": "0.1"},
             "architecture": {"output_modalities": ["text"]}},
            {"id": "openai/not-nvidia:free", "pricing": {"prompt": "0", "completion": "0"},
             "architecture": {"output_modalities": ["text"]}},
            {"id": "nvidia/embedding:free", "pricing": {"prompt": "0", "completion": "0"},
             "architecture": {"output_modalities": ["embeddings"]}},
        ]}
        self.assertEqual([NVIDIA_FREE_MODEL_DEFAULTS[0]], free_nvidia_chat_models(payload))

    async def test_unstructured_free_model_response_returns_safe_advisory(self):
        service, provider = self.service(plain_output=True)
        result = await service.chat(self.organization_id, self.owner["user_id"], {
            "question": "Return a useful answer even when the free model ignores JSON",
        })
        self.assertEqual(3, provider.calls)
        self.assertTrue(result["insufficient_evidence"])
        self.assertEqual(0, result["confidence"])
        self.assertIn("cautious advisory", result["answer"])
        self.assertTrue(any("unstructured" in warning for warning in result["warnings"]))

    async def test_cached_answer_reports_delivery_latency_without_new_tokens(self):
        service, provider = self.service()
        payload = {"question": "Cache this governed answer and return it again"}
        live = await service.chat(self.organization_id, self.owner["user_id"], payload)
        cached = await service.chat(self.organization_id, self.owner["user_id"], payload)
        self.assertFalse(live["cache_hit"])
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(1, provider.calls)
        self.assertEqual(170, sum(live["usage"].values()))
        self.assertEqual(0, sum(cached["usage"].values()))
        self.assertGreaterEqual(cached["latency_ms"], 0)

    async def test_quota_blocks_provider_and_agent_tools_are_audited(self):
        service, provider = self.service()
        conn = database._get_connection()
        now = database._utcnow()
        conn.execute("INSERT OR REPLACE INTO AI_ORGANIZATION_SETTINGS VALUES (?,1,1,'[\"PUBLIC\",\"INTERNAL\"]',1000,1000,1,90,?,?)",
                     (self.organization_id, self.owner["user_id"], now))
        conn.execute("INSERT INTO AI_TOKEN_LEDGER VALUES ('TOK_QUOTA','REQ_QUOTA',?,?, 'TEST',NULL,1000,0,0,0,?)",
                     (self.organization_id, self.owner["user_id"], now))
        conn.commit(); conn.close()
        with self.assertRaises(AIProviderError) as raised:
            await service.chat(self.organization_id, self.owner["user_id"], {"question": "This must be blocked by quota"})
        self.assertEqual("AI_ORGANIZATION_QUOTA_EXCEEDED", raised.exception.code)
        self.assertEqual(0, provider.calls)

        conn = database._get_connection()
        conn.execute("DELETE FROM AI_TOKEN_LEDGER WHERE LEDGER_ID='TOK_QUOTA'")
        conn.commit(); conn.close()
        run = await service.run_agent(self.organization_id, self.owner["user_id"], {
            "agent_key": "INVESTIGATION_AGENT", "goal": "Investigate the governed repository state",
            "repository_id": self.repository["repository_id"], "resource_type": "REPOSITORY",
            "resource_id": self.repository["repository_id"],
            "requested_action": {"action_type": "RUN_INTEGRATION", "connection_id": "CONNECTION_REQUIRES_REVIEW"},
        })
        self.assertEqual("WAITING_CONFIRMATION", run["status"])
        self.assertEqual("PENDING_CONFIRMATION", run["actions"][0]["status"])
        conn = database._get_connection()
        try:
            tool_call = conn.execute("SELECT STATUS,AUTHORIZATION_DECISION FROM AI_TOOL_CALLS WHERE AGENT_RUN_ID=?", (run["agent_run_id"],)).fetchone()
            self.assertEqual(("COMPLETED", "ALLOWED"), tuple(tool_call))
            action = conn.execute("SELECT STATUS,REQUIRED_PERMISSION FROM AI_ACTIONS WHERE AGENT_RUN_ID=?", (run["agent_run_id"],)).fetchone()
            self.assertEqual(("PENDING_CONFIRMATION", "integration.execute"), tuple(action))
        finally: conn.close()

    def test_controls_and_safety_evaluation_are_deterministic(self):
        service, _ = self.service()
        administration = service.administration(self.organization_id, self.owner["user_id"])
        model_id = administration["models"][0]["model_id"]
        policy = service.update_model_policy(self.organization_id, self.owner["user_id"], "ENTERPRISE_COPILOT", {
            "model_role": administration["models"][0]["model_role"], "model_id": model_id,
            "allow_external": True, "allowed_classifications": ["PUBLIC", "INTERNAL"],
            "max_input_tokens": 24000, "max_output_tokens": 3000, "temperature": 0.1,
        })
        self.assertEqual(model_id, policy["model_id"])
        controls = service.generate_controls(self.organization_id, self.owner["user_id"], self.repository["repository_id"])
        self.assertEqual(0, controls["generated"])
        evaluation = service.run_evaluation(self.organization_id, self.owner["user_id"])
        self.assertEqual("PASSED", evaluation["status"])
        self.assertEqual(evaluation["total_cases"], evaluation["passed_cases"])
        self.assertEqual(1, len(service.evaluations(self.organization_id, self.owner["user_id"])))

    def test_policy_write_rejects_a_non_free_nvidia_model_at_save_time(self):
        """The honesty-gap fix (Phase 2 AI/LLM audit, task 1, Option A): a
        policy referencing a disallowed model must be rejected AT WRITE
        TIME with a clear, non-transient AIProviderError (which the API
        layer maps to 422) — not silently accepted and only discovered
        later when a request actually tries to route through it."""
        service, _ = self.service()
        conn = database._get_connection()
        try:
            now = database._utcnow()
            conn.execute(
                "INSERT INTO AI_MODELS VALUES (?, 'OPENROUTER','openai/gpt-4o','GPT-4o','REASONING','[\"CHAT\"]',NULL,0,0,1,0,1,10,?,?)",
                ("AIM_PAID_TEST", now, now),
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(AIProviderError) as ctx:
            service.update_model_policy(self.organization_id, self.owner["user_id"], "ENTERPRISE_COPILOT", {
                "model_role": "REASONING", "model_id": "AIM_PAID_TEST",
                "allow_external": True, "allowed_classifications": ["PUBLIC", "INTERNAL"],
                "max_input_tokens": 24000, "max_output_tokens": 3000, "temperature": 0.1,
            })
        self.assertEqual("AI_MODEL_NOT_ALLOWED", ctx.exception.code)
        self.assertFalse(ctx.exception.transient)
        self.assertIn("free-tier NVIDIA", str(ctx.exception))

        # The rejected write must not have persisted a policy at all.
        administration = service.administration(self.organization_id, self.owner["user_id"])
        self.assertFalse(
            any(p["feature"] == "ENTERPRISE_COPILOT" for p in administration["policies"])
        )
        # And the disabled/disallowed model must never appear as a pickable
        # option in the admin UI's model dropdown.
        self.assertNotIn("AIM_PAID_TEST", [m["model_id"] for m in administration["models"]])

    async def test_api_layer_maps_the_rejection_to_422(self):
        """End-to-end through the FastAPI route, not just the service
        function, since that's what an operator following the audit's
        curl/UI workflow actually hits."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.security import create_session_token

        conn = database._get_connection()
        try:
            now = database._utcnow()
            conn.execute(
                "INSERT INTO AI_MODELS VALUES (?, 'OPENROUTER','openai/gpt-4o','GPT-4o','REASONING','[\"CHAT\"]',NULL,0,0,1,0,1,10,?,?)",
                ("AIM_PAID_TEST2", now, now),
            )
            conn.commit()
        finally:
            conn.close()

        token = create_session_token(self.owner["user_id"], self.owner["email"])
        client = TestClient(app)
        response = client.put(
            f"/api/v1/ai-platform/administration/model-policies/ENTERPRISE_COPILOT?organization_id={self.organization_id}",
            json={
                "model_role": "REASONING", "model_id": "AIM_PAID_TEST2",
                "allow_external": True, "allowed_classifications": ["PUBLIC", "INTERNAL"],
                "max_input_tokens": 24000, "max_output_tokens": 3000, "temperature": 0.1,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(422, response.status_code)
        self.assertEqual("AI_MODEL_NOT_ALLOWED", response.json()["detail"]["code"])


if __name__ == "__main__":
    unittest.main()
