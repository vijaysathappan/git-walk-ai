"""
Unit & Integration Tests for zero-touch device-locked workbook identity:
the first device to successfully open a working copy is auto-bound to it,
and any later open from a DIFFERENT device is refused — even with a
byte-identical file path, a valid signature, and a valid OTP — until the
repository owner explicitly trusts the new device.
"""

import shutil
import tempfile
import unittest
import uuid
from pathlib import Path

from app.database import (
    _get_connection,
    add_dataset_member,
    bind_or_verify_device,
    device_trust_status,
    get_or_create_user,
    initialize_product_schema,
    record_device_fingerprint,
    register_dataset,
    set_device_trust_status,
    update_branch_local_path,
    verify_workbook_access,
)
from app.services.workbook_service import issue_branch_workbook


class BindOrVerifyDeviceUnitTests(unittest.TestCase):
    def setUp(self):
        initialize_product_schema()
        self.temp_dir = Path(tempfile.mkdtemp(prefix="gitwalk_device_unit_"))
        self.owner = get_or_create_user(f"device_unit_owner_{uuid.uuid4().hex[:8]}@example.com")
        self.table_id = "QUEUE_BOARD_DEVUNIT"
        conn = _get_connection()
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{self.table_id}"')
            conn.execute(f'CREATE TABLE "{self.table_id}" (ROW_ID INTEGER PRIMARY KEY AUTOINCREMENT, NAME TEXT)')
            conn.execute(f'INSERT INTO "{self.table_id}" (NAME) VALUES (?)', ("Row 1",))
            conn.commit()
            register_dataset(self.table_id, self.owner["user_id"], "unit.xlsx", 1, 1)
        finally:
            conn.close()

    def tearDown(self):
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _issue(self):
        dest = self.temp_dir / "authorized"
        dest.mkdir(parents=True, exist_ok=True)
        result = issue_branch_workbook(
            main_table_id=self.table_id, user_id=self.owner["user_id"],
            user_email=self.owner["email"], local_target_dir=str(dest),
        )
        update_branch_local_path(result["branch_id"], result["local_file_path"])
        return result

    def test_no_machine_id_is_unverified_not_blocking(self):
        result = self._issue()
        binding = bind_or_verify_device(result["working_copy_id"], None)
        self.assertEqual("UNVERIFIED", binding["status"])

    def test_first_open_auto_binds_then_matches_on_same_device(self):
        result = self._issue()
        first = bind_or_verify_device(result["working_copy_id"], "MACHINE-A")
        self.assertEqual("BOUND", first["status"])
        second = bind_or_verify_device(result["working_copy_id"], "MACHINE-A")
        self.assertEqual("MATCH", second["status"])

    def test_different_device_is_a_mismatch(self):
        result = self._issue()
        bind_or_verify_device(result["working_copy_id"], "MACHINE-A")
        mismatch = bind_or_verify_device(result["working_copy_id"], "MACHINE-B")
        self.assertEqual("MISMATCH", mismatch["status"])
        self.assertEqual("MACHINE-A", mismatch["bound_machine_id"])
        self.assertEqual("MACHINE-B", mismatch["attempted_machine_id"])

    def test_trusting_a_device_rebinds_the_users_working_copies(self):
        result = self._issue()
        bind_or_verify_device(result["working_copy_id"], "MACHINE-A")
        bind_or_verify_device(result["working_copy_id"], "MACHINE-B")  # still MACHINE-A bound
        device = record_device_fingerprint(
            self.owner["user_id"], session_id=None, ip_address="203.0.113.5",
            user_agent="pytest", machine_id="MACHINE-B",
        )
        self.assertEqual("UNKNOWN", device_trust_status(self.owner["user_id"], "MACHINE-B"))

        set_device_trust_status(device["fingerprint_id"], "TRUSTED")

        rebound = bind_or_verify_device(result["working_copy_id"], "MACHINE-B")
        self.assertEqual("MATCH", rebound["status"])
        # The old device is no longer bound.
        stale = bind_or_verify_device(result["working_copy_id"], "MACHINE-A")
        self.assertEqual("MISMATCH", stale["status"])


class WorkbookAccessDeviceLockTests(unittest.TestCase):
    def setUp(self):
        initialize_product_schema()
        self.temp_dir = Path(tempfile.mkdtemp(prefix="gitwalk_device_access_"))
        self.owner = get_or_create_user(f"device_access_owner_{uuid.uuid4().hex[:8]}@example.com")
        self.table_id = "QUEUE_BOARD_DEVACCESS"
        conn = _get_connection()
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{self.table_id}"')
            conn.execute(f'CREATE TABLE "{self.table_id}" (ROW_ID INTEGER PRIMARY KEY AUTOINCREMENT, NAME TEXT)')
            conn.execute(f'INSERT INTO "{self.table_id}" (NAME) VALUES (?)', ("Row 1",))
            conn.commit()
            register_dataset(self.table_id, self.owner["user_id"], "access.xlsx", 1, 1)
        finally:
            conn.close()

    def tearDown(self):
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _get_otp(self, email: str) -> str:
        conn = _get_connection()
        try:
            conn.execute("DELETE FROM AUTH_LOGIN_CODES WHERE EMAIL=?", (email.lower(),))
            conn.commit()
        finally:
            conn.close()
        from app.security import send_login_code
        return send_login_code(email)

    def test_same_file_at_identical_path_still_blocked_from_a_second_device(self):
        """The core anti-leakage scenario: a byte-identical copy of the
        workbook, opened from the EXACT same recorded path, must still be
        refused once a different device has already opened it — proving
        path-matching alone (the pre-existing check) is not what's
        stopping the leak here; the device lock is."""
        dest = self.temp_dir / "authorized"
        dest.mkdir(parents=True, exist_ok=True)
        result = issue_branch_workbook(
            main_table_id=self.table_id, user_id=self.owner["user_id"],
            user_email=self.owner["email"], local_target_dir=str(dest),
        )
        auth_path = result["local_file_path"]
        update_branch_local_path(result["branch_id"], auth_path)

        first_code = self._get_otp(self.owner["email"])
        first_access = verify_workbook_access(
            repository_id=result["repository_id"], branch_id=result["branch_id"],
            working_copy_id=result["working_copy_id"], email=self.owner["email"],
            code=first_code, current_file_path=auth_path, machine_id="ORIGINAL-LAPTOP",
        )
        self.assertEqual(self.owner["user_id"], first_access["user"]["user_id"])

        second_code = self._get_otp(self.owner["email"])
        with self.assertRaises(PermissionError) as ctx:
            verify_workbook_access(
                repository_id=result["repository_id"], branch_id=result["branch_id"],
                working_copy_id=result["working_copy_id"], email=self.owner["email"],
                code=second_code, current_file_path=auth_path, machine_id="COPIED-TO-OTHER-PC",
            )
        self.assertIn("DEVICE_MISMATCH", str(ctx.exception))

    def test_owner_trusting_the_new_device_unblocks_it(self):
        dest = self.temp_dir / "authorized2"
        dest.mkdir(parents=True, exist_ok=True)
        result = issue_branch_workbook(
            main_table_id=self.table_id, user_id=self.owner["user_id"],
            user_email=self.owner["email"], local_target_dir=str(dest),
        )
        auth_path = result["local_file_path"]
        update_branch_local_path(result["branch_id"], auth_path)

        first_code = self._get_otp(self.owner["email"])
        verify_workbook_access(
            repository_id=result["repository_id"], branch_id=result["branch_id"],
            working_copy_id=result["working_copy_id"], email=self.owner["email"],
            code=first_code, current_file_path=auth_path, machine_id="LAPTOP-1",
        )

        second_code = self._get_otp(self.owner["email"])
        with self.assertRaises(PermissionError):
            verify_workbook_access(
                repository_id=result["repository_id"], branch_id=result["branch_id"],
                working_copy_id=result["working_copy_id"], email=self.owner["email"],
                code=second_code, current_file_path=auth_path, machine_id="LAPTOP-2",
            )

        device = record_device_fingerprint(
            self.owner["user_id"], session_id=None, ip_address="203.0.113.5",
            user_agent="pytest", machine_id="LAPTOP-2",
        )
        set_device_trust_status(device["fingerprint_id"], "TRUSTED")

        third_code = self._get_otp(self.owner["email"])
        access = verify_workbook_access(
            repository_id=result["repository_id"], branch_id=result["branch_id"],
            working_copy_id=result["working_copy_id"], email=self.owner["email"],
            code=third_code, current_file_path=auth_path, machine_id="LAPTOP-2",
        )
        self.assertEqual(self.owner["user_id"], access["user"]["user_id"])

    def test_workbook_issued_to_one_user_rejects_a_different_valid_account(self):
        """A coworker with their own valid OTP and editor access to the same
        repository must still be rejected when authenticating against a
        working copy issued to someone else — the assigned-email lock."""
        coworker = get_or_create_user(f"device_access_coworker_{uuid.uuid4().hex[:8]}@example.com")
        add_dataset_member(self.table_id, self.owner["user_id"], coworker["email"], "editor")

        dest = self.temp_dir / "authorized3"
        dest.mkdir(parents=True, exist_ok=True)
        result = issue_branch_workbook(
            main_table_id=self.table_id, user_id=self.owner["user_id"],
            user_email=self.owner["email"], local_target_dir=str(dest),
        )
        auth_path = result["local_file_path"]
        update_branch_local_path(result["branch_id"], auth_path)

        coworker_code = self._get_otp(coworker["email"])
        with self.assertRaises(PermissionError) as ctx:
            verify_workbook_access(
                repository_id=result["repository_id"], branch_id=result["branch_id"],
                working_copy_id=result["working_copy_id"], email=coworker["email"],
                code=coworker_code, current_file_path=auth_path, machine_id="COWORKER-LAPTOP",
            )
        self.assertIn("ASSIGNED_USER_MISMATCH", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
