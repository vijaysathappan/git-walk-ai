import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database
from app.access_control.service import security_overview


class SecurityOverviewScopeNameTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "scope_names.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("scope-owner@example.com")
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_SCOPE" (ROW_ID INTEGER PRIMARY KEY, VALUE TEXT)')
        conn.execute('INSERT INTO "QUEUE_BOARD_SCOPE" VALUES (1, "seed")')
        conn.commit()
        conn.close()
        self.registered = database.register_dataset(
            "QUEUE_BOARD_SCOPE", self.owner["user_id"], "scope.xlsx", 1, 1, repository_name="Loan Portfolio",
        )
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (self.registered["repository_id"],)
        ).fetchone()[0]
        conn.close()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_repository_scoped_assignment_carries_a_resolved_name(self):
        overview = security_overview(self.organization_id, self.owner["user_id"])

        repository_scoped = [item for item in overview["assignments"] if item["scope_type"] == "REPOSITORY"]
        self.assertTrue(repository_scoped, "Expected at least one repository-scoped assignment from repo creation")
        self.assertTrue(
            any(item["scope_name"] == "Loan Portfolio" for item in repository_scoped),
            f"Expected a resolved repository name, got: {[item['scope_name'] for item in repository_scoped]}",
        )


if __name__ == "__main__":
    unittest.main()
