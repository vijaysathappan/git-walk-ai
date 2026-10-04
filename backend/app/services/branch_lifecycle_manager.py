"""
Enterprise Branch Lifecycle Manager — Post-Merge EUC Excel Cleanup & Branch DB Purging.

Handles:
  1. Tracing local EUC Excel working copy files on C: or D: drive.
  2. Safely deleting downloaded branch workbooks upon merge.
  3. Purging/marking deleted branch records and working copy leases in SQLite.
  4. Productized CLI and programmatic Python APIs.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..database import (
    _get_connection,
    get_system_setting,
)
from ..observability import record_audit_event

logger = logging.getLogger("gitwalk.branch_lifecycle")

DRIVE_REGEX = re.compile(r"^[cCdD]:[/\\]")

# Directories no caller-supplied download path may resolve into or under,
# regardless of drive letter -- checking the drive alone let any
# authenticated editor direct the server to write a workbook into, e.g.,
# another user's Startup folder or a web root (a path-traversal /
# arbitrary-write primitive). This is a denylist rather than an allowlist
# so the existing "pick any folder on C:/D:" feature keeps working for
# ordinary folders -- it only blocks the specific sensitive locations.
_PROTECTED_DIR_NAMES = (
    "windows", "program files", "program files (x86)", "programdata",
    "inetpub", "system volume information", "$recycle.bin",
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_storage_drive(path_str: str | Path) -> Path:
    """
    Validate that the specified path resides strictly on the C: or D: drive,
    and is not a protected system directory or another OS user's profile.

    Raises:
        ValueError: If path is not on C: or D: drive, or falls inside a
            denylisted system directory / a different user's home folder.
    """
    raw = str(path_str).strip()
    if not DRIVE_REGEX.match(raw):
        drive = Path(raw).drive.upper()
        if drive not in ("C:", "D:"):
            raise ValueError(
                f"Security Policy Violation: Local folder must reside on C: or D: drive. "
                f"Received path: '{path_str}'"
            )
    resolved = Path(raw).resolve()
    drive_letter = resolved.drive.upper()
    if drive_letter not in ("C:", "D:"):
        raise ValueError(
            f"Security Policy Violation: Resolved path must reside on C: or D: drive. "
            f"Resolved: '{resolved}'"
        )
    parts_lower = [part.lower() for part in resolved.parts]
    if any(name in parts_lower for name in _PROTECTED_DIR_NAMES):
        raise ValueError(
            f"Security Policy Violation: '{resolved}' is inside a protected system directory."
        )
    home = Path.home().resolve()
    users_root = home.parent
    try:
        relative_to_users = resolved.relative_to(users_root)
    except ValueError:
        relative_to_users = None
    if relative_to_users is not None and relative_to_users.parts:
        owning_profile = relative_to_users.parts[0]
        if owning_profile.lower() not in (home.name.lower(), "public"):
            raise ValueError(
                "Security Policy Violation: cannot save into another user's profile directory."
            )
    return resolved


_UNSAFE_DIR_CHARS = re.compile(r'[<>:"/\\|?*]')


def default_gitwalk_directory(repository_name: str) -> Path:
    """Where a workbook is saved when the caller hasn't configured (or
    overridden) a download directory: <home>/gitwalk/<repository>, e.g.
    C:\\Users\\<username>\\gitwalk\\<repo> on Windows. Every workbook a user
    gets lands in a predictable, per-repository folder instead of a
    browser Save-As dialog going to a generic Downloads folder."""
    safe_name = _UNSAFE_DIR_CHARS.sub("_", repository_name).strip(" .") or "repository"
    return Path.home() / "gitwalk" / safe_name


class BranchLifecycleManager:
    """Productized manager for branch workbook deletion and DB lifecycle state."""

    @classmethod
    def cleanup_merged_branch(
        cls,
        branch_id: str,
        local_dir: str | Path | None = None,
        actor_user_id: str = "USR_SYSTEM",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """
        Trace the local EUC Excel file for a merged branch, delete it from disk,
        and mark the branch as DELETED in the database so it no longer appears in UI.
        """
        conn = _get_connection()
        try:
            row = conn.execute(
                """
                SELECT BRANCH_ID, REPOSITORY_ID, DATA_TABLE_ID, BRANCH_NAME, BRANCH_TYPE,
                       STATUS, LOCAL_DOWNLOAD_PATH, CREATED_BY
                FROM BRANCHES
                WHERE BRANCH_ID = ?
                """,
                (branch_id,),
            ).fetchone()

            if not row:
                raise ValueError(f"Branch '{branch_id}' does not exist.")

            branch_name = row["BRANCH_NAME"]
            repo_id = row["REPOSITORY_ID"]
            recorded_path = row["LOCAL_DOWNLOAD_PATH"]
            current_status = row["STATUS"]

            logger.info(
                "Starting lifecycle cleanup for branch %s (%s), status=%s",
                branch_name, branch_id, current_status,
            )

            # Determine candidate file paths to trace
            candidate_paths: list[Path] = []
            if recorded_path:
                try:
                    candidate_paths.append(validate_storage_drive(recorded_path))
                except ValueError as err:
                    logger.warning("Recorded path failed drive validation: %s", err)

            # Also inspect local_dir or configured system setting
            target_dir = local_dir or get_system_setting("euc_download_dir")
            if target_dir:
                try:
                    valid_dir = validate_storage_drive(target_dir)
                    sanitized_name = branch_name.replace("/", "_").replace("\\", "_")
                    candidate_paths.append(valid_dir / f"gitwalk_{sanitized_name}.xlsm")
                    candidate_paths.append(valid_dir / f"gitwalk_{branch_id}.xlsm")
                    candidate_paths.append(valid_dir / f"{sanitized_name}.xlsm")
                    candidate_paths.append(valid_dir / f"gitwalk_{sanitized_name}.xlsx")
                    candidate_paths.append(valid_dir / f"gitwalk_{branch_id}.xlsx")
                    candidate_paths.append(valid_dir / f"{sanitized_name}.xlsx")
                except ValueError as err:
                    logger.warning("Target directory failed drive validation: %s", err)

            # Trace and remove matching files
            deleted_paths: list[str] = []
            file_locked: bool = False
            lock_warning: str | None = None

            for file_candidate in candidate_paths:
                if file_candidate.exists() and file_candidate.is_file():
                    logger.info("Found branch EUC Excel file at %s", file_candidate)
                    if dry_run:
                        deleted_paths.append(str(file_candidate))
                        logger.info("[DRY RUN] Would delete %s", file_candidate)
                        continue

                    try:
                        file_candidate.unlink()
                        deleted_paths.append(str(file_candidate))
                        logger.info("Successfully deleted local EUC file: %s", file_candidate)
                    except PermissionError as exc:
                        file_locked = True
                        lock_warning = (
                            f"File {file_candidate.name} is currently open in Excel or another program. "
                            f"Please close Excel to complete file removal."
                        )
                        logger.warning(
                            "Permission error deleting file %s (likely locked by Excel): %s",
                            file_candidate, exc,
                        )
                    except OSError as exc:
                        logger.error("OS error deleting file %s: %s", file_candidate, exc)

            # Update database status to DELETED
            now = _utcnow()
            if not dry_run:
                conn.execute(
                    """
                    UPDATE BRANCHES
                    SET STATUS = 'DELETED', ARCHIVED_AT = ?, UPDATED_AT = ?
                    WHERE BRANCH_ID = ?
                    """,
                    (now, now, branch_id),
                )
                conn.execute(
                    """
                    UPDATE WORKING_COPIES
                    SET STATUS = 'REVOKED', LAST_SEEN_AT = ?
                    WHERE BRANCH_ID = ? AND STATUS = 'ACTIVE'
                    """,
                    (now, branch_id),
                )
                conn.commit()

                record_audit_event(
                    "BRANCH_DELETED_POST_MERGE",
                    actor_user_id=actor_user_id,
                    repository_id=repo_id,
                    branch_id=branch_id,
                    payload={
                        "branch_name": branch_name,
                        "deleted_files": deleted_paths,
                        "file_locked": file_locked,
                        "lock_warning": lock_warning,
                    },
                )

            return {
                "branch_id": branch_id,
                "branch_name": branch_name,
                "previous_status": current_status,
                "new_status": "DELETED",
                "file_deleted": len(deleted_paths) > 0,
                "deleted_files": deleted_paths,
                "file_locked": file_locked,
                "lock_warning": lock_warning,
                "dry_run": dry_run,
                "message": (
                    f"Branch '{branch_name}' deleted from database. "
                    f"Local files deleted: {len(deleted_paths)}."
                    + (f" Note: {lock_warning}" if lock_warning else "")
                ),
            }
        finally:
            conn.close()

    @classmethod
    def cleanup_all_merged(
        cls,
        local_dir: str | Path | None = None,
        actor_user_id: str = "USR_SYSTEM",
        dry_run: bool = False,
    ) -> list[dict[str, Any]]:
        """Scan and clean up all branches marked as MERGED in the database."""
        conn = _get_connection()
        try:
            rows = conn.execute(
                "SELECT BRANCH_ID FROM BRANCHES WHERE STATUS = 'MERGED' ORDER BY UPDATED_AT ASC"
            ).fetchall()
            branch_ids = [row["BRANCH_ID"] for row in rows]
        finally:
            conn.close()

        results = []
        for b_id in branch_ids:
            try:
                res = cls.cleanup_merged_branch(
                    branch_id=b_id,
                    local_dir=local_dir,
                    actor_user_id=actor_user_id,
                    dry_run=dry_run,
                )
                results.append(res)
            except Exception as exc:
                logger.error("Failed cleaning up merged branch %s: %s", b_id, exc)
                results.append({"branch_id": b_id, "error": str(exc)})
        return results


def main() -> None:
    """CLI Entry Point."""
    parser = argparse.ArgumentParser(
        description="Git Walk EUC Branch Lifecycle Manager — Post-Merge File & DB Purging"
    )
    parser.add_argument(
        "--branch-id",
        type=str,
        help="Specific branch ID to trace and delete.",
    )
    parser.add_argument(
        "--all-merged",
        action="store_true",
        help="Scan and delete all merged branches and their local EUC files.",
    )
    parser.add_argument(
        "--local-dir",
        type=str,
        default=None,
        help="Local download folder on C: or D: drive (overrides system setting).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate cleanup without deleting files or modifying database.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON results.",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not args.branch_id and not args.all_merged:
        parser.error("Specify either --branch-id <ID> or --all-merged.")

    if args.branch_id:
        result = BranchLifecycleManager.cleanup_merged_branch(
            branch_id=args.branch_id,
            local_dir=args.local_dir,
            dry_run=args.dry_run,
        )
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(result["message"])
    elif args.all_merged:
        results = BranchLifecycleManager.cleanup_all_merged(
            local_dir=args.local_dir,
            dry_run=args.dry_run,
        )
        if args.json:
            print(json.dumps(results, indent=2))
        else:
            print(f"Processed {len(results)} merged branch(es).")
            for item in results:
                print(f" - {item.get('branch_name', item.get('branch_id'))}: {item.get('message', item.get('error'))}")


if __name__ == "__main__":
    main()
