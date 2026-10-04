"""Phase 5 onboarding unification: uploading a .xlsm through the normal
"create repository" endpoint must, in one step, version its data (existing
pipeline, untouched) AND auto-register+extract its embedded macros (the
existing Virtual Run pipeline) -- no separate manual upload required. A
plain .xlsx upload must behave exactly as before (no macro registration
attempted at all)."""

import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from vba_fixture_builder import build_xlsm  # noqa: E402

from app import database  # noqa: E402
from app.main import app  # noqa: E402
from app.macros import run_service  # noqa: E402
from app.security import create_session_token  # noqa: E402

MACRO_SOURCE = (
    "Public Sub Recalc()\n"
    "    Dim i As Long\n"
    "    For i = 2 To 4\n"
    "        Cells(i, 3).Value = Cells(i, 1).Value * Cells(i, 2).Value\n"
    "    Next i\n"
    "End Sub\n"
)


class OnboardingMacroAutoRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.user = database.get_or_create_user(f"onboarding_{id(self)}@example.com")
        self.token = create_session_token(self.user["user_id"], self.user["email"])

    def _auth(self):
        return {"Authorization": f"Bearer {self.token}"}

    def test_uploading_an_xlsm_auto_registers_and_extracts_its_macros(self):
        xlsm_bytes = build_xlsm(
            [["Qty", "Price", "Total"], [2, 10, None], [3, 5, None], [1, 4, None]],
            "Module1", MACRO_SOURCE,
        )
        response = self.client.post(
            "/api/v1/upload-and-provision",
            headers=self._auth(),
            files={"file": ("commission.xlsm", xlsm_bytes, "application/vnd.ms-excel.sheet.macroEnabled.12")},
            data={"repository_name": f"onboarding-macro-test-{id(self)}"},
        )
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertTrue(body["macros_registered"])
        self.assertEqual(1, body["macro_count"])
        self.assertEqual(1, body["macro_runnable_count"])

        table_id = body["table_id"]
        listing = run_service.list_macros(table_id, self.user["user_id"])
        self.assertTrue(listing["has_source"])
        self.assertEqual("COMPLETED", listing["extraction_status"])
        self.assertEqual(1, len(listing["macros"]))
        self.assertEqual("Recalc", listing["macros"][0]["proc_name"])
        self.assertTrue(listing["macros"][0]["runnable"])
        self.assertEqual("SQL", listing["macros"][0]["execution_lane"])

    def test_uploading_a_plain_xlsx_registers_no_macros(self):
        import openpyxl
        import io as _io

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["Qty", "Price", "Total"])
        sheet.append([2, 10, None])
        buffer = _io.BytesIO()
        workbook.save(buffer)

        response = self.client.post(
            "/api/v1/upload-and-provision",
            headers=self._auth(),
            files={"file": ("plain.xlsx", buffer.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            data={"repository_name": f"onboarding-plain-test-{id(self)}"},
        )
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertFalse(body["macros_registered"])
        self.assertEqual(0, body["macro_count"])

        table_id = body["table_id"]
        listing = run_service.list_macros(table_id, self.user["user_id"])
        self.assertFalse(listing["has_source"])


if __name__ == "__main__":
    unittest.main()
