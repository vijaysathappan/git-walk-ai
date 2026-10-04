"""Orchestration for Virtual Run: register a macro source file, extract and
classify its macros, list results, and run a macro through whichever lane
it was classified into -- the SQL fast lane for narrow per-row transforms,
or the general interpreter for everything else safe (see static_gate.py)
-- all gated by the ``macro.view``/``macro.run`` permissions.

prepare_run()/confirm_run() mirror AIService._prepare_action/confirm_action
(app/ai/service.py) exactly: a MACRO_RUNS row moves
PENDING_CONFIRMATION -> EXECUTED|FAILED|REJECTED|EXPIRED, confirm re-checks
the permission AND the branch HEAD before writing anything, and the actual
write goes through commit_semantic_delta -- the same function every Excel
taskpane commit goes through, called directly (not via CommitService.commit,
which additionally requires a taskpane-issued signed WORKING_COPY; here the
"client" is our own previewed, server-side computation, not an unverified
external claim).
"""

from __future__ import annotations

import dataclasses
import difflib
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import database
from ..access_control.engine import authorization_engine, repository_resource
from ..euc.fingerprint import content_fingerprint
from ..euc.validation import EUCValidationError, validate_euc_file
from ..observability import record_audit_event
from ..repositories.commit_store import BranchHeadChangedError, commit_semantic_delta
from ..services.semantic_ledger_service import ledger_for_connection
from . import execution, interpreted_execution, interpreter, sql_lane, static_gate
from .extractor import extract_macros as run_extraction
from .parser.statement_parser import ParseError, parse_sub

_RUN_EXPIRY_MINUTES = 30


def _id(prefix: str, size: int = 18) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:size].upper()}"


def _canonicalize_ast(node: Any) -> Any:
    """Strips ``line`` fields (present on most Stmt nodes, for parse-error
    reporting only) before hashing -- otherwise a comment or blank line
    that shifts every subsequent statement down by one row would change
    every node's repr() even though nothing about the logic changed."""
    if dataclasses.is_dataclass(node):
        return (
            type(node).__name__,
            tuple(
                (field.name, _canonicalize_ast(getattr(node, field.name)))
                for field in dataclasses.fields(node) if field.name != "line"
            ),
        )
    if isinstance(node, (list, tuple)):
        return tuple(_canonicalize_ast(item) for item in node)
    return node


def _ast_hash(source: str) -> str | None:
    """Content hash of the *canonical* parsed structure, not the raw text
    -- a comment or whitespace-only change re-parses to the same canonical
    shape, so it never counts as drift. None if the source doesn't parse
    at all (drift detection then falls back to comparing raw source, see
    _record_drift_if_changed)."""
    try:
        sub_ast = parse_sub(source)
    except ParseError:
        return None
    canonical = repr(_canonicalize_ast(sub_ast))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _record_drift_if_changed(
    conn, ledger, *, macro_id: str, previous_row, repository_id: str, module_name: str, proc_name: str,
    new_source: str, new_ast_hash: str | None, new_static_risk: str, new_execution_lane: str | None,
) -> None:
    """Inserts a MACRO_DRIFT_EVENTS row iff this proc's logic actually
    changed since the last time it was extracted (by AST hash when both
    versions parse; by raw source otherwise). Silent no-op when unchanged
    -- re-extracting identical logic must never be reported as drift."""
    previous_ast_hash = previous_row["PARSED_AST_OBJECT_HASH"]
    previous_source = ledger.objects.get_bytes(conn, previous_row["SOURCE_OBJECT_HASH"]).decode("utf-8")
    if new_ast_hash is not None and previous_ast_hash is not None:
        changed = new_ast_hash != previous_ast_hash
    else:
        changed = previous_source != new_source
    if not changed:
        return
    risk_changed = (
        previous_row["STATIC_RISK"] != new_static_risk or previous_row["EXECUTION_LANE"] != new_execution_lane
    )
    diff_lines = list(difflib.unified_diff(
        previous_source.splitlines(), new_source.splitlines(), fromfile="previous", tofile="current", lineterm="",
    ))
    conn.execute(
        """
        INSERT INTO MACRO_DRIFT_EVENTS
            (DRIFT_ID, MACRO_ID, PREVIOUS_MACRO_ID, REPOSITORY_ID, MODULE_NAME, PROC_NAME,
             PREVIOUS_STATIC_RISK, NEW_STATIC_RISK, PREVIOUS_EXECUTION_LANE, NEW_EXECUTION_LANE,
             RISK_CHANGED, DIFF_JSON, CREATED_AT)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (_id("DRIFT"), macro_id, previous_row["MACRO_ID"], repository_id, module_name, proc_name,
         previous_row["STATIC_RISK"], new_static_risk, previous_row["EXECUTION_LANE"], new_execution_lane,
         int(risk_changed), json.dumps(diff_lines), database._utcnow()),
    )


def _repository_access(conn, table_id: str, user_id: str, permission: str):
    """Resolves the URL-facing ``table_id`` to the repository's real
    REPOSITORY_ID and checks ``permission``. TABLE_ID (the physical SQLite
    table name, e.g. QUEUE_BOARD_XXXX) and REPOSITORY_ID (e.g. REP_XXXX)
    are different identifiers -- every Virtual Run table is keyed by the
    latter, so every entry point must resolve through here first."""
    row = conn.execute(
        """
        SELECT R.*, COALESCE(M.ROLE, CASE WHEN R.CREATED_BY=? THEN 'owner' END) AS ACCESS_ROLE
        FROM WORKBOOK_REPOSITORIES R
        LEFT JOIN REPOSITORY_MEMBERS M ON M.REPOSITORY_ID=R.REPOSITORY_ID AND M.USER_ID=?
        WHERE R.TABLE_ID=? AND R.STATUS='ACTIVE'
        """,
        (user_id, user_id, table_id),
    ).fetchone()
    if not row or not row["ACCESS_ROLE"]:
        raise PermissionError("You do not have access to this repository.")
    authorization_engine.require(
        user_id, permission,
        repository_resource(row["REPOSITORY_ID"], organization_id=row["ORGANIZATION_ID"]), conn=conn,
    )
    return row


def register_source(table_id: str, filename: str, payload: bytes, user_id: str) -> dict[str, Any]:
    """Registers (or replaces) the .xlsm macro source for a repository.
    Independent of the repository's own semantic (cell-level) branch/commit
    model -- a workbook's macro project is one opaque artifact, not
    per-cell data, so it is not versioned through the commit graph."""
    conn = database._get_connection()
    try:
        repository = _repository_access(conn, table_id, user_id, "macro.run")
        repository_id = repository["REPOSITORY_ID"]
        validation = validate_euc_file(filename, payload)
        if validation.file_type != "xlsm":
            raise EUCValidationError("UNSUPPORTED_FORMAT", "Virtual Run requires a macro-enabled .xlsm file.")
        file_hash = content_fingerprint(payload)
        stored = ledger_for_connection(conn).objects.put_bytes(conn, "MACRO_SOURCE_FILE", payload)
        now = database._utcnow()
        conn.execute(
            """
            INSERT INTO MACRO_SOURCE_FILES (REPOSITORY_ID, ORIGINAL_FILENAME, SOURCE_OBJECT_HASH, FILE_HASH, SIZE_BYTES, REGISTERED_BY, REGISTERED_AT)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(REPOSITORY_ID) DO UPDATE SET
                ORIGINAL_FILENAME=excluded.ORIGINAL_FILENAME, SOURCE_OBJECT_HASH=excluded.SOURCE_OBJECT_HASH,
                FILE_HASH=excluded.FILE_HASH, SIZE_BYTES=excluded.SIZE_BYTES,
                REGISTERED_BY=excluded.REGISTERED_BY, REGISTERED_AT=excluded.REGISTERED_AT
            """,
            (repository_id, filename, stored["object_hash"], file_hash, len(payload), user_id, now),
        )
        conn.commit()
        record_audit_event(
            "MACRO_SOURCE_REGISTERED", actor_user_id=user_id, repository_id=repository_id,
            payload={"filename": filename, "file_hash": file_hash, "size_bytes": len(payload)},
        )
        return {"repository_id": repository_id, "filename": filename, "file_hash": file_hash, "size_bytes": len(payload)}
    finally:
        conn.close()


def extract(table_id: str, user_id: str) -> dict[str, Any]:
    """Extracts and classifies every macro in the repository's currently
    registered source file. Idempotent per file_hash: re-extracting the
    same bytes returns the existing run rather than duplicating rows."""
    conn = database._get_connection()
    try:
        repository = _repository_access(conn, table_id, user_id, "macro.view")
        repository_id = repository["REPOSITORY_ID"]
        source_row = conn.execute(
            "SELECT * FROM MACRO_SOURCE_FILES WHERE REPOSITORY_ID=?", (repository_id,)
        ).fetchone()
        if not source_row:
            raise KeyError("No macro source file has been registered for this repository yet.")
        existing = conn.execute(
            "SELECT * FROM MACRO_EXTRACTION_RUNS WHERE REPOSITORY_ID=? AND SOURCE_FILE_HASH=? AND STATUS='COMPLETED'",
            (repository_id, source_row["FILE_HASH"]),
        ).fetchone()
        if existing:
            return {"extraction_run_id": existing["EXTRACTION_RUN_ID"], "status": "COMPLETED", "deduplicated": True}

        ledger = ledger_for_connection(conn)
        payload = ledger.objects.get_bytes(conn, source_row["SOURCE_OBJECT_HASH"])
        extraction_run_id = _id("MEX")
        now = database._utcnow()
        conn.execute(
            """INSERT INTO MACRO_EXTRACTION_RUNS (EXTRACTION_RUN_ID, REPOSITORY_ID, SOURCE_FILE_HASH, STATUS, CREATED_AT)
               VALUES (?,?,?,'RUNNING',?)""",
            (extraction_run_id, repository_id, source_row["FILE_HASH"], now),
        )
        conn.commit()

        outcome = run_extraction(payload)
        if outcome.error_message:
            conn.execute(
                "UPDATE MACRO_EXTRACTION_RUNS SET STATUS='FAILED', ERROR_MESSAGE=?, COMPLETED_AT=? WHERE EXTRACTION_RUN_ID=?",
                (outcome.error_message, database._utcnow(), extraction_run_id),
            )
            conn.commit()
            record_audit_event(
                "MACRO_EXTRACTED", actor_user_id=user_id, repository_id=repository_id,
                payload={"extraction_run_id": extraction_run_id, "macro_count": 0}, status="FAILED",
                failure_reason=outcome.error_message,
            )
            return {"extraction_run_id": extraction_run_id, "status": "FAILED", "error_message": outcome.error_message}

        runnable_count = 0
        parser_version = "1.0.0"
        for macro in outcome.macros:
            static_risk, reasons, execution_lane = static_gate.classify(macro)
            if static_risk == "RUNNABLE":
                runnable_count += 1
            source_stored = ledger.objects.put_bytes(conn, "MACRO_SOURCE_CODE", macro.source.encode("utf-8"))
            ast_hash = _ast_hash(macro.source)
            macro_id = _id("MAC")

            # Looked up BEFORE this macro's own row is inserted below, so it
            # naturally finds the most recent row from an EARLIER extraction
            # (if any) for this same logical macro -- macro_id itself isn't
            # stable across re-extractions, so identity here is by name.
            previous = conn.execute(
                """SELECT * FROM MACRO_DEFINITIONS
                   WHERE REPOSITORY_ID=? AND MODULE_NAME=? AND PROC_NAME=?
                   ORDER BY CREATED_AT DESC LIMIT 1""",
                (repository_id, macro.module_name, macro.proc_name),
            ).fetchone()

            conn.execute(
                """
                INSERT INTO MACRO_DEFINITIONS
                    (MACRO_ID, EXTRACTION_RUN_ID, REPOSITORY_ID, MODULE_NAME, PROC_NAME,
                     SOURCE_OBJECT_HASH, SOURCE_LINE_COUNT, IS_AUTO_EXEC, HAS_PARAMETERS,
                     STATIC_RISK, BLOCK_REASONS_JSON, EXECUTION_LANE, PARSED_AST_OBJECT_HASH, PARSER_VERSION, CREATED_AT)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (macro_id, extraction_run_id, repository_id, macro.module_name, macro.proc_name,
                 source_stored["object_hash"], macro.source.count("\n") + 1, int(macro.is_auto_exec), int(macro.has_parameters),
                 static_risk, _reasons_json(reasons), execution_lane, ast_hash, parser_version, database._utcnow()),
            )
            if previous:
                _record_drift_if_changed(
                    conn, ledger, macro_id=macro_id, previous_row=previous,
                    repository_id=repository_id, module_name=macro.module_name, proc_name=macro.proc_name,
                    new_source=macro.source, new_ast_hash=ast_hash,
                    new_static_risk=static_risk, new_execution_lane=execution_lane,
                )
        conn.execute(
            """UPDATE MACRO_EXTRACTION_RUNS SET STATUS='COMPLETED', MACRO_COUNT=?, RUNNABLE_COUNT=?, COMPLETED_AT=?
               WHERE EXTRACTION_RUN_ID=?""",
            (len(outcome.macros), runnable_count, database._utcnow(), extraction_run_id),
        )
        conn.commit()
        record_audit_event(
            "MACRO_EXTRACTED", actor_user_id=user_id, repository_id=repository_id,
            payload={"extraction_run_id": extraction_run_id, "macro_count": len(outcome.macros), "runnable_count": runnable_count},
        )
        return {"extraction_run_id": extraction_run_id, "status": "COMPLETED", "macro_count": len(outcome.macros), "runnable_count": runnable_count}
    finally:
        conn.close()


def list_macros(table_id: str, user_id: str) -> dict[str, Any]:
    conn = database._get_connection()
    try:
        repository = _repository_access(conn, table_id, user_id, "macro.view")
        repository_id = repository["REPOSITORY_ID"]
        source_row = conn.execute(
            "SELECT * FROM MACRO_SOURCE_FILES WHERE REPOSITORY_ID=?", (repository_id,)
        ).fetchone()
        if not source_row:
            return {"has_source": False, "extraction_status": None, "macros": []}
        latest_run = conn.execute(
            """SELECT * FROM MACRO_EXTRACTION_RUNS WHERE REPOSITORY_ID=? AND SOURCE_FILE_HASH=?
               ORDER BY CREATED_AT DESC LIMIT 1""",
            (repository_id, source_row["FILE_HASH"]),
        ).fetchone()
        if not latest_run:
            return {"has_source": True, "extraction_status": "NOT_STARTED", "macros": []}
        rows = conn.execute(
            """SELECT * FROM MACRO_DEFINITIONS WHERE EXTRACTION_RUN_ID=? ORDER BY MODULE_NAME, PROC_NAME""",
            (latest_run["EXTRACTION_RUN_ID"],),
        ).fetchall()
        drift_by_macro = {}
        if rows:
            placeholders = ",".join("?" for _ in rows)
            drift_rows = conn.execute(
                f"SELECT * FROM MACRO_DRIFT_EVENTS WHERE MACRO_ID IN ({placeholders})",
                [row["MACRO_ID"] for row in rows],
            ).fetchall()
            drift_by_macro = {row["MACRO_ID"]: row for row in drift_rows}
        macros = []
        for row in rows:
            drift_row = drift_by_macro.get(row["MACRO_ID"])
            drift = None
            if drift_row:
                drift = {
                    "risk_changed": bool(drift_row["RISK_CHANGED"]),
                    "previous_static_risk": drift_row["PREVIOUS_STATIC_RISK"],
                    "previous_execution_lane": drift_row["PREVIOUS_EXECUTION_LANE"],
                    "diff": json.loads(drift_row["DIFF_JSON"]),
                }
            assurance_row = conn.execute(
                """SELECT STATUS, STALE_REASON FROM MACRO_ASSURANCE_CHECKS WHERE REPOSITORY_ID=? AND MODULE_NAME=? AND PROC_NAME=?
                   ORDER BY CHECKED_AT DESC LIMIT 1""",
                (repository_id, row["MODULE_NAME"], row["PROC_NAME"]),
            ).fetchone()
            assurance = None
            if assurance_row and assurance_row["STATUS"] == "STALE":
                assurance = {"status": "STALE", "reason": assurance_row["STALE_REASON"]}
            macros.append({
                "macro_id": row["MACRO_ID"],
                "module_name": row["MODULE_NAME"],
                "proc_name": row["PROC_NAME"],
                "static_risk": row["STATIC_RISK"],
                "block_reasons": json.loads(row["BLOCK_REASONS_JSON"]),
                "runnable": row["STATIC_RISK"] == "RUNNABLE",
                "execution_lane": row["EXECUTION_LANE"],
                "source_line_count": row["SOURCE_LINE_COUNT"],
                "drift": drift,
                "assurance": assurance,
            })
        return {"has_source": True, "extraction_status": latest_run["STATUS"], "macros": macros}
    finally:
        conn.close()


def get_cached_macro_explanation(table_id: str, macro_id: str, user_id: str) -> dict[str, Any] | None:
    """Repository-access-checked lookup for GET .../explanation -- a
    request for a fully-completed explanation, not the run itself (that's
    macro_explainer.start_macro_explanation, polled via the existing
    generic GET /ai-platform/agent-runs/{id})."""
    conn = database._get_connection()
    try:
        repository = _repository_access(conn, table_id, user_id, "macro.view")
        row = conn.execute(
            "SELECT * FROM MACRO_DEFINITIONS WHERE MACRO_ID=? AND REPOSITORY_ID=?", (macro_id, repository["REPOSITORY_ID"])
        ).fetchone()
        if not row:
            raise KeyError("Macro does not exist")
    finally:
        conn.close()
    from .macro_explainer import get_cached_explanation
    return get_cached_explanation(repository["REPOSITORY_ID"], row["MODULE_NAME"], row["PROC_NAME"], row["SOURCE_OBJECT_HASH"])


def get_macro_source(table_id: str, macro_id: str, user_id: str) -> dict[str, Any]:
    conn = database._get_connection()
    try:
        repository = _repository_access(conn, table_id, user_id, "macro.view")
        row = conn.execute(
            "SELECT * FROM MACRO_DEFINITIONS WHERE MACRO_ID=? AND REPOSITORY_ID=?", (macro_id, repository["REPOSITORY_ID"])
        ).fetchone()
        if not row:
            raise KeyError("Macro does not exist")
        source = ledger_for_connection(conn).objects.get_bytes(conn, row["SOURCE_OBJECT_HASH"]).decode("utf-8")
        return {"source": source, "line_count": row["SOURCE_LINE_COUNT"]}
    finally:
        conn.close()


def _branch_for_run(conn, repository_id: str, branch_id: str, user_id: str, access_role: str | None):
    branch = conn.execute(
        "SELECT * FROM BRANCHES WHERE BRANCH_ID=? AND REPOSITORY_ID=? AND STATUS='ACTIVE'",
        (branch_id, repository_id),
    ).fetchone()
    if not branch:
        raise KeyError("Branch does not exist on this repository.")
    if branch["BRANCH_TYPE"] == "MAIN":
        raise PermissionError(
            "Macros cannot run directly against main -- main only advances through an "
            "owner-reviewed merge request. Run this macro against a personal branch instead."
        )
    # A personal branch's normal Excel-taskpane commit path is implicitly
    # owner-scoped (CommitService.commit requires a signed WORKING_COPY,
    # which can only ever be issued for the caller's own branch) -- Virtual
    # Run calls commit_semantic_delta directly and has no equivalent
    # signature check, so without this it's the one path where any repo
    # editor could run a macro against, and commit to, someone ELSE's
    # personal branch, attributed under their own name instead of the
    # branch owner's.
    if branch["BRANCH_TYPE"] == "USER" and branch["CREATED_BY"] != user_id and access_role != "owner":
        raise PermissionError("You can only run macros against your own personal branch.")
    return branch


def _base_version(conn, data_table_id: str) -> int:
    row = conn.execute("SELECT CURRENT_VERSION FROM DATASET_REGISTRY WHERE TABLE_ID=?", (data_table_id,)).fetchone()
    if not row:
        raise KeyError("Branch dataset is not registered.")
    return int(row["CURRENT_VERSION"])


def _build_changes(conn, *, branch_id: str, repository_id: str, data_table_id: str, sub_ast, execution_lane: str):
    """Dispatches to whichever lane the macro was classified into at
    extraction time -- both lanes return the identical (changes, summary)
    shape, so neither this nor either caller needs to special-case which
    one actually ran."""
    try:
        if execution_lane == "SQL":
            return execution.build_change_list(
                conn, branch_id=branch_id, repository_id=repository_id, data_table_id=data_table_id, sub_ast=sub_ast,
            )
        if execution_lane == "INTERPRETED":
            return interpreted_execution.build_change_list(
                conn, branch_id=branch_id, repository_id=repository_id, data_table_id=data_table_id, sub_ast=sub_ast,
            )
        raise PermissionError(f"Macro has no valid execution lane ({execution_lane!r}).")
    except sql_lane.NotEligibleError as exc:
        raise PermissionError(f"This macro no longer fits the fast lane: {exc}") from exc
    except interpreter.InterpreterError as exc:
        raise PermissionError(f"This macro failed to execute safely against the current data: {exc}") from exc


def prepare_run(table_id: str, macro_id: str, branch_id: str, user_id: str, recipe_id: str | None = None) -> dict[str, Any]:
    """Compiles and evaluates the macro against the branch's CURRENT data,
    read-only, and stores the result as a PENDING_CONFIRMATION run for the
    editor to review before anything is written. ``recipe_id`` is purely
    for traceability (see prepare_run_from_recipe) -- it changes nothing
    about the governance rails below."""
    conn = database._get_connection()
    try:
        repository = _repository_access(conn, table_id, user_id, "macro.run")
        repository_id = repository["REPOSITORY_ID"]
        macro = conn.execute(
            "SELECT * FROM MACRO_DEFINITIONS WHERE MACRO_ID=? AND REPOSITORY_ID=?", (macro_id, repository_id)
        ).fetchone()
        if not macro:
            raise KeyError("Macro does not exist.")
        if macro["STATIC_RISK"] != "RUNNABLE":
            # Should be unreachable via the UI (the Run button only renders
            # for a runnable macro) -- this is defense in depth against a
            # direct API call, so it gets its own audit trail rather than
            # just a PermissionError nobody ever sees.
            record_audit_event(
                "MACRO_RUN_BLOCKED", actor_user_id=user_id, repository_id=repository_id,
                payload={"macro_id": macro_id, "static_risk": macro["STATIC_RISK"]}, status="FAILED",
                failure_reason="Macro is not classified RUNNABLE.",
            )
            raise PermissionError("This macro is not runnable.")
        branch = _branch_for_run(conn, repository_id, branch_id, user_id, repository["ACCESS_ROLE"])

        source = ledger_for_connection(conn).objects.get_bytes(conn, macro["SOURCE_OBJECT_HASH"]).decode("utf-8")
        try:
            sub_ast = parse_sub(source)
        except ParseError as exc:
            raise PermissionError(f"This macro no longer parses: {exc}") from exc

        execution_lane = macro["EXECUTION_LANE"]
        changes, summary = _build_changes(
            conn, branch_id=branch_id, repository_id=repository_id,
            data_table_id=branch["DATA_TABLE_ID"], sub_ast=sub_ast, execution_lane=execution_lane,
        )
        if not changes:
            raise ValueError("This macro would not change anything on the current data.")

        ledger = ledger_for_connection(conn)
        preview_stored = ledger.objects.put(conn, "MACRO_RUN_PREVIEW", changes)
        run_id = _id("MRUN")
        now = database._utcnow()
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=_RUN_EXPIRY_MINUTES)).isoformat()
        idempotency_key = hashlib.sha256(f"{macro_id}:{branch['HEAD_COMMIT_ID']}:{user_id}:{now}".encode()).hexdigest()
        commit_message = f"Virtual Run: {macro['PROC_NAME']} ({macro['MODULE_NAME']})"
        conn.execute(
            """
            INSERT INTO MACRO_RUNS
                (RUN_ID, MACRO_ID, REPOSITORY_ID, BRANCH_ID, REQUESTED_BY_USER_ID, STATUS,
                 EXPECTED_HEAD_COMMIT_ID, PREVIEW_OBJECT_HASH, PREVIEW_CHANGE_COUNT, EXECUTION_LANE,
                 COMMIT_MESSAGE, IDEMPOTENCY_KEY, EXPIRES_AT, RECIPE_ID, CREATED_AT)
            VALUES (?,?,?,?,?,'PENDING_CONFIRMATION',?,?,?,?,?,?,?,?,?)
            """,
            (run_id, macro_id, repository_id, branch_id, user_id, branch["HEAD_COMMIT_ID"],
             preview_stored["object_hash"], len(changes), execution_lane, commit_message, idempotency_key, expires_at, recipe_id, now),
        )
        conn.commit()
        record_audit_event(
            "MACRO_RUN_PREPARED", actor_user_id=user_id, repository_id=repository_id, branch_id=branch_id,
            payload={"macro_id": macro_id, "run_id": run_id, "change_count": len(changes), "execution_lane": execution_lane, **summary},
        )
        return {
            "run_id": run_id, "status": "PENDING_CONFIRMATION", "preview_change_count": len(changes),
            "expires_at": expires_at, "commit_message": commit_message, "preview": changes, "summary": summary,
        }
    finally:
        conn.close()


def get_run(table_id: str, run_id: str, user_id: str) -> dict[str, Any]:
    conn = database._get_connection()
    try:
        repository = _repository_access(conn, table_id, user_id, "macro.run")
        run = conn.execute(
            "SELECT * FROM MACRO_RUNS WHERE RUN_ID=? AND REPOSITORY_ID=?", (run_id, repository["REPOSITORY_ID"])
        ).fetchone()
        if not run:
            raise KeyError("Run does not exist.")
        preview = ledger_for_connection(conn).objects.get(conn, run["PREVIEW_OBJECT_HASH"])
        return {
            "run_id": run["RUN_ID"], "status": run["STATUS"], "execution_lane": run["EXECUTION_LANE"],
            "preview_change_count": run["PREVIEW_CHANGE_COUNT"], "commit_message": run["COMMIT_MESSAGE"],
            "expires_at": run["EXPIRES_AT"], "commit_id": run["COMMIT_ID"], "error_message": run["ERROR_MESSAGE"],
            "preview": preview,
        }
    finally:
        conn.close()


def confirm_run(table_id: str, run_id: str, user_id: str, commit_message: str | None = None) -> dict[str, Any]:
    """Re-checks permission, expiry, and branch HEAD, recomputes the change
    list fresh against current data (never trusts the stored preview for
    the write), then commits through commit_semantic_delta -- fully
    attributed to the confirming editor, never a system/bot identity."""
    conn = database._get_connection()
    try:
        repository = _repository_access(conn, table_id, user_id, "macro.run")
        repository_id = repository["REPOSITORY_ID"]
        run = conn.execute(
            "SELECT * FROM MACRO_RUNS WHERE RUN_ID=? AND REPOSITORY_ID=?", (run_id, repository_id)
        ).fetchone()
        if not run:
            raise KeyError("Run does not exist.")
        if run["STATUS"] != "PENDING_CONFIRMATION":
            raise ValueError(f"This run is {run['STATUS']}, not pending confirmation.")
        if run["EXPIRES_AT"] <= database._utcnow():
            conn.execute("UPDATE MACRO_RUNS SET STATUS='EXPIRED' WHERE RUN_ID=?", (run_id,))
            conn.commit()
            record_audit_event(
                "MACRO_RUN_REJECTED", actor_user_id=user_id, repository_id=repository_id,
                payload={"run_id": run_id, "reason": "expired"},
            )
            raise ValueError("This run has expired. Run the macro again to get a fresh preview.")

        branch = _branch_for_run(conn, repository_id, run["BRANCH_ID"], user_id, repository["ACCESS_ROLE"])
        if branch["HEAD_COMMIT_ID"] != run["EXPECTED_HEAD_COMMIT_ID"]:
            conn.execute("UPDATE MACRO_RUNS SET STATUS='REJECTED' WHERE RUN_ID=?", (run_id,))
            conn.commit()
            record_audit_event(
                "MACRO_RUN_REJECTED", actor_user_id=user_id, repository_id=repository_id, branch_id=run["BRANCH_ID"],
                payload={"run_id": run_id, "reason": "branch_head_changed"},
            )
            raise BranchHeadChangedError(run["EXPECTED_HEAD_COMMIT_ID"], branch["HEAD_COMMIT_ID"])

        macro = conn.execute("SELECT * FROM MACRO_DEFINITIONS WHERE MACRO_ID=?", (run["MACRO_ID"],)).fetchone()
        source = ledger_for_connection(conn).objects.get_bytes(conn, macro["SOURCE_OBJECT_HASH"]).decode("utf-8")
        sub_ast = parse_sub(source)
        changes, summary = _build_changes(
            conn, branch_id=run["BRANCH_ID"], repository_id=repository_id,
            data_table_id=branch["DATA_TABLE_ID"], sub_ast=sub_ast, execution_lane=run["EXECUTION_LANE"],
        )
        if not changes:
            raise ValueError("This macro would not change anything on the current data.")

        user = conn.execute("SELECT EMAIL FROM APP_USERS WHERE USER_ID=?", (user_id,)).fetchone()
        conn.execute("UPDATE MACRO_RUNS SET STATUS='CONFIRMED', CONFIRMED_BY=?, CONFIRMED_AT=? WHERE RUN_ID=?",
                     (user_id, database._utcnow(), run_id))
        conn.commit()

        try:
            result = commit_semantic_delta(
                table_id=branch["DATA_TABLE_ID"], repository_id=repository_id, branch_id=run["BRANCH_ID"],
                expected_head_commit_id=run["EXPECTED_HEAD_COMMIT_ID"], base_version=_base_version(conn, branch["DATA_TABLE_ID"]),
                changes=changes, user_id=user_id, user_email=user["EMAIL"] if user else "",
                message=commit_message or run["COMMIT_MESSAGE"],
            )
        except Exception as exc:
            conn.execute("UPDATE MACRO_RUNS SET STATUS='FAILED', ERROR_CODE=?, ERROR_MESSAGE=? WHERE RUN_ID=?",
                         (type(exc).__name__, str(exc), run_id))
            conn.commit()
            record_audit_event(
                "MACRO_RUN_EXECUTED", actor_user_id=user_id, repository_id=repository_id, branch_id=run["BRANCH_ID"],
                payload={"run_id": run_id, "macro_id": run["MACRO_ID"]}, status="FAILED", failure_reason=str(exc),
            )
            raise

        conn.execute("UPDATE MACRO_RUNS SET STATUS='EXECUTED', COMMIT_ID=? WHERE RUN_ID=?", (result["commit_id"], run_id))
        conn.commit()
        record_audit_event(
            "MACRO_RUN_EXECUTED", actor_user_id=user_id, repository_id=repository_id, branch_id=run["BRANCH_ID"],
            commit_id=result["commit_id"],
            payload={"run_id": run_id, "macro_id": run["MACRO_ID"], "change_count": len(changes)},
        )
        return {"run_id": run_id, "status": "EXECUTED", "commit_id": result["commit_id"]}
    finally:
        conn.close()


def _reasons_json(reasons) -> str:
    return json.dumps([reason.to_dict() for reason in reasons])


# ---------------------------------------------------------------------------
# Phase 5 USP3: Saved Macro Recipes. Pure metadata over the unmodified
# prepare_run()/confirm_run() pipeline -- see run_service.py's module
# docstring and macro_governance.md's Phase 5 plan for why this doesn't
# reuse AI_ACTIONS (built for a single 30-minute-expiring, agent-originated
# action; the wrong shape for a durable, user-authored, repeatedly-replayed
# artifact).
# ---------------------------------------------------------------------------

def save_recipe(table_id: str, macro_id: str, user_id: str, name: str, description: str | None = None) -> dict[str, Any]:
    """Saves a RUNNABLE macro as a reusable one-click recipe, keyed by
    (module_name, proc_name) -- not macro_id, which isn't stable across
    re-extractions (see list_macros' MACRO_ID note)."""
    conn = database._get_connection()
    try:
        repository = _repository_access(conn, table_id, user_id, "macro.run")
        repository_id = repository["REPOSITORY_ID"]
        macro = conn.execute(
            "SELECT * FROM MACRO_DEFINITIONS WHERE MACRO_ID=? AND REPOSITORY_ID=?", (macro_id, repository_id)
        ).fetchone()
        if not macro:
            raise KeyError("Macro does not exist.")
        if macro["STATIC_RISK"] != "RUNNABLE":
            raise PermissionError("Only a runnable macro can be saved as a recipe.")
        if not name or not name.strip():
            raise ValueError("A recipe needs a name.")
        recipe_id = _id("MREC")
        now = database._utcnow()
        conn.execute(
            """
            INSERT INTO MACRO_RECIPES (RECIPE_ID, REPOSITORY_ID, MODULE_NAME, PROC_NAME, NAME, DESCRIPTION, CREATED_BY, CREATED_AT, RUN_COUNT)
            VALUES (?,?,?,?,?,?,?,?,0)
            """,
            (recipe_id, repository_id, macro["MODULE_NAME"], macro["PROC_NAME"], name.strip(), description, user_id, now),
        )
        conn.commit()
        record_audit_event(
            "MACRO_RECIPE_SAVED", actor_user_id=user_id, repository_id=repository_id,
            payload={"recipe_id": recipe_id, "macro_id": macro_id, "name": name.strip()},
        )
        return {
            "recipe_id": recipe_id, "module_name": macro["MODULE_NAME"], "proc_name": macro["PROC_NAME"],
            "name": name.strip(), "description": description, "created_at": now, "last_run_at": None, "run_count": 0,
        }
    finally:
        conn.close()


def list_recipes(table_id: str, user_id: str) -> dict[str, Any]:
    conn = database._get_connection()
    try:
        repository = _repository_access(conn, table_id, user_id, "macro.view")
        rows = conn.execute(
            "SELECT * FROM MACRO_RECIPES WHERE REPOSITORY_ID=? ORDER BY CREATED_AT DESC", (repository["REPOSITORY_ID"],)
        ).fetchall()
        # A recipe is only runnable right now if its (module_name, proc_name)
        # currently resolves to a RUNNABLE macro -- surfaced here so the UI
        # never offers a one-click Run that would just fail-closed.
        recipes = []
        for row in rows:
            current = conn.execute(
                """SELECT STATIC_RISK FROM MACRO_DEFINITIONS WHERE REPOSITORY_ID=? AND MODULE_NAME=? AND PROC_NAME=?
                   ORDER BY CREATED_AT DESC LIMIT 1""",
                (repository["REPOSITORY_ID"], row["MODULE_NAME"], row["PROC_NAME"]),
            ).fetchone()
            recipes.append({
                "recipe_id": row["RECIPE_ID"], "module_name": row["MODULE_NAME"], "proc_name": row["PROC_NAME"],
                "name": row["NAME"], "description": row["DESCRIPTION"], "created_at": row["CREATED_AT"],
                "last_run_at": row["LAST_RUN_AT"], "run_count": row["RUN_COUNT"],
                "currently_runnable": bool(current and current["STATIC_RISK"] == "RUNNABLE"),
            })
        return {"recipes": recipes}
    finally:
        conn.close()


def delete_recipe(table_id: str, recipe_id: str, user_id: str) -> dict[str, Any]:
    conn = database._get_connection()
    try:
        repository = _repository_access(conn, table_id, user_id, "macro.run")
        row = conn.execute(
            "SELECT * FROM MACRO_RECIPES WHERE RECIPE_ID=? AND REPOSITORY_ID=?", (recipe_id, repository["REPOSITORY_ID"])
        ).fetchone()
        if not row:
            raise KeyError("Recipe does not exist.")
        conn.execute("DELETE FROM MACRO_RECIPES WHERE RECIPE_ID=?", (recipe_id,))
        conn.commit()
        record_audit_event(
            "MACRO_RECIPE_DELETED", actor_user_id=user_id, repository_id=repository["REPOSITORY_ID"],
            payload={"recipe_id": recipe_id},
        )
        return {"status": "DELETED"}
    finally:
        conn.close()


def prepare_run_from_recipe(table_id: str, recipe_id: str, branch_id: str, user_id: str) -> dict[str, Any]:
    """Resolves a saved recipe to the CURRENT runnable macro for its
    (module_name, proc_name) and delegates to the unmodified prepare_run --
    fails closed (never silently skips or substitutes) if that proc no
    longer exists or is no longer RUNNABLE, e.g. because the macro source
    was replaced with something that changed or removed it."""
    conn = database._get_connection()
    try:
        repository = _repository_access(conn, table_id, user_id, "macro.view")
        repository_id = repository["REPOSITORY_ID"]
        recipe = conn.execute(
            "SELECT * FROM MACRO_RECIPES WHERE RECIPE_ID=? AND REPOSITORY_ID=?", (recipe_id, repository_id)
        ).fetchone()
        if not recipe:
            raise KeyError("Recipe does not exist.")
        current = conn.execute(
            """SELECT * FROM MACRO_DEFINITIONS WHERE REPOSITORY_ID=? AND MODULE_NAME=? AND PROC_NAME=?
               ORDER BY CREATED_AT DESC LIMIT 1""",
            (repository_id, recipe["MODULE_NAME"], recipe["PROC_NAME"]),
        ).fetchone()
        if not current or current["STATIC_RISK"] != "RUNNABLE":
            raise PermissionError(
                f"'{recipe['PROC_NAME']}' ({recipe['MODULE_NAME']}) is no longer runnable -- "
                "the registered macro source may have changed. Review it in the macro list before saving a new recipe."
            )
        macro_id = current["MACRO_ID"]
    finally:
        conn.close()

    result = prepare_run(table_id, macro_id, branch_id, user_id, recipe_id=recipe_id)

    conn = database._get_connection()
    try:
        conn.execute(
            "UPDATE MACRO_RECIPES SET LAST_RUN_AT=?, RUN_COUNT=RUN_COUNT+1 WHERE RECIPE_ID=?",
            (database._utcnow(), recipe_id),
        )
        conn.commit()
    finally:
        conn.close()
    return result
