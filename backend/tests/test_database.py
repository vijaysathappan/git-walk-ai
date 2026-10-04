import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from app import database
from app.secret_store import decrypt_secret, encrypt_secret


class DatabaseSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "queue_board.db"

        conn = sqlite3.connect(database.DB_PATH)
        conn.execute(
            'CREATE TABLE "QUEUE_BOARD_TEST" ('
            'ROW_ID INTEGER PRIMARY KEY, "BATTING_TEAM" TEXT, '
            '"BOWLING_TEAM" TEXT)'
        )
        conn.execute(
            'INSERT INTO "QUEUE_BOARD_TEST" '
            '(ROW_ID, "BATTING_TEAM", "BOWLING_TEAM") '
            "VALUES (1, 'KKR', 'RCB')"
        )
        conn.execute(
            'INSERT INTO "QUEUE_BOARD_TEST" '
            '(ROW_ID, "BATTING_TEAM", "BOWLING_TEAM") '
            "VALUES (2, 'CSK', 'MI')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_update_returns_committed_readback(self):
        result = database.update_cell(
            "QUEUE_BOARD_TEST", "BATTING_TEAM", 1, "SRH"
        )

        self.assertTrue(result["changed"])
        self.assertEqual("SRH", result["persisted_value"])
        self.assertEqual(
            "SRH", database.read_cell("QUEUE_BOARD_TEST", "BATTING_TEAM", 1)
        )

    def test_dataframe_dates_are_serialized_for_sqlite(self):
        frame = pd.DataFrame(
            {
                "ORDER_DATE": [pd.Timestamp("2026-08-14 09:30:00"), pd.NaT],
                "QUANTITY": [1, 2],
            }
        )

        result = database.create_sqlite_table_from_df("QUEUE_BOARD_DATES", frame)

        self.assertEqual(2, result["row_count"])
        conn = sqlite3.connect(database.DB_PATH)
        try:
            values = conn.execute(
                'SELECT "ORDER_DATE" FROM "QUEUE_BOARD_DATES" ORDER BY ROW_ID'
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual("2026-08-14 09:30:00", values[0][0])
        self.assertIsNone(values[1][0])

    def test_repeated_update_is_reported_as_no_change(self):
        database.update_cell("QUEUE_BOARD_TEST", "BATTING_TEAM", 1, "SRH")
        result = database.update_cell(
            "QUEUE_BOARD_TEST", "BATTING_TEAM", 1, "SRH"
        )

        self.assertFalse(result["changed"])
        self.assertEqual("SRH", result["persisted_value"])

    def _registered_user(self):
        database.initialize_product_schema()
        user = database.get_or_create_user("editor@example.com")
        database.register_dataset(
            "QUEUE_BOARD_TEST", user["user_id"], "teams.xlsx", 2, 2
        )
        return user

    def test_bulk_update_is_versioned_and_audited(self):
        user = self._registered_user()
        result = database.apply_bulk_updates(
            "QUEUE_BOARD_TEST",
            [
                {"row_id": 1, "column_name": "BOWLING_TEAM", "new_value": "CSK"},
                {"row_id": 2, "column_name": "BATTING_TEAM", "new_value": "SRH"},
            ],
            user["user_id"],
            user["email"],
            source="test",
            commit_message="Updated two Excel cells",
        )

        self.assertEqual(1, result["version"])
        self.assertEqual(2, result["changed_count"])
        self.assertEqual("CSK", database.read_cell("QUEUE_BOARD_TEST", "BOWLING_TEAM", 1))
        self.assertEqual("SRH", database.read_cell("QUEUE_BOARD_TEST", "BATTING_TEAM", 2))
        history = database.get_audit_history("QUEUE_BOARD_TEST")
        self.assertEqual(2, len(history))
        self.assertEqual({result["batch_id"]}, {item["batch_id"] for item in history})
        self.assertEqual({user["user_id"]}, {item["user_id"] for item in history})

    def test_bulk_update_rolls_back_every_cell_when_one_change_is_invalid(self):
        user = self._registered_user()

        with self.assertRaises(ValueError):
            database.apply_bulk_updates(
                "QUEUE_BOARD_TEST",
                [
                    {"row_id": 1, "column_name": "BATTING_TEAM", "new_value": "SRH"},
                    {"row_id": 99, "column_name": "BATTING_TEAM", "new_value": "GT"},
                ],
                user["user_id"],
                user["email"],
            )

        self.assertEqual("KKR", database.read_cell("QUEUE_BOARD_TEST", "BATTING_TEAM", 1))
        self.assertEqual([], database.get_audit_history("QUEUE_BOARD_TEST"))

    def test_change_set_can_be_reverted_as_a_new_version(self):
        user = self._registered_user()
        committed = database.apply_bulk_updates(
            "QUEUE_BOARD_TEST",
            [{"row_id": 1, "column_name": "BATTING_TEAM", "new_value": "SRH"}],
            user["user_id"],
            user["email"],
        )
        reverted = database.rollback_batch(
            "QUEUE_BOARD_TEST", committed["batch_id"], user["user_id"], user["email"]
        )

        self.assertEqual(2, reverted["version"])
        self.assertEqual("KKR", database.read_cell("QUEUE_BOARD_TEST", "BATTING_TEAM", 1))
        self.assertEqual("rollback", database.get_audit_history("QUEUE_BOARD_TEST")[0]["source"])

    def test_login_code_is_single_use(self):
        database.initialize_product_schema()
        expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        database.create_login_code("editor@example.com", "hashed-code", expires)

        self.assertTrue(database.consume_login_code("editor@example.com", "hashed-code"))
        self.assertFalse(database.consume_login_code("editor@example.com", "hashed-code"))

    def test_login_code_locks_after_failed_attempts(self):
        database.initialize_product_schema()
        expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        database.create_login_code("locked@example.com", "correct-hash", expires)

        for _ in range(5):
            self.assertFalse(database.consume_login_code("locked@example.com", "wrong-hash"))
        self.assertFalse(database.consume_login_code("locked@example.com", "correct-hash"))

    def test_auth_session_can_be_revoked(self):
        database.initialize_product_schema()
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        database.create_auth_session("SES_TEST", "USR_TEST", "token-hash", expires)
        self.assertTrue(database.auth_session_is_active("SES_TEST", "USR_TEST", "token-hash"))
        database.revoke_auth_session("SES_TEST", "USR_TEST")
        self.assertFalse(database.auth_session_is_active("SES_TEST", "USR_TEST", "token-hash"))

    def test_dataset_roles_separate_read_and_write_access(self):
        owner = self._registered_user()
        viewer = database.get_or_create_user("viewer@example.com")
        editor = database.get_or_create_user("collaborator@example.com")

        database.add_dataset_member(
            "QUEUE_BOARD_TEST", owner["user_id"], viewer["email"], "viewer"
        )
        database.add_dataset_member(
            "QUEUE_BOARD_TEST", owner["user_id"], editor["email"], "editor"
        )

        self.assertTrue(database.user_can_access_table("QUEUE_BOARD_TEST", viewer["user_id"]))
        self.assertFalse(database.user_can_edit_table("QUEUE_BOARD_TEST", viewer["user_id"]))
        self.assertTrue(database.user_can_edit_table("QUEUE_BOARD_TEST", editor["user_id"]))
        self.assertIn(
            "QUEUE_BOARD_TEST",
            {item["table_id"] for item in database.list_datasets(viewer["user_id"])},
        )

    def test_branch_access_owner_member_creator_and_non_member(self):
        """Pins user_can_access_branch's current behavior before task 6
        consolidates it with the other two access-check functions: MAIN is
        open to any dataset member, a non-MAIN branch is only open to its
        creator/the owner (not just any dataset member), and a non-member
        is refused both."""
        database.initialize_product_schema()
        owner = database.get_or_create_user("branch-owner@example.com")
        registered = database.register_dataset(
            "QUEUE_BOARD_TEST", owner["user_id"], "teams.xlsx", 2, 2
        )
        main_branch_id = registered["main_branch_id"]

        viewer = database.get_or_create_user("branch-viewer@example.com")
        editor = database.get_or_create_user("branch-editor@example.com")
        outsider = database.get_or_create_user("branch-outsider@example.com")
        database.add_dataset_member("QUEUE_BOARD_TEST", owner["user_id"], viewer["email"], "viewer")
        database.add_dataset_member("QUEUE_BOARD_TEST", owner["user_id"], editor["email"], "editor")

        branch = database.create_semantic_branch(
            "QUEUE_BOARD_TEST", "feature/branch-access-test", None, editor["user_id"]
        )
        feature_branch_id = branch["branch_id"]

        # MAIN is open to every dataset member, including a viewer.
        self.assertTrue(database.user_can_access_branch(main_branch_id, owner["user_id"]))
        self.assertTrue(database.user_can_access_branch(main_branch_id, viewer["user_id"]))
        self.assertTrue(database.user_can_access_branch(main_branch_id, editor["user_id"]))
        self.assertFalse(database.user_can_access_branch(main_branch_id, outsider["user_id"]))

        # A non-MAIN branch is only open to its creator and the owner, not
        # every dataset member (a viewer who didn't create it is refused).
        self.assertTrue(database.user_can_access_branch(feature_branch_id, editor["user_id"]))
        self.assertTrue(database.user_can_access_branch(feature_branch_id, owner["user_id"]))
        self.assertFalse(database.user_can_access_branch(feature_branch_id, viewer["user_id"]))
        self.assertFalse(database.user_can_access_branch(feature_branch_id, outsider["user_id"]))

    def test_workbook_commit_applies_full_diff_as_one_version(self):
        user = self._registered_user()
        result = database.apply_workbook_commit(
            table_id="QUEUE_BOARD_TEST",
            base_version=0,
            updates=[
                {"row_id": 1, "column_name": "BATTING_TEAM", "new_value": "SRH"}
            ],
            insert_rows=[
                {"BATTING_TEAM": "GT", "BOWLING_TEAM": "LSG", "NOTES": "new row"}
            ],
            delete_row_ids=[2],
            new_columns=["NOTES"],
            delete_columns=[],
            user_id=user["user_id"],
            user_email=user["email"],
            source="test_commit",
            commit_message="Reworked team sheet",
        )

        self.assertEqual(1, result["version"])
        self.assertEqual(1, result["changed_updates"])
        self.assertEqual(1, result["inserted_count"])
        self.assertEqual(1, result["deleted_count"])
        self.assertEqual(["NOTES"], result["columns_added"])
        snapshot = database.get_table_snapshot("QUEUE_BOARD_TEST")
        self.assertEqual(2, snapshot["total"])
        self.assertIn("NOTES", snapshot["columns"])
        rows = {row[0]: row for row in snapshot["rows"]}
        self.assertNotIn(2, rows)
        self.assertEqual("SRH", rows[1][1])
        inserted = rows[result["inserted_row_ids"][0]]
        self.assertEqual("GT", inserted[1])
        self.assertEqual("new row", inserted[3])
        original = database.get_table_snapshot("QUEUE_BOARD_TEST", version=0)
        self.assertEqual(2, original["total"])
        self.assertNotIn("NOTES", original["columns"])
        self.assertEqual("KKR", original["rows"][0][1])
        statuses = {item["status"] for item in database.get_audit_history("QUEUE_BOARD_TEST")}
        self.assertTrue({"UPDATED", "INSERTED", "DELETED", "COLUMN_ADDED"}.issubset(statuses))

    def test_workbook_commit_rejects_stale_base_version(self):
        user = self._registered_user()
        common = {
            "table_id": "QUEUE_BOARD_TEST",
            "insert_rows": [],
            "delete_row_ids": [],
            "new_columns": [],
            "delete_columns": [],
            "user_id": user["user_id"],
            "user_email": user["email"],
            "source": "test_commit",
            "commit_message": "Versioned edit",
        }
        database.apply_workbook_commit(
            base_version=0,
            updates=[{"row_id": 1, "column_name": "BATTING_TEAM", "new_value": "SRH"}],
            **common,
        )

        with self.assertRaises(database.VersionConflictError) as raised:
            database.apply_workbook_commit(
                base_version=0,
                updates=[{"row_id": 1, "column_name": "BATTING_TEAM", "new_value": "GT"}],
                **common,
            )

        self.assertEqual(1, raised.exception.current_version)
        self.assertEqual("SRH", database.read_cell("QUEUE_BOARD_TEST", "BATTING_TEAM", 1))

    def test_workbook_change_set_rollback_restores_rows_and_schema(self):
        user = self._registered_user()
        committed = database.apply_workbook_commit(
            table_id="QUEUE_BOARD_TEST",
            base_version=0,
            updates=[{"row_id": 1, "column_name": "BATTING_TEAM", "new_value": "SRH"}],
            insert_rows=[{"BATTING_TEAM": "GT", "NOTES": "temporary"}],
            delete_row_ids=[2],
            new_columns=["NOTES"],
            delete_columns=[],
            user_id=user["user_id"],
            user_email=user["email"],
            source="test_commit",
            commit_message="Temporary restructuring",
        )
        reverted = database.rollback_batch(
            "QUEUE_BOARD_TEST", committed["batch_id"], user["user_id"], user["email"]
        )

        restored = database.get_table_snapshot("QUEUE_BOARD_TEST")
        self.assertEqual(2, reverted["version"])
        self.assertEqual(0, reverted["restored_version"])
        self.assertEqual(["ROW_ID", "BATTING_TEAM", "BOWLING_TEAM"], restored["columns"])
        self.assertEqual([[1, "KKR", "RCB"], [2, "CSK", "MI"]], restored["rows"])

    def test_presence_tracks_and_removes_active_dataset_user(self):
        user = self._registered_user()
        database.touch_dataset_presence(
            "QUEUE_BOARD_TEST", user["user_id"], "browser_client_123", "browser", "viewing"
        )
        active = database.list_active_presence("QUEUE_BOARD_TEST")
        self.assertEqual(1, len(active))
        self.assertEqual(user["user_id"], active[0]["user_id"])
        self.assertEqual(["browser"], active[0]["surfaces"])

        database.remove_dataset_presence(
            "QUEUE_BOARD_TEST", user["user_id"], "browser_client_123"
        )
        self.assertEqual([], database.list_active_presence("QUEUE_BOARD_TEST"))

    def test_user_ai_key_is_encrypted_at_rest_and_round_trips(self):
        user = self._registered_user()
        raw_key = "sk-or-v1-test-secret-value"
        encrypted = encrypt_secret(raw_key)
        database.save_user_ai_settings(user["user_id"], encrypted, "vendor/model")

        stored = database.get_user_ai_settings(user["user_id"])
        self.assertNotIn(raw_key, stored["api_key_encrypted"])
        self.assertEqual(raw_key, decrypt_secret(stored["api_key_encrypted"]))
        self.assertEqual("vendor/model", stored["model"])

    def test_pending_workspace_invitation_activates_on_first_login(self):
        owner = self._registered_user()
        invited = database.add_dataset_member(
            "QUEUE_BOARD_TEST", owner["user_id"], "new.member@example.com", "editor"
        )
        self.assertEqual("pending", invited["status"])
        before = database.get_workspace_snapshot("QUEUE_BOARD_TEST")
        self.assertEqual(1, len(before["invitations"]))

        member = database.get_or_create_user("new.member@example.com")
        after = database.get_workspace_snapshot("QUEUE_BOARD_TEST")
        self.assertEqual([], after["invitations"])
        self.assertIn(member["user_id"], {item["user_id"] for item in after["members"]})
        self.assertTrue(database.user_can_edit_table("QUEUE_BOARD_TEST", member["user_id"]))
        self.assertGreater(after["revision"], before["revision"])

    def test_workspace_owner_can_revoke_active_member(self):
        owner = self._registered_user()
        member = database.get_or_create_user("member@example.com")
        database.add_dataset_member(
            "QUEUE_BOARD_TEST", owner["user_id"], member["email"], "viewer"
        )
        database.remove_dataset_member(
            "QUEUE_BOARD_TEST", owner["user_id"], member["email"]
        )

        self.assertFalse(database.user_can_access_table("QUEUE_BOARD_TEST", member["user_id"]))
        workspace = database.get_workspace_snapshot("QUEUE_BOARD_TEST")
        self.assertNotIn(member["user_id"], {item["user_id"] for item in workspace["members"]})

    def test_personal_branch_is_isolated_signed_and_main_is_protected(self):
        user = self._registered_user()
        working_copy = database.create_working_copy(
            "QUEUE_BOARD_TEST", user["user_id"], user["email"]
        )

        self.assertTrue(working_copy["table_id"].startswith("BRANCH_DATA_"))
        self.assertFalse(database.user_can_edit_table("QUEUE_BOARD_TEST", user["user_id"]))
        self.assertTrue(database.user_can_edit_table(working_copy["table_id"], user["user_id"]))
        database.update_cell(
            working_copy["table_id"], "BATTING_TEAM", 1, "SRH"
        )
        self.assertEqual(
            "KKR", database.read_cell("QUEUE_BOARD_TEST", "BATTING_TEAM", 1)
        )
        verified = database.validate_working_copy(
            table_id=working_copy["table_id"],
            user_id=user["user_id"],
            repository_id=working_copy["repository_id"],
            branch_id=working_copy["branch_id"],
            working_copy_id=working_copy["working_copy_id"],
            base_commit_id=working_copy["base_commit_id"],
            issued_at=working_copy["issued_at"],
            signature=working_copy["signature"],
        )
        self.assertEqual(working_copy["branch_id"], verified["branch_id"])
        with self.assertRaises(PermissionError):
            database.validate_working_copy(
                table_id=working_copy["table_id"],
                user_id="USR_ATTACKER",
                repository_id=working_copy["repository_id"],
                branch_id=working_copy["branch_id"],
                working_copy_id=working_copy["working_copy_id"],
                base_commit_id=working_copy["base_commit_id"],
                issued_at=working_copy["issued_at"],
                signature=working_copy["signature"],
            )

    def test_categories_and_repository_metadata_are_queryable(self):
        user = self._registered_user()
        category = database.create_category("Payments", "Payment operations", "CAT_HOME")
        move_result = database.move_repository("QUEUE_BOARD_TEST", category["category_id"], user["user_id"])
        repository = database.get_repository("QUEUE_BOARD_TEST", user["user_id"])

        # Pins the exact shape the API layer depends on — a prior version
        # of move_repository() omitted "repository_id", which the
        # PATCH /repositories/{table_id}/category route reads unconditionally
        # to record an audit event; the missing key raised an uncaught
        # KeyError there, surfaced to the client as a raw HTTP 500 on every
        # single business-area change.
        self.assertEqual(repository["repository_id"], move_result["repository_id"])
        self.assertEqual(category["category_id"], repository["category_id"])
        self.assertEqual("Payments", repository["category_name"])
        self.assertEqual(1, len(repository["sheets"]))
        self.assertIn(
            category["category_id"],
            {item["category_id"] for item in database.list_categories()},
        )

    def test_change_business_area_api_endpoint_does_not_500(self):
        """End-to-end through the real FastAPI route, not just the service
        function — a unit test on move_repository() alone wouldn't catch a
        route/service dict-key mismatch like the one this pins."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.security import create_session_token

        user = self._registered_user()
        category = database.create_category("Finance", "Finance operations", "CAT_HOME")
        token = create_session_token(user["user_id"], user["email"])
        client = TestClient(app)

        response = client.patch(
            "/api/v1/repositories/QUEUE_BOARD_TEST/category",
            json={"category_id": category["category_id"]},
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(category["category_id"], response.json()["category_id"])


if __name__ == "__main__":
    unittest.main()
