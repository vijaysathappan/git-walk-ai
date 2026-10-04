"""Tests for the repository permanent-deletion pipeline
(app/services/repository_purge_service.py): export-then-email-then-notify-
then-hard-delete, for both the manual (owner delete) and automatic
(inactivity/soft-delete sweep) entry points, plus the legal-hold safety
gate and the multi-level closure that finds deeply-nested child rows
(merge conflicts, EUC findings) that aren't tagged with REPOSITORY_ID
directly.
"""

import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import database
from app.services.repository_purge_service import (
    LegalHoldBlocksDeletion,
    find_purge_candidates,
    purge_repository,
    sweep_repositories_for_deletion,
)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class RepositoryPurgeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.export_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "purge.db"
        database.initialize_product_schema()

        from app.config import settings
        self.original_export_dir = settings.repository_purge_export_dir
        settings.repository_purge_export_dir = self.export_dir.name

        self.owner = database.get_or_create_user(f"purge-owner-{uuid.uuid4().hex[:8]}@example.com")
        self.table_id = "QUEUE_BOARD_PURGE"
        conn = database._get_connection()
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{self.table_id}"')
            conn.execute(f'CREATE TABLE "{self.table_id}" (ROW_ID INTEGER PRIMARY KEY AUTOINCREMENT, NAME TEXT)')
            conn.execute(f'INSERT INTO "{self.table_id}" (NAME) VALUES (?)', ("Row 1",))
            conn.commit()
        finally:
            conn.close()
        registered = database.register_dataset(self.table_id, self.owner["user_id"], "purge.xlsx", 1, 1)
        self.repository_id = registered["repository_id"]
        self.main_branch_id = registered["main_branch_id"]

    def tearDown(self):
        from app.config import settings
        settings.repository_purge_export_dir = self.original_export_dir
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()
        self.export_dir.cleanup()

    def _seed_deep_merge_conflict_chain(self):
        """Merge request -> conflict -> AI suggestion — none of the last
        two carry REPOSITORY_ID directly, proving the closure finds them."""
        conn = database._get_connection()
        try:
            now = database._utcnow()
            head = conn.execute(
                "SELECT HEAD_COMMIT_ID FROM BRANCHES WHERE BRANCH_ID=?", (self.main_branch_id,)
            ).fetchone()
            head_commit_id = head[0] if head and head[0] else f"CMT_{uuid.uuid4().hex[:12].upper()}"
            merge_request_id = f"MR_{uuid.uuid4().hex[:12].upper()}"
            conn.execute(
                """INSERT INTO MERGE_REQUESTS VALUES
                   (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)""",
                (merge_request_id, self.repository_id, self.main_branch_id, self.main_branch_id,
                 head_commit_id, head_commit_id, head_commit_id, self.owner["user_id"],
                 "Test MR", "desc", "OPEN", "CONFLICTED", "PENDING", now, now),
            )
            conflict_id = f"CFL_{uuid.uuid4().hex[:12].upper()}"
            conn.execute(
                """INSERT INTO MERGE_CONFLICTS VALUES
                   (?,?,NULL,NULL,NULL,?,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'OPEN',?)""",
                (conflict_id, merge_request_id, "CELL_VALUE_CONFLICT", now),
            )
            suggestion_id = f"SUG_{uuid.uuid4().hex[:12].upper()}"
            conn.execute(
                """INSERT INTO MERGE_CONFLICT_AI_SUGGESTIONS VALUES
                   (?,?,?,?,NULL,?,?,?,?,NULL,'PROPOSED',?,NULL,NULL)""",
                (suggestion_id, merge_request_id, conflict_id, "KEEP_MAIN", "rationale",
                 0.8, "LOW", "[]", now),
            )
            conn.commit()
        finally:
            conn.close()
        return merge_request_id, conflict_id, suggestion_id

    def _seed_deep_euc_finding_chain(self):
        """EUC asset -> intelligence run -> finding -> occurrence — three
        levels deep, none but the asset itself carries REPOSITORY_ID."""
        conn = database._get_connection()
        try:
            now = database._utcnow()
            euc_id = f"EUC_{uuid.uuid4().hex[:12].upper()}"
            conn.execute(
                """INSERT INTO EUC_ASSETS VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,NULL)""",
                (euc_id, self.repository_id, "test.xlsx", "XLSX", "application/xlsx",
                 "hash1", "hash2", "structhash", 1024, "COMPLETED", self.owner["user_id"], now, now),
            )
            run_id = f"IRUN_{uuid.uuid4().hex[:12].upper()}"
            conn.execute(
                """INSERT INTO EUC_INTELLIGENCE_RUNS VALUES
                   (?,?,NULL,?,?,?,?,?,?,'COMPLETED',100,NULL,0,0,0,0,0,0,NULL,NULL,NULL,'{}','[]',?,?,NULL)""",
                (run_id, euc_id, "ANALYSIS_X", "DEP_X", "engine1", "rules1", "profile1", "v1", now, now),
            )
            finding_id = f"FND_{uuid.uuid4().hex[:12].upper()}"
            conn.execute(
                """INSERT INTO EUC_FINDINGS VALUES
                   (?,?,?,?,?,?,'OPEN',?,?,NULL,NULL,0,NULL,NULL,NULL,?,?)""",
                (finding_id, euc_id, "fingerprint1", "RULE_1", "PATTERN_BREAK", "Test finding",
                 run_id, run_id, now, now),
            )
            conn.execute(
                """INSERT INTO EUC_FINDING_OCCURRENCES VALUES (?,?,?,?,NULL,NULL,NULL,?,NULL,?,?,?)""",
                (run_id, finding_id, "HIGH", 0.9, "Occurrence description", "manifesthash", "{}", now),
            )
            conn.commit()
        finally:
            conn.close()
        return euc_id, run_id, finding_id

    def test_export_captures_top_level_and_deeply_nested_rows(self):
        merge_request_id, conflict_id, suggestion_id = self._seed_deep_merge_conflict_chain()
        euc_id, run_id, finding_id = self._seed_deep_euc_finding_chain()

        from app.services.repository_purge_service import export_repository_traces
        conn = database._get_connection()
        try:
            export = export_repository_traces(conn, self.repository_id)
        finally:
            conn.close()

        self.assertEqual(self.repository_id, export["repository"]["repository_id"])
        self.assertTrue(any(row["merge_request_id"] == merge_request_id for row in export["tables"]["MERGE_REQUESTS"]))
        # Deeply nested rows, none carrying REPOSITORY_ID directly:
        self.assertTrue(any(row["conflict_id"] == conflict_id for row in export["tables"]["MERGE_CONFLICTS"]))
        self.assertTrue(any(row["suggestion_id"] == suggestion_id for row in export["tables"]["MERGE_CONFLICT_AI_SUGGESTIONS"]))
        self.assertTrue(any(row["euc_id"] == euc_id for row in export["tables"]["EUC_ASSETS"]))
        self.assertTrue(any(row["intelligence_run_id"] == run_id for row in export["tables"]["EUC_INTELLIGENCE_RUNS"]))
        self.assertTrue(any(row["finding_id"] == finding_id for row in export["tables"]["EUC_FINDINGS"]))
        self.assertTrue(any(row["finding_id"] == finding_id for row in export["tables"]["EUC_FINDING_OCCURRENCES"]))
        self.assertIn(self.table_id, export["physical_data"])

    def test_purge_deletes_top_level_and_deeply_nested_rows(self):
        merge_request_id, conflict_id, suggestion_id = self._seed_deep_merge_conflict_chain()
        euc_id, run_id, finding_id = self._seed_deep_euc_finding_chain()

        result = purge_repository(self.repository_id, self.owner["user_id"], "TEST_REASON")
        self.assertEqual("PURGED", result["status"])

        conn = database._get_connection()
        try:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (self.repository_id,)
            ).fetchone())
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM MERGE_REQUESTS WHERE MERGE_REQUEST_ID=?", (merge_request_id,)
            ).fetchone())
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM MERGE_CONFLICTS WHERE CONFLICT_ID=?", (conflict_id,)
            ).fetchone())
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM MERGE_CONFLICT_AI_SUGGESTIONS WHERE SUGGESTION_ID=?", (suggestion_id,)
            ).fetchone())
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM EUC_ASSETS WHERE EUC_ID=?", (euc_id,)
            ).fetchone())
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM EUC_INTELLIGENCE_RUNS WHERE INTELLIGENCE_RUN_ID=?", (run_id,)
            ).fetchone())
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM EUC_FINDINGS WHERE FINDING_ID=?", (finding_id,)
            ).fetchone())
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM EUC_FINDING_OCCURRENCES WHERE FINDING_ID=?", (finding_id,)
            ).fetchone())
            # The physical data table itself is gone.
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (self.table_id,)
            ).fetchone())
            # DATASET_REGISTRY (legacy TABLE_ID-keyed) is gone too.
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM DATASET_REGISTRY WHERE TABLE_ID=?", (self.table_id,)
            ).fetchone())
        finally:
            conn.close()

    def test_purge_writes_json_export_file_before_deleting(self):
        result = purge_repository(self.repository_id, self.owner["user_id"], "TEST_REASON")
        export_path = Path(result["export_file"])
        self.assertTrue(export_path.exists())
        payload = json.loads(export_path.read_text(encoding="utf-8"))
        self.assertEqual(self.repository_id, payload["repository"]["repository_id"])

    def test_purge_creates_an_in_app_notification_for_the_owner(self):
        purge_repository(self.repository_id, self.owner["user_id"], "TEST_REASON")
        notifications = database.list_notifications(self.owner["user_id"])
        purge_notifications = [n for n in notifications if n["type"] == "REPOSITORY_PURGED"]
        self.assertEqual(1, len(purge_notifications))
        self.assertIn("permanently deleted", purge_notifications[0]["title"])

    def test_purge_records_an_audit_event_that_survives_the_deletion(self):
        purge_repository(self.repository_id, self.owner["user_id"], "TEST_REASON")
        conn = database._get_connection()
        try:
            event = conn.execute(
                "SELECT * FROM AUDIT_EVENTS WHERE EVENT_TYPE='REPOSITORY_PURGED' AND REPOSITORY_ID=?",
                (self.repository_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(event)

    def test_legal_hold_blocks_purge_entirely(self):
        conn = database._get_connection()
        try:
            conn.execute(
                "INSERT INTO LEGAL_HOLDS VALUES (?,?,?,?,?,?,NULL)",
                (f"HOLD_{uuid.uuid4().hex[:12].upper()}", self.repository_id, "litigation",
                 "ACTIVE", self.owner["user_id"], database._utcnow()),
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(LegalHoldBlocksDeletion):
            purge_repository(self.repository_id, self.owner["user_id"], "TEST_REASON")

        conn = database._get_connection()
        try:
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (self.repository_id,)
            ).fetchone())
        finally:
            conn.close()

    def test_owner_delete_endpoint_hard_deletes_immediately(self):
        """delete_repository() (the store function the DELETE endpoint
        calls) must now run the full purge pipeline, not the old
        soft-delete-and-leave-it-forever behavior."""
        result = database.delete_repository(self.table_id, self.owner["user_id"])
        # The purge pipeline's own status ("PURGED") takes precedence over
        # the store function's default, since it's the more precise signal
        # that the full export->email->notify->hard-delete pipeline ran.
        self.assertEqual("PURGED", result["status"])
        conn = database._get_connection()
        try:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (self.repository_id,)
            ).fetchone())
        finally:
            conn.close()

    def test_owner_delete_blocked_by_legal_hold_leaves_repository_active(self):
        conn = database._get_connection()
        try:
            conn.execute(
                "INSERT INTO LEGAL_HOLDS VALUES (?,?,?,?,?,?,NULL)",
                (f"HOLD_{uuid.uuid4().hex[:12].upper()}", self.repository_id, "litigation",
                 "ACTIVE", self.owner["user_id"], database._utcnow()),
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(PermissionError):
            database.delete_repository(self.table_id, self.owner["user_id"])

        conn = database._get_connection()
        try:
            status = conn.execute(
                "SELECT STATUS FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (self.repository_id,)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual("ACTIVE", status[0])


class RepositoryPurgeSweepTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.export_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "sweep.db"
        database.initialize_product_schema()
        from app.config import settings
        self.original_export_dir = settings.repository_purge_export_dir
        settings.repository_purge_export_dir = self.export_dir.name
        self.owner = database.get_or_create_user(f"sweep-owner-{uuid.uuid4().hex[:8]}@example.com")

    def tearDown(self):
        from app.config import settings
        settings.repository_purge_export_dir = self.original_export_dir
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()
        self.export_dir.cleanup()

    def _register(self, table_id: str, created_at: str) -> str:
        conn = database._get_connection()
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{table_id}"')
            conn.execute(f'CREATE TABLE "{table_id}" (ROW_ID INTEGER PRIMARY KEY AUTOINCREMENT, NAME TEXT)')
            conn.commit()
        finally:
            conn.close()
        registered = database.register_dataset(table_id, self.owner["user_id"], f"{table_id}.xlsx", 0, 1)
        conn = database._get_connection()
        try:
            conn.execute(
                "UPDATE WORKBOOK_REPOSITORIES SET CREATED_AT=? WHERE REPOSITORY_ID=?",
                (created_at, registered["repository_id"]),
            )
            # register_dataset() also creates a baseline checkpoint commit
            # at registration time (see _ensure_stage2_commit_foundation) —
            # that commit IS the "last activity" signal find_purge_candidates
            # reads, so it must be backdated too to genuinely simulate an
            # old, inactive repository.
            conn.execute(
                "UPDATE COMMITS SET CREATED_AT=? WHERE REPOSITORY_ID=?",
                (created_at, registered["repository_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        return registered["repository_id"]

    def test_finds_repository_inactive_for_30_days_with_no_commits(self):
        old = _iso(datetime.now(timezone.utc) - timedelta(days=35))
        repository_id = self._register("QUEUE_BOARD_OLD1", old)
        candidates = find_purge_candidates(inactivity_days=30)
        self.assertIn(repository_id, [c["repository_id"] for c in candidates])

    def test_does_not_find_a_recently_created_repository(self):
        recent = _iso(datetime.now(timezone.utc) - timedelta(days=5))
        repository_id = self._register("QUEUE_BOARD_NEW1", recent)
        candidates = find_purge_candidates(inactivity_days=30)
        self.assertNotIn(repository_id, [c["repository_id"] for c in candidates])

    def test_legal_hold_excludes_an_otherwise_eligible_repository(self):
        old = _iso(datetime.now(timezone.utc) - timedelta(days=40))
        repository_id = self._register("QUEUE_BOARD_HELD1", old)
        conn = database._get_connection()
        try:
            conn.execute(
                "INSERT INTO LEGAL_HOLDS VALUES (?,?,?,?,?,?,NULL)",
                (f"HOLD_{uuid.uuid4().hex[:12].upper()}", repository_id, "litigation",
                 "ACTIVE", self.owner["user_id"], database._utcnow()),
            )
            conn.commit()
        finally:
            conn.close()
        candidates = find_purge_candidates(inactivity_days=30)
        self.assertNotIn(repository_id, [c["repository_id"] for c in candidates])

    def test_explicit_retention_policy_overrides_the_default_inactivity_window(self):
        """A repository 40 days old with no commits would normally be
        eligible at the 30-day default, but an explicit RETENTION_POLICIES
        row requiring 90 days must hold it back."""
        old = _iso(datetime.now(timezone.utc) - timedelta(days=40))
        repository_id = self._register("QUEUE_BOARD_RETAIN1", old)
        conn = database._get_connection()
        try:
            conn.execute(
                "INSERT INTO RETENTION_POLICIES VALUES (?,?,?,?,?,?)",
                (f"POL_{uuid.uuid4().hex[:12].upper()}", repository_id, 90, 7,
                 database._utcnow(), database._utcnow()),
            )
            conn.commit()
        finally:
            conn.close()
        candidates = find_purge_candidates(inactivity_days=30)
        self.assertNotIn(repository_id, [c["repository_id"] for c in candidates])

    def test_sweep_purges_every_candidate_it_finds(self):
        old = _iso(datetime.now(timezone.utc) - timedelta(days=35))
        repository_id = self._register("QUEUE_BOARD_SWEEP1", old)
        results = sweep_repositories_for_deletion()
        self.assertTrue(any(r.get("repository_id") == repository_id and r.get("status") == "PURGED" for r in results))
        conn = database._get_connection()
        try:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (repository_id,)
            ).fetchone())
        finally:
            conn.close()

    def test_sweep_dry_run_does_not_delete_anything(self):
        old = _iso(datetime.now(timezone.utc) - timedelta(days=35))
        repository_id = self._register("QUEUE_BOARD_DRY1", old)
        results = sweep_repositories_for_deletion(dry_run=True)
        self.assertTrue(any(r.get("repository_id") == repository_id and r.get("status") == "WOULD_PURGE" for r in results))
        conn = database._get_connection()
        try:
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (repository_id,)
            ).fetchone())
        finally:
            conn.close()

    def test_previously_soft_deleted_repository_is_swept_regardless_of_age(self):
        recent = _iso(datetime.now(timezone.utc) - timedelta(days=1))
        repository_id = self._register("QUEUE_BOARD_LEGACY1", recent)
        conn = database._get_connection()
        try:
            conn.execute("UPDATE WORKBOOK_REPOSITORIES SET STATUS='DELETED' WHERE REPOSITORY_ID=?", (repository_id,))
            conn.commit()
        finally:
            conn.close()
        candidates = find_purge_candidates(inactivity_days=30)
        self.assertIn(repository_id, [c["repository_id"] for c in candidates])
        self.assertTrue(any(c["repository_id"] == repository_id and c["reason"] == "PREVIOUSLY_SOFT_DELETED" for c in candidates))


if __name__ == "__main__":
    unittest.main()
