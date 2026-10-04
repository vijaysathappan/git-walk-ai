"""
Local Workbook Sanitizer Service — Reverts Closed Workbooks to Protected ~9KB State.

Features:
  1. Detects when Excel has closed a working copy workbook (file lock released).
  2. Strips data rows while preserving headers, sheet names, defined names, and taskpane auto-open XML.
  3. Reverts file size from ~26KB back to ~9KB at rest on disk.
  4. Prevents data leakage when files are transported, emailed, or moved.
  5. Continuous background watcher automatically sanitizes closed workbooks.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.styles import Font

from ..config import settings
from ..database import _get_connection
from ..excel.clipboard_dlp_macro import MODULE_NAME as DLP_MODULE_NAME, MODULE_SOURCE as DLP_MODULE_SOURCE
from ..excel.vba_writer import embed_vba_project
from ..observability import record_audit_event
from ..openxml_injector import inject_taskpane_manifest

logger = logging.getLogger("gitwalk.local_sanitizer")

_watcher_thread: threading.Thread | None = None
_watcher_stop_event = threading.Event()


def is_file_locked(file_path: Path | str) -> bool:
    """
    Check if a file is currently opened and locked by Excel on Windows.
    Returns True if locked, False if accessible or non-existent.
    """
    target = Path(file_path)
    if not target.exists():
        return False
    try:
        # On Windows, Excel acquires a sharing lock. Opening in r+b mode fails if Excel is running.
        with open(target, "r+b"):
            pass
        return False
    except (PermissionError, OSError):
        return True


def needs_sanitization(file_path: Path | str) -> bool:
    """Check if the workbook contains populated data rows beyond the header and locked notice."""
    target = Path(file_path)
    if not target.exists():
        return False
    try:
        wb = load_workbook(str(target), data_only=True, read_only=True)
        for name in wb.sheetnames:
            sheet = wb[name]
            if sheet.max_row and sheet.max_row > 2:
                wb.close()
                return True
            val = str(sheet.cell(row=2, column=1).value or "")
            if val and not val.startswith("[LOCKED]"):
                wb.close()
                return True
        wb.close()
        return False
    except Exception:
        return target.stat().st_size > 10752


def sanitize_local_workbook_file(
    file_path: str | Path,
    table_id: str | None = None,
    lock_notice: str = "[LOCKED] Verify Path & Sign In via Git Walk Taskpane to load data",
    force: bool = False,
) -> dict[str, Any]:
    """
    Sanitize an Excel workbook on disk by removing all populated data rows,
    leaving only headers and the locked placeholder, and reinjecting taskpane XML.
    Reverts the file size from ~26KB back to ~9KB.
    """
    target = Path(file_path)
    if not target.exists():
        return {"success": False, "reason": "File does not exist", "path": str(target)}

    if is_file_locked(target):
        return {"success": False, "reason": "File is currently locked by Excel", "path": str(target)}

    original_size = target.stat().st_size
    if not force and not needs_sanitization(target):
        return {
            "success": True,
            "already_sanitized": True,
            "original_size": original_size,
            "sanitized_size": original_size,
            "path": str(target),
        }

    temp_dir = Path(tempfile.mkdtemp(prefix="gitwalk_sanitizer_"))
    try:
        working_copy = temp_dir / "working.xlsx"
        injected_output = temp_dir / "injected.xlsx"
        final_output = temp_dir / "final.xlsm"
        shutil.copyfile(target, working_copy)

        wb = load_workbook(str(working_copy), data_only=False)
        for sheet in wb.worksheets:
            max_r = sheet.max_row
            max_c = sheet.max_column
            if max_r > 1:
                # Delete all data rows from row 2 onwards
                sheet.delete_rows(2, max_r - 1)
                # Place locked notice in cell A2
                cell = sheet.cell(row=2, column=1, value=lock_notice)
                cell.font = Font(italic=True, color="58A6FF")
            # Shrink tables to reference only row 1 and row 2
            if sheet.tables:
                for tbl in list(sheet.tables.values()):
                    tbl.ref = f"A1:{sheet.cell(row=2, column=max_c).coordinate}"

        wb.save(str(working_copy))

        # Re-extract embedded metadata defined names
        wb_meta = load_workbook(str(working_copy), data_only=False)
        metadata: dict[str, str] = {}
        for name_key, def_obj in wb_meta.defined_names.items():
            val = getattr(def_obj, "value", def_obj)
            if isinstance(val, str) and val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            clean_key = (
                name_key.lower()
                .replace("_gitwalk_", "")
                .replace("_excel_sqlite_sync_", "")
            )
            metadata[clean_key] = str(val)

        detected_table_id = table_id or metadata.get("table_id") or "QUEUE_BOARD"

        # Reinject taskpane auto-open XML & OpenXML definitions
        inject_taskpane_manifest(
            input_xlsx_path=str(working_copy),
            output_xlsx_path=str(injected_output),
            manifest_url=f"{settings.office_addin_url}/taskpane.html",
            table_id=detected_table_id,
            metadata=metadata,
        )

        # Re-embed the clipboard-DLP macro as the unconditional last step --
        # never rely on an intermediate step having implicitly preserved it.
        embed_vba_project(str(injected_output), str(final_output), DLP_MODULE_NAME, DLP_MODULE_SOURCE)

        sanitized_size = final_output.stat().st_size

        # Replace target file atomically
        shutil.copyfile(str(final_output), str(target))

        record_audit_event(
            "LOCAL_WORKBOOK_SANITIZED",
            actor_user_id="USR_SYSTEM",
            actor_type="SYSTEM",
            payload={
                "file_path": str(target),
                "original_size": original_size,
                "sanitized_size": sanitized_size,
                "table_id": detected_table_id,
            },
        )
        logger.info(
            "Sanitized %s: %d bytes -> %d bytes",
            target.name,
            original_size,
            sanitized_size,
        )
        return {
            "success": True,
            "original_size": original_size,
            "sanitized_size": sanitized_size,
            "path": str(target),
        }
    except Exception as exc:
        logger.error("Failed to sanitize workbook %s: %s", target, exc, exc_info=True)
        return {"success": False, "reason": str(exc), "path": str(target)}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def scan_and_sanitize_closed_workbooks() -> list[dict[str, Any]]:
    """
    Scan all active registered working copies and sanitize any that are closed
    and currently holding data on disk (>10.5 KB).
    """
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT W.WORKING_COPY_ID, W.LOCAL_FILE_PATH, B.DATA_TABLE_ID
            FROM WORKING_COPIES W
            JOIN BRANCHES B ON B.BRANCH_ID = W.BRANCH_ID
            WHERE W.STATUS = 'ACTIVE' AND W.LOCAL_FILE_PATH IS NOT NULL AND W.LOCAL_FILE_PATH != ''
            """
        ).fetchall()
    finally:
        conn.close()

    results = []
    for row in rows:
        local_path = row["LOCAL_FILE_PATH"]
        if not local_path:
            continue
        target = Path(local_path)
        if target.exists() and not is_file_locked(target):
            try:
                if needs_sanitization(target):
                    res = sanitize_local_workbook_file(target, table_id=row["DATA_TABLE_ID"], force=True)
                    if res.get("success") and not res.get("already_sanitized"):
                        results.append(res)
            except OSError:
                pass
    return results


def _watcher_loop(interval_seconds: float = 3.0) -> None:
    """Continuous background loop checking for closed workbooks to sanitize."""
    logger.info("Local workbook sanitizer watcher loop started (interval: %.1fs)", interval_seconds)
    while not _watcher_stop_event.is_set():
        try:
            scan_and_sanitize_closed_workbooks()
        except Exception as exc:
            logger.debug("Sanitizer watcher check error: %s", exc)
        _watcher_stop_event.wait(interval_seconds)


def start_local_sanitizer_watcher(interval_seconds: float = 3.0) -> None:
    """Start the background sanitizer watcher thread."""
    global _watcher_thread
    if _watcher_thread and _watcher_thread.is_alive():
        return
    _watcher_stop_event.clear()
    _watcher_thread = threading.Thread(
        target=_watcher_loop,
        args=(interval_seconds,),
        name="GitWalkLocalSanitizerWatcher",
        daemon=True,
    )
    _watcher_thread.start()
    logger.info("Local workbook sanitizer watcher initiated.")


def stop_local_sanitizer_watcher() -> None:
    """Stop the background sanitizer watcher thread."""
    _watcher_stop_event.set()
    if _watcher_thread and _watcher_thread.is_alive():
        _watcher_thread.join(timeout=2.0)
    logger.info("Local workbook sanitizer watcher stopped.")
