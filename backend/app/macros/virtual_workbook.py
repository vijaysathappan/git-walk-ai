"""The closed-world safe object model the general-lane interpreter executes
macros against. This IS the safety boundary described throughout the
Virtual Run design: these classes expose Range/Cells/Rows/Columns/
Worksheets/Dictionary/Collection members and nothing else -- there is no
``Shell``, no ``FileSystemObject``, no network client, no Win32 API
surface anywhere in this file. A macro cannot reach outside the workbook
not because an attempt is detected and blocked at runtime, but because the
capability to do so was never implemented here. static_gate additionally
name-checks the interpreter's AST against this exact same surface before
anything runs, so an unsupported reference fails closed at classification
time, not only at execution time.

Every mutation both updates the in-memory working copy AND records a
pending SemanticChange -- so the diff/preview app.macros.run_service shows
an editor falls straight out of interpretation, with no separate "compute
the diff" pass. Nothing here writes to the database; app.macros.
interpreted_execution hands the collected changes to the exact same
commit_semantic_delta every other Git Walk edit goes through.
"""

from __future__ import annotations

import uuid
from typing import Any

from ..excel.identity import ensure_branch_identities, sheet_table_id


class InterpreterError(RuntimeError):
    """A runtime error from executing a macro against the virtual
    workbook -- e.g. referencing a worksheet that doesn't exist. Caught by
    the interpreter's ``On Error Resume Next`` handling when active."""


def _stable_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16].upper()}"


class VirtualRange:
    """A single cell, addressed by 1-based Excel (row, col) -- ``Cells(r,
    c)``. Only ``.Value`` is supported; cell formatting (``.Interior.Color``,
    ``.NumberFormat``) and anything else (``.Formula`` write, ``.Sort``,
    merged ranges, multi-cell ``Range("A1:B2")``) is out of scope for this
    phase and simply isn't implemented here -- see interpreter.py's
    _SAFE_MEMBERS for why formatting specifically is deferred, not just
    unwritten."""

    def __init__(self, sheet: "VirtualSheet", row: int, col: int):
        self._sheet = sheet
        self._row = int(row)
        self._col = int(col)

    @property
    def value(self) -> Any:
        return self._sheet._get_cell(self._row, self._col)

    @value.setter
    def value(self, new_value: Any) -> None:
        self._sheet._set_cell(self._row, self._col, new_value)


class VirtualRows:
    def __init__(self, sheet: "VirtualSheet"):
        self._sheet = sheet

    @property
    def count(self) -> int:
        return self._sheet._max_row - 1 if self._sheet._max_row >= 1 else 0  # data rows only (row 1 is the header)


class VirtualColumns:
    def __init__(self, sheet: "VirtualSheet"):
        self._sheet = sheet

    @property
    def count(self) -> int:
        return self._sheet._max_col


class VirtualSheet:
    def __init__(self, workbook: "VirtualWorkbook", sheet_id: str | None, name: str, position: int, is_new: bool = False):
        self.workbook = workbook
        self.sheet_id = sheet_id
        self._name = name
        self.position = position
        self.is_new = is_new
        self.deleted = False
        self._renamed_from: str | None = None
        self._cells: dict[tuple[int, int], Any] = {}
        self._loaded_cells: dict[tuple[int, int], Any] = {}  # pristine baseline as of load; never mutated after
        self._dirty: set[tuple[int, int]] = set()            # cells the macro actually wrote (see _set_cell)
        self._row_identity: dict[int, dict[str, Any]] = {}   # excel_row -> {row_id, physical_row_id}
        self._col_identity: dict[int, dict[str, Any]] = {}   # excel_col -> {column_id, column_name}
        self._max_row = 0
        self._max_col = 0

    def cells(self, row: Any, col: Any) -> VirtualRange:
        if self.deleted:
            raise InterpreterError(f"Worksheet '{self.name}' has been deleted and can no longer be used")
        return VirtualRange(self, int(row), int(col))

    @property
    def rows(self) -> VirtualRows:
        return VirtualRows(self)

    @property
    def columns(self) -> VirtualColumns:
        return VirtualColumns(self)

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, new_name: str) -> None:
        # Routed through _rename() -- not a plain attribute -- so a macro
        # writing `sheet.Name = "X"` gets the same validation (empty name,
        # collision check) and the same _renamed_from bookkeeping that
        # pending_changes() depends on to emit SHEET_RENAME. A bare
        # attribute here would silently lose the rename.
        self._rename(new_name)

    def delete(self) -> None:
        if self.is_new:
            # Never committed -- just drop it from the pending set entirely.
            self.workbook.sheets.remove(self)
            return
        self.deleted = True

    def _rename(self, new_name: str) -> None:
        new_name = str(new_name).strip()
        if not new_name:
            raise InterpreterError("A worksheet name cannot be empty")
        if any(s is not self and not s.deleted and s.name.lower() == new_name.lower() for s in self.workbook.sheets):
            raise InterpreterError(f"A worksheet named '{new_name}' already exists")
        if not self.is_new and self._renamed_from is None:
            self._renamed_from = self.name
        self._name = new_name

    def _get_cell(self, row: int, col: int) -> Any:
        return self._cells.get((row, col))

    def _set_cell(self, row: int, col: int, value: Any) -> None:
        if self.deleted:
            raise InterpreterError(f"Worksheet '{self.name}' has been deleted and can no longer be used")
        self._cells[(row, col)] = value
        self._dirty.add((row, col))
        self._max_row = max(self._max_row, row)
        self._max_col = max(self._max_col, col)


class VirtualWorksheets:
    """``Worksheets`` / ``Sheets`` -- both bound to the same collection,
    matching real Excel where the two names are near-synonyms for a
    normal workbook."""

    def __init__(self, workbook: "VirtualWorkbook"):
        self._workbook = workbook

    def __call__(self, key: Any) -> VirtualSheet:
        return self._workbook.worksheet(key)

    def add(self, *args: Any) -> VirtualSheet:
        return self._workbook.add_worksheet()

    @property
    def count(self) -> int:
        return len([s for s in self._workbook.sheets if not s.deleted])


class VBADictionary:
    """``CreateObject("Scripting.Dictionary")`` -- an in-memory key/value
    store with no persistence and no file-system involvement whatsoever."""

    def __init__(self) -> None:
        self._data: dict[Any, Any] = {}

    def add(self, key: Any, value: Any) -> None:
        if key in self._data:
            raise InterpreterError(f"key {key!r} is already in this Dictionary (Add does not overwrite -- use totals(key) = value instead)")
        self._data[key] = value

    def exists(self, key: Any) -> bool:
        return key in self._data

    def item(self, key: Any) -> Any:
        return self._data.get(key)

    def __call__(self, key: Any) -> Any:
        return self.item(key)

    def set_index(self, key: Any, value: Any) -> None:
        self._data[key] = value

    @property
    def keys(self) -> list[Any]:
        return list(self._data.keys())

    @property
    def count(self) -> int:
        return len(self._data)


class VBACollection:
    """``New Collection`` -- an ordered, 1-based-indexed list."""

    def __init__(self) -> None:
        self._items: list[Any] = []

    def add(self, item: Any) -> None:
        self._items.append(item)

    def item(self, index: Any) -> Any:
        i = int(index)
        if i < 1 or i > len(self._items):
            raise InterpreterError(f"Collection index {i} is out of range (1..{len(self._items)})")
        return self._items[i - 1]

    def __call__(self, index: Any) -> Any:
        return self.item(index)

    @property
    def count(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)


class VirtualWorkbook:
    """Loads every worksheet on a branch eagerly into memory (bounded by
    the product's own existing MAX_WORKBOOK_ROWS/MAX_WORKBOOK_COLUMNS
    limits), lets the interpreter mutate the in-memory copy, and exposes
    the resulting pending SemanticChange list -- nothing here ever touches
    the database directly; app.macros.interpreted_execution does the one
    read at load and hands the collected changes to commit_semantic_delta."""

    def __init__(self, conn, branch_id: str, repository_id: str, data_table_id: str):
        self.conn = conn
        self.branch_id = branch_id
        self.repository_id = repository_id
        self.data_table_id = data_table_id
        self.sheets: list[VirtualSheet] = []
        self._load()

    def _load(self) -> None:
        ensure_branch_identities(self.conn, self.branch_id, self.repository_id, self.data_table_id)
        sheet_rows = self.conn.execute(
            "SELECT SHEET_ID, SHEET_NAME, SHEET_POSITION FROM BRANCH_SHEETS WHERE BRANCH_ID=? AND STATUS='ACTIVE' ORDER BY SHEET_POSITION",
            (self.branch_id,),
        ).fetchall()
        for row in sheet_rows:
            sheet = VirtualSheet(self, sheet_id=row["SHEET_ID"], name=row["SHEET_NAME"], position=row["SHEET_POSITION"])
            self._load_sheet_data(sheet)
            sheet._loaded_cells = dict(sheet._cells)  # snapshot the pristine baseline before any macro write
            self.sheets.append(sheet)

    def _load_sheet_data(self, sheet: VirtualSheet) -> None:
        columns = self.conn.execute(
            "SELECT COLUMN_ID, COLUMN_NAME, COLUMN_POSITION FROM SHEET_COLUMNS WHERE BRANCH_ID=? AND SHEET_ID=? AND STATUS='ACTIVE'",
            (self.branch_id, sheet.sheet_id),
        ).fetchall()
        for column in columns:
            excel_col = column["COLUMN_POSITION"] + 1
            sheet._col_identity[excel_col] = {"column_id": column["COLUMN_ID"], "column_name": column["COLUMN_NAME"]}
            sheet._max_col = max(sheet._max_col, excel_col)
        rows = self.conn.execute(
            "SELECT ROW_ID, PHYSICAL_ROW_ID, ROW_POSITION FROM SHEET_ROWS WHERE BRANCH_ID=? AND SHEET_ID=? AND STATUS='ACTIVE'",
            (self.branch_id, sheet.sheet_id),
        ).fetchall()
        for row in rows:
            excel_row = row["ROW_POSITION"] + 2
            sheet._row_identity[excel_row] = {"row_id": row["ROW_ID"], "physical_row_id": row["PHYSICAL_ROW_ID"]}
            sheet._max_row = max(sheet._max_row, excel_row)
        if not columns or not rows:
            return
        physical_table = sheet_table_id(self.conn, self.branch_id, sheet.sheet_id)
        select_columns = ", ".join(f'"{column["COLUMN_NAME"]}"' for column in columns)
        physical_rows = self.conn.execute(f'SELECT ROW_ID, {select_columns} FROM "{physical_table}"').fetchall()
        by_physical_id = {physical_row["ROW_ID"]: physical_row for physical_row in physical_rows}
        for excel_row, identity in sheet._row_identity.items():
            physical = by_physical_id.get(identity["physical_row_id"])
            if physical is None:
                continue
            for column in columns:
                excel_col = column["COLUMN_POSITION"] + 1
                sheet._cells[(excel_row, excel_col)] = physical[column["COLUMN_NAME"]]

    def default_sheet(self) -> VirtualSheet:
        active = [s for s in self.sheets if not s.deleted]
        if not active:
            raise InterpreterError("This branch has no worksheets")
        return active[0]

    # Bound globals -- what a bare, unqualified `Cells(...)`/`Rows`/`Columns`
    # in the macro implicitly means: the first active worksheet.
    def cells(self, row: Any, col: Any) -> VirtualRange:
        return self.default_sheet().cells(row, col)

    @property
    def rows(self) -> VirtualRows:
        return VirtualRows(self.default_sheet())

    @property
    def columns(self) -> VirtualColumns:
        return VirtualColumns(self.default_sheet())

    def worksheet(self, key: Any) -> VirtualSheet:
        if isinstance(key, (int, float)) and not isinstance(key, bool):
            index = int(key)
            active = [s for s in self.sheets if not s.deleted]
            if index < 1 or index > len(active):
                raise InterpreterError(f"Worksheet index {index} is out of range")
            return active[index - 1]
        name = str(key)
        for sheet in self.sheets:
            if not sheet.deleted and sheet.name.lower() == name.lower():
                return sheet
        raise InterpreterError(f"Worksheet '{name}' does not exist")

    def add_worksheet(self) -> VirtualSheet:
        position = len([s for s in self.sheets if not s.deleted])
        sheet = VirtualSheet(self, sheet_id=None, name=f"Sheet{len(self.sheets) + 1}", position=position, is_new=True)
        self.sheets.append(sheet)
        return sheet

    # ---- change collection ---------------------------------------------

    def pending_changes(self) -> list[dict[str, Any]]:
        """Everything the interpreter run touched, translated into the
        product's own SemanticChange shape -- the same one produced by a
        live Excel-taskpane edit. Order matters here only for readability;
        commit_semantic_delta re-sorts by its own operation priority."""
        changes: list[dict[str, Any]] = []
        for sheet in self.sheets:
            if sheet.is_new:
                changes.extend(self._new_sheet_changes(sheet))
            elif sheet.deleted:
                changes.append({"operation_type": "SHEET_DELETE", "sheet_id": sheet.sheet_id})
            else:
                if sheet._renamed_from is not None:
                    changes.append({
                        "operation_type": "SHEET_RENAME", "sheet_id": sheet.sheet_id,
                        "old_value": sheet._renamed_from, "new_value": sheet.name,
                    })
                changes.extend(self._existing_sheet_cell_changes(sheet))
        return changes

    def _existing_sheet_cell_changes(self, sheet: VirtualSheet) -> list[dict[str, Any]]:
        # Only cells the macro actually wrote (sheet._dirty) -- NOT every
        # loaded cell in sheet._cells, which is the full baseline plus any
        # writes merged together. Walking _cells directly would report
        # every untouched cell on the sheet as a "change" too.
        changes = []
        for (row, col) in sheet._dirty:
            new_value = sheet._cells.get((row, col))
            old_value = sheet._loaded_cells.get((row, col))
            if old_value == new_value:
                continue
            row_identity = sheet._row_identity.get(row)
            col_identity = sheet._col_identity.get(col)
            if not row_identity or not col_identity:
                continue  # writing beyond the sheet's current extent is out of scope on an existing sheet (see module docstring)
            changes.append({
                "operation_type": "CELL_VALUE_UPDATE", "sheet_id": sheet.sheet_id,
                "row_id": row_identity["row_id"], "column_id": col_identity["column_id"],
                "old_value": old_value, "new_value": new_value,
            })
        return changes

    def _new_sheet_changes(self, sheet: VirtualSheet) -> list[dict[str, Any]]:
        if not sheet._cells:
            return []  # an added-but-never-written sheet has nothing to commit
        from ..store.schema import _sanitize_column_name

        changes: list[dict[str, Any]] = [{
            "operation_type": "SHEET_CREATE", "sheet_id": _stable_id("SHEET"),
            "new_value": sheet.name, "new_row_position": sheet.position,
        }]
        sheet_id = changes[0]["sheet_id"]
        used_columns = sorted({col for (_row, col) in sheet._cells})
        column_ids: dict[int, str] = {}
        used_names: set[str] = set()
        for position, col in enumerate(used_columns):
            column_id = _stable_id("COL")
            column_ids[col] = column_id
            # Row 1 is the header row (matching every other sheet in this
            # product): whatever the macro wrote there becomes the real
            # column name, sanitized the same way an uploaded workbook's
            # headers are. A header-less column falls back to a synthetic
            # name so it's still usable.
            header_value = sheet._cells.get((1, col))
            base_name = _sanitize_column_name(str(header_value)) if header_value else f"COL_{col}"
            name = base_name
            suffix = 2
            while name in used_names:
                name = f"{base_name}_{suffix}"[:30]
                suffix += 1
            used_names.add(name)
            changes.append({
                "operation_type": "COLUMN_INSERT", "sheet_id": sheet_id, "column_id": column_id,
                "new_value": name, "new_column_position": position, "new_data_type": "TEXT",
            })
        used_rows = sorted({row for (row, _col) in sheet._cells if row >= 2})  # row 1 is the header; nothing to insert there
        for position, row in enumerate(used_rows):
            values = {column_ids[col]: value for (r, col), value in sheet._cells.items() if r == row}
            changes.append({
                "operation_type": "ROW_INSERT", "sheet_id": sheet_id,
                "new_value": values, "new_row_position": position,
            })
        return changes
