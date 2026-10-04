"""Repository permanent-deletion pipeline.

Covers two entry points the product needs:

  1. Manual: an owner deletes their repository (WORKBOOK_REPOSITORIES via
     dataset_store.delete_repository). This now performs the full pipeline
     immediately (export -> email -> notify -> hard delete) rather than the
     old soft-delete-and-leave-it-forever behavior.
  2. Automatic: a daily sweep (start_repository_purge_watcher, wired into
     main.py's lifespan) finds repositories that are either already
     soft-deleted or inactive for REPOSITORY_PURGE_INACTIVITY_DAYS (no
     commit in that window, or never committed and created that long ago)
     and runs the same pipeline on each.

Both paths funnel through purge_repository(), so the safety behavior
(export first, respect legal holds, notify the owner, audit the action) is
identical regardless of who or what triggered it.

Design decisions worth being explicit about, since this is a destructive,
irreversible operation:

  - LEGAL_HOLDS is a hard blocker for BOTH paths. An active hold means the
    repository cannot be purged at all — the caller gets a clear error
    instead of a silent no-op or a silent bypass.
  - RETENTION_POLICIES (if an explicit row exists for the repository) sets
    a minimum age floor for the AUTOMATIC sweep only — an explicitly
    configured longer retention period overrides the 30-day inactivity
    default. A repository with no explicit retention-policy row is not
    held to the table's schema default (1095 days); that column default
    is for policy rows that opt in, not an implicit floor on every repo.
    The manual/owner path is unaffected by retention policy — it's the
    owner's own explicit, deliberate action, same as deleting your own
    account.
  - AUDIT_EVENTS is never touched. It has DB-level triggers that make it
    genuinely append-only (see schema.py), and the whole point of an audit
    trail is that it survives the deletion of what it's about — the
    REPOSITORY_PURGED event (and every prior event for that repository)
    stays queryable by REPOSITORY_ID after the repository itself is gone.
  - MERGE_RESOLUTION_KNOWLEDGE is deliberately excluded from the cascade
    delete even though it carries REPOSITORY_ID on HISTORICAL rows. It's a
    shared, cross-repository learning corpus (see ai/merge_knowledge.py) —
    deleting one repository's contributed precedents would silently
    degrade AI merge-conflict guidance for every OTHER repository too,
    which is not something deleting your own repository should do.
  - Content-addressed storage objects (STORAGE_OBJECTS/OBJECT_REFERENCES)
    are never deleted directly, because they're deduplicated across
    commits and repositories (copy-on-write). Removing a repository's
    COMMITS/COMMIT_MANIFESTS rows (the "roots" that keep objects
    reachable) and then running the existing mark-and-sweep GC
    (SemanticLedgerService.collect_garbage) is the correct, already-built
    mechanism for reclaiming now-orphaned objects.
"""

from __future__ import annotations

import json
import logging
import smtplib
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from .. import database
from ..config import settings
from ..observability import record_audit_event
from ..store.identifiers import SafeIdentifier
from .semantic_ledger_service import ledger_for_connection

logger = logging.getLogger("gitwalk.repository_purge")

# Tables carrying REPOSITORY_ID that hold real repository content — deleted
# as part of every purge. Deliberately excludes AUDIT_EVENTS (immutable,
# see module docstring) and MERGE_RESOLUTION_KNOWLEDGE (shared corpus, see
# module docstring). WORKBOOK_REPOSITORIES is last since every other row
# logically belongs to it.
_REPOSITORY_ID_TABLES: tuple[str, ...] = (
    "BRANCH_CHECKPOINTS", "BRANCH_PROTECTION_RULES", "COMMIT_AI_REVIEWS",
    "COMMIT_CHANGES", "COMMIT_MANIFESTS", "COMMITS", "EUC_BRANCH_COMPARISONS",
    "EUC_ASSETS", "LEGAL_HOLDS", "MERGE_REQUESTS", "OPERATION_METRICS",
    "RETENTION_POLICIES", "TASKS", "VALIDATION_RUNS", "WORKBOOK_SHEETS",
    "WORKING_COPIES", "REPOSITORY_MEMBERS", "BRANCHES", "WORKBOOK_REPOSITORIES",
)

# Legacy, table-centric-era tables keyed by TABLE_ID rather than
# REPOSITORY_ID — same repository, older schema generation.
_TABLE_ID_TABLES: tuple[str, ...] = (
    "AUDIT_COMMITS", "DATASET_INVITATIONS", "DATASET_MEMBERS",
    "DATASET_PRESENCE", "DATASET_VERSIONS", "WORKSPACE_EVENTS",
    "DATASET_REGISTRY",
)

# Second- and third-order children — rows that belong to this repository but
# aren't tagged with REPOSITORY_ID directly (e.g. a merge conflict is keyed
# by MERGE_REQUEST_ID, an EUC finding by FINDING_ID which itself traces back
# to EUC_ID). The EUC intelligence/migration subtree alone is 4 levels deep
# (EUC_ASSETS -> *_RUNS -> findings/blocks/components -> occurrences/actions)
# with ~35 tables — hand-enumerating that exhaustively risks silently
# missing a level every time a new one is added. Instead this is a bounded
# breadth-first closure: starting from the repository's own id, it follows
# ONLY these explicit column names to discover every transitively-linked
# row, however many levels deep, and is safe against pulling in unrelated
# data because propagation never follows a shared/identity column (user_id,
# created_by, actor_user_id, etc. are deliberately not in this list) — only
# columns that are exclusively resource-hierarchy identifiers.
_CLOSURE_PROPAGATION_KEYS: tuple[str, ...] = (
    "BRANCH_ID", "COMMIT_ID", "MERGE_REQUEST_ID", "CONFLICT_ID", "WORKING_COPY_ID",
    "EUC_ID", "ANALYSIS_RUN_ID", "DEPENDENCY_RUN_ID", "INTELLIGENCE_RUN_ID",
    "MIGRATION_RUN_ID", "APPLICATION_MODEL_ID", "FINDING_ID", "SHEET_ID", "DATA_TABLE_ID",
)
_CLOSURE_EXCLUDED_TABLES = frozenset({
    "AUDIT_EVENTS", "MERGE_RESOLUTION_KNOWLEDGE", "MERGE_RESOLUTION_KNOWLEDGE_FTS",
    *_REPOSITORY_ID_TABLES, *_TABLE_ID_TABLES,
})


def _table_columns(conn) -> dict[str, list[str]]:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return {t: [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')] for t in tables}


def _discover_repository_closure(conn, repository_id: str) -> tuple[dict[str, set[str]], dict[str, list[dict[str, Any]]]]:
    """Breadth-first fixed-point closure — see the module-level comment
    above _CLOSURE_PROPAGATION_KEYS for why this exists instead of a flat
    hardcoded list. Read-only; returns (known_id_values_by_column,
    matched_rows_by_table) for both the export and the delete pass to
    share, so they can never disagree about what "everything" means."""
    schema_columns = _table_columns(conn)
    known: dict[str, set[str]] = {"REPOSITORY_ID": {repository_id}}
    exported: dict[str, list[dict[str, Any]]] = {}
    all_keys = (*_CLOSURE_PROPAGATION_KEYS, "REPOSITORY_ID")

    # Seed: the closure loop below deliberately never re-matches
    # _REPOSITORY_ID_TABLES (they're deleted separately, in explicit FK-safe
    # order) — but their OWN id columns (MERGE_REQUESTS.MERGE_REQUEST_ID,
    # BRANCHES.BRANCH_ID, COMMITS.COMMIT_ID, EUC_ASSETS.EUC_ID, etc.) are
    # exactly what the deeper child tables (MERGE_CONFLICTS, EUC_*_RUNS...)
    # are keyed by. Without this seeding step `known` would never contain a
    # single MERGE_REQUEST_ID/EUC_ID/etc. value and the whole closure would
    # silently discover nothing beyond the top level.
    for table in _REPOSITORY_ID_TABLES:
        cols = schema_columns.get(table, [])
        if "REPOSITORY_ID" not in cols:
            continue
        id_cols = [key for key in _CLOSURE_PROPAGATION_KEYS if key in cols]
        if not id_cols:
            continue
        for row in conn.execute(f'SELECT * FROM "{table}" WHERE REPOSITORY_ID=?', (repository_id,)):
            row = _row(row)
            for key in id_cols:
                value = row.get(key.lower())
                if value is not None:
                    known.setdefault(key, set()).add(str(value))

    eligible = {
        table: cols for table, cols in schema_columns.items()
        if table not in _CLOSURE_EXCLUDED_TABLES and any(key in cols for key in all_keys)
    }
    for _pass in range(12):  # bounded: this schema is at most ~4-5 levels deep
        changed = False
        for table, cols in eligible.items():
            match_cols = [key for key in all_keys if key in cols and known.get(key)]
            if not match_cols:
                continue
            clause = " OR ".join(f'"{col}" IN ({",".join("?" for _ in known[col])})' for col in match_cols)
            params = [value for col in match_cols for value in known[col]]
            rows = [_row(row) for row in conn.execute(f'SELECT * FROM "{table}" WHERE {clause}', params)]
            if not rows:
                continue
            if len(rows) != len(exported.get(table, [])):
                changed = True
            exported[table] = rows
            for key in _CLOSURE_PROPAGATION_KEYS:
                if key in cols:
                    values = {str(row[key.lower()]) for row in rows if row.get(key.lower()) is not None}
                    before = len(known.get(key, set()))
                    known.setdefault(key, set()).update(values)
                    if len(known[key]) != before:
                        changed = True
        if not changed:
            break
    return known, exported


def _delete_repository_closure(conn, known: dict[str, set[str]], exported: dict[str, list[dict[str, Any]]]) -> None:
    """Deletes every row _discover_repository_closure found. A handful of
    these tables have real declared FK constraints among themselves (e.g.
    MERGE_CONFLICT_AI_SUGGESTIONS -> MERGE_CONFLICTS) — rather than
    hand-deriving that ordering, this retries in rounds and lets SQLite's
    own constraint check tell it what still has dependents; each round
    must make progress or something is genuinely wrong."""
    all_keys = (*_CLOSURE_PROPAGATION_KEYS, "REPOSITORY_ID")
    schema_columns = _table_columns(conn)
    pending = set(exported.keys())
    for _pass in range(len(pending) + 1):
        if not pending:
            return
        progressed = set()
        for table in list(pending):
            cols = schema_columns[table]
            match_cols = [key for key in all_keys if key in cols and known.get(key)]
            if not match_cols:
                progressed.add(table)  # nothing to actually delete by; drop it
                continue
            clause = " OR ".join(f'"{col}" IN ({",".join("?" for _ in known[col])})' for col in match_cols)
            params = [value for col in match_cols for value in known[col]]
            try:
                conn.execute(f'DELETE FROM "{table}" WHERE {clause}', params)
                progressed.add(table)
            except sqlite3.IntegrityError:
                continue  # still referenced by an undeleted child — retry next round
        if not progressed:
            raise RuntimeError(
                f"Repository purge could not delete tables {sorted(pending)} — "
                "unresolvable foreign-key dependency."
            )
        pending -= progressed


class LegalHoldBlocksDeletion(PermissionError):
    """Raised when a repository cannot be purged because of an active
    LEGAL_HOLDS row — never silently bypassed."""


def _row(row) -> dict[str, Any]:
    return {key.lower(): row[key] for key in row.keys()}


def _has_active_legal_hold(conn, repository_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM LEGAL_HOLDS WHERE REPOSITORY_ID=? AND STATUS='ACTIVE'", (repository_id,)
    ).fetchone() is not None


def _repository_owners(conn, repository_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT U.USER_ID, U.EMAIL, U.DISPLAY_NAME FROM REPOSITORY_MEMBERS M
           JOIN APP_USERS U ON U.USER_ID=M.USER_ID
           WHERE M.REPOSITORY_ID=? AND M.ROLE='owner'""",
        (repository_id,),
    ).fetchall()
    return [_row(row) for row in rows]


def _physical_table_names(conn, repository_id: str, table_id: str) -> list[str]:
    names = {table_id}
    for row in conn.execute(
        """SELECT DISTINCT T.DATA_TABLE_ID FROM BRANCH_SHEET_TABLES T
           JOIN BRANCHES B ON B.BRANCH_ID=T.BRANCH_ID WHERE B.REPOSITORY_ID=?""",
        (repository_id,),
    ):
        names.add(row[0])
    return sorted(names)


def export_repository_traces(
    conn, repository_id: str,
    closure: tuple[dict[str, set[str]], dict[str, list[dict[str, Any]]]] | None = None,
) -> dict[str, Any]:
    """Everything this repository ever held, as a plain JSON-able dict —
    the safety net captured before anything is deleted. `closure` lets
    purge_repository() share one discovery pass between the export and the
    delete, so they can never disagree about what "everything" means;
    left None, a standalone export computes its own (read-only either way)."""
    repository_row = conn.execute(
        "SELECT * FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (repository_id,)
    ).fetchone()
    if not repository_row:
        raise KeyError(f"Repository {repository_id} does not exist")
    repository = _row(repository_row)
    table_id = repository["table_id"]

    export: dict[str, Any] = {
        "exported_at": database._utcnow(),
        "repository": repository,
        "tables": {},
        "physical_data": {},
    }

    for table in (*_REPOSITORY_ID_TABLES, "AUDIT_EVENTS"):
        rows = conn.execute(f"SELECT * FROM {table} WHERE REPOSITORY_ID=?", (repository_id,)).fetchall()
        export["tables"][table] = [_row(row) for row in rows]

    for table in _TABLE_ID_TABLES:
        rows = conn.execute(f"SELECT * FROM {table} WHERE TABLE_ID=?", (table_id,)).fetchall()
        export["tables"][table] = [_row(row) for row in rows]

    # Deeper children not tagged with REPOSITORY_ID directly — merge
    # conflicts, EUC analysis/dependency/intelligence/migration runs and
    # everything under them, etc. — see _discover_repository_closure.
    _known, closure_rows = closure if closure is not None else _discover_repository_closure(conn, repository_id)
    export["tables"].update(closure_rows)

    limit = settings.repository_purge_max_physical_rows_per_table
    for physical_table in _physical_table_names(conn, repository_id, table_id):
        safe_name = SafeIdentifier(physical_table)
        try:
            columns = [row["name"] for row in conn.execute(f'PRAGMA table_info("{safe_name}")')]
            if not columns:
                continue
            total = conn.execute(f'SELECT COUNT(*) FROM "{safe_name}"').fetchone()[0]
            rows = conn.execute(f'SELECT * FROM "{safe_name}" LIMIT ?', (limit,)).fetchall()
            export["physical_data"][physical_table] = {
                "columns": columns,
                "total_rows": total,
                "exported_rows": len(rows),
                "truncated": total > limit,
                "rows": [list(row) for row in rows],
            }
        except Exception as exc:  # a missing/already-gone physical table must not abort the export
            export["physical_data"][physical_table] = {"error": str(exc)}

    return export


def _write_export_file(repository: dict[str, Any], export: dict[str, Any]) -> Path:
    export_dir = Path(settings.repository_purge_export_dir) if settings.repository_purge_export_dir else (
        Path(__file__).resolve().parent.parent.parent / "repository_exports"
    )
    export_dir.mkdir(parents=True, exist_ok=True)
    slug = (repository.get("repository_slug") or repository["repository_id"]).replace("/", "_")
    filename = f"{slug}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    path = export_dir / filename
    path.write_text(json.dumps(export, default=str, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _send_export_email(owner_emails: list[str], repository_name: str, export_path: Path, reason: str) -> bool:
    """Best-effort: returns True only if the email was actually sent.
    Never raises — a delivery failure must not block the deletion the
    owner already consented to (or that the retention sweep determined is
    due); the export file on disk and the in-app notification are the
    fallback record either way."""
    if not (settings.smtp_user and settings.smtp_pass and settings.smtp_from):
        logger.warning(
            "Repository purge export for %s written to %s but not emailed — SMTP is not configured.",
            repository_name, export_path,
        )
        return False
    if not owner_emails:
        logger.warning("Repository purge export for %s has no owner email to send to.", repository_name)
        return False
    try:
        message = EmailMessage()
        message["Subject"] = f"Git Walk: '{repository_name}' has been permanently deleted"
        message["From"] = settings.smtp_from
        message["To"] = ", ".join(owner_emails)
        message.set_content(
            f"Repository '{repository_name}' was permanently deleted ({reason}).\n\n"
            "A complete export of every record this repository held — commits, branches, "
            "merge requests, working copies, and worksheet data — is attached as JSON. "
            "This is the only remaining copy; the repository itself has been removed from Git Walk."
        )
        message.add_attachment(
            export_path.read_bytes(), maintype="application", subtype="json", filename=export_path.name,
        )
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_pass)
            smtp.send_message(message)
        return True
    except Exception:
        logger.exception("Failed to email repository purge export for %s", repository_name)
        return False


def purge_repository(repository_id: str, actor_user_id: str, reason: str) -> dict[str, Any]:
    """Export, email, notify, then permanently delete. Raises
    LegalHoldBlocksDeletion (never proceeds silently) if the repository is
    under an active legal hold."""
    conn = database._get_connection()
    try:
        if _has_active_legal_hold(conn, repository_id):
            raise LegalHoldBlocksDeletion(
                "This repository is under an active legal hold and cannot be deleted."
            )
        repository_row = conn.execute(
            "SELECT * FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (repository_id,)
        ).fetchone()
        if not repository_row:
            raise KeyError(f"Repository {repository_id} does not exist")
        repository = _row(repository_row)
        table_id = repository["table_id"]
        owners = _repository_owners(conn, repository_id)
        organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (repository_id,)
        ).fetchone()
        organization_id = organization_id[0] if organization_id else None

        closure = _discover_repository_closure(conn, repository_id)
        export = export_repository_traces(conn, repository_id, closure=closure)
        export_path = _write_export_file(repository, export)
        owner_emails = [owner["email"] for owner in owners if owner.get("email")]
        emailed = _send_export_email(owner_emails, repository["repository_name"], export_path, reason)

        record_audit_event(
            "REPOSITORY_PURGED", actor_user_id=actor_user_id, repository_id=repository_id,
            payload={
                "repository_name": repository["repository_name"], "reason": reason,
                "export_file": str(export_path), "export_emailed": emailed,
                "owner_emails": owner_emails, "table_id": table_id,
            },
        )

        for owner in owners:
            try:
                database.create_notification(
                    owner["user_id"], "REPOSITORY_PURGED",
                    f"Repository '{repository['repository_name']}' was permanently deleted",
                    (
                        f"Reason: {reason}. A full JSON export of everything it held was "
                        f"{'emailed to you' if emailed else 'saved on the server (email delivery was not configured or failed)'}."
                    ),
                    resource_type="REPOSITORY", resource_id=repository_id, organization_id=organization_id,
                )
            except Exception:
                logger.exception("Failed to create purge notification for owner %s", owner["user_id"])

        physical_tables = _physical_table_names(conn, repository_id, table_id)
        conn.execute("BEGIN IMMEDIATE")
        for physical_table in physical_tables:
            conn.execute(f'DROP TABLE IF EXISTS "{SafeIdentifier(physical_table)}"')
        # Deeper children first (merge conflicts, EUC run subtree, etc.) so
        # the hardcoded top-level deletes below never hit a still-referenced
        # row — see _delete_repository_closure's docstring for the retry
        # strategy used instead of a hand-derived deletion order.
        known, closure_rows = closure
        _delete_repository_closure(conn, known, closure_rows)
        for table in _REPOSITORY_ID_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE REPOSITORY_ID=?", (repository_id,))
        for table in _TABLE_ID_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE TABLE_ID=?", (table_id,))
        conn.commit()

        gc_result = ledger_for_connection(conn).collect_garbage(conn, dry_run=False)

        return {
            "repository_id": repository_id, "repository_name": repository["repository_name"],
            "status": "PURGED", "reason": reason, "export_file": str(export_path),
            "export_emailed": emailed, "owner_emails": owner_emails,
            "physical_tables_dropped": physical_tables, "gc": gc_result,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _inactivity_cutoff_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def find_purge_candidates(inactivity_days: int | None = None) -> list[dict[str, Any]]:
    """Repositories eligible for the automatic sweep: already soft-deleted
    (STATUS='DELETED', e.g. left over from before this pipeline existed),
    or ACTIVE with no commit in `inactivity_days` (falling back to
    repository creation date for repositories that never had a commit).
    Excludes anything under an active legal hold, and applies an explicit
    RETENTION_POLICIES floor when one is configured for that repository —
    see the module docstring for why.
    """
    days = inactivity_days if inactivity_days is not None else settings.repository_purge_inactivity_days
    cutoff = _inactivity_cutoff_iso(days)
    conn = database._get_connection()
    try:
        deleted = conn.execute(
            """SELECT R.REPOSITORY_ID, R.REPOSITORY_NAME FROM WORKBOOK_REPOSITORIES R
               WHERE R.STATUS='DELETED' AND NOT EXISTS (
                   SELECT 1 FROM LEGAL_HOLDS L WHERE L.REPOSITORY_ID=R.REPOSITORY_ID AND L.STATUS='ACTIVE'
               )"""
        ).fetchall()
        candidates = [{"repository_id": row[0], "repository_name": row[1], "reason": "PREVIOUSLY_SOFT_DELETED"} for row in deleted]

        inactive = conn.execute(
            """SELECT R.REPOSITORY_ID, R.REPOSITORY_NAME, R.CREATED_AT,
                      (SELECT MAX(C.CREATED_AT) FROM COMMITS C WHERE C.REPOSITORY_ID=R.REPOSITORY_ID) AS LAST_COMMIT_AT,
                      RP.RETAIN_DAYS
               FROM WORKBOOK_REPOSITORIES R
               LEFT JOIN RETENTION_POLICIES RP ON RP.REPOSITORY_ID=R.REPOSITORY_ID
               WHERE R.STATUS='ACTIVE' AND NOT EXISTS (
                   SELECT 1 FROM LEGAL_HOLDS L WHERE L.REPOSITORY_ID=R.REPOSITORY_ID AND L.STATUS='ACTIVE'
               )"""
        ).fetchall()
        now = datetime.now(timezone.utc)
        for row in inactive:
            last_activity = row["LAST_COMMIT_AT"] or row["CREATED_AT"]
            try:
                last_activity_dt = datetime.fromisoformat(last_activity)
            except (TypeError, ValueError):
                continue
            if last_activity_dt.tzinfo is None:
                last_activity_dt = last_activity_dt.replace(tzinfo=timezone.utc)
            age_days = (now - last_activity_dt).total_seconds() / 86400
            if age_days < days:
                continue
            if row["RETAIN_DAYS"] and age_days < row["RETAIN_DAYS"]:
                continue
            candidates.append({
                "repository_id": row["REPOSITORY_ID"], "repository_name": row["REPOSITORY_NAME"],
                "reason": f"INACTIVE_{days}_DAYS",
            })
        return candidates
    finally:
        conn.close()


def sweep_repositories_for_deletion(dry_run: bool = False, inactivity_days: int | None = None) -> list[dict[str, Any]]:
    candidates = find_purge_candidates(inactivity_days)
    results = []
    for candidate in candidates:
        if dry_run:
            results.append({**candidate, "status": "WOULD_PURGE", "dry_run": True})
            continue
        try:
            results.append(purge_repository(candidate["repository_id"], "USR_SYSTEM", candidate["reason"]))
        except LegalHoldBlocksDeletion as exc:
            results.append({**candidate, "status": "SKIPPED", "error": str(exc)})
        except Exception as exc:
            logger.exception("Repository purge sweep failed for %s", candidate["repository_id"])
            results.append({**candidate, "status": "ERROR", "error": str(exc)})
    return results


# ---------------------------------------------------------------------------
# Background daily sweep — same threading.Thread + Event pattern as
# local_workbook_sanitizer.py's watcher.
# ---------------------------------------------------------------------------
_watcher_thread: threading.Thread | None = None
_watcher_stop_event = threading.Event()


def _watcher_loop(interval_seconds: float) -> None:
    while not _watcher_stop_event.wait(interval_seconds):
        try:
            results = sweep_repositories_for_deletion()
            if results:
                logger.info("Repository purge sweep processed %d candidate(s).", len(results))
        except Exception:
            logger.exception("Repository purge sweep iteration failed")


def start_repository_purge_watcher(interval_seconds: float | None = None) -> None:
    global _watcher_thread
    if not settings.repository_purge_sweep_enabled:
        return
    if _watcher_thread and _watcher_thread.is_alive():
        return
    _watcher_stop_event.clear()
    _watcher_thread = threading.Thread(
        target=_watcher_loop,
        args=(interval_seconds or settings.repository_purge_sweep_interval_seconds,),
        name="GitWalkRepositoryPurgeWatcher",
        daemon=True,
    )
    _watcher_thread.start()
    logger.info("Repository purge watcher started (interval=%ss).", interval_seconds or settings.repository_purge_sweep_interval_seconds)


def stop_repository_purge_watcher() -> None:
    _watcher_stop_event.set()
    if _watcher_thread and _watcher_thread.is_alive():
        _watcher_thread.join(timeout=2.0)
    logger.info("Repository purge watcher stopped.")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Git Walk repository permanent-deletion pipeline")
    parser.add_argument("--sweep", action="store_true", help="Run the inactivity/soft-delete sweep once.")
    parser.add_argument("--repository-id", type=str, help="Purge one specific repository immediately.")
    parser.add_argument("--reason", type=str, default="MANUAL_CLI", help="Reason recorded for a single --repository-id purge.")
    parser.add_argument("--inactivity-days", type=int, default=None, help="Override REPOSITORY_PURGE_INACTIVITY_DAYS for this run.")
    parser.add_argument("--dry-run", action="store_true", help="List what would be purged without deleting anything.")
    parser.add_argument("--json", action="store_true", help="Print structured JSON output.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if not args.sweep and not args.repository_id:
        parser.error("Specify either --sweep or --repository-id.")

    if args.repository_id:
        if args.dry_run:
            result = {"repository_id": args.repository_id, "status": "WOULD_PURGE", "dry_run": True}
        else:
            result = purge_repository(args.repository_id, "USR_SYSTEM", args.reason)
        print(json.dumps(result, default=str, indent=2) if args.json else result)
        return

    results = sweep_repositories_for_deletion(dry_run=args.dry_run, inactivity_days=args.inactivity_days)
    if args.json:
        print(json.dumps(results, default=str, indent=2))
    else:
        print(f"Processed {len(results)} candidate(s).")
        for item in results:
            print(f" - {item.get('repository_name', item.get('repository_id'))}: {item.get('status')} ({item.get('reason')})")


if __name__ == "__main__":
    main()
