import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import database
from app.store import presence_store


def _iso(offset_seconds: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


class ExcelPresenceStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "presence.db"
        database.initialize_product_schema()

        self.owner = database.get_or_create_user("presence-owner@example.com")
        self.helper_seeker = database.get_or_create_user("presence-seeker@example.com")

        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_PRESENCE" (ROW_ID INTEGER PRIMARY KEY, VALUE TEXT)')
        conn.execute('INSERT INTO "QUEUE_BOARD_PRESENCE" VALUES (1, "seed")')
        conn.commit()
        conn.close()
        self.registered = database.register_dataset("QUEUE_BOARD_PRESENCE", self.owner["user_id"], "presence.xlsx", 1, 1)
        self.repository_id = self.registered["repository_id"]
        database.add_dataset_member("QUEUE_BOARD_PRESENCE", self.owner["user_id"], self.helper_seeker["email"], "editor")

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_excel_heartbeat_shows_online_immediately(self):
        database.touch_dataset_presence("QUEUE_BOARD_PRESENCE", self.owner["user_id"], "client-1", "excel", "viewing")

        entry = database.repository_activity_overview(self.repository_id)[0]

        self.assertTrue(entry["is_active_now"])
        self.assertEqual("ONLINE", entry["status"])

    def test_stale_heartbeat_falls_back_to_offline_without_a_background_job(self):
        """No sweep/cron is needed: status is derived reactively from
        heartbeat freshness at read time, so once the TTL has elapsed the
        very next read already reports OFFLINE."""
        conn = database._get_connection()
        try:
            stale = _iso(-(presence_store._EXCEL_PRESENCE_TTL_SECONDS + 30))
            conn.execute(
                "INSERT INTO DATASET_PRESENCE (TABLE_ID,USER_ID,CLIENT_ID,SURFACE,ACTIVITY,LAST_SEEN,STATUS) VALUES (?,?,?,?,?,?,?)",
                ("QUEUE_BOARD_PRESENCE", self.owner["user_id"], "client-1", "excel", "viewing", stale, "ONLINE"),
            )
            conn.commit()
        finally:
            conn.close()

        entry = database.repository_activity_overview(self.repository_id)[0]

        self.assertFalse(entry["is_active_now"])
        self.assertEqual("OFFLINE", entry["status"])

    def test_need_help_reflects_in_overview_and_notifies_the_owner(self):
        database.touch_dataset_presence(
            "QUEUE_BOARD_PRESENCE", self.helper_seeker["user_id"], "client-2", "excel", "viewing", "NEED_HELP"
        )

        entry = next(item for item in database.repository_activity_overview(self.repository_id) if item["user_id"] == self.helper_seeker["user_id"])
        self.assertEqual("NEED_HELP", entry["status"])
        self.assertTrue(entry["is_active_now"])

        notifications = database.list_notifications(self.owner["user_id"])
        need_help = [item for item in notifications if item["type"] == "NEED_HELP_REQUESTED"]
        self.assertEqual(1, len(need_help))
        self.assertIn(self.helper_seeker["email"], need_help[0]["title"])
        self.assertEqual(self.repository_id, need_help[0]["resource_id"])

    def test_need_help_only_notifies_once_per_transition_not_every_heartbeat(self):
        database.touch_dataset_presence("QUEUE_BOARD_PRESENCE", self.helper_seeker["user_id"], "client-2", "excel", "viewing", "NEED_HELP")
        database.touch_dataset_presence("QUEUE_BOARD_PRESENCE", self.helper_seeker["user_id"], "client-2", "excel", "viewing", "NEED_HELP")
        database.touch_dataset_presence("QUEUE_BOARD_PRESENCE", self.helper_seeker["user_id"], "client-2", "excel", "viewing", "NEED_HELP")

        need_help = [item for item in database.list_notifications(self.owner["user_id"]) if item["type"] == "NEED_HELP_REQUESTED"]
        self.assertEqual(1, len(need_help))

    def test_toggling_back_to_online_clears_need_help_status(self):
        database.touch_dataset_presence("QUEUE_BOARD_PRESENCE", self.helper_seeker["user_id"], "client-2", "excel", "viewing", "NEED_HELP")
        database.touch_dataset_presence("QUEUE_BOARD_PRESENCE", self.helper_seeker["user_id"], "client-2", "excel", "viewing", "ONLINE")

        entry = next(item for item in database.repository_activity_overview(self.repository_id) if item["user_id"] == self.helper_seeker["user_id"])
        self.assertEqual("ONLINE", entry["status"])

    def test_active_seconds_accumulate_across_heartbeats_and_reset_after_a_gap(self):
        conn = database._get_connection()
        try:
            t0 = _iso(-600)
            conn.execute(
                "INSERT INTO REPOSITORY_DAILY_ACTIVITY (USER_ID,REPOSITORY_ID,ACTIVITY_DATE,FIRST_SEEN_AT,LAST_SEEN_AT,ACTIVE_SECONDS,HEARTBEAT_COUNT) VALUES (?,?,?,?,?,?,?)",
                (self.owner["user_id"], self.repository_id, t0[:10], t0, t0, 0, 1),
            )
            conn.commit()
        finally:
            conn.close()

        # A heartbeat 60s later is within the TTL: the gap is added.
        conn = database._get_connection()
        try:
            presence_store._accumulate_repository_activity(conn, self.owner["user_id"], self.repository_id, _iso(-540))
            conn.commit()
        finally:
            conn.close()

        # A heartbeat after a long idle gap (Excel was closed) must NOT
        # count that idle stretch as active time.
        conn = database._get_connection()
        try:
            presence_store._accumulate_repository_activity(
                conn, self.owner["user_id"], self.repository_id, _iso(-540 + presence_store._EXCEL_PRESENCE_TTL_SECONDS + 3600)
            )
            conn.commit()
        finally:
            conn.close()

        row = database._get_connection()
        try:
            result = row.execute(
                "SELECT ACTIVE_SECONDS, HEARTBEAT_COUNT FROM REPOSITORY_DAILY_ACTIVITY WHERE USER_ID=? AND REPOSITORY_ID=? AND ACTIVITY_DATE=?",
                (self.owner["user_id"], self.repository_id, t0[:10]),
            ).fetchone()
        finally:
            row.close()

        # Only the first (in-TTL) 60s gap should have been added; the long
        # gap after Excel presumably closed contributes zero.
        self.assertEqual(60, result["ACTIVE_SECONDS"])
        self.assertEqual(3, result["HEARTBEAT_COUNT"])

    def test_heartbeat_from_an_open_branch_shows_online_and_accumulates_time(self):
        """The taskpane heartbeats against whichever data table is actually
        open — a branch's own DATA_TABLE_ID, not the repository's main
        TABLE_ID, when the user has a branch checked out. Both the live
        status and the active-time accumulation must still work in that
        case, not just when working directly on main."""
        conn = database._get_connection()
        try:
            now = _iso()
            conn.execute(
                """
                INSERT INTO BRANCHES (BRANCH_ID, REPOSITORY_ID, DATA_TABLE_ID, BRANCH_NAME, BRANCH_TYPE,
                    CREATED_BY, STATUS, CREATED_AT, UPDATED_AT)
                VALUES (?, ?, ?, ?, 'USER', ?, 'ACTIVE', ?, ?)
                """,
                ("BRANCH_PRESENCE_TEST", self.repository_id, "BRANCH_DATA_PRESENCE_TEST",
                 "users/owner/branch", self.owner["user_id"], now, now),
            )
            conn.commit()
        finally:
            conn.close()

        database.touch_dataset_presence("BRANCH_DATA_PRESENCE_TEST", self.owner["user_id"], "client-3", "excel", "editing")

        entry = next(item for item in database.repository_activity_overview(self.repository_id) if item["user_id"] == self.owner["user_id"])
        self.assertTrue(entry["is_active_now"])
        self.assertEqual("ONLINE", entry["status"])

        activity_row = database._get_connection()
        try:
            result = activity_row.execute(
                "SELECT ACTIVE_SECONDS FROM REPOSITORY_DAILY_ACTIVITY WHERE USER_ID=? AND REPOSITORY_ID=?",
                (self.owner["user_id"], self.repository_id),
            ).fetchone()
        finally:
            activity_row.close()
        self.assertIsNotNone(result)

    def test_matrix_returns_a_dense_per_user_per_day_grid(self):
        database.touch_dataset_presence("QUEUE_BOARD_PRESENCE", self.owner["user_id"], "client-1", "excel", "viewing")

        matrix = database.repository_activity_matrix(self.repository_id, days=7)

        self.assertEqual(7, len(matrix["dates"]))
        self.assertEqual(len(matrix["dates"]), len(set(matrix["dates"])))
        owner_row = next(item for item in matrix["users"] if item["user_id"] == self.owner["user_id"])
        self.assertEqual(7, len(owner_row["cells"]))
        self.assertGreaterEqual(owner_row["total_seconds"], 0)
        # The helper (never heartbeat) still appears with an all-zero row —
        # a dense grid, not a sparse one that silently drops idle members.
        seeker_row = next(item for item in matrix["users"] if item["user_id"] == self.helper_seeker["user_id"])
        self.assertEqual([{"seconds": 0, "focus": 0.0}] * 7, seeker_row["cells"])
        self.assertIsNone(seeker_row["average_focus"], "a member with zero active days has no meaningful focus average")

    def test_matrix_focus_ratio_distinguishes_continuous_from_fragmented_activity(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn = database._get_connection()
        try:
            # Owner: one continuous 2-hour stretch — active the whole span.
            conn.execute(
                "INSERT OR REPLACE INTO REPOSITORY_DAILY_ACTIVITY "
                "(USER_ID,REPOSITORY_ID,ACTIVITY_DATE,FIRST_SEEN_AT,LAST_SEEN_AT,ACTIVE_SECONDS,HEARTBEAT_COUNT) VALUES (?,?,?,?,?,?,?)",
                (self.owner["user_id"], self.repository_id, today, _iso(-7200), _iso(0), 7200, 120),
            )
            # Helper seeker: same total active time, but spread across a
            # much longer wall-clock span — popping in and out all day.
            conn.execute(
                "INSERT OR REPLACE INTO REPOSITORY_DAILY_ACTIVITY "
                "(USER_ID,REPOSITORY_ID,ACTIVITY_DATE,FIRST_SEEN_AT,LAST_SEEN_AT,ACTIVE_SECONDS,HEARTBEAT_COUNT) VALUES (?,?,?,?,?,?,?)",
                (self.helper_seeker["user_id"], self.repository_id, today, _iso(-28800), _iso(0), 7200, 40),
            )
            conn.commit()
        finally:
            conn.close()

        matrix = database.repository_activity_matrix(self.repository_id, days=1)
        owner_row = next(item for item in matrix["users"] if item["user_id"] == self.owner["user_id"])
        seeker_row = next(item for item in matrix["users"] if item["user_id"] == self.helper_seeker["user_id"])

        self.assertEqual(owner_row["cells"][0]["seconds"], seeker_row["cells"][0]["seconds"], "both have identical total active time")
        self.assertGreater(owner_row["cells"][0]["focus"], seeker_row["cells"][0]["focus"], "the continuous session must score a higher focus ratio than the fragmented one")
        self.assertAlmostEqual(owner_row["cells"][0]["focus"], 1.0, places=1)
        self.assertLess(seeker_row["cells"][0]["focus"], 0.5)


if __name__ == "__main__":
    unittest.main()
