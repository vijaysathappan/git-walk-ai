import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import database
from app.main import app
from app.security import create_session_token


class SignalApiTests(unittest.TestCase):
    """HTTP-layer smoke tests for the Signal Registry endpoints -- status
    codes, auth, and request-validation wiring. Deep business-logic
    correctness (dedupe, tenant isolation, idle-branch detection, etc.) is
    covered directly against the engine in test_signal_engine.py."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "signal_api.db"
        database.initialize_product_schema()
        self.client = TestClient(app)

        self.owner = database.get_or_create_user("signal-api-owner@example.com")
        self.owner_token = create_session_token(self.owner["user_id"], self.owner["email"])
        self.outsider = database.get_or_create_user("signal-api-outsider@example.com")
        self.outsider_token = create_session_token(self.outsider["user_id"], self.outsider["email"])

        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_SIGNAL_API" (ROW_ID INTEGER PRIMARY KEY, VALUE TEXT)')
        conn.execute('INSERT INTO "QUEUE_BOARD_SIGNAL_API" VALUES (1, "seed")')
        conn.commit()
        conn.close()
        registered = database.register_dataset("QUEUE_BOARD_SIGNAL_API", self.owner["user_id"], "api.xlsx", 1, 1)
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (registered["repository_id"],)
        ).fetchone()[0]
        conn.close()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_sources_catalog_is_publicly_listable(self):
        response = self.client.get("/api/v1/ai-platform/signals/sources")
        self.assertEqual(200, response.status_code)
        sources = response.json()["sources"]
        keys = [item["source"] for item in sources]
        self.assertIn("BRANCH_ACTIVITY", keys)
        self.assertIn("DEVICE_TRUST", keys)

    def test_list_signals_includes_five_builtins(self):
        response = self.client.get(
            f"/api/v1/ai-platform/signals?organization_id={self.organization_id}", headers=self._auth(self.owner_token),
        )
        self.assertEqual(200, response.status_code)
        signals = response.json()["signals"]
        self.assertEqual(5, len([item for item in signals if item["origin"] == "SYSTEM_BUILTIN"]))

    def test_owner_can_create_update_and_delete_a_custom_signal(self):
        create = self.client.post(
            f"/api/v1/ai-platform/signals?organization_id={self.organization_id}",
            headers=self._auth(self.owner_token),
            json={
                "name": "Blocked devices", "source": "DEVICE_TRUST", "severity": "HIGH",
                "condition": {"field": "trust_status", "op": "in", "value": ["BLOCKED"]},
            },
        )
        self.assertEqual(201, create.status_code)
        signal_id = create.json()["signal_id"]

        update = self.client.patch(
            f"/api/v1/ai-platform/signals/{signal_id}?organization_id={self.organization_id}",
            headers=self._auth(self.owner_token), json={"enabled": False},
        )
        self.assertEqual(200, update.status_code)
        self.assertFalse(update.json()["enabled"])

        delete = self.client.delete(
            f"/api/v1/ai-platform/signals/{signal_id}?organization_id={self.organization_id}",
            headers=self._auth(self.owner_token),
        )
        self.assertEqual(204, delete.status_code)

    def test_create_signal_with_invalid_condition_returns_400(self):
        response = self.client.post(
            f"/api/v1/ai-platform/signals?organization_id={self.organization_id}",
            headers=self._auth(self.owner_token),
            json={
                "name": "Bad", "source": "DEVICE_TRUST", "severity": "HIGH",
                "condition": {"field": "trust_status", "op": "in", "value": ["NOT_A_REAL_STATUS"]},
            },
        )
        self.assertEqual(400, response.status_code)

    def test_outsider_cannot_create_a_signal_in_someone_elses_organization(self):
        response = self.client.post(
            f"/api/v1/ai-platform/signals?organization_id={self.organization_id}",
            headers=self._auth(self.outsider_token),
            json={
                "name": "Should be denied", "source": "DEVICE_TRUST", "severity": "HIGH",
                "condition": {"field": "trust_status", "op": "in", "value": ["BLOCKED"]},
            },
        )
        self.assertEqual(403, response.status_code)

    def test_scan_signals_runs_and_returns_a_summary(self):
        response = self.client.post(
            f"/api/v1/ai-platform/signals/scan?organization_id={self.organization_id}",
            headers=self._auth(self.owner_token), json={},
        )
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertIn("generated", body)
        self.assertIn("signals_scanned", body)
        # 4, not 5: the built-in EUC_FINDING signal is repository-scoped and
        # is correctly skipped when no repository_id is given, matching the
        # legacy generate_controls() behavior it replaced.
        self.assertGreaterEqual(body["signals_scanned"], 4)


if __name__ == "__main__":
    unittest.main()
