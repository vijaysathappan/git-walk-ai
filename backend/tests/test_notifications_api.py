import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import database
from app.main import app
from app.security import create_session_token


class NotificationsApiTests(unittest.TestCase):
    """HTTP-layer tests for /api/v1/notifications -- status codes, auth,
    per-user scoping, and request validation. Core notification-table
    behavior (dedupe, preference suppression, side-effects from other
    features) is covered directly against the database layer in
    test_notifications.py."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "notifications_api.db"
        database.initialize_product_schema()
        self.client = TestClient(app)

        self.user = database.get_or_create_user("ntf-api-user@example.com")
        self.token = create_session_token(self.user["user_id"], self.user["email"])
        self.other = database.get_or_create_user("ntf-api-other@example.com")
        self.other_token = create_session_token(self.other["user_id"], self.other["email"])

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_requires_authentication(self):
        response = self.client.get("/api/v1/notifications")
        self.assertEqual(401, response.status_code)

    def test_list_notifications_returns_items_and_unread_count(self):
        database.create_notification(self.user["user_id"], "TEST_TYPE", "Title one", "Body one")
        database.create_notification(self.user["user_id"], "TEST_TYPE", "Title two", "Body two")

        response = self.client.get("/api/v1/notifications", headers=self._auth(self.token))

        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual(2, len(body["notifications"]))
        self.assertEqual(2, body["unread_count"])

    def test_list_notifications_is_scoped_to_caller(self):
        database.create_notification(self.user["user_id"], "TEST_TYPE", "Mine")
        database.create_notification(self.other["user_id"], "TEST_TYPE", "Not mine")

        response = self.client.get("/api/v1/notifications", headers=self._auth(self.token))

        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual(1, len(body["notifications"]))
        self.assertEqual("Mine", body["notifications"][0]["title"])

    def test_unread_only_filters_out_read_items(self):
        first = database.create_notification(self.user["user_id"], "TEST_TYPE", "Title one")
        database.create_notification(self.user["user_id"], "TEST_TYPE", "Title two")
        database.mark_notification_read(first["notification_id"], self.user["user_id"])

        response = self.client.get(
            "/api/v1/notifications", params={"unread_only": True}, headers=self._auth(self.token),
        )

        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual(1, len(body["notifications"]))
        self.assertEqual(1, body["unread_count"])

    def test_limit_is_bounded_between_1_and_200(self):
        too_high = self.client.get(
            "/api/v1/notifications", params={"limit": 201}, headers=self._auth(self.token),
        )
        self.assertEqual(422, too_high.status_code)

        too_low = self.client.get(
            "/api/v1/notifications", params={"limit": 0}, headers=self._auth(self.token),
        )
        self.assertEqual(422, too_low.status_code)

    def test_mark_notification_read_returns_status(self):
        created = database.create_notification(self.user["user_id"], "TEST_TYPE", "Title")

        response = self.client.post(
            f"/api/v1/notifications/{created['notification_id']}/read", headers=self._auth(self.token),
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("READ", response.json()["status"])
        unread = self.client.get(
            "/api/v1/notifications", params={"unread_only": True}, headers=self._auth(self.token),
        )
        self.assertEqual(0, unread.json()["unread_count"])

    def test_mark_unknown_notification_read_returns_404(self):
        response = self.client.post(
            "/api/v1/notifications/NTF_DOES_NOT_EXIST/read", headers=self._auth(self.token),
        )
        self.assertEqual(404, response.status_code)

    def test_cannot_mark_another_users_notification_read(self):
        created = database.create_notification(self.other["user_id"], "TEST_TYPE", "Theirs")

        response = self.client.post(
            f"/api/v1/notifications/{created['notification_id']}/read", headers=self._auth(self.token),
        )

        self.assertEqual(404, response.status_code)

    def test_read_all_marks_only_callers_notifications(self):
        database.create_notification(self.user["user_id"], "TEST_TYPE", "Mine one")
        database.create_notification(self.user["user_id"], "TEST_TYPE", "Mine two")
        database.create_notification(self.other["user_id"], "TEST_TYPE", "Not mine")

        response = self.client.post("/api/v1/notifications/read-all", headers=self._auth(self.token))

        self.assertEqual(200, response.status_code)
        self.assertEqual(2, response.json()["marked_read"])
        other_unread = self.client.get(
            "/api/v1/notifications", params={"unread_only": True}, headers=self._auth(self.other_token),
        )
        self.assertEqual(1, other_unread.json()["unread_count"])

    def test_get_preferences_defaults_to_enabled(self):
        response = self.client.get("/api/v1/notifications/preferences", headers=self._auth(self.token))

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["preferences"]["DEVICE_BLOCKED"])

    def test_update_preference_persists_and_is_scoped_to_caller(self):
        response = self.client.put(
            "/api/v1/notifications/preferences",
            headers=self._auth(self.token), json={"type": "DEVICE_BLOCKED", "enabled": False},
        )

        self.assertEqual(200, response.status_code)
        self.assertFalse(response.json()["preferences"]["DEVICE_BLOCKED"])

        other_prefs = self.client.get(
            "/api/v1/notifications/preferences", headers=self._auth(self.other_token),
        )
        self.assertTrue(other_prefs.json()["preferences"]["DEVICE_BLOCKED"])

    def test_update_preference_rejects_malformed_payload(self):
        response = self.client.put(
            "/api/v1/notifications/preferences",
            headers=self._auth(self.token), json={"type": "DEVICE_BLOCKED"},
        )
        self.assertEqual(422, response.status_code)


if __name__ == "__main__":
    unittest.main()
