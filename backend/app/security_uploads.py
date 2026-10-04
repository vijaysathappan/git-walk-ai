"""Bounded validation for untrusted XLSX uploads."""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any

from .config import settings


XLSX_MIME_TYPES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroenabled.12",  # lowercase -- compared against content_type.lower()
    "application/octet-stream",
    "application/zip",
}
REQUIRED_PARTS = {"[Content_Types].xml", "xl/workbook.xml", "_rels/.rels"}


class UnsafeWorkbookError(ValueError):
    pass


def safe_filename(filename: str) -> str:
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return (name or "workbook.xlsx")[:180]


def inspect_xlsx(contents: bytes, filename: str, content_type: str | None) -> dict[str, Any]:
    if not filename.lower().endswith((".xlsx", ".xlsm")):
        raise UnsafeWorkbookError("Only .xlsx and .xlsm files are supported")
    if content_type and content_type.lower() not in XLSX_MIME_TYPES:
        raise UnsafeWorkbookError("The upload MIME type is not an XLSX/XLSM workbook")
    if not contents:
        raise UnsafeWorkbookError("The uploaded workbook is empty")
    if len(contents) > settings.max_upload_bytes:
        raise UnsafeWorkbookError(
            f"Workbook exceeds the {settings.max_upload_bytes // (1024 * 1024)} MB upload limit"
        )
    if not contents.startswith(b"PK\x03\x04"):
        raise UnsafeWorkbookError("The file signature is not a valid XLSX ZIP package")

    try:
        with zipfile.ZipFile(BytesIO(contents)) as archive:
            entries = archive.infolist()
            names = {item.filename for item in entries}
            if not REQUIRED_PARTS.issubset(names):
                raise UnsafeWorkbookError("The XLSX package is missing required OpenXML parts")
            if len(entries) > settings.max_xlsx_entries:
                raise UnsafeWorkbookError("The XLSX package contains too many ZIP entries")
            expanded = sum(item.file_size for item in entries)
            if expanded > settings.max_xlsx_uncompressed_bytes:
                raise UnsafeWorkbookError("The expanded XLSX package exceeds the safety limit")
            for item in entries:
                path = PurePosixPath(item.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise UnsafeWorkbookError("The XLSX package contains an unsafe path")
                if item.compress_size and item.file_size / item.compress_size > 500:
                    raise UnsafeWorkbookError("The XLSX package contains a suspicious compression ratio")
    except zipfile.BadZipFile as exc:
        raise UnsafeWorkbookError("The upload is not a valid ZIP-based XLSX workbook") from exc

    return {
        "safe_filename": safe_filename(filename),
        "upload_bytes": len(contents),
        "zip_entries": len(entries),
        "uncompressed_bytes": expanded,
    }


def validate_workbook_dimensions(sheet_names: list[str], rows: int, columns: int) -> None:
    if len(sheet_names) > settings.max_workbook_sheets:
        raise UnsafeWorkbookError("Workbook exceeds the configured sheet limit")
    if rows > settings.max_workbook_rows:
        raise UnsafeWorkbookError("Workbook exceeds the configured row limit")
    if columns > settings.max_workbook_columns:
        raise UnsafeWorkbookError("Workbook exceeds the configured column limit")


def worksheet_data_dimensions(sheet: Any) -> tuple[int, int]:
    """Return data rows and columns even when XLSX dimension metadata is absent."""
    if sheet.max_row is None or sheet.max_column is None:
        try:
            sheet.calculate_dimension(force=True)
        except TypeError:
            # Normal worksheets do not accept ``force``; read-only sheets do.
            sheet.calculate_dimension()

    max_row = int(sheet.max_row or 0)
    max_column = int(sheet.max_column or 0)
    return max(0, max_row - 1), max_column
