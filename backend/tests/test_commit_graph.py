import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database
from app.repositories.commit_store import list_repository_commits
from app.repositories.merge_store import branch_context
from app.repositories.commit_store import commit_semantic_delta
from app.services.merge_service import MergeActor, merge_service


class CommitGraphTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "commit_graph.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("graph-owner@example.com")
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_GRAPH" (ROW_ID INTEGER PRIMARY KEY, "TEAM" TEXT)')
        conn.execute('INSERT INTO "QUEUE_BOARD_GRAPH" (ROW_ID, "TEAM") VALUES (1, "KKR")')
        conn.commit()
        conn.close()
        registered = database.register_dataset("QUEUE_BOARD_GRAPH", self.owner["user_id"], "graph.xlsx", 1, 1)
        self.repository_id = registered["repository_id"]
        self.main_branch_id = registered["main_branch_id"]
        self.table_id = "QUEUE_BOARD_GRAPH"
        self.actor = MergeActor(self.owner["user_id"], self.owner["email"])

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _commit_cell(self, branch_id, table_id, value):
        snapshot = database.get_table_snapshot(table_id)
        sheet = snapshot["semantic"]["sheets"][0]
        row = sheet["rows"][0]
        column = next(item for item in sheet["columns"] if item["name"] == "TEAM")
        context = branch_context(branch_id)
        return commit_semantic_delta(
            table_id=table_id, repository_id=context["repository_id"], branch_id=branch_id,
            expected_head_commit_id=snapshot["head_commit_id"], base_version=snapshot["version"],
            changes=[{"operation_type": "CELL_VALUE_UPDATE", "sheet_id": sheet["sheet_id"], "row_id": row["row_id"], "column_id": column["column_id"], "new_value": value}],
            user_id=self.owner["user_id"], user_email=self.owner["email"], message="Set TEAM",
        )

    def test_list_repository_commits_includes_merge_commit_with_both_parents(self):
        copy = database.create_working_copy(self.table_id, self.owner["user_id"], self.owner["email"])
        branch_commit = self._commit_cell(copy["branch_id"], copy["table_id"], "SRH")

        request = merge_service.create_request(
            source_branch_id=copy["branch_id"], target_branch_id=self.main_branch_id,
            title="Update team", description=None, actor=self.actor,
        )
        merge_service.review(
            merge_request_id=request["merge_request_id"], decision="APPROVED",
            comment="Owner reviewed", actor=self.actor,
        )
        merged = merge_service.merge(request["merge_request_id"], self.actor)

        commits = list_repository_commits(self.repository_id)
        by_id = {item["commit_id"]: item for item in commits}

        self.assertIn(merged["commit_id"], by_id)
        merge_commit = by_id[merged["commit_id"]]
        self.assertEqual(2, len(merge_commit["parent_commit_ids"]))
        self.assertIn(branch_commit["commit_id"], merge_commit["parent_commit_ids"])

        # Commits from BOTH branches are present, not just main's.
        branch_ids_seen = {item["branch_id"] for item in commits}
        self.assertIn(copy["branch_id"], branch_ids_seen)
        self.assertIn(self.main_branch_id, branch_ids_seen)

        # Each commit carries its own branch name for lane assignment.
        source_commit = by_id[branch_commit["commit_id"]]
        self.assertEqual(copy["branch_id"], source_commit["branch_id"])
        self.assertTrue(source_commit["branch_name"])

    def test_list_repository_commits_raises_nothing_for_empty_repository(self):
        commits = list_repository_commits(self.repository_id)
        self.assertGreaterEqual(len(commits), 1)  # at least the root commit


if __name__ == "__main__":
    unittest.main()
