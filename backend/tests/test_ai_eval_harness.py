"""Runs the real AI evaluation harness (app/ai/eval_harness.py) against its
golden dataset — see that module's docstring for why AIService.run_evaluation()
does not already cover this (it's a synthetic safety-baseline smoke test,
not a golden-dataset evaluation).

Also runnable standalone (for later Phase 5 CI wiring):
    cd backend && py -m app.ai.eval_harness
"""

import tempfile
import unittest
from pathlib import Path

from app import database
from app.ai.eval_harness import GOLDEN_CASES, run_harness
from app.ai.service import AIService
from app.secret_store import encrypt_secret


class AIEvalHarnessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "eval_harness.db"
        database.initialize_product_schema()
        self.user = database.get_or_create_user("eval-harness-test@example.com")
        database.save_user_ai_settings(
            self.user["user_id"], encrypt_secret("sk-or-eval-harness-test"),
            "nvidia/nemotron-3-super-120b-a12b:free",
        )
        self.organization_id = "ORG_EVAL_HARNESS_TEST"
        conn = database._get_connection()
        try:
            AIService._ensure_settings(conn, self.organization_id, self.user["user_id"])
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_golden_dataset_has_realistic_coverage(self):
        """Sanity check on the dataset itself before running it: enough
        cases, covering the domain scenarios the audit named explicitly
        (commit changes, merge request approvals), plus at least one
        explicit no-evidence / insufficient_evidence case."""
        self.assertGreaterEqual(len(GOLDEN_CASES), 15)
        self.assertLessEqual(len(GOLDEN_CASES), 20)
        questions = " ".join(case.question.lower() for case in GOLDEN_CASES)
        self.assertIn("commit", questions)
        self.assertIn("merge request", questions)
        self.assertTrue(any(not case.evidence for case in GOLDEN_CASES))
        self.assertTrue(any(case.expect_insufficient for case in GOLDEN_CASES))

    async def test_full_harness_passes(self):
        report = await run_harness(self.organization_id, self.user["user_id"])
        failed = [(r.case_id, r.failures) for r in report.results if not r.passed]
        self.assertEqual([], failed, msg=f"{report.summary}\n{failed}")

    async def test_fabricated_citation_is_never_trusted(self):
        """Directly re-asserts the single most important property from the
        golden dataset's fabricated-citation cases, independent of the
        aggregate harness pass/fail, so a regression here fails loudly."""
        report = await run_harness(self.organization_id, self.user["user_id"])
        by_id = {r.case_id: r for r in report.results}

        partial = by_id["fabricated_citation_is_stripped"]
        self.assertTrue(partial.passed, partial.failures)
        self.assertEqual({("AUDIT_EVENT", "AUD_C010")}, {(e["type"], e["id"]) for e in partial.answer["evidence"]})

        total = by_id["all_evidence_fabricated"]
        self.assertTrue(total.passed, total.failures)
        self.assertEqual([], total.answer["evidence"])

    async def test_no_evidence_case_forces_insufficient(self):
        report = await run_harness(self.organization_id, self.user["user_id"])
        by_id = {r.case_id: r for r in report.results}
        case = by_id["no_evidence_is_insufficient"]
        self.assertTrue(case.passed, case.failures)
        self.assertTrue(case.answer["insufficient_evidence"])
        self.assertEqual([], case.answer["evidence"])


if __name__ == "__main__":
    unittest.main()
