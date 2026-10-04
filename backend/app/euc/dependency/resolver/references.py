"""Typed interpretation of tokenized Excel reference operands."""

import re
from dataclasses import dataclass


CELL = re.compile(r"^(\$?)([A-Z]{1,3})(\$?)(\d+)$", re.I)
CELL_RANGE = re.compile(r"^(\$?[A-Z]{1,3}\$?\d+):(\$?[A-Z]{1,3}\$?\d+)$", re.I)
COLUMN_RANGE = re.compile(r"^(\$?[A-Z]{1,3}):(\$?[A-Z]{1,3})$", re.I)
ROW_RANGE = re.compile(r"^(\$?\d+):(\$?\d+)$")
EXTERNAL = re.compile(r"\[([^\]]+)\]")
STRUCTURED = re.compile(r"^(?:(?P<table>[A-Za-z_][\w.]*)\s*)?(?P<selectors>\[.+\])$")


@dataclass(frozen=True)
class FormulaReference:
    kind: str
    raw: str
    sheet_name: str | None = None
    workbook_name: str | None = None
    start: str | None = None
    end: str | None = None
    name: str | None = None
    table_name: str | None = None
    table_column: str | None = None
    absolute_column: bool = False
    absolute_row: bool = False


def _split_qualifier(raw: str) -> tuple[str | None, str | None, str]:
    if "!" not in raw:
        return None, None, raw
    qualifier, address = raw.rsplit("!", 1)
    qualifier = qualifier.strip("'").replace("''", "'")
    external = EXTERNAL.search(qualifier)
    workbook = external.group(1) if external else None
    sheet = EXTERNAL.sub("", qualifier) or None
    return workbook, sheet, address


def parse_reference(raw: str) -> FormulaReference:
    value = raw.strip()
    if "#REF!" in value.upper():
        return FormulaReference("BROKEN", raw)
    workbook, sheet, address = _split_qualifier(value)
    structured = STRUCTURED.match(address)
    if structured:
        selectors = structured.group("selectors")
        columns = re.findall(r"\[([^\]#@]+)\]", selectors)
        return FormulaReference(
            "TABLE_COLUMN", raw, sheet, workbook, table_name=structured.group("table"),
            table_column=columns[-1].strip() if columns else selectors.strip("[]@"),
        )
    match = CELL.match(address)
    if match:
        return FormulaReference("CELL", raw, sheet, workbook, start=address,
                                absolute_column=bool(match.group(1)), absolute_row=bool(match.group(3)))
    match = CELL_RANGE.match(address)
    if match:
        return FormulaReference("RANGE", raw, sheet, workbook, start=match.group(1), end=match.group(2))
    match = COLUMN_RANGE.match(address)
    if match:
        return FormulaReference("COLUMN_RANGE", raw, sheet, workbook, start=match.group(1), end=match.group(2))
    match = ROW_RANGE.match(address)
    if match:
        return FormulaReference("ROW_RANGE", raw, sheet, workbook, start=match.group(1), end=match.group(2))
    if workbook:
        return FormulaReference("EXTERNAL", raw, sheet, workbook, name=address)
    return FormulaReference("NAMED_RANGE", raw, sheet, name=address)
