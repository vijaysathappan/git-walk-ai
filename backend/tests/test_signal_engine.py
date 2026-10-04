import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import database
from app.ai import signal_engine, signal_sources
from app.ai.gateway import ai_gateway
from app.ai.provider import LLMProvider, ProviderResult


class FakeExplainProvider(LLMProvider):
    provider_name = "OPENROUTER"

    async def complete(self, **kwargs) -> ProviderResult:
        content = json.dumps({
            "answer": "This connection has failed recent health checks and should be reviewed before it affects downstream freshness.",
            "evidence": [], "confidence": 0.72, "insufficient_evidence": False,
            "recommended_actions": [], "warnings": [],
        })
        return ProviderResult(content, input_tokens=45, output_tokens=25, reasoning_tokens=0)


class SignalEngineTests(unittest.IsolatedAsyncioTestCase):
    """Exercises the Signal Registry against real repositories/branches
    created through the actual application code paths (register_dataset,
    create_semantic_branch) -- not hand-rolled fixtures -- so these tests
    prove the engine against the same shapes of data a real deployment
    produces, matching how test_commit_ai_review.py / test_merge_conflict_
    agent.py already validate their own agents."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "signals.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("signal-owner@example.com")
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_SIGNALS" (ROW_ID INTEGER PRIMARY KEY, "TEAM" TEXT)')
        conn.executemany(
            'INSERT INTO "QUEUE_BOARD_SIGNALS" (ROW_ID, "TEAM") VALUES (?, ?)',
            [(i, f"T{i}") for i in range(1, 4)],
        )
        conn.commit()
        conn.close()
        registered = database.register_dataset("QUEUE_BOARD_SIGNALS", self.owner["user_id"], "signals.xlsx", 3, 1)
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (registered["repository_id"],)
        ).fetchone()[0]
        conn.close()
        self.repository_id = registered["repository_id"]
        self.table_id = "QUEUE_BOARD_SIGNALS"
        self._original_provider = ai_gateway.providers.get("OPENROUTER")

    def tearDown(self):
        if self._original_provider is not None:
            ai_gateway.providers["OPENROUTER"] = self._original_provider
        else:
            ai_gateway.providers.pop("OPENROUTER", None)
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _insight_row(self, dedupe_key: str):
        conn = database._get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM AI_INSIGHTS WHERE ORGANIZATION_ID=? AND DEDUPLICATION_KEY=?",
                (self.organization_id, dedupe_key),
            ).fetchone()
            return {key.lower(): row[key] for key in row.keys()} if row else None
        finally:
            conn.close()

    # ---------------------------------------------------------------
    # Built-in migration correctness: the whole point of this migration
    # is that it must be indistinguishable in behavior from the hardcoded
    # checks it replaces.
    # ---------------------------------------------------------------

    def test_exactly_five_builtin_signals_are_seeded_per_organization(self):
        signals = signal_engine.list_signal_definitions(self.organization_id, self.owner["user_id"])
        builtins = [item for item in signals if item["origin"] == "SYSTEM_BUILTIN"]
        self.assertEqual(5, len(builtins))
        self.assertTrue(all(item["enabled"] for item in builtins))

    def test_integration_health_signal_flags_unhealthy_connection(self):
        conn = database._get_connection()
        now = database._utcnow()
        conn.execute(
            "INSERT INTO INTEGRATION_CONNECTIONS VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("CONN_UNHEALTHY", self.organization_id, "REST", "Vendor Feed", "PROD", "{}", None,
             "ACTIVE", "UNHEALTHY", None, None, None, self.owner["user_id"], now, now),
        )
        conn.commit()
        conn.close()

        result = signal_engine.run_signal_scan(self.organization_id, self.owner["user_id"])
        self.assertEqual(1, result["generated"])

        insight = self._insight_row("CONNECTION_HEALTH:CONN_UNHEALTHY")
        self.assertIsNotNone(insight)
        self.assertEqual("HIGH", insight["severity"])
        self.assertEqual("SYSTEM_INTEGRATION_UNHEALTHY", insight["generated_by"])
        self.assertEqual("OPEN", insight["status"])

    def test_sync_conflict_queue_signal_is_critical_severity(self):
        conn = database._get_connection()
        now = database._utcnow()
        conn.execute(
            "INSERT INTO INTEGRATION_CONFLICTS VALUES (?,?,?,?,?,?,?,?,?,?,'OPEN',?,NULL,NULL)",
            ("CFL_1", self.organization_id, None, None, "VALUE_MISMATCH", None, None, None, None, None, now),
        )
        conn.commit()
        conn.close()

        signal_engine.run_signal_scan(self.organization_id, self.owner["user_id"])
        insight = self._insight_row("SYNC_CONFLICT:CFL_1")
        self.assertIsNotNone(insight)
        self.assertEqual("CRITICAL", insight["severity"])
        self.assertEqual("SYSTEM_SYNC_CONFLICT", insight["generated_by"])

    def test_rescanning_never_duplicates_an_already_open_insight(self):
        conn = database._get_connection()
        now = database._utcnow()
        conn.execute(
            "INSERT INTO INTEGRATION_CONNECTIONS VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("CONN_REPEAT", self.organization_id, "REST", "Repeat Feed", "PROD", "{}", None,
             "ACTIVE", "DEGRADED", None, None, None, self.owner["user_id"], now, now),
        )
        conn.commit()
        conn.close()

        signal_engine.run_signal_scan(self.organization_id, self.owner["user_id"])
        signal_engine.run_signal_scan(self.organization_id, self.owner["user_id"])
        signal_engine.run_signal_scan(self.organization_id, self.owner["user_id"])

        conn = database._get_connection()
        count = conn.execute(
            "SELECT COUNT(*) FROM AI_INSIGHTS WHERE ORGANIZATION_ID=? AND DEDUPLICATION_KEY=?",
            (self.organization_id, "CONNECTION_HEALTH:CONN_REPEAT"),
        ).fetchone()[0]
        conn.close()
        self.assertEqual(1, count, "re-scanning must update the same insight row, never duplicate it")

    def test_euc_finding_source_is_skipped_without_a_repository_id(self):
        # Matches the legacy behavior: EUC risk was only ever scanned when a
        # repository was explicitly selected -- never crashes, just yields nothing.
        conn = database._get_connection()
        try:
            matches = signal_sources.SOURCES["EUC_FINDING"].evaluate(
                conn, self.organization_id, {"field": "severity", "op": "in", "value": ["HIGH", "CRITICAL"]}, None,
            )
        finally:
            conn.close()
        self.assertEqual([], matches)

    # ---------------------------------------------------------------
    # Custom signal: full create -> scan -> insight lifecycle against a
    # REAL branch created through create_semantic_branch, not a fixture.
    # ---------------------------------------------------------------

    def test_custom_branch_activity_signal_flags_a_genuinely_idle_branch(self):
        branch = database.create_semantic_branch(self.table_id, "feature/idle-branch", None, self.owner["user_id"])
        old_timestamp = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        conn = database._get_connection()
        conn.execute("UPDATE BRANCHES SET CREATED_AT=? WHERE BRANCH_ID=?", (old_timestamp, branch["branch_id"]))
        conn.commit()
        conn.close()

        created = signal_engine.create_signal_definition(self.organization_id, self.owner["user_id"], {
            "name": "Idle branches past 30 days", "source": "BRANCH_ACTIVITY", "severity": "MEDIUM",
            "condition": {"field": "idle_days", "op": "older_than_days", "value": 30},
        })
        self.assertTrue(created["signal_key"].startswith("CUSTOM_"))

        result = signal_engine.run_signal_scan(self.organization_id, self.owner["user_id"])
        self.assertGreaterEqual(result["generated"], 1)

        insight = self._insight_row(f"BRANCH_ACTIVITY:{branch['branch_id']}")
        self.assertIsNotNone(insight)
        self.assertEqual("MEDIUM", insight["severity"])
        self.assertEqual(created["signal_key"], insight["generated_by"])

    def test_custom_signal_does_not_flag_a_freshly_created_branch(self):
        branch = database.create_semantic_branch(self.table_id, "feature/fresh-branch", None, self.owner["user_id"])
        signal_engine.create_signal_definition(self.organization_id, self.owner["user_id"], {
            "name": "Idle branches past 30 days", "source": "BRANCH_ACTIVITY", "severity": "MEDIUM",
            "condition": {"field": "idle_days", "op": "older_than_days", "value": 30},
        })
        signal_engine.run_signal_scan(self.organization_id, self.owner["user_id"])
        insight = self._insight_row(f"BRANCH_ACTIVITY:{branch['branch_id']}")
        self.assertIsNone(insight, "a branch created moments ago must never be flagged as idle")

    def test_disabling_a_custom_signal_stops_it_from_running(self):
        branch = database.create_semantic_branch(self.table_id, "feature/idle-disabled", None, self.owner["user_id"])
        old_timestamp = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        conn = database._get_connection()
        conn.execute("UPDATE BRANCHES SET CREATED_AT=? WHERE BRANCH_ID=?", (old_timestamp, branch["branch_id"]))
        conn.commit()
        conn.close()

        created = signal_engine.create_signal_definition(self.organization_id, self.owner["user_id"], {
            "name": "Idle branches (disable test)", "source": "BRANCH_ACTIVITY", "severity": "MEDIUM",
            "condition": {"field": "idle_days", "op": "older_than_days", "value": 30},
        })
        signal_engine.update_signal_definition(self.organization_id, self.owner["user_id"], created["signal_id"], {"enabled": False})
        signal_engine.run_signal_scan(self.organization_id, self.owner["user_id"])
        insight = self._insight_row(f"BRANCH_ACTIVITY:{branch['branch_id']}")
        self.assertIsNone(insight, "a disabled signal must not produce insights")

    def test_deleting_a_custom_signal_removes_its_tool_registration(self):
        created = signal_engine.create_signal_definition(self.organization_id, self.owner["user_id"], {
            "name": "Temporary signal", "source": "DEVICE_TRUST", "severity": "HIGH",
            "condition": {"field": "trust_status", "op": "in", "value": ["BLOCKED"]},
        })
        conn = database._get_connection()
        tool_name = f"signal.{created['signal_key'].lower()}"
        exists_before = conn.execute("SELECT 1 FROM AI_TOOLS WHERE TOOL_NAME=?", (tool_name,)).fetchone()
        conn.close()
        self.assertIsNotNone(exists_before)

        signal_engine.delete_signal_definition(self.organization_id, self.owner["user_id"], created["signal_id"])
        conn = database._get_connection()
        exists_after = conn.execute("SELECT 1 FROM AI_TOOLS WHERE TOOL_NAME=?", (tool_name,)).fetchone()
        remaining_definition = conn.execute("SELECT 1 FROM AI_SIGNAL_DEFINITIONS WHERE SIGNAL_ID=?", (created["signal_id"],)).fetchone()
        conn.close()
        self.assertIsNone(exists_after)
        self.assertIsNone(remaining_definition)

    def test_builtin_signal_cannot_be_deleted_or_have_its_condition_edited(self):
        signals = signal_engine.list_signal_definitions(self.organization_id, self.owner["user_id"])
        builtin = next(item for item in signals if item["origin"] == "SYSTEM_BUILTIN")
        with self.assertRaises(ValueError):
            signal_engine.delete_signal_definition(self.organization_id, self.owner["user_id"], builtin["signal_id"])
        with self.assertRaises(ValueError):
            signal_engine.update_signal_definition(
                self.organization_id, self.owner["user_id"], builtin["signal_id"],
                {"condition": {"field": "health_status", "op": "in", "value": ["HEALTHY"]}},
            )
        # Disabling a builtin, however, is explicitly still allowed.
        updated = signal_engine.update_signal_definition(self.organization_id, self.owner["user_id"], builtin["signal_id"], {"enabled": False})
        self.assertFalse(updated["enabled"])

    # ---------------------------------------------------------------
    # Validation: the whitelisted-condition model must reject anything
    # outside its schema before it is ever stored, not just at query time.
    # ---------------------------------------------------------------

    def test_create_signal_rejects_unknown_source(self):
        with self.assertRaises(ValueError):
            signal_engine.create_signal_definition(self.organization_id, self.owner["user_id"], {
                "name": "Bad source", "source": "DROP_TABLE_USERS", "severity": "HIGH",
                "condition": {"field": "x", "op": "equals", "value": "y"},
            })

    def test_create_signal_rejects_value_outside_the_enum(self):
        with self.assertRaises(ValueError):
            signal_engine.create_signal_definition(self.organization_id, self.owner["user_id"], {
                "name": "Bad enum value", "source": "DEVICE_TRUST", "severity": "HIGH",
                "condition": {"field": "trust_status", "op": "in", "value": ["SUPER_ADMIN_BACKDOOR"]},
            })

    def test_create_signal_rejects_wrong_field_for_source(self):
        with self.assertRaises(ValueError):
            signal_engine.create_signal_definition(self.organization_id, self.owner["user_id"], {
                "name": "Wrong field", "source": "BRANCH_ACTIVITY", "severity": "MEDIUM",
                "condition": {"field": "health_status", "op": "in", "value": ["UNHEALTHY"]},
            })

    def test_create_signal_rejects_out_of_range_number(self):
        with self.assertRaises(ValueError):
            signal_engine.create_signal_definition(self.organization_id, self.owner["user_id"], {
                "name": "Too many days", "source": "BRANCH_ACTIVITY", "severity": "MEDIUM",
                "condition": {"field": "idle_days", "op": "older_than_days", "value": 99999},
            })

    def test_create_signal_rejects_empty_name(self):
        with self.assertRaises(ValueError):
            signal_engine.create_signal_definition(self.organization_id, self.owner["user_id"], {
                "name": "   ", "source": "DEVICE_TRUST", "severity": "HIGH",
                "condition": {"field": "trust_status", "op": "in", "value": ["BLOCKED"]},
            })

    # ---------------------------------------------------------------
    # Permission gating and tenant isolation ("no leakage" is the
    # explicit product requirement this whole feature was built under).
    # ---------------------------------------------------------------

    def test_create_signal_denied_for_a_user_with_no_role_in_the_organization(self):
        outsider = database.get_or_create_user("outsider@example.com")
        with self.assertRaises(PermissionError):
            signal_engine.create_signal_definition(self.organization_id, outsider["user_id"], {
                "name": "Should not be allowed", "source": "DEVICE_TRUST", "severity": "HIGH",
                "condition": {"field": "trust_status", "op": "in", "value": ["BLOCKED"]},
            })

    def test_custom_signal_tool_is_scoped_to_its_own_organization_only(self):
        # A second, genuinely distinct organization: register_dataset for a
        # different user bootstraps its own organization automatically
        # (ensure_repository_security derives org id from the creator).
        other_owner = database.get_or_create_user("other-owner@example.com")
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_OTHER_ORG" (ROW_ID INTEGER PRIMARY KEY, "TEAM" TEXT)')
        conn.execute('INSERT INTO "QUEUE_BOARD_OTHER_ORG" (ROW_ID, "TEAM") VALUES (1, "T1")')
        conn.commit()
        conn.close()
        other_registered = database.register_dataset("QUEUE_BOARD_OTHER_ORG", other_owner["user_id"], "other.xlsx", 1, 1)
        conn = database._get_connection()
        other_organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (other_registered["repository_id"],)
        ).fetchone()[0]
        conn.close()
        self.assertNotEqual(self.organization_id, other_organization_id)

        created = signal_engine.create_signal_definition(self.organization_id, self.owner["user_id"], {
            "name": "Only visible to my org", "source": "DEVICE_TRUST", "severity": "HIGH",
            "condition": {"field": "trust_status", "op": "in", "value": ["BLOCKED"]},
        })
        tool_name = f"signal.{created['signal_key'].lower()}"

        from app.ai.service import ai_service
        my_admin = ai_service.administration(self.organization_id, self.owner["user_id"])
        other_admin = ai_service.administration(other_organization_id, other_owner["user_id"])
        self.assertIn(tool_name, [tool["tool_name"] for tool in my_admin["tools"]])
        self.assertNotIn(tool_name, [tool["tool_name"] for tool in other_admin["tools"]],
                          "a custom signal's tool must never leak into another organization's catalogue")
        # Shared system built-in tools remain visible everywhere.
        self.assertIn("get_repository_summary", [tool["tool_name"] for tool in other_admin["tools"]])

    # ---------------------------------------------------------------
    # The dormant AI-explanation wiring, now connected.
    # ---------------------------------------------------------------

    async def test_explain_insight_produces_and_stores_a_recommendation(self):
        ai_gateway.providers["OPENROUTER"] = FakeExplainProvider()
        from app.config import settings
        from app.secret_store import encrypt_secret
        database.save_user_ai_settings(self.owner["user_id"], encrypt_secret("sk-or-test-signals"), settings.openrouter_model)

        conn = database._get_connection()
        now = database._utcnow()
        conn.execute(
            "INSERT INTO INTEGRATION_CONNECTIONS VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("CONN_EXPLAIN", self.organization_id, "REST", "Explain Feed", "PROD", "{}", None,
             "ACTIVE", "UNHEALTHY", None, None, None, self.owner["user_id"], now, now),
        )
        conn.commit()
        conn.close()
        signal_engine.run_signal_scan(self.organization_id, self.owner["user_id"])
        insight = self._insight_row("CONNECTION_HEALTH:CONN_EXPLAIN")
        self.assertIsNotNone(insight)

        result = await signal_engine.explain_insight(self.organization_id, self.owner["user_id"], insight["insight_id"])
        self.assertTrue(result["explanation"])
        self.assertNotIn("{", result["explanation"])  # never raw JSON

        conn = database._get_connection()
        stored = conn.execute(
            "SELECT * FROM AI_RECOMMENDATIONS WHERE INSIGHT_ID=?", (insight["insight_id"],)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(stored)
        self.assertEqual(result["explanation"], stored["RECOMMENDATION"])


if __name__ == "__main__":
    unittest.main()
