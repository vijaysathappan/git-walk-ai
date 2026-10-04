import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from app import database
from app.repositories.commit_store import commit_semantic_delta, reconstruct_branch
from app.services.semantic_ledger_service import ledger_for_connection
from app.storage.serializer import canonical_serialize


class SemanticStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "ledger.db"
        database.initialize_product_schema()
        self.user = database.get_or_create_user("ledger-owner@example.com")
        database.create_sqlite_table_from_df(
            "QUEUE_BOARD_LEDGER",
            pd.DataFrame([
                {"SKU": "A-1", "AMOUNT": 10},
                {"SKU": "B-2", "AMOUNT": 20},
            ]),
        )
        registered = database.register_dataset(
            "QUEUE_BOARD_LEDGER", self.user["user_id"], "ledger.xlsx", 2, 2,
        )
        self.repository_id = registered["repository_id"]
        self.main_branch_id = registered["main_branch_id"]

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_canonical_serialization_is_order_independent(self):
        self.assertEqual(
            canonical_serialize({"a": 1, "b": 2}),
            canonical_serialize({"b": 2, "a": 1}),
        )

    def test_initial_import_is_reconstructable_and_verified(self):
        state = reconstruct_branch(self.main_branch_id)
        self.assertEqual(2, len(state["sheets"][0]["rows"]))
        self.assertEqual("A-1", next(iter(state["sheets"][0]["rows"][0]["values"].values())))

        conn = database._get_connection()
        try:
            root = conn.execute(
                "SELECT ROOT_MANIFEST_HASH FROM COMMIT_MANIFESTS WHERE BRANCH_ID=?",
                (self.main_branch_id,),
            ).fetchone()
            self.assertEqual(64, len(root[0]))
            self.assertGreater(
                conn.execute("SELECT COUNT(*) FROM STORAGE_OBJECTS").fetchone()[0], 2
            )
        finally:
            conn.close()

    def test_branch_pointer_reuses_objects_and_cell_commit_is_copy_on_write(self):
        conn = database._get_connection()
        try:
            before = conn.execute("SELECT COUNT(*) FROM STORAGE_OBJECTS").fetchone()[0]
        finally:
            conn.close()
        copy = database.create_working_copy(
            "QUEUE_BOARD_LEDGER", self.user["user_id"], self.user["email"]
        )
        conn = database._get_connection()
        try:
            after_branch = conn.execute("SELECT COUNT(*) FROM STORAGE_OBJECTS").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(before, after_branch)

        snapshot = database.get_table_snapshot(copy["table_id"])
        sheet = snapshot["semantic"]["sheets"][0]
        amount = next(column for column in sheet["columns"] if column["name"] == "AMOUNT")
        result = commit_semantic_delta(
            table_id=copy["table_id"], repository_id=copy["repository_id"],
            branch_id=copy["branch_id"], expected_head_commit_id=snapshot["head_commit_id"],
            base_version=snapshot["version"],
            changes=[{"operation_type": "CELL_VALUE_UPDATE", "sheet_id": sheet["sheet_id"],
                      "row_id": sheet["rows"][0]["row_id"], "column_id": amount["column_id"],
                      "new_value": 99}],
            user_id=self.user["user_id"], user_email=self.user["email"], message="Adjust amount",
        )
        self.assertEqual(3, result["objects_created"])
        self.assertGreaterEqual(result["objects_reused"], 1)
        rebuilt = reconstruct_branch(copy["branch_id"])
        self.assertIn(99, rebuilt["sheets"][0]["rows"][0]["values"].values())

    def test_explicit_branch_is_constant_time_pointer_and_can_be_materialized(self):
        conn = database._get_connection()
        try:
            before = conn.execute("SELECT COUNT(*) FROM STORAGE_OBJECTS").fetchone()[0]
        finally:
            conn.close()
        branch = database.create_semantic_branch(
            "QUEUE_BOARD_LEDGER", "users/ledger/forecast", None, self.user["user_id"]
        )
        self.assertEqual(0, branch["storage_bytes_added"])
        conn = database._get_connection()
        try:
            row = conn.execute("SELECT DATA_TABLE_ID FROM BRANCHES WHERE BRANCH_ID=?", (branch["branch_id"],)).fetchone()
            self.assertTrue(row[0].startswith("POINTER_"))
            self.assertEqual(before, conn.execute("SELECT COUNT(*) FROM STORAGE_OBJECTS").fetchone()[0])
        finally:
            conn.close()

        checkout = database.create_working_copy(
            "QUEUE_BOARD_LEDGER", self.user["user_id"], self.user["email"],
            branch_mode="continue", branch_id=branch["branch_id"],
        )
        self.assertTrue(checkout["table_id"].startswith("BRANCH_DATA_"))
        self.assertEqual(2, database.get_table_page(checkout["table_id"])["total"])

    def test_gc_marks_only_unreachable_objects(self):
        conn = database._get_connection()
        try:
            ledger = ledger_for_connection(conn)
            orphan = ledger.objects.put(conn, "VALUE_BLOCK", {"orphan": True})
            conn.commit()
            preview = ledger.collect_garbage(conn, dry_run=True)
            self.assertGreaterEqual(preview["objects_candidates"], 1)
            self.assertTrue(ledger.objects.store.exists(orphan["object_hash"]))
            executed = ledger.collect_garbage(conn, dry_run=False)
            conn.commit()
            self.assertGreaterEqual(executed["objects_candidates"], 1)
            self.assertFalse(ledger.objects.store.exists(orphan["object_hash"]))
        finally:
            conn.close()

    def test_idempotency_replays_response_and_rejects_payload_change(self):
        conn = database._get_connection()
        try:
            ledger = ledger_for_connection(conn)
            self.assertIsNone(ledger.begin_idempotent(conn, "same-key", self.user["user_id"], "COMMIT", {"a": 1}))
            ledger.finish_idempotent(conn, "same-key", self.user["user_id"], "COMMIT", {"commit_id": "C1"})
            replay = ledger.begin_idempotent(conn, "same-key", self.user["user_id"], "COMMIT", {"a": 1})
            self.assertEqual("C1", replay["commit_id"])
            with self.assertRaises(ValueError):
                ledger.begin_idempotent(conn, "same-key", self.user["user_id"], "COMMIT", {"a": 2})
        finally:
            conn.close()

    def test_expired_processing_row_is_abandoned_not_permanently_stuck(self):
        """A request that crashed mid-flight leaves a PROCESSING row behind.
        Once its EXPIRES_AT has passed, a later begin_idempotent() call for
        the same key must treat it as abandoned and succeed, instead of
        raising 'already being processed' forever (the bug this test
        pins down: EXPIRES_AT was written but never read)."""
        conn = database._get_connection()
        try:
            request_hash = hashlib.sha256(
                json.dumps({"a": 1}, sort_keys=True, default=str, separators=(",", ":")).encode()
            ).hexdigest()
            past = datetime.now(timezone.utc) - timedelta(hours=1)
            conn.execute(
                "INSERT INTO IDEMPOTENCY_KEYS VALUES (?,?,?,?,NULL,'PROCESSING',?,?)",
                (
                    "stuck-key", self.user["user_id"], "COMMIT", request_hash,
                    (past - timedelta(hours=1)).isoformat(), past.isoformat(),
                ),
            )
            conn.commit()

            ledger = ledger_for_connection(conn)
            result = ledger.begin_idempotent(conn, "stuck-key", self.user["user_id"], "COMMIT", {"a": 1})
            self.assertIsNone(result)
            row = conn.execute(
                "SELECT STATUS FROM IDEMPOTENCY_KEYS WHERE IDEMPOTENCY_KEY=? AND ACTOR_ID=? AND OPERATION=?",
                ("stuck-key", self.user["user_id"], "COMMIT"),
            ).fetchone()
            self.assertEqual("PROCESSING", row[0])
        finally:
            conn.close()

    def test_non_expired_processing_row_still_blocks_a_concurrent_retry(self):
        """The fix must not weaken the concurrency guard for a request that
        is still genuinely in flight (EXPIRES_AT in the future)."""
        conn = database._get_connection()
        try:
            ledger = ledger_for_connection(conn)
            self.assertIsNone(ledger.begin_idempotent(conn, "live-key", self.user["user_id"], "COMMIT", {"a": 1}))
            with self.assertRaises(RuntimeError):
                ledger.begin_idempotent(conn, "live-key", self.user["user_id"], "COMMIT", {"a": 1})
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
