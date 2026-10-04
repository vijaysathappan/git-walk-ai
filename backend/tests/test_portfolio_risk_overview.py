import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from app import database
from app.euc.dependency.services import DependencyService
from app.euc.intelligence import IntelligenceService
from app.euc.portfolio import portfolio_risk_overview
from app.euc.service import analyze_euc, ingest_euc


def _xlsx_bytes(formulas):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet.append(["SCORE", "CALC"])
    for index, formula in enumerate(formulas, start=1):
        sheet.append([index * 10, formula])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class PortfolioRiskOverviewTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "portfolio.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("portfolio-owner@example.com")
        self.outsider = database.get_or_create_user("portfolio-outsider@example.com")

        self.scored_repo = self._make_repository("QUEUE_BOARD_SCORED")
        self.unscored_repo = self._make_repository("QUEUE_BOARD_UNSCORED")

        self.dependency_service = DependencyService()
        self.intelligence_service = IntelligenceService()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _make_repository(self, table_id):
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute(f'CREATE TABLE "{table_id}" (ROW_ID INTEGER PRIMARY KEY, "SCORE" INTEGER, "CALC" INTEGER)')
        conn.executemany(
            f'INSERT INTO "{table_id}" (ROW_ID, "SCORE", "CALC") VALUES (?, ?, ?)',
            [(i, i * 10, 0) for i in range(1, 4)],
        )
        conn.commit()
        conn.close()
        return database.register_dataset(table_id, self.owner["user_id"], f"{table_id.lower()}.xlsx", 3, 2)

    def _fully_score(self, repository_id, filename, formulas):
        asset = ingest_euc(repository_id, filename, _xlsx_bytes(formulas), self.owner["user_id"])
        analyze_euc(asset["euc_id"], self.owner["user_id"])
        self.dependency_service.build(asset["euc_id"], self.owner["user_id"])
        self.intelligence_service.build(asset["euc_id"], self.owner["user_id"])
        return asset["euc_id"]

    def test_scored_asset_appears_with_real_scores_and_finding_counts(self):
        # A broken reference so the run produces at least one finding.
        euc_id = self._fully_score(
            self.scored_repo["repository_id"], "scored.xlsx",
            ["=SCORE*2", "=NOTASHEET!Z99", "=SCORE*2"],
        )

        overview = portfolio_risk_overview(self.owner["user_id"])
        entry = next(item for item in overview["assets"] if item["euc_id"] == euc_id)

        self.assertEqual("SCORED", entry["status"])
        self.assertIsNotNone(entry["residual_risk"])
        self.assertIn(entry["residual_risk_classification"], ("VERY_LOW", "LOW", "MEDIUM", "HIGH", "VERY_HIGH"))
        self.assertEqual(self.scored_repo["repository_id"], entry["repository_id"])
        self.assertEqual(self.owner["email"], entry["owner_email"])

    def test_unanalyzed_asset_reports_not_scored_without_erroring(self):
        asset = ingest_euc(self.unscored_repo["repository_id"], "unscored.xlsx", _xlsx_bytes(["=SCORE*2", "=SCORE*2", "=SCORE*2"]), self.owner["user_id"])

        overview = portfolio_risk_overview(self.owner["user_id"])
        entry = next(item for item in overview["assets"] if item["euc_id"] == asset["euc_id"])

        self.assertEqual("NOT_SCORED", entry["status"])
        self.assertIsNone(entry["residual_risk"])

    def test_summary_counts_are_consistent_with_the_asset_list(self):
        self._fully_score(self.scored_repo["repository_id"], "scored2.xlsx", ["=SCORE*2", "=SCORE*2", "=SCORE*2"])
        ingest_euc(self.unscored_repo["repository_id"], "unscored2.xlsx", _xlsx_bytes(["=SCORE*2", "=SCORE*2", "=SCORE*2"]), self.owner["user_id"])

        overview = portfolio_risk_overview(self.owner["user_id"])
        scored = [item for item in overview["assets"] if item["status"] == "SCORED"]
        not_scored = [item for item in overview["assets"] if item["status"] == "NOT_SCORED"]

        self.assertEqual(len(overview["assets"]), overview["summary"]["total_assets"])
        self.assertEqual(len(scored), overview["summary"]["scored_assets"])
        self.assertEqual(len(not_scored), overview["summary"]["not_scored_assets"])

    def test_a_user_with_no_repository_access_sees_nothing(self):
        self._fully_score(self.scored_repo["repository_id"], "scored3.xlsx", ["=SCORE*2", "=SCORE*2", "=SCORE*2"])

        overview = portfolio_risk_overview(self.outsider["user_id"])

        self.assertEqual([], overview["assets"])
        self.assertEqual(0, overview["summary"]["total_assets"])


if __name__ == "__main__":
    unittest.main()
