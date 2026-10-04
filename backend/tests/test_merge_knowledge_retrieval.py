import tempfile
import unittest
from pathlib import Path

from app import database
from app.ai.merge_knowledge import (
    SYNTHETIC_PRECEDENTS,
    record_resolution_outcome,
    retrieve_precedents,
    seed_synthetic_corpus,
)


class MergeKnowledgeRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "merge_knowledge.db"
        database.initialize_product_schema()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _conflict(self, conflict_type: str, **overrides) -> dict:
        conflict = {
            "conflict_type": conflict_type, "sheet_id": "SHEET_1", "row_id": "ROW_1",
            "column_id": "COL_1", "base_state": {"property": "value", "value": 10},
            "main_state": {"property": "value", "value": 20},
            "branch_state": {"property": "value", "value": 30},
        }
        conflict.update(overrides)
        return conflict

    def test_synthetic_corpus_covers_every_conflict_type_seen_by_the_merge_engine(self):
        conflict_types = {
            "CELL_VALUE_CONFLICT", "FORMULA_CONFLICT", "CELL_FORMAT_CONFLICT", "CELL_COMMENT_CONFLICT",
            "STRUCTURAL_CONFLICT", "SHEET_RENAME_CONFLICT", "SHEET_MOVE_CONFLICT", "SHEET_DELETE_MODIFY_CONFLICT",
            "COLUMN_RENAME_CONFLICT", "COLUMN_MOVE_CONFLICT", "COLUMN_DELETE_MODIFY_CONFLICT",
            "ROW_MOVE_CONFLICT", "ROW_DELETE_MODIFY_CONFLICT",
        }
        present = {doc["conflict_type"] for doc in SYNTHETIC_PRECEDENTS}
        self.assertEqual(conflict_types, present)

    def test_seeding_is_idempotent(self):
        conn = database._get_connection()
        try:
            first_count = conn.execute("SELECT COUNT(*) FROM MERGE_RESOLUTION_KNOWLEDGE").fetchone()[0]
            seed_synthetic_corpus(conn)
            second_count = conn.execute("SELECT COUNT(*) FROM MERGE_RESOLUTION_KNOWLEDGE").fetchone()[0]
            fts_count = conn.execute("SELECT COUNT(*) FROM MERGE_RESOLUTION_KNOWLEDGE_FTS").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(len(SYNTHETIC_PRECEDENTS), first_count)
        self.assertEqual(first_count, second_count)
        self.assertEqual(first_count, fts_count)

    def test_retrieval_returns_relevant_precedents_for_conflict_type(self):
        conflict = self._conflict("FORMULA_CONFLICT")
        results = retrieve_precedents(conflict, limit=5)
        self.assertTrue(results)
        self.assertTrue(all(item["conflict_type"] == "FORMULA_CONFLICT" for item in results))

    def test_record_resolution_outcome_grows_corpus_and_is_retrievable(self):
        conflict = self._conflict("CELL_VALUE_CONFLICT")
        before = retrieve_precedents(conflict, limit=50)
        record_resolution_outcome(conflict, "ACCEPT_BRANCH", 30, repository_id="REP_TEST")
        after = retrieve_precedents(conflict, limit=50)

        self.assertEqual(len(before) + 1, len(after))
        historical = [item for item in after if item["source"] == "HISTORICAL"]
        self.assertEqual(1, len(historical))
        self.assertEqual("ACCEPT_BRANCH", historical[0]["recommended_resolution"])
        self.assertEqual("REP_TEST", historical[0]["repository_id"])


if __name__ == "__main__":
    unittest.main()
