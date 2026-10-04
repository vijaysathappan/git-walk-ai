import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import database
from app.main import app
from app.security import create_session_token


class DeviceFingerprintFunctionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "devices.db"
        database.initialize_product_schema()
        self.user = database.get_or_create_user("device-user@example.com")

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_record_device_fingerprint_requires_machine_id(self):
        self.assertIsNone(database.record_device_fingerprint(
            self.user["user_id"], session_id=None, ip_address="203.0.113.5",
            user_agent="pytest", machine_id=None,
        ))

    def test_record_device_fingerprint_upserts_on_user_and_machine(self):
        first = database.record_device_fingerprint(
            self.user["user_id"], session_id=None, ip_address="203.0.113.5",
            user_agent="pytest-a", machine_id="MACHINE-1",
        )
        second = database.record_device_fingerprint(
            self.user["user_id"], session_id=None, ip_address="198.51.100.9",
            user_agent="pytest-b", machine_id="MACHINE-1",
        )
        self.assertEqual(first["fingerprint_id"], second["fingerprint_id"])
        self.assertEqual("198.51.100.9", second["ip_address"])
        self.assertEqual("UNKNOWN", second["trust_status"])

        conn = database._get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM DEVICE_FINGERPRINTS WHERE USER_ID=?", (self.user["user_id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(1, count)

    def test_two_machines_produce_two_distinct_fingerprints(self):
        database.record_device_fingerprint(
            self.user["user_id"], session_id=None, ip_address="203.0.113.5",
            user_agent="laptop", machine_id="MACHINE-A",
        )
        database.record_device_fingerprint(
            self.user["user_id"], session_id=None, ip_address="198.51.100.9",
            user_agent="desktop", machine_id="MACHINE-B",
        )
        conn = database._get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM DEVICE_FINGERPRINTS WHERE USER_ID=?", (self.user["user_id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(2, count)

    def test_device_trust_status_defaults_to_unknown_then_can_be_blocked(self):
        device = database.record_device_fingerprint(
            self.user["user_id"], session_id=None, ip_address="203.0.113.5",
            user_agent="pytest", machine_id="MACHINE-1",
        )
        self.assertEqual("UNKNOWN", database.device_trust_status(self.user["user_id"], "MACHINE-1"))
        self.assertIsNone(database.device_trust_status(self.user["user_id"], None))
        self.assertIsNone(database.device_trust_status(self.user["user_id"], "NEVER-SEEN"))

        database.set_device_trust_status(device["fingerprint_id"], "BLOCKED")
        self.assertEqual("BLOCKED", database.device_trust_status(self.user["user_id"], "MACHINE-1"))

    def test_set_device_trust_status_rejects_unknown_value(self):
        device = database.record_device_fingerprint(
            self.user["user_id"], session_id=None, ip_address="203.0.113.5",
            user_agent="pytest", machine_id="MACHINE-1",
        )
        with self.assertRaises(ValueError):
            database.set_device_trust_status(device["fingerprint_id"], "MALICIOUS")

    def test_set_device_trust_status_missing_fingerprint_raises_keyerror(self):
        with self.assertRaises(KeyError):
            database.set_device_trust_status("FGP_DOES_NOT_EXIST", "TRUSTED")

    def test_list_repository_devices_scoped_to_repository_membership(self):
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_DEVICES" (ROW_ID INTEGER PRIMARY KEY, VALUE TEXT)')
        conn.execute('INSERT INTO "QUEUE_BOARD_DEVICES" VALUES (1, "seed")')
        conn.commit()
        conn.close()
        registered = database.register_dataset("QUEUE_BOARD_DEVICES", self.user["user_id"], "devices.xlsx", 1, 1)
        outsider = database.get_or_create_user("outsider@example.com")

        database.record_device_fingerprint(
            self.user["user_id"], session_id=None, ip_address="203.0.113.5",
            user_agent="owner-laptop", machine_id="OWNER-MACHINE",
        )
        database.record_device_fingerprint(
            outsider["user_id"], session_id=None, ip_address="198.51.100.1",
            user_agent="outsider-laptop", machine_id="OUTSIDER-MACHINE",
        )

        devices = database.list_repository_devices(registered["repository_id"])
        self.assertEqual(1, len(devices))
        self.assertEqual(self.user["user_id"], devices[0]["user_id"])


class RepositoryActivityOverviewTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "activity.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("activity-owner@example.com")
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_ACTIVITY" (ROW_ID INTEGER PRIMARY KEY, VALUE TEXT)')
        conn.execute('INSERT INTO "QUEUE_BOARD_ACTIVITY" VALUES (1, "seed")')
        conn.commit()
        conn.close()
        self.registered = database.register_dataset("QUEUE_BOARD_ACTIVITY", self.owner["user_id"], "activity.xlsx", 1, 1)

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_overview_lists_owner_with_role_and_device(self):
        database.record_device_fingerprint(
            self.owner["user_id"], session_id=None, ip_address="203.0.113.5",
            user_agent="pytest", machine_id="OWNER-MACHINE",
        )
        database.touch_dataset_presence("QUEUE_BOARD_ACTIVITY", self.owner["user_id"], "client-1", "excel", "editing")

        overview = database.repository_activity_overview(self.registered["repository_id"])

        self.assertEqual(1, len(overview))
        entry = overview[0]
        self.assertEqual("owner", entry["role"])
        self.assertTrue(entry["is_active_now"])
        self.assertIsNotNone(entry["device"])

    def test_browser_only_presence_is_not_live(self):
        """A browser tab left open on the repository must never show as
        ONLINE — only an actual open Excel taskpane (surface='excel')
        counts as live; browser presence is ignored entirely for status."""
        database.touch_dataset_presence("QUEUE_BOARD_ACTIVITY", self.owner["user_id"], "client-1", "browser", "viewing")

        entry = database.repository_activity_overview(self.registered["repository_id"])[0]

        self.assertFalse(entry["is_active_now"])
        self.assertEqual("OFFLINE", entry["status"])

    def test_overview_raises_for_unknown_repository(self):
        with self.assertRaises(ValueError):
            database.repository_activity_overview("REP_DOES_NOT_EXIST")


class DeviceApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "device_api.db"
        database.initialize_product_schema()
        self.client = TestClient(app)
        self.owner = database.get_or_create_user("device-api-owner@example.com")
        self.owner_token = create_session_token(self.owner["user_id"], self.owner["email"])
        self.outsider = database.get_or_create_user("device-api-outsider@example.com")
        self.outsider_token = create_session_token(self.outsider["user_id"], self.outsider["email"])

        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_DEVICE_API" (ROW_ID INTEGER PRIMARY KEY, VALUE TEXT)')
        conn.execute('INSERT INTO "QUEUE_BOARD_DEVICE_API" VALUES (1, "seed")')
        conn.commit()
        conn.close()
        self.registered = database.register_dataset("QUEUE_BOARD_DEVICE_API", self.owner["user_id"], "api.xlsx", 1, 1)
        database.record_device_fingerprint(
            self.owner["user_id"], session_id=None, ip_address="203.0.113.5",
            user_agent="pytest", machine_id="OWNER-MACHINE",
        )

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_owner_can_list_devices(self):
        response = self.client.get(
            f"/api/v1/repositories/QUEUE_BOARD_DEVICE_API/devices", headers=self._auth(self.owner_token),
        )
        self.assertEqual(200, response.status_code)
        devices = response.json()["devices"]
        self.assertEqual(1, len(devices))
        self.assertEqual("OWNER-MACHINE", devices[0]["machine_id"])

    def test_non_owner_is_forbidden(self):
        response = self.client.get(
            f"/api/v1/repositories/QUEUE_BOARD_DEVICE_API/devices", headers=self._auth(self.outsider_token),
        )
        self.assertEqual(404, response.status_code)

    def test_owner_can_block_then_trust_a_device(self):
        devices = self.client.get(
            f"/api/v1/repositories/QUEUE_BOARD_DEVICE_API/devices", headers=self._auth(self.owner_token),
        ).json()["devices"]
        fingerprint_id = devices[0]["fingerprint_id"]

        blocked = self.client.post(
            f"/api/v1/repositories/QUEUE_BOARD_DEVICE_API/devices/{fingerprint_id}/block",
            headers=self._auth(self.owner_token),
        )
        self.assertEqual(200, blocked.status_code)
        self.assertEqual("BLOCKED", blocked.json()["trust_status"])
        self.assertEqual("BLOCKED", database.device_trust_status(self.owner["user_id"], "OWNER-MACHINE"))

        trusted = self.client.post(
            f"/api/v1/repositories/QUEUE_BOARD_DEVICE_API/devices/{fingerprint_id}/trust",
            headers=self._auth(self.owner_token),
        )
        self.assertEqual(200, trusted.status_code)
        self.assertEqual("TRUSTED", trusted.json()["trust_status"])

    def test_blocked_device_is_rejected_at_login(self):
        device = database.record_device_fingerprint(
            self.owner["user_id"], session_id=None, ip_address="203.0.113.5",
            user_agent="pytest", machine_id="OWNER-MACHINE",
        )
        database.set_device_trust_status(device["fingerprint_id"], "BLOCKED")
        self.assertEqual("BLOCKED", database.device_trust_status(self.owner["user_id"], "OWNER-MACHINE"))

    def test_owner_can_view_activity_overview(self):
        response = self.client.get(
            f"/api/v1/repositories/QUEUE_BOARD_DEVICE_API/activity", headers=self._auth(self.owner_token),
        )
        self.assertEqual(200, response.status_code)
        activity = response.json()["activity"]
        self.assertEqual(1, len(activity))
        self.assertEqual("owner", activity[0]["role"])


if __name__ == "__main__":
    unittest.main()
