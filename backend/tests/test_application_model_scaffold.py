import sqlite3
import tempfile
import unittest
import zipfile
import io
from pathlib import Path

from app import database
from app.euc.application_model.services.application_service import ApplicationModelService
from app.euc.branch_comparison import snapshot_branch_for_comparison
from app.euc.migration import MigrationService
from app.repositories.commit_store import commit_semantic_delta
from app.repositories.merge_store import branch_context


class ApplicationModelScaffoldTests(unittest.TestCase):
    """Initiative 4 (Scaffold Generation from the Native Model) turned out to
    already be fully implemented -- ApplicationModelService.generate(mode=
    "SCAFFOLD") + ScaffoldCompiler already produce a real, downloadable,
    provenance-tagged codebase. This file is the test coverage that never
    existed for it; it doesn't add new production code."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "application_model.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("air-owner@example.com")

        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_AIR" (ROW_ID INTEGER PRIMARY KEY, "SCORE" INTEGER, "CALC" INTEGER)')
        conn.executemany(
            'INSERT INTO "QUEUE_BOARD_AIR" (ROW_ID, "SCORE", "CALC") VALUES (?, ?, ?)',
            [(i, i * 10, 0) for i in range(1, 6)],
        )
        conn.commit()
        conn.close()
        registered = database.register_dataset("QUEUE_BOARD_AIR", self.owner["user_id"], "air.xlsx", 5, 3)
        self.repository_id = registered["repository_id"]
        self.main_branch_id = registered["main_branch_id"]
        self.table_id = "QUEUE_BOARD_AIR"

        self._set_formulas(self.main_branch_id, self.table_id, ["=SCORE*2"] * 5)
        baseline = snapshot_branch_for_comparison(
            self.repository_id, self.main_branch_id, "main", self.table_id, self.owner["user_id"],
        )
        self.euc_id = baseline["euc_id"]
        MigrationService().analyze(self.euc_id, self.owner["user_id"])

        self.air_service = ApplicationModelService()

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

    def _confirm_all_reviews(self):
        components = self.air_service.components(self.euc_id, self.owner["user_id"])["items"]
        for component in components:
            if component["review_state"] == "REVIEW_REQUIRED":
                self.air_service.review(
                    self.euc_id, component["component_id"], self.owner["user_id"],
                    "CONFIRMED", "Confirmed against the source workbook for this test",
                )

    def test_build_produces_a_review_required_air_with_real_components(self):
        overview = self.air_service.build(self.euc_id, self.owner["user_id"])

        self.assertIn(overview["status"], ("REVIEW_REQUIRED", "GENERATED"))
        self.assertEqual(0, overview["validation_error_count"])
        self.assertGreater(overview["summary"]["counts"]["entities"], 0)
        self.assertGreater(overview["summary"]["counts"]["calculations"], 0)

    def test_scaffold_generation_is_blocked_before_approval(self):
        self.air_service.build(self.euc_id, self.owner["user_id"])

        with self.assertRaises(ValueError):
            self.air_service.generate(self.euc_id, self.owner["user_id"], "SCAFFOLD")

    def test_model_only_export_does_not_require_approval(self):
        self.air_service.build(self.euc_id, self.owner["user_id"])

        generated = self.air_service.generate(self.euc_id, self.owner["user_id"], "MODEL_ONLY")

        self.assertEqual("COMPLETED", generated["status"])
        self.assertEqual("MODEL_ONLY", generated["generation_mode"])
        filename, payload = self.air_service.download(self.euc_id, generated["generation_run_id"], self.owner["user_id"])
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(["air/application-model.json"], archive.namelist())

    def test_approving_with_open_reviews_is_rejected(self):
        self.air_service.build(self.euc_id, self.owner["user_id"])

        with self.assertRaises(ValueError):
            self.air_service.approve(self.euc_id, self.owner["user_id"])

    def test_full_scaffold_pipeline_produces_a_real_provenance_tagged_codebase(self):
        self.air_service.build(self.euc_id, self.owner["user_id"])
        self._confirm_all_reviews()

        approved = self.air_service.approve(self.euc_id, self.owner["user_id"])
        self.assertEqual("APPROVED", approved["status"])

        generated = self.air_service.generate(self.euc_id, self.owner["user_id"], "SCAFFOLD")
        self.assertEqual("COMPLETED", generated["status"])
        self.assertEqual("SCAFFOLD", generated["generation_mode"])
        self.assertGreater(generated["files_generated"], 5)

        overview_after = self.air_service.overview(self.euc_id, self.owner["user_id"])
        self.assertEqual("CODE_GENERATED", overview_after["status"])

        filename, payload = self.air_service.download(self.euc_id, generated["generation_run_id"], self.owner["user_id"])
        self.assertTrue(filename.endswith(".zip"))
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = set(archive.namelist())
            for expected in ("backend/app/main.py", "backend/app/models.py", "backend/app/api/generated.py",
                              "database/schema/001_initial.sql", "frontend/src/App.tsx", "provenance.json"):
                self.assertIn(expected, names)
            models_source = archive.read("backend/app/models.py").decode()
            self.assertIn("BaseModel", models_source)
            self.assertIn("provenance:", models_source)

    def test_rejected_components_are_excluded_from_generated_code(self):
        self.air_service.build(self.euc_id, self.owner["user_id"])
        components = self.air_service.components(self.euc_id, self.owner["user_id"], component_type="ENTITY")["items"]
        entity = components[0]

        self.air_service.review(
            self.euc_id, entity["component_id"], self.owner["user_id"],
            "REJECTED", "Not needed in the target application",
        )
        remaining = self.air_service.components(self.euc_id, self.owner["user_id"])["items"]
        for component in remaining:
            if component["review_state"] == "REVIEW_REQUIRED" and component["component_id"] != entity["component_id"]:
                self.air_service.review(
                    self.euc_id, component["component_id"], self.owner["user_id"],
                    "CONFIRMED", "Confirmed for this test",
                )

        self.air_service.approve(self.euc_id, self.owner["user_id"])
        generated = self.air_service.generate(self.euc_id, self.owner["user_id"], "SCAFFOLD")
        _, payload = self.air_service.download(self.euc_id, generated["generation_run_id"], self.owner["user_id"])
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            models_source = archive.read("backend/app/models.py").decode()
        self.assertNotIn(entity["component_id"], models_source)

    def test_bulk_review_confirms_every_open_component_in_one_call(self):
        overview = self.air_service.build(self.euc_id, self.owner["user_id"])
        open_before = overview["review_required_count"]
        self.assertGreater(open_before, 0, "fixture should produce at least one review-required component")

        result = self.air_service.bulk_review(
            self.euc_id, self.owner["user_id"], "CONFIRMED",
            "Bulk-confirmed after spot-checking a sample against the source workbook.",
        )

        self.assertEqual(open_before, result["bulk_reviewed_count"])
        self.assertEqual(0, result["review_required_count"])
        self.assertEqual("GENERATED", result["status"])
        components = self.air_service.components(self.euc_id, self.owner["user_id"])["items"]
        self.assertFalse(any(item["review_state"] == "REVIEW_REQUIRED" for item in components))

    def test_bulk_review_can_be_scoped_by_type_and_confidence(self):
        self.air_service.build(self.euc_id, self.owner["user_id"])
        controls_before = self.air_service.components(self.euc_id, self.owner["user_id"], component_type="CONTROL")["items"]
        open_controls = [item for item in controls_before if item["review_state"] == "REVIEW_REQUIRED"]

        result = self.air_service.bulk_review(
            self.euc_id, self.owner["user_id"], "CONFIRMED", "Confirming controls only for this test.",
            component_type="CONTROL", min_confidence=0.0,
        )

        self.assertEqual(len(open_controls), result["bulk_reviewed_count"])
        remaining = self.air_service.components(self.euc_id, self.owner["user_id"])["items"]
        non_control_still_open = [item for item in remaining if item["component_type"] != "CONTROL" and item["review_state"] == "REVIEW_REQUIRED"]
        if result["review_required_count"]:
            self.assertTrue(non_control_still_open, "scoping to CONTROL should leave other types untouched")

    def test_bulk_review_rejects_invalid_decision_and_short_reason(self):
        self.air_service.build(self.euc_id, self.owner["user_id"])
        with self.assertRaises(ValueError):
            self.air_service.bulk_review(self.euc_id, self.owner["user_id"], "MAYBE", "A valid reason here")
        with self.assertRaises(ValueError):
            self.air_service.bulk_review(self.euc_id, self.owner["user_id"], "CONFIRMED", "ok")

    def test_generated_files_can_be_listed_and_previewed_without_downloading(self):
        self.air_service.build(self.euc_id, self.owner["user_id"])
        self.air_service.bulk_review(self.euc_id, self.owner["user_id"], "CONFIRMED", "Bulk-confirmed for this test.")
        self.air_service.approve(self.euc_id, self.owner["user_id"])
        generated = self.air_service.generate(self.euc_id, self.owner["user_id"], "SCAFFOLD")

        files = self.air_service.generation_files(self.euc_id, self.owner["user_id"], generated["generation_run_id"])
        by_path = {item["path"]: item["size_bytes"] for item in files}
        self.assertIn("backend/app/models.py", by_path)
        self.assertGreater(by_path["backend/app/models.py"], 0)

        content = self.air_service.generation_file(
            self.euc_id, self.owner["user_id"], generated["generation_run_id"], "backend/app/models.py",
        )
        self.assertIn("BaseModel", content)
        self.assertIn("provenance:", content)

        with self.assertRaises(KeyError):
            self.air_service.generation_file(
                self.euc_id, self.owner["user_id"], generated["generation_run_id"], "does/not/exist.py",
            )


if __name__ == "__main__":
    unittest.main()
