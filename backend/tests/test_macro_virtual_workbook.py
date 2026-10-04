"""Unit tests on the closed-world object model directly (no macro parsing
or interpretation involved) -- cell/sheet mutators must produce exactly the
SemanticChange shapes commit_semantic_delta expects, and the safety
boundary (no formatting writes, deleted-sheet guards) must hold."""

import tempfile
import unittest
import uuid
from pathlib import Path

from app import database
from app.macros.virtual_workbook import (
    InterpreterError, VBACollection, VBADictionary, VirtualWorkbook,
)


class VirtualWorkbookTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "virtual_workbook.db"
        database.initialize_product_schema()

        self.owner = database.get_or_create_user(f"owner_{uuid.uuid4().hex[:8]}@example.com")
        self.table_id = "QUEUE_BOARD_VWTEST"
        conn = database._get_connection()
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{self.table_id}"')
            conn.execute(f'''
                CREATE TABLE "{self.table_id}" (
                    ROW_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                    CATEGORY TEXT, AMOUNT INTEGER
                )
            ''')
            conn.executemany(
                f'INSERT INTO "{self.table_id}" (CATEGORY, AMOUNT) VALUES (?, ?)',
                [("Food", 10), ("Travel", 20), ("Food", 5)],
            )
            conn.commit()
        finally:
            conn.close()
        database.register_dataset(self.table_id, self.owner["user_id"], "vw.xlsx", 3, 2)
        repository = database.get_repository(self.table_id, self.owner["user_id"])
        self.repository_id = repository["repository_id"]

        working_copy = database.create_working_copy(self.table_id, self.owner["user_id"], self.owner["email"], branch_mode="new")
        self.branch_id = working_copy["branch_id"]
        self.branch_table_id = working_copy["table_id"]

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _workbook(self) -> VirtualWorkbook:
        # VirtualWorkbook only reads during __init__ (_load()/_load_sheet_data());
        # nothing it does afterward touches the connection, so it's safe to
        # close right away -- keeping it open would leak a Windows file lock
        # past tearDown's tempdir cleanup.
        conn = database._get_connection()
        try:
            return VirtualWorkbook(conn, branch_id=self.branch_id, repository_id=self.repository_id, data_table_id=self.branch_table_id)
        finally:
            conn.close()

    def test_loads_existing_cell_values(self):
        wb = self._workbook()
        self.assertEqual("Food", wb.cells(2, 1).value)
        self.assertEqual(20, wb.cells(3, 2).value)

    def test_writing_a_cell_produces_a_cell_value_update(self):
        wb = self._workbook()
        wb.cells(2, 2).value = 99
        changes = wb.pending_changes()
        self.assertEqual(1, len(changes))
        change = changes[0]
        self.assertEqual("CELL_VALUE_UPDATE", change["operation_type"])
        self.assertEqual(99, change["new_value"])
        self.assertNotIn(change["row_id"], (None, ""))
        self.assertNotIn(change["column_id"], (None, ""))

    def test_writing_beyond_the_sheet_extent_on_an_existing_sheet_is_dropped_not_raised(self):
        wb = self._workbook()
        wb.cells(50, 1).value = "out of range"  # row 50 doesn't exist yet
        self.assertEqual([], wb.pending_changes())

    def test_worksheet_lookup_by_index_and_by_name(self):
        wb = self._workbook()
        sheet_name = wb.default_sheet().name
        self.assertIs(wb.worksheet(1), wb.default_sheet())
        self.assertIs(wb.worksheet(sheet_name), wb.default_sheet())
        self.assertIs(wb.worksheet(sheet_name.upper()), wb.default_sheet())
        with self.assertRaises(InterpreterError):
            wb.worksheet("DoesNotExist")

    def test_deleting_an_existing_sheet_produces_sheet_delete(self):
        wb = self._workbook()
        sheet = wb.default_sheet()
        sheet.delete()
        changes = wb.pending_changes()
        self.assertEqual([{"operation_type": "SHEET_DELETE", "sheet_id": sheet.sheet_id}], changes)

    def test_renaming_an_existing_sheet_produces_sheet_rename(self):
        wb = self._workbook()
        sheet = wb.default_sheet()
        old_name = sheet.name
        sheet._rename("Renamed")
        changes = wb.pending_changes()
        self.assertEqual(1, len(changes))
        self.assertEqual("SHEET_RENAME", changes[0]["operation_type"])
        self.assertEqual(old_name, changes[0]["old_value"])
        self.assertEqual("Renamed", changes[0]["new_value"])

    def test_renaming_to_an_existing_name_raises(self):
        wb = self._workbook()
        wb.add_worksheet()  # "Sheet2"
        with self.assertRaises(InterpreterError):
            wb.worksheet(2)._rename(wb.default_sheet().name)

    def test_adding_a_worksheet_with_no_writes_produces_no_changes(self):
        wb = self._workbook()
        wb.add_worksheet()
        self.assertEqual([], wb.pending_changes())

    def test_deleting_a_never_committed_new_sheet_removes_it_entirely(self):
        wb = self._workbook()
        new_sheet = wb.add_worksheet()
        new_sheet.cells(1, 1).value = "Header"
        new_sheet.delete()
        self.assertNotIn(new_sheet, wb.sheets)
        self.assertEqual([], wb.pending_changes())

    def test_new_sheet_with_header_and_rows_produces_correct_create_column_row_changes(self):
        wb = self._workbook()
        summary = wb.add_worksheet()
        summary.cells(1, 1).value = "Category"
        summary.cells(1, 2).value = "Total"
        summary.cells(2, 1).value = "Food"
        summary.cells(2, 2).value = 15
        summary.cells(3, 1).value = "Travel"
        summary.cells(3, 2).value = 20

        changes = wb.pending_changes()
        creates = [c for c in changes if c["operation_type"] == "SHEET_CREATE"]
        columns = [c for c in changes if c["operation_type"] == "COLUMN_INSERT"]
        rows = [c for c in changes if c["operation_type"] == "ROW_INSERT"]
        self.assertEqual(1, len(creates))
        self.assertEqual(2, len(columns))
        self.assertEqual(2, len(rows))

        # Column names come from the real header text the macro wrote,
        # sanitized the same way an uploaded workbook's headers are --
        # not synthetic COL_N placeholders.
        column_names = sorted(c["new_value"] for c in columns)
        self.assertEqual(["CATEGORY", "TOTAL"], column_names)

        category_col = next(c for c in columns if c["new_value"] == "CATEGORY")["column_id"]
        total_col = next(c for c in columns if c["new_value"] == "TOTAL")["column_id"]
        rows_by_position = sorted(rows, key=lambda r: r["new_row_position"])
        self.assertEqual({category_col: "Food", total_col: 15}, rows_by_position[0]["new_value"])
        self.assertEqual({category_col: "Travel", total_col: 20}, rows_by_position[1]["new_value"])

    def test_new_sheet_without_a_header_falls_back_to_synthetic_column_names(self):
        wb = self._workbook()
        blank = wb.add_worksheet()
        blank.cells(2, 1).value = "no header written for column 1"
        changes = wb.pending_changes()
        columns = [c for c in changes if c["operation_type"] == "COLUMN_INSERT"]
        self.assertEqual(["COL_1"], [c["new_value"] for c in columns])

    def test_deleted_sheet_cannot_be_written_to(self):
        wb = self._workbook()
        sheet = wb.default_sheet()
        sheet.delete()
        with self.assertRaises(InterpreterError):
            sheet.cells(1, 1).value = "nope"


class VBADictionaryTests(unittest.TestCase):
    def test_add_then_exists_item_keys_count(self):
        d = VBADictionary()
        d.add("Food", 10)
        d.add("Travel", 20)
        self.assertTrue(d.exists("Food"))
        self.assertFalse(d.exists("Other"))
        self.assertEqual(10, d.item("Food"))
        self.assertEqual(10, d("Food"))
        self.assertEqual({"Food", "Travel"}, set(d.keys))
        self.assertEqual(2, d.count)

    def test_add_twice_raises(self):
        d = VBADictionary()
        d.add("Food", 10)
        with self.assertRaises(InterpreterError):
            d.add("Food", 99)

    def test_set_index_overwrites_without_raising(self):
        d = VBADictionary()
        d.add("Food", 10)
        d.set_index("Food", 15)
        self.assertEqual(15, d.item("Food"))


class VBACollectionTests(unittest.TestCase):
    def test_add_then_one_based_item_and_count(self):
        c = VBACollection()
        c.add("a")
        c.add("b")
        self.assertEqual(2, c.count)
        self.assertEqual("a", c.item(1))
        self.assertEqual("b", c(2))
        self.assertEqual(["a", "b"], list(c))

    def test_out_of_range_index_raises(self):
        c = VBACollection()
        c.add("a")
        with self.assertRaises(InterpreterError):
            c.item(2)
        with self.assertRaises(InterpreterError):
            c.item(0)


if __name__ == "__main__":
    unittest.main()
