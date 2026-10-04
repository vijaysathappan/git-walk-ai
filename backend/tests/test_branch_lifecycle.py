"""
Unit & Integration Tests for EUC Local Download, Drive Validation, and Post-Merge Branch Purge.
"""

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.database import (
    _get_connection,
    get_or_create_user,
    get_system_setting,
    set_system_setting,
    register_dataset,
    initialize_product_schema,
)
from app.main import app
from app.security import create_session_token
from app.services.branch_lifecycle_manager import (
    BranchLifecycleManager,
    validate_storage_drive,
)


class BranchLifecycleTests(unittest.TestCase):
    def setUp(self):
        initialize_product_schema()
        self.client = TestClient(app)
        # Temporary directory on C: drive for testing
        self.temp_dir = Path(tempfile.mkdtemp(prefix="gitwalk_euc_test_"))
        self.user = get_or_create_user("lifecycle_tester@example.com")
        self.token = create_session_token(self.user["user_id"], self.user["email"])
        self.auth_headers = {
            "Authorization": f"Bearer {self.token}"
        }

    def tearDown(self):
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_drive_validation_accepts_c_and_d_drives(self):
        """Must strictly allow C: and D: drives and reject others."""
        # Valid paths
        c_path = validate_storage_drive("C:\\GitWalk_Workbooks")
        self.assertEqual("C:", c_path.drive.upper())

        d_path = validate_storage_drive("D:\\EUC_Files")
        self.assertEqual("D:", d_path.drive.upper())

        c_slash = validate_storage_drive("c:/work/excel")
        self.assertEqual("C:", c_slash.drive.upper())

        # Invalid paths
        with self.assertRaises(ValueError):
            validate_storage_drive("E:\\Unauthorized_Drive")

        with self.assertRaises(ValueError):
            validate_storage_drive("F:/AnotherDrive/test")

        with self.assertRaises(ValueError):
            validate_storage_drive("relative/path/not/allowed")

    def test_system_settings_persistence(self):
        """Settings must persist in SQLite SYSTEM_SETTINGS table."""
        test_folder = str(self.temp_dir / "custom_download")
        set_system_setting("euc_download_dir", test_folder, "USR_TEST")

        val = get_system_setting("euc_download_dir")
        self.assertEqual(test_folder, val)

    def test_settings_api_get_and_post(self):
        """API must reject non-C/D drives and accept and auto-create valid directories."""
        # 1. Reject E: drive
        bad_resp = self.client.post(
            "/api/v1/settings/euc-storage",
            json={"local_download_dir": "E:\\IllegalDrive\\Folder"},
            headers=self.auth_headers,
        )
        self.assertEqual(400, bad_resp.status_code)
        self.assertIn("C: or D: drive", bad_resp.json()["detail"])

        # 2. Accept and create C: drive folder
        target_subfolder = self.temp_dir / "nested" / "euc_store"
        self.assertFalse(target_subfolder.exists())

        good_resp = self.client.post(
            "/api/v1/settings/euc-storage",
            json={"local_download_dir": str(target_subfolder)},
            headers=self.auth_headers,
        )
        self.assertEqual(200, good_resp.status_code)
        self.assertEqual("SAVED", good_resp.json()["status"])
        self.assertTrue(target_subfolder.exists())

        # 3. Verify GET returns configured folder
        get_resp = self.client.get(
            "/api/v1/settings/euc-storage",
            headers=self.auth_headers,
        )
        self.assertEqual(200, get_resp.status_code)
        self.assertEqual(str(target_subfolder.resolve()), get_resp.json()["local_download_dir"])
        self.assertTrue(get_resp.json()["exists"])

    def test_work_on_workbook_saves_to_local_folder_and_cleans_up(self):
        """
        Verify that work_on_workbook saves the workbook into the local folder,
        sets X-Local-Path, updates DB, and post-merge cleanup unlinks the file and deletes the branch in DB.
        """
        import uuid
        table_id = f"QUEUE_BOARD_LC_{uuid.uuid4().hex[:8].upper()}"
        conn = _get_connection()
        try:
            conn.execute(
                f'CREATE TABLE "{table_id}" (ROW_ID INTEGER PRIMARY KEY, "NAME" TEXT)'
            )
            conn.execute(f'INSERT INTO "{table_id}" (ROW_ID, "NAME") VALUES (1, "Test")')
            conn.commit()
        finally:
            conn.close()

        registered = register_dataset(table_id, self.user["user_id"], "lifecycle.xlsx", 1, 1)
        target_download_dir = self.temp_dir / "euc_downloads"

        # Request working copy with local_download_dir
        resp = self.client.post(
            f"/api/v1/repositories/{table_id}/work-on-workbook",
            json={
                "mode": "new",
                "local_download_dir": str(target_download_dir),
            },
            headers=self.auth_headers,
        )
        self.assertEqual(200, resp.status_code)
        body = resp.json()
        branch_id = body["branch_id"]
        local_path = body["local_path"]
        self.assertIsNotNone(local_path)
        saved_file = Path(local_path)
        self.assertTrue(saved_file.exists())
        self.assertGreater(saved_file.stat().st_size, 0)

        # Verify DB recorded LOCAL_DOWNLOAD_PATH
        conn = _get_connection()
        try:
            row = conn.execute(
                "SELECT STATUS, LOCAL_DOWNLOAD_PATH FROM BRANCHES WHERE BRANCH_ID = ?",
                (branch_id,),
            ).fetchone()
            self.assertEqual("ACTIVE", row["STATUS"])
            self.assertEqual(str(saved_file), row["LOCAL_DOWNLOAD_PATH"])
        finally:
            conn.close()

        # Run BranchLifecycleManager cleanup
        cleanup_res = BranchLifecycleManager.cleanup_merged_branch(branch_id=branch_id)
        self.assertEqual("DELETED", cleanup_res["new_status"])
        self.assertTrue(cleanup_res["file_deleted"])
        self.assertFalse(saved_file.exists())

        # Verify DB status is now DELETED
        conn = _get_connection()
        try:
            row_after = conn.execute(
                "SELECT STATUS FROM BRANCHES WHERE BRANCH_ID = ?",
                (branch_id,),
            ).fetchone()
            self.assertEqual("DELETED", row_after["STATUS"])
        finally:
            conn.close()

    def test_cli_cleanup_tool_execution(self):
        """Verify the productized CLI script runs and returns JSON."""
        # Create a dummy branch
        conn = _get_connection()
        branch_id = "BR_TEST_CLI_PURGE"
        branch_file = self.temp_dir / "gitwalk_cli_test.xlsx"
        branch_file.write_bytes(b"CLI test bytes")
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO BRANCHES
                    (BRANCH_ID, REPOSITORY_ID, DATA_TABLE_ID, BRANCH_NAME, BRANCH_TYPE,
                     CREATED_BY, STATUS, CREATED_AT, UPDATED_AT, LOCAL_DOWNLOAD_PATH)
                VALUES (?, 'REPO_CLI', 'TABLE_CLI', 'cli_branch', 'USER', 'USR_SYSTEM',
                        'MERGED', datetime('now'), datetime('now'), ?)
                """,
                (branch_id, str(branch_file)),
            )
            conn.commit()
        finally:
            conn.close()

        script_path = Path(__file__).resolve().parent.parent / "scripts" / "merge_branch_cleanup.py"
        res = subprocess.run(
            [sys.executable, str(script_path), "--branch-id", branch_id, "--json"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, res.returncode, f"CLI failed: {res.stderr}")
        self.assertIn('"new_status": "DELETED"', res.stdout)
        self.assertIn('"file_deleted": true', res.stdout)
        self.assertFalse(branch_file.exists())
