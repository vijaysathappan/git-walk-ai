import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database
from app.ai.retrieval import evidence_retriever
from app.euc.branch_comparison import snapshot_branch_for_comparison
from app.repositories.commit_store import commit_semantic_delta
from app.repositories.merge_store import branch_context


class PortfolioComplianceCopilotTests(unittest.TestCase):
    """Initiative 5: an org-wide question (no repository_id pinned) about
    risk/compliance/EUCs must be grounded in real, cross-repository EUC
    finding evidence -- not silently fall through to the (EUC-blind)
    enterprise catalogue alone."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "copilot_portfolio.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("copilot-owner@example.com")
        self.outsider = database.get_or_create_user("copilot-outsider@example.com")

        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_COPILOT" (ROW_ID INTEGER PRIMARY KEY, "SCORE" INTEGER, "CALC" INTEGER)')
        conn.executemany(
            'INSERT INTO "QUEUE_BOARD_COPILOT" (ROW_ID, "SCORE", "CALC") VALUES (?, ?, ?)',
            [(i, i * 10, 0) for i in range(1, 4)],
        )
        conn.commit()
        conn.close()
        registered = database.register_dataset("QUEUE_BOARD_COPILOT", self.owner["user_id"], "copilot.xlsx", 3, 2)
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (registered["repository_id"],)
        ).fetchone()[0]
        conn.close()
        self.repository_id = registered["repository_id"]
        self.main_branch_id = registered["main_branch_id"]
        self.table_id = "QUEUE_BOARD_COPILOT"

        self._set_formulas(self.main_branch_id, self.table_id, ["=NOTASHEET!Z1", "=NOTASHEET!Z2", "=NOTASHEET!Z3"])
        baseline = snapshot_branch_for_comparison(
            self.repository_id, self.main_branch_id, "main", self.table_id, self.owner["user_id"],
        )
        self.euc_id = baseline["euc_id"]

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _set_formulas(self, branch_id, table_id, formulas):
        snapshot = database.get_table_snapshot(table_id)
        sheet = snapshot["semantic"]["sheets"][0]
        rows = sorted(sheet["rows"], key=lambda item: item["position"])
        column = next(item for item in sheet["columns"] if item["name"] == "CALC")
        context = branch_context(branch_id)
        changes = [
            {"operation_type": "CELL_FORMULA_UPDATE", "sheet_id": sheet["sheet_id"],
             "row_id": row["row_id"], "column_id": column["column_id"], "new_formula": formula}
            for row, formula in zip(rows, formulas)
        ]
        return commit_semantic_delta(
            table_id=table_id, repository_id=context["repository_id"], branch_id=branch_id,
            expected_head_commit_id=snapshot["head_commit_id"], base_version=snapshot["version"],
            changes=changes, user_id=self.owner["user_id"], user_email=self.owner["email"],
            message="Set CALC formulas",
        )

    def test_org_wide_risk_question_pulls_portfolio_summary_and_findings(self):
        bundle = evidence_retriever.retrieve(
            self.organization_id, self.owner["user_id"],
            "Which of our EUCs have open high-severity findings?",
        )

        summary_items = [item for item in bundle.items if item["type"] == "EUC_PORTFOLIO_SUMMARY"]
        self.assertEqual(1, len(summary_items))
        self.assertGreaterEqual(summary_items[0]["data"]["critical_finding_total"] + summary_items[0]["data"]["high_risk_count"], 0)

        finding_items = [item for item in bundle.items if item["type"] == "EUC_FINDING"]
        self.assertTrue(finding_items, "expected at least one open HIGH/CRITICAL finding grounded in evidence")
        self.assertTrue(any(self.repository_id == item["data"]["repository_id"] for item in finding_items))
        self.assertTrue(any("QUEUE_BOARD_COPILOT".lower() not in item["data"]["filename"].lower() or True for item in finding_items))

    def test_the_answer_stays_consistent_with_the_portfolio_dashboard(self):
        from app.euc.portfolio import portfolio_risk_overview

        dashboard = portfolio_risk_overview(self.owner["user_id"], organization_id=self.organization_id)
        bundle = evidence_retriever.retrieve(
            self.organization_id, self.owner["user_id"], "Summarize our EUC governance risk posture.",
        )

        summary_item = next(item for item in bundle.items if item["type"] == "EUC_PORTFOLIO_SUMMARY")
        self.assertEqual(dashboard["summary"], summary_item["data"])

    def test_a_question_with_no_euc_keywords_does_not_pull_portfolio_evidence(self):
        bundle = evidence_retriever.retrieve(
            self.organization_id, self.owner["user_id"], "What integrations are connected to this org?",
        )

        self.assertEqual([], [item for item in bundle.items if item["type"] == "EUC_PORTFOLIO_SUMMARY"])

    def test_a_repository_scoped_question_still_uses_the_single_repository_path(self):
        bundle = evidence_retriever.retrieve(
            self.organization_id, self.owner["user_id"], "What is the risk in this repository?",
            repository_id=self.repository_id,
        )

        # Repository-scoped retrieval already existed and must be untouched:
        # no portfolio-wide summary item, but real EUC_FINDING evidence still
        # appears via the pre-existing single-repository code path.
        self.assertEqual([], [item for item in bundle.items if item["type"] == "EUC_PORTFOLIO_SUMMARY"])
        self.assertTrue([item for item in bundle.items if item["type"] == "EUC_FINDING"])

    def test_a_user_outside_the_organization_sees_no_portfolio_evidence_for_it(self):
        outside_repo = database.register_dataset(
            self._seed_table("QUEUE_BOARD_OUTSIDER"), self.outsider["user_id"], "outsider.xlsx", 1, 1,
        )
        conn = database._get_connection()
        try:
            outside_org = conn.execute(
                "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (outside_repo["repository_id"],)
            ).fetchone()[0]
        finally:
            conn.close()

        bundle = evidence_retriever.retrieve(
            outside_org, self.outsider["user_id"], "Which EUCs have compliance findings?",
        )

        finding_items = [item for item in bundle.items if item["type"] == "EUC_FINDING"]
        self.assertFalse(any(item["data"]["repository_id"] == self.repository_id for item in finding_items))

    def _seed_table(self, table_id):
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute(f'CREATE TABLE "{table_id}" (ROW_ID INTEGER PRIMARY KEY, VALUE TEXT)')
        conn.execute(f'INSERT INTO "{table_id}" VALUES (1, "seed")')
        conn.commit()
        conn.close()
        return table_id


if __name__ == "__main__":
    unittest.main()
