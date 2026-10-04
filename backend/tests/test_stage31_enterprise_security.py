import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import database
from app.access_control.engine import authorization_engine, repository_resource
from app.access_control.service import (
    add_group_member,
    assign_role,
    create_group,
    create_policy,
    create_service_account,
    revoke_assignment,
    set_user_status,
    verify_service_credential,
)


class EnterpriseSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "security.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("owner@example.com")
        self.viewer = database.get_or_create_user("viewer@example.com")
        self.other_owner = database.get_or_create_user("other@example.com")
        conn = sqlite3.connect(database.DB_PATH)
        for suffix in ("A", "B"):
            conn.execute(f'CREATE TABLE "QUEUE_BOARD_SEC_{suffix}" (ROW_ID INTEGER PRIMARY KEY, VALUE TEXT)')
            conn.execute(f'INSERT INTO "QUEUE_BOARD_SEC_{suffix}" VALUES (1, "seed")')
        conn.commit(); conn.close()
        self.repo = database.register_dataset("QUEUE_BOARD_SEC_A", self.owner["user_id"], "alpha.xlsx", 1, 1)
        self.other_repo = database.register_dataset("QUEUE_BOARD_SEC_B", self.other_owner["user_id"], "beta.xlsx", 1, 1)
        database.add_dataset_member("QUEUE_BOARD_SEC_A", self.owner["user_id"], self.viewer["email"], "viewer")
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (self.repo["repository_id"],)
        ).fetchone()[0]
        conn.close()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_cross_tenant_access_is_denied_even_with_guessed_repository_id(self):
        decision = authorization_engine.authorize(
            self.owner["user_id"], "repository.read", repository_resource(self.other_repo["repository_id"])
        )
        self.assertFalse(decision.allowed)
        self.assertEqual("CROSS_TENANT_DENIED", decision.reason_code)

    def test_legacy_viewer_is_migrated_to_read_only_permission(self):
        read = authorization_engine.authorize(self.viewer["user_id"], "repository.read", repository_resource(self.repo["repository_id"]))
        write = authorization_engine.authorize(self.viewer["user_id"], "commit.create", repository_resource(self.repo["repository_id"]))
        self.assertTrue(read.allowed)
        self.assertFalse(write.allowed)
        self.assertEqual("DEFAULT_DENY", write.reason_code)

    def test_group_roles_and_immediate_assignment_revocation(self):
        group = create_group(self.organization_id, "Reviewers", None, self.owner["user_id"])
        add_group_member(self.organization_id, group["group_id"], self.viewer["user_id"], self.owner["user_id"])
        assignment = assign_role(
            self.organization_id, "CHECKER", "REPOSITORY", self.repo["repository_id"],
            self.owner["user_id"], group_id=group["group_id"],
        )
        self.assertTrue(authorization_engine.authorize(
            self.viewer["user_id"], "merge_request.review", repository_resource(self.repo["repository_id"])
        ).allowed)
        revoke_assignment(self.organization_id, assignment["assignment_id"], self.owner["user_id"])
        self.assertFalse(authorization_engine.authorize(
            self.viewer["user_id"], "merge_request.review", repository_resource(self.repo["repository_id"])
        ).allowed)

    def test_explicit_conditional_deny_wins_over_owner_role(self):
        create_policy(self.organization_id, {
            "name": "Block restricted exports", "effect": "DENY", "permission_key": "repository.read",
            "subject_type": "USER", "subject_id": self.owner["user_id"], "resource_type": "REPOSITORY",
            "resource_id": self.repo["repository_id"], "conditions": {"context.network_zone": {"eq": "UNTRUSTED"}},
            "priority": 1,
        }, self.owner["user_id"])
        denied = authorization_engine.authorize(
            self.owner["user_id"], "repository.read", repository_resource(self.repo["repository_id"]),
            {"network_zone": "UNTRUSTED"},
        )
        allowed = authorization_engine.authorize(
            self.owner["user_id"], "repository.read", repository_resource(self.repo["repository_id"]),
            {"network_zone": "CORPORATE"},
        )
        self.assertFalse(denied.allowed)
        self.assertEqual("EXPLICIT_POLICY_DENY", denied.reason_code)
        self.assertTrue(allowed.allowed)

    def test_suspension_revokes_sessions_and_service_secret_is_hashed(self):
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        database.create_auth_session("SES_VIEWER", self.viewer["user_id"], "hash", expires)
        set_user_status(self.organization_id, self.viewer["user_id"], "SUSPENDED", self.owner["user_id"])
        self.assertFalse(database.auth_session_is_active("SES_VIEWER", self.viewer["user_id"], "hash"))
        self.assertEqual("ACTOR_SUSPENDED", authorization_engine.authorize(
            self.viewer["user_id"], "repository.read", repository_resource(self.repo["repository_id"])
        ).reason_code)
        account = create_service_account(
            self.organization_id, "Inventory bot", "VIEWER", "REPOSITORY",
            self.repo["repository_id"], self.owner["user_id"],
        )
        verified = verify_service_credential(account["credential"])
        self.assertEqual(account["service_account_id"], verified["SERVICE_ACCOUNT_ID"])
        conn = database._get_connection()
        stored = conn.execute("SELECT SECRET_HASH FROM SERVICE_ACCOUNTS WHERE SERVICE_ACCOUNT_ID=?", (account["service_account_id"],)).fetchone()[0]
        protection = conn.execute("SELECT REQUIRED_APPROVALS,ALLOW_DIRECT_COMMITS FROM BRANCH_PROTECTION_RULES WHERE REPOSITORY_ID=?", (self.repo["repository_id"],)).fetchone()
        conn.close()
        self.assertNotIn(account["credential"].split(".", 1)[1], stored)
        self.assertEqual((1, 0), tuple(protection))


if __name__ == "__main__":
    unittest.main()
