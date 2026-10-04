import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database
from app.ai import commit_review
from app.ai.gateway import ai_gateway
from app.ai.provider import LLMProvider, ProviderResult
from app.config import settings
from app.repositories.commit_store import commit_semantic_delta
from app.secret_store import encrypt_secret


class FakeCommitReviewProvider(LLMProvider):
    provider_name = "OPENROUTER"

    def __init__(self, recommendation: str = "LOOKS_GOOD", risk_level: str = "LOW", field_explanations: dict | None = None):
        self.recommendation = recommendation
        self.risk_level = risk_level
        self.field_explanations = field_explanations or {}
        self.calls = 0

    async def complete(self, **kwargs) -> ProviderResult:
        self.calls += 1
        actions = [
            {"title": column, "rationale": explanation, "action_type": "FIELD_EXPLANATION", "risk_level": "LOW"}
            for column, explanation in self.field_explanations.items()
        ]
        actions.append({
            "title": "Author summary", "rationale": "Deterministic risk breakdown explained in plain language.",
            "action_type": self.recommendation, "risk_level": self.risk_level,
        })
        content = json.dumps({
            "answer": "Commit risk review.", "evidence": [], "confidence": 0.8,
            "insufficient_evidence": False, "recommended_actions": actions, "warnings": [],
        })
        return ProviderResult(content, input_tokens=70, output_tokens=30, reasoning_tokens=0)


class CommitAIReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "commit_review.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("commit-owner@example.com")
        database.save_user_ai_settings(self.owner["user_id"], encrypt_secret("sk-or-test-commit-review"), settings.openrouter_model)
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_COMMIT_REVIEW" (ROW_ID INTEGER PRIMARY KEY, "TEAM" TEXT, "SCORE" INTEGER)')
        conn.executemany(
            'INSERT INTO "QUEUE_BOARD_COMMIT_REVIEW" (ROW_ID, "TEAM", "SCORE") VALUES (?, ?, ?)',
            [(i, f"TEAM{i}", 50) for i in range(1, 11)],
        )
        conn.commit()
        conn.close()
        registered = database.register_dataset("QUEUE_BOARD_COMMIT_REVIEW", self.owner["user_id"], "commits.xlsx", 2, 2)
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (registered["repository_id"],)
        ).fetchone()[0]
        conn.close()
        self.repository_id = registered["repository_id"]
        self.branch_id = registered["main_branch_id"]
        self.table_id = "QUEUE_BOARD_COMMIT_REVIEW"
        self._original_provider = ai_gateway.providers.get("OPENROUTER")

    def tearDown(self):
        if self._original_provider is not None:
            ai_gateway.providers["OPENROUTER"] = self._original_provider
        else:
            ai_gateway.providers.pop("OPENROUTER", None)
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _commit(self, changes, message="Update data"):
        snapshot = database.get_table_snapshot(self.table_id)
        return commit_semantic_delta(
            table_id=self.table_id, repository_id=self.repository_id, branch_id=self.branch_id,
            expected_head_commit_id=snapshot["head_commit_id"], base_version=snapshot["version"],
            changes=changes, user_id=self.owner["user_id"], user_email=self.owner["email"], message=message,
        )

    def _sheet_row_column(self):
        snapshot = database.get_table_snapshot(self.table_id)
        sheet = snapshot["semantic"]["sheets"][0]
        row = sheet["rows"][0]
        column = next(item for item in sheet["columns"] if item["name"] == "TEAM")
        return sheet["sheet_id"], row["row_id"], column["column_id"]

    def _sheet_columns_rows(self):
        snapshot = database.get_table_snapshot(self.table_id)
        sheet = snapshot["semantic"]["sheets"][0]
        columns = {item["name"]: item["column_id"] for item in sheet["columns"]}
        return sheet["sheet_id"], [row["row_id"] for row in sheet["rows"]], columns

    async def test_clean_commit_scores_low_and_looks_good(self):
        ai_gateway.providers["OPENROUTER"] = FakeCommitReviewProvider("LOOKS_GOOD", "LOW")
        sheet_id, row_id, column_id = self._sheet_row_column()
        result = self._commit([
            {"operation_type": "CELL_VALUE_UPDATE", "sheet_id": sheet_id, "row_id": row_id, "column_id": column_id, "new_value": "SRH"},
        ])

        review = await commit_review.review_commit(self.organization_id, self.owner["user_id"], result["commit_id"])

        self.assertEqual("LOW", review["risk_level"])
        self.assertEqual("LOOKS_GOOD", review["recommendation"])
        self.assertEqual([], review["field_investigations"])

        stored = commit_review.get_latest_review(result["commit_id"])
        self.assertEqual("LOOKS_GOOD", stored["recommendation"])
        self.assertEqual(result["commit_id"], stored["commit_id"])

    async def test_cleared_field_is_investigated_and_explained(self):
        ai_gateway.providers["OPENROUTER"] = FakeCommitReviewProvider(
            "REVIEW_RECOMMENDED", "MEDIUM", field_explanations={"TEAM": "Cleared pending reassignment."},
        )
        sheet_id, row_id, column_id = self._sheet_row_column()
        result = self._commit([
            {"operation_type": "CELL_VALUE_UPDATE", "sheet_id": sheet_id, "row_id": row_id, "column_id": column_id, "new_value": ""},
        ])

        review = await commit_review.review_commit(self.organization_id, self.owner["user_id"], result["commit_id"])

        self.assertEqual(1, len(review["field_investigations"]))
        field = review["field_investigations"][0]
        self.assertEqual("TEAM", field["column_name"])
        self.assertEqual("TEAM1", field["previous_value"])
        self.assertEqual(self.owner["email"], field["changed_by"])
        self.assertEqual("Cleared pending reassignment.", field["explanation"])

    def test_ai_cannot_downgrade_below_deterministic_baseline(self):
        # Unit-level test of the guardrail itself (the fixture's small table
        # can't easily produce a real HIGH-risk commit through the full
        # validated commit path — see test_merge_risk_engine.py for the
        # deterministic scoring extremes). Build a genuinely HIGH-risk
        # ScoreResult directly and confirm the parser refuses to let a
        # LOOKS_GOOD AI claim override it.
        from app.merge_risk import evaluate_change_risk
        row_ids = [str(i) for i in range(10)]
        changes = (
            [{"operation_type": "CELL_FORMULA_UPDATE", "sheet_id": "S", "row_id": r, "column_id": "TEAM",
              "new_formula": "=A1&A2"} for r in row_ids[:6]]
            + [{"operation_type": "ROW_DELETE", "sheet_id": "S", "row_id": r} for r in row_ids[6:10]]
            + [{"operation_type": "CELL_VALUE_UPDATE", "sheet_id": "S", "row_id": r, "column_id": "SCORE",
                "old_value": "50", "new_value": "-50"} for r in row_ids[:5]]
        )
        field_investigations = [{"column_name": "TEAM", "row_id": "0", "previous_value": "TEAM1", "prior_edit_count": 3}]
        risk_result = evaluate_change_risk(changes, field_investigations=field_investigations, include_conflicts=False)
        self.assertIn(commit_review.deterministic_risk_level(risk_result.score), {"HIGH", "CRITICAL"})
        baseline = commit_review._RISK_TO_BASELINE_RECOMMENDATION[commit_review.deterministic_risk_level(risk_result.score)]
        self.assertEqual("REVIEW_RECOMMENDED", baseline)

        ai_result = {
            "answer": "Looks fine.", "confidence": 0.9, "recommended_actions": [
                {"title": "Author summary", "rationale": "Nothing to worry about.", "action_type": "LOOKS_GOOD", "risk_level": "LOW"},
            ],
            "warnings": [],
        }
        parsed = commit_review._parse_commit_review(ai_result, field_investigations, risk_result, baseline)

        self.assertEqual("LOOKS_GOOD", parsed["ai_recommendation"])
        self.assertEqual("REVIEW_RECOMMENDED", parsed["recommendation"])
        self.assertEqual(commit_review.deterministic_risk_level(risk_result.score), parsed["risk_level"])

    async def test_commit_succeeds_even_when_ai_review_is_never_called(self):
        # The commit itself must never depend on AI review succeeding —
        # prove commit_semantic_delta works standalone with no review step.
        sheet_id, row_id, column_id = self._sheet_row_column()
        result = self._commit([
            {"operation_type": "CELL_VALUE_UPDATE", "sheet_id": sheet_id, "row_id": row_id, "column_id": column_id, "new_value": "SRH"},
        ])
        self.assertIn("commit_id", result)
        self.assertIsNone(commit_review.get_latest_review(result["commit_id"]))

    async def test_review_is_upserted_on_recall(self):
        ai_gateway.providers["OPENROUTER"] = FakeCommitReviewProvider("LOOKS_GOOD", "LOW")
        sheet_id, row_id, column_id = self._sheet_row_column()
        result = self._commit([
            {"operation_type": "CELL_VALUE_UPDATE", "sheet_id": sheet_id, "row_id": row_id, "column_id": column_id, "new_value": "SRH"},
        ])

        first = await commit_review.review_commit(self.organization_id, self.owner["user_id"], result["commit_id"])
        second = await commit_review.review_commit(self.organization_id, self.owner["user_id"], result["commit_id"])

        self.assertNotEqual(first["review_id"], second["review_id"])
        conn = database._get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM COMMIT_AI_REVIEWS WHERE COMMIT_ID=?", (result["commit_id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(1, count)


if __name__ == "__main__":
    unittest.main()
