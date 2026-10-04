"""Schema/DDL and migration-shim logic, plus the low-level DB-connection and
identifier-adjacent primitives (DB_PATH, _get_connection, _utcnow,
VersionConflictError, table_exists/column_exists) that every other store
module depends on. This is intentionally the "base" module in the store/
package: it imports from no other store/ module at load time."""

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ..config import settings
from ..excel.identity import ensure_branch_identities, semantic_snapshot, stable_id
from .identifiers import SafeIdentifier, validate_identifier

_DB_DIR = Path(__file__).resolve().parent.parent.parent  # backend/ (this file is app/store/schema.py)
_configured_database = settings.database_url.removeprefix("sqlite:///")
DB_PATH = Path(_configured_database).expanduser() if _configured_database else _DB_DIR / "queue_board.db"


class VersionConflictError(RuntimeError):
    def __init__(self, current_version: int):
        self.current_version = current_version
        super().__init__(f"Server is at version {current_version}")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_product_schema() -> None:
    """Create product metadata tables without touching provisioned datasets."""
    conn = _get_connection()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS APP_USERS (
                USER_ID TEXT PRIMARY KEY,
                EMAIL TEXT NOT NULL UNIQUE COLLATE NOCASE,
                DISPLAY_NAME TEXT,
                ROLE TEXT NOT NULL DEFAULT 'member',
                PASSWORD_HASH TEXT,
                CREATED_AT TEXT NOT NULL,
                LAST_LOGIN_AT TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS AUTH_LOGIN_CODES (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                EMAIL TEXT NOT NULL COLLATE NOCASE,
                CODE_HASH TEXT NOT NULL,
                EXPIRES_AT TEXT NOT NULL,
                USED_AT TEXT,
                ATTEMPTS INTEGER NOT NULL DEFAULT 0,
                MAX_ATTEMPTS INTEGER NOT NULL DEFAULT 5,
                CREATED_AT TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS AUTH_SESSIONS (
                SESSION_ID TEXT PRIMARY KEY,
                USER_ID TEXT NOT NULL,
                TOKEN_HASH TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL,
                EXPIRES_AT TEXT NOT NULL,
                REVOKED_AT TEXT
            );

            CREATE TABLE IF NOT EXISTS DATASET_REGISTRY (
                TABLE_ID TEXT PRIMARY KEY,
                OWNER_USER_ID TEXT NOT NULL,
                ORIGINAL_FILENAME TEXT,
                ROW_COUNT INTEGER NOT NULL DEFAULT 0,
                COLUMN_COUNT INTEGER NOT NULL DEFAULT 0,
                CREATED_AT TEXT NOT NULL,
                UPDATED_AT TEXT NOT NULL,
                CURRENT_VERSION INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (OWNER_USER_ID) REFERENCES APP_USERS(USER_ID)
            );

            CREATE TABLE IF NOT EXISTS AUDIT_COMMITS (
                AUDIT_ID TEXT PRIMARY KEY,
                BATCH_ID TEXT NOT NULL,
                VERSION INTEGER NOT NULL,
                TABLE_ID TEXT NOT NULL,
                ROW_ID INTEGER NOT NULL,
                COLUMN_NAME TEXT NOT NULL,
                OLD_VALUE TEXT,
                NEW_VALUE TEXT,
                USER_ID TEXT NOT NULL,
                USER_EMAIL TEXT NOT NULL,
                SOURCE TEXT NOT NULL,
                STATUS TEXT NOT NULL,
                COMMIT_MESSAGE TEXT,
                RISK_SCORE INTEGER NOT NULL DEFAULT 0,
                CREATED_AT TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS DATASET_MEMBERS (
                TABLE_ID TEXT NOT NULL,
                USER_ID TEXT NOT NULL,
                ROLE TEXT NOT NULL CHECK (ROLE IN ('viewer', 'editor')),
                ADDED_AT TEXT NOT NULL,
                PRIMARY KEY (TABLE_ID, USER_ID),
                FOREIGN KEY (TABLE_ID) REFERENCES DATASET_REGISTRY(TABLE_ID),
                FOREIGN KEY (USER_ID) REFERENCES APP_USERS(USER_ID)
            );

            CREATE TABLE IF NOT EXISTS DATASET_INVITATIONS (
                INVITATION_ID TEXT PRIMARY KEY,
                TABLE_ID TEXT NOT NULL,
                EMAIL TEXT NOT NULL COLLATE NOCASE,
                ROLE TEXT NOT NULL CHECK (ROLE IN ('viewer', 'editor')),
                STATUS TEXT NOT NULL CHECK (STATUS IN ('pending', 'accepted', 'revoked')),
                INVITED_BY TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL,
                UPDATED_AT TEXT NOT NULL,
                UNIQUE (TABLE_ID, EMAIL),
                FOREIGN KEY (TABLE_ID) REFERENCES DATASET_REGISTRY(TABLE_ID)
            );

            CREATE TABLE IF NOT EXISTS WORKSPACE_EVENTS (
                REVISION INTEGER PRIMARY KEY AUTOINCREMENT,
                TABLE_ID TEXT NOT NULL,
                EVENT_TYPE TEXT NOT NULL,
                ACTOR_USER_ID TEXT NOT NULL,
                PAYLOAD_JSON TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS CATEGORIES (
                CATEGORY_ID TEXT PRIMARY KEY, PARENT_CATEGORY_ID TEXT,
                NAME TEXT NOT NULL, DESCRIPTION TEXT, DISPLAY_ORDER INTEGER NOT NULL DEFAULT 0,
                STATUS TEXT NOT NULL DEFAULT 'ACTIVE', CREATED_AT TEXT NOT NULL, UPDATED_AT TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS WORKBOOK_REPOSITORIES (
                REPOSITORY_ID TEXT PRIMARY KEY, TABLE_ID TEXT NOT NULL UNIQUE,
                CATEGORY_ID TEXT NOT NULL, REPOSITORY_NAME TEXT NOT NULL, DESCRIPTION TEXT,
                DEFAULT_BRANCH_ID TEXT, CREATED_BY TEXT NOT NULL, CREATED_AT TEXT NOT NULL,
                UPDATED_AT TEXT NOT NULL, STATUS TEXT NOT NULL DEFAULT 'ACTIVE', MAIN_PROTECTED INTEGER NOT NULL DEFAULT 0,
                REPOSITORY_SLUG TEXT, VISIBILITY TEXT NOT NULL DEFAULT 'private', BUSINESS_OWNER TEXT,
                DATA_CLASSIFICATION TEXT NOT NULL DEFAULT 'internal', RETENTION_POLICY TEXT,
                UNIQUE(REPOSITORY_SLUG)
            );

            CREATE TABLE IF NOT EXISTS REPOSITORY_MEMBERS (
                REPOSITORY_ID TEXT NOT NULL,
                USER_ID TEXT NOT NULL,
                ROLE TEXT NOT NULL CHECK (ROLE IN ('owner', 'editor', 'viewer')),
                GRANTED_BY TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL,
                UPDATED_AT TEXT NOT NULL,
                PRIMARY KEY (REPOSITORY_ID, USER_ID)
            );

            CREATE TABLE IF NOT EXISTS WORKBOOK_SHEETS (
                SHEET_ID TEXT PRIMARY KEY, REPOSITORY_ID TEXT NOT NULL, SHEET_NAME TEXT NOT NULL,
                SHEET_ORDER INTEGER NOT NULL, STATUS TEXT NOT NULL DEFAULT 'ACTIVE', CREATED_AT TEXT NOT NULL,
                UNIQUE(REPOSITORY_ID, SHEET_ID)
            );

            CREATE TABLE IF NOT EXISTS BRANCHES (
                BRANCH_ID TEXT PRIMARY KEY, REPOSITORY_ID TEXT NOT NULL, DATA_TABLE_ID TEXT NOT NULL UNIQUE,
                BRANCH_NAME TEXT NOT NULL, BRANCH_TYPE TEXT NOT NULL CHECK(BRANCH_TYPE IN ('MAIN','USER')),
                CREATED_BY TEXT NOT NULL, BASE_COMMIT_ID TEXT, HEAD_COMMIT_ID TEXT,
                STATUS TEXT NOT NULL DEFAULT 'ACTIVE', CREATED_AT TEXT NOT NULL, UPDATED_AT TEXT NOT NULL,
                MERGED_AT TEXT, ARCHIVED_AT TEXT, LOCAL_DOWNLOAD_PATH TEXT, UNIQUE(REPOSITORY_ID, BRANCH_NAME)
            );

            CREATE TABLE IF NOT EXISTS WORKING_COPIES (
                WORKING_COPY_ID TEXT PRIMARY KEY, REPOSITORY_ID TEXT NOT NULL, BRANCH_ID TEXT NOT NULL,
                USER_ID TEXT NOT NULL, BASE_COMMIT_ID TEXT, GENERATED_AT TEXT NOT NULL, LAST_SEEN_AT TEXT NOT NULL,
                STATUS TEXT NOT NULL DEFAULT 'ACTIVE', WORKBOOK_FINGERPRINT TEXT NOT NULL,
                ISSUED_AT TEXT NOT NULL, SIGNATURE TEXT NOT NULL, LOCAL_FILE_PATH TEXT,
                BOUND_MACHINE_ID TEXT
            );

            CREATE TABLE IF NOT EXISTS SYSTEM_SETTINGS (
                SETTING_KEY TEXT PRIMARY KEY,
                SETTING_VALUE TEXT NOT NULL,
                UPDATED_AT TEXT NOT NULL,
                UPDATED_BY TEXT
            );

            CREATE TABLE IF NOT EXISTS SHEET_ROWS (
                BRANCH_ID TEXT NOT NULL,
                SHEET_ID TEXT NOT NULL,
                ROW_ID TEXT NOT NULL,
                PHYSICAL_ROW_ID INTEGER NOT NULL,
                ROW_POSITION INTEGER NOT NULL,
                STATUS TEXT NOT NULL DEFAULT 'ACTIVE',
                CREATED_AT TEXT NOT NULL,
                UPDATED_AT TEXT NOT NULL,
                PRIMARY KEY (BRANCH_ID, SHEET_ID, ROW_ID),
                UNIQUE (BRANCH_ID, SHEET_ID, PHYSICAL_ROW_ID)
            );

            CREATE TABLE IF NOT EXISTS BRANCH_SHEETS (
                BRANCH_ID TEXT NOT NULL,
                SHEET_ID TEXT NOT NULL,
                SHEET_NAME TEXT NOT NULL,
                SHEET_POSITION INTEGER NOT NULL,
                STATUS TEXT NOT NULL DEFAULT 'ACTIVE',
                CREATED_AT TEXT NOT NULL,
                UPDATED_AT TEXT NOT NULL,
                PRIMARY KEY (BRANCH_ID, SHEET_ID)
            );

            CREATE TABLE IF NOT EXISTS BRANCH_SHEET_TABLES (
                BRANCH_ID TEXT NOT NULL,
                SHEET_ID TEXT NOT NULL,
                DATA_TABLE_ID TEXT NOT NULL UNIQUE,
                IS_PRIMARY INTEGER NOT NULL DEFAULT 0,
                CREATED_AT TEXT NOT NULL,
                UPDATED_AT TEXT NOT NULL,
                PRIMARY KEY (BRANCH_ID, SHEET_ID)
            );

            CREATE TABLE IF NOT EXISTS SHEET_COLUMNS (
                BRANCH_ID TEXT NOT NULL,
                SHEET_ID TEXT NOT NULL,
                COLUMN_ID TEXT NOT NULL,
                COLUMN_NAME TEXT NOT NULL,
                COLUMN_POSITION INTEGER NOT NULL,
                DATA_TYPE TEXT NOT NULL DEFAULT 'TEXT',
                STATUS TEXT NOT NULL DEFAULT 'ACTIVE',
                CREATED_AT TEXT NOT NULL,
                UPDATED_AT TEXT NOT NULL,
                PRIMARY KEY (BRANCH_ID, SHEET_ID, COLUMN_ID)
            );

            CREATE TABLE IF NOT EXISTS CELL_METADATA (
                BRANCH_ID TEXT NOT NULL,
                SHEET_ID TEXT NOT NULL,
                ROW_ID TEXT NOT NULL,
                COLUMN_ID TEXT NOT NULL,
                FORMULA TEXT,
                STYLE_HASH TEXT,
                COMMENT_TEXT TEXT,
                UPDATED_AT TEXT NOT NULL,
                PRIMARY KEY (BRANCH_ID, SHEET_ID, ROW_ID, COLUMN_ID)
            );

            CREATE TABLE IF NOT EXISTS COMMITS (
                COMMIT_ID TEXT PRIMARY KEY,
                REPOSITORY_ID TEXT NOT NULL,
                BRANCH_ID TEXT NOT NULL,
                AUTHOR_USER_ID TEXT NOT NULL,
                AUTHOR_EMAIL TEXT NOT NULL,
                MESSAGE TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL,
                CHANGE_COUNT INTEGER NOT NULL,
                COMMIT_HASH TEXT NOT NULL UNIQUE,
                STATUS TEXT NOT NULL DEFAULT 'COMMITTED',
                REVERTS_COMMIT_ID TEXT,
                DATASET_VERSION INTEGER
            );

            CREATE TABLE IF NOT EXISTS COMMIT_PARENTS (
                COMMIT_ID TEXT NOT NULL,
                PARENT_COMMIT_ID TEXT NOT NULL,
                PARENT_ORDER INTEGER NOT NULL,
                PRIMARY KEY (COMMIT_ID, PARENT_ORDER)
            );

            CREATE TABLE IF NOT EXISTS COMMIT_CHANGES (
                CHANGE_ID TEXT PRIMARY KEY,
                COMMIT_ID TEXT NOT NULL,
                REPOSITORY_ID TEXT NOT NULL,
                BRANCH_ID TEXT NOT NULL,
                SHEET_ID TEXT NOT NULL,
                OPERATION_TYPE TEXT NOT NULL,
                ROW_ID TEXT,
                COLUMN_ID TEXT,
                PREVIOUS_ROW_POSITION INTEGER,
                NEW_ROW_POSITION INTEGER,
                PREVIOUS_COLUMN_POSITION INTEGER,
                NEW_COLUMN_POSITION INTEGER,
                PREVIOUS_CELL_REFERENCE TEXT,
                NEW_CELL_REFERENCE TEXT,
                OLD_VALUE TEXT,
                NEW_VALUE TEXT,
                OLD_FORMULA TEXT,
                NEW_FORMULA TEXT,
                OLD_DATA_TYPE TEXT,
                NEW_DATA_TYPE TEXT,
                OLD_STYLE_HASH TEXT,
                NEW_STYLE_HASH TEXT,
                OLD_COMMENT TEXT,
                NEW_COMMENT TEXT,
                METADATA_JSON TEXT,
                CREATED_AT TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS BRANCH_CHECKPOINTS (
                CHECKPOINT_ID TEXT PRIMARY KEY,
                REPOSITORY_ID TEXT NOT NULL,
                BRANCH_ID TEXT NOT NULL,
                COMMIT_ID TEXT NOT NULL UNIQUE,
                SNAPSHOT_JSON TEXT NOT NULL,
                REASON TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS MERGE_REQUESTS (
                MERGE_REQUEST_ID TEXT PRIMARY KEY,
                REPOSITORY_ID TEXT NOT NULL,
                SOURCE_BRANCH_ID TEXT NOT NULL,
                TARGET_BRANCH_ID TEXT NOT NULL,
                SOURCE_HEAD_COMMIT_ID TEXT NOT NULL,
                TARGET_HEAD_COMMIT_ID TEXT NOT NULL,
                MERGE_BASE_COMMIT_ID TEXT NOT NULL,
                CREATED_BY TEXT NOT NULL,
                TITLE TEXT NOT NULL,
                DESCRIPTION TEXT,
                STATUS TEXT NOT NULL,
                CONFLICT_STATUS TEXT NOT NULL,
                VALIDATION_STATUS TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL,
                UPDATED_AT TEXT NOT NULL,
                MERGED_AT TEXT,
                MERGED_BY TEXT,
                MERGE_COMMIT_ID TEXT
            );

            CREATE TABLE IF NOT EXISTS MERGE_CONFLICTS (
                CONFLICT_ID TEXT PRIMARY KEY,
                MERGE_REQUEST_ID TEXT NOT NULL,
                SHEET_ID TEXT,
                ROW_ID TEXT,
                COLUMN_ID TEXT,
                CONFLICT_TYPE TEXT NOT NULL,
                BASE_STATE TEXT,
                MAIN_STATE TEXT,
                BRANCH_STATE TEXT,
                RESOLUTION_TYPE TEXT,
                RESOLVED_STATE TEXT,
                RESOLVED_BY TEXT,
                RESOLVED_AT TEXT,
                STATUS TEXT NOT NULL DEFAULT 'OPEN',
                CREATED_AT TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS MERGE_REQUEST_REVIEWS (
                REVIEW_ID TEXT PRIMARY KEY,
                MERGE_REQUEST_ID TEXT NOT NULL,
                REVIEWER_USER_ID TEXT NOT NULL,
                DECISION TEXT NOT NULL,
                COMMENT_TEXT TEXT,
                CREATED_AT TEXT NOT NULL,
                UNIQUE(MERGE_REQUEST_ID, REVIEWER_USER_ID)
            );

            CREATE TABLE IF NOT EXISTS MERGE_REVIEWER_REQUESTS (
                REQUEST_ID TEXT PRIMARY KEY,
                MERGE_REQUEST_ID TEXT NOT NULL,
                REPOSITORY_ID TEXT NOT NULL,
                REQUESTED_BY_USER_ID TEXT NOT NULL,
                REVIEWER_USER_ID TEXT NOT NULL,
                REVIEWER_EMAIL TEXT NOT NULL,
                STATUS TEXT NOT NULL DEFAULT 'PENDING' CHECK (STATUS IN ('PENDING','RESPONDED','AI_FALLBACK','CANCELLED')),
                DECISION TEXT CHECK (DECISION IS NULL OR DECISION IN ('APPROVED','REJECTED')),
                COMMENT_TEXT TEXT,
                CREATED_AT TEXT NOT NULL,
                EXPIRES_AT TEXT NOT NULL,
                RESPONDED_AT TEXT
            );
            CREATE INDEX IF NOT EXISTS IDX_MERGE_REVIEWER_REQUESTS_MR ON MERGE_REVIEWER_REQUESTS(MERGE_REQUEST_ID, CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_MERGE_REVIEWER_REQUESTS_REVIEWER ON MERGE_REVIEWER_REQUESTS(REVIEWER_USER_ID, STATUS);

            CREATE TABLE IF NOT EXISTS VALIDATION_RUNS (
                VALIDATION_RUN_ID TEXT PRIMARY KEY,
                MERGE_REQUEST_ID TEXT,
                REPOSITORY_ID TEXT NOT NULL,
                BRANCH_ID TEXT NOT NULL,
                COMMIT_ID TEXT,
                STATUS TEXT NOT NULL,
                ERROR_COUNT INTEGER NOT NULL DEFAULT 0,
                WARNING_COUNT INTEGER NOT NULL DEFAULT 0,
                STARTED_AT TEXT NOT NULL,
                COMPLETED_AT TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS VALIDATION_RESULTS (
                VALIDATION_RESULT_ID TEXT PRIMARY KEY,
                VALIDATION_RUN_ID TEXT NOT NULL,
                RULE_CODE TEXT NOT NULL,
                SEVERITY TEXT NOT NULL,
                STATUS TEXT NOT NULL,
                SHEET_ID TEXT,
                ROW_ID TEXT,
                COLUMN_ID TEXT,
                MESSAGE TEXT NOT NULL,
                DETAILS_JSON TEXT,
                CREATED_AT TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS DATASET_VERSIONS (
                TABLE_ID TEXT NOT NULL,
                VERSION INTEGER NOT NULL,
                BATCH_ID TEXT,
                SNAPSHOT_JSON TEXT NOT NULL,
                COMMIT_MESSAGE TEXT,
                USER_ID TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL,
                PRIMARY KEY (TABLE_ID, VERSION)
            );

            CREATE TABLE IF NOT EXISTS DATASET_PRESENCE (
                TABLE_ID TEXT NOT NULL,
                USER_ID TEXT NOT NULL,
                CLIENT_ID TEXT NOT NULL,
                SURFACE TEXT NOT NULL,
                ACTIVITY TEXT NOT NULL,
                LAST_SEEN TEXT NOT NULL,
                PRIMARY KEY (TABLE_ID, USER_ID, CLIENT_ID)
            );

            CREATE TABLE IF NOT EXISTS USER_AI_SETTINGS (
                USER_ID TEXT PRIMARY KEY,
                API_KEY_ENCRYPTED TEXT NOT NULL,
                MODEL TEXT NOT NULL,
                UPDATED_AT TEXT NOT NULL,
                FOREIGN KEY (USER_ID) REFERENCES APP_USERS(USER_ID)
            );

            CREATE TABLE IF NOT EXISTS AUDIT_EVENTS (
                EVENT_ID TEXT PRIMARY KEY,
                CREATED_AT TEXT NOT NULL,
                ACTOR_USER_ID TEXT NOT NULL,
                ACTOR_TYPE TEXT NOT NULL CHECK(ACTOR_TYPE IN ('USER','SYSTEM','AI')),
                REPOSITORY_ID TEXT,
                BRANCH_ID TEXT,
                WORKING_COPY_ID TEXT,
                COMMIT_ID TEXT,
                MERGE_REQUEST_ID TEXT,
                EVENT_TYPE TEXT NOT NULL,
                EVENT_PAYLOAD TEXT NOT NULL,
                REQUEST_ID TEXT NOT NULL,
                TRACE_ID TEXT NOT NULL,
                STATUS TEXT NOT NULL,
                FAILURE_REASON TEXT,
                PREVIOUS_EVENT_HASH TEXT,
                EVENT_HASH TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS OPERATION_METRICS (
                METRIC_ID TEXT PRIMARY KEY,
                CREATED_AT TEXT NOT NULL,
                METRIC_NAME TEXT NOT NULL,
                METRIC_VALUE REAL NOT NULL,
                UNIT TEXT NOT NULL,
                STATUS TEXT NOT NULL,
                REQUEST_ID TEXT,
                TRACE_ID TEXT,
                USER_ID TEXT,
                REPOSITORY_ID TEXT,
                BRANCH_ID TEXT,
                TAGS_JSON TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS SECURITY_EVENTS (
                SECURITY_EVENT_ID TEXT PRIMARY KEY,
                CREATED_AT TEXT NOT NULL,
                EVENT_TYPE TEXT NOT NULL,
                SEVERITY TEXT NOT NULL,
                USER_ID TEXT,
                REQUEST_ID TEXT,
                TRACE_ID TEXT,
                CLIENT_IP TEXT,
                DETAILS_JSON TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS STORAGE_OBJECTS (
                OBJECT_HASH TEXT PRIMARY KEY,
                OBJECT_TYPE TEXT NOT NULL,
                STORAGE_KEY TEXT NOT NULL,
                RAW_SIZE INTEGER NOT NULL,
                COMPRESSED_SIZE INTEGER NOT NULL,
                COMPRESSION TEXT NOT NULL,
                ENCODING TEXT NOT NULL,
                STATUS TEXT NOT NULL DEFAULT 'AVAILABLE',
                VERIFICATION_STATUS TEXT NOT NULL DEFAULT 'VALID',
                CREATED_AT TEXT NOT NULL,
                LAST_ACCESSED_AT TEXT,
                LAST_VERIFIED_AT TEXT
            );

            CREATE TABLE IF NOT EXISTS OBJECT_REFERENCES (
                PARENT_HASH TEXT NOT NULL,
                CHILD_HASH TEXT NOT NULL,
                REFERENCE_TYPE TEXT NOT NULL,
                POSITION INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (PARENT_HASH, CHILD_HASH, REFERENCE_TYPE, POSITION)
            );

            CREATE TABLE IF NOT EXISTS COMMIT_MANIFESTS (
                COMMIT_ID TEXT PRIMARY KEY,
                REPOSITORY_ID TEXT NOT NULL,
                BRANCH_ID TEXT NOT NULL,
                ROOT_MANIFEST_HASH TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS IDEMPOTENCY_KEYS (
                IDEMPOTENCY_KEY TEXT NOT NULL,
                ACTOR_ID TEXT NOT NULL,
                OPERATION TEXT NOT NULL,
                REQUEST_HASH TEXT NOT NULL,
                RESPONSE_JSON TEXT,
                STATUS TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL,
                EXPIRES_AT TEXT NOT NULL,
                PRIMARY KEY (IDEMPOTENCY_KEY, ACTOR_ID, OPERATION)
            );

            CREATE TABLE IF NOT EXISTS OUTBOX_EVENTS (
                EVENT_ID TEXT PRIMARY KEY,
                EVENT_TYPE TEXT NOT NULL,
                AGGREGATE_ID TEXT NOT NULL,
                PAYLOAD_JSON TEXT NOT NULL,
                STATUS TEXT NOT NULL DEFAULT 'PENDING',
                CREATED_AT TEXT NOT NULL,
                PUBLISHED_AT TEXT
            );

            CREATE TABLE IF NOT EXISTS RETENTION_POLICIES (
                POLICY_ID TEXT PRIMARY KEY,
                REPOSITORY_ID TEXT NOT NULL UNIQUE,
                RETAIN_DAYS INTEGER NOT NULL DEFAULT 1095,
                GC_GRACE_DAYS INTEGER NOT NULL DEFAULT 7,
                CREATED_AT TEXT NOT NULL,
                UPDATED_AT TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS LEGAL_HOLDS (
                HOLD_ID TEXT PRIMARY KEY,
                REPOSITORY_ID TEXT NOT NULL,
                REASON TEXT NOT NULL,
                STATUS TEXT NOT NULL DEFAULT 'ACTIVE',
                CREATED_BY TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL,
                RELEASED_AT TEXT
            );

            CREATE TABLE IF NOT EXISTS STORAGE_GC_RUNS (
                GC_RUN_ID TEXT PRIMARY KEY,
                STARTED_AT TEXT NOT NULL,
                COMPLETED_AT TEXT,
                STATUS TEXT NOT NULL,
                OBJECTS_SCANNED INTEGER NOT NULL DEFAULT 0,
                OBJECTS_DELETED INTEGER NOT NULL DEFAULT 0,
                BYTES_RECLAIMED INTEGER NOT NULL DEFAULT 0,
                DETAILS_JSON TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS EUC_ASSETS (
                EUC_ID TEXT PRIMARY KEY, REPOSITORY_ID TEXT NOT NULL,
                ORIGINAL_FILENAME TEXT NOT NULL, FILE_TYPE TEXT NOT NULL, MIME_TYPE TEXT,
                ORIGINAL_OBJECT_HASH TEXT NOT NULL, FILE_HASH TEXT NOT NULL,
                STRUCTURE_HASH TEXT, SIZE_BYTES INTEGER NOT NULL,
                ANALYSIS_STATUS TEXT NOT NULL DEFAULT 'PENDING', LATEST_ANALYSIS_ID TEXT,
                CREATED_BY TEXT NOT NULL, CREATED_AT TEXT NOT NULL, UPDATED_AT TEXT NOT NULL,
                UNIQUE(REPOSITORY_ID, FILE_HASH)
            );

            CREATE TABLE IF NOT EXISTS EUC_ANALYSIS_RUNS (
                ANALYSIS_ID TEXT PRIMARY KEY, EUC_ID TEXT NOT NULL, SOURCE_COMMIT_ID TEXT,
                ANALYZER_VERSION TEXT NOT NULL, STATUS TEXT NOT NULL, PROGRESS INTEGER NOT NULL DEFAULT 0,
                CURRENT_STEP TEXT, STARTED_AT TEXT NOT NULL, COMPLETED_AT TEXT, DURATION_MS INTEGER,
                WARNING_COUNT INTEGER NOT NULL DEFAULT 0, ERROR_COUNT INTEGER NOT NULL DEFAULT 0,
                RESULT_MANIFEST_HASH TEXT, SUMMARY_JSON TEXT NOT NULL DEFAULT '{}',
                WARNINGS_JSON TEXT NOT NULL DEFAULT '[]', ERRORS_JSON TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS EUC_SHEET_INVENTORY (
                SHEET_INVENTORY_ID TEXT PRIMARY KEY, ANALYSIS_ID TEXT NOT NULL, SHEET_ID TEXT NOT NULL,
                SHEET_NAME TEXT NOT NULL, SHEET_POSITION INTEGER, VISIBILITY TEXT,
                MAX_ROW INTEGER, MAX_COLUMN INTEGER, USED_RANGE TEXT, USED_CELL_COUNT INTEGER,
                FORMULA_CELL_COUNT INTEGER, CONSTANT_CELL_COUNT INTEGER, BLANK_STYLED_CELL_COUNT INTEGER,
                MERGED_RANGE_COUNT INTEGER, HIDDEN_ROW_COUNT INTEGER, HIDDEN_COLUMN_COUNT INTEGER,
                TABLE_COUNT INTEGER, CHART_COUNT INTEGER, PIVOT_COUNT INTEGER,
                VALIDATION_COUNT INTEGER, COMMENT_COUNT INTEGER, HYPERLINK_COUNT INTEGER,
                STRUCTURE_HASH TEXT
            );

            CREATE TABLE IF NOT EXISTS EUC_FORMULA_PATTERNS (
                PATTERN_ID TEXT PRIMARY KEY, ANALYSIS_ID TEXT NOT NULL,
                NORMALIZED_FORMULA TEXT NOT NULL, NORMALIZED_HASH TEXT NOT NULL,
                OCCURRENCE_COUNT INTEGER NOT NULL, FUNCTION_COUNT INTEGER NOT NULL DEFAULT 0,
                FUNCTIONS_JSON TEXT NOT NULL DEFAULT '[]', CROSS_SHEET_REFERENCE INTEGER NOT NULL DEFAULT 0,
                EXTERNAL_REFERENCE INTEGER NOT NULL DEFAULT 0, VOLATILE INTEGER NOT NULL DEFAULT 0,
                OCCURRENCE_OBJECT_HASH TEXT
            );

            CREATE TABLE IF NOT EXISTS EUC_FORMULA_OCCURRENCES (
                OCCURRENCE_ID TEXT PRIMARY KEY, ANALYSIS_ID TEXT NOT NULL, PATTERN_ID TEXT NOT NULL,
                SHEET_ID TEXT NOT NULL, ROW_ID TEXT, COLUMN_ID TEXT, CELL_ADDRESS TEXT NOT NULL,
                RAW_FORMULA TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS EUC_OBJECT_INVENTORY (
                INVENTORY_ID TEXT PRIMARY KEY, ANALYSIS_ID TEXT NOT NULL, SHEET_ID TEXT,
                OBJECT_TYPE TEXT NOT NULL, OBJECT_NAME TEXT, CELL_OR_RANGE TEXT,
                DETAILS_JSON TEXT NOT NULL DEFAULT '{}', OBJECT_HASH TEXT
            );

            CREATE TABLE IF NOT EXISTS EUC_EXTERNAL_LINKS (
                LINK_ID TEXT PRIMARY KEY, ANALYSIS_ID TEXT NOT NULL, SOURCE_EUC_REFERENCE TEXT,
                SOURCE_SHEET TEXT, SOURCE_ADDRESS TEXT, TARGET_SHEET_ID TEXT, TARGET_CELL TEXT,
                RELATIONSHIP_TYPE TEXT NOT NULL, RESOLUTION_STATUS TEXT NOT NULL DEFAULT 'UNRESOLVED'
            );

            CREATE TABLE IF NOT EXISTS EUC_CONNECTIONS (
                CONNECTION_ID TEXT PRIMARY KEY, ANALYSIS_ID TEXT NOT NULL, CONNECTION_NAME TEXT,
                CONNECTION_TYPE TEXT, PROVIDER TEXT, SERVER_NAME TEXT, DATABASE_NAME TEXT,
                CREDENTIAL_PRESENT INTEGER NOT NULL DEFAULT 0, DETAILS_JSON TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS EUC_WARNINGS (
                WARNING_ID TEXT PRIMARY KEY, ANALYSIS_ID TEXT NOT NULL, WARNING_CODE TEXT NOT NULL,
                SEVERITY TEXT NOT NULL, SHEET_ID TEXT, MESSAGE TEXT NOT NULL,
                DETAILS_JSON TEXT NOT NULL DEFAULT '{}'
            );

            -- Virtual Run: governed extraction/execution of VBA macros against a
            -- repository, independent of the repository's own semantic (cell-level)
            -- version control. MACRO_SOURCE_FILES holds the current registered
            -- .xlsm bytes for a repository (macros are not part of the branch/commit
            -- model — a workbook's macro project is a single opaque artifact, not
            -- per-cell data). Re-registering replaces the row; history is kept via
            -- the content-addressed object store itself (old bytes are never deleted
            -- while referenced).
            CREATE TABLE IF NOT EXISTS MACRO_SOURCE_FILES (
                REPOSITORY_ID TEXT PRIMARY KEY,
                ORIGINAL_FILENAME TEXT NOT NULL,
                SOURCE_OBJECT_HASH TEXT NOT NULL,
                FILE_HASH TEXT NOT NULL,
                SIZE_BYTES INTEGER NOT NULL,
                REGISTERED_BY TEXT NOT NULL,
                REGISTERED_AT TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS MACRO_EXTRACTION_RUNS (
                EXTRACTION_RUN_ID TEXT PRIMARY KEY,
                REPOSITORY_ID TEXT NOT NULL,
                SOURCE_FILE_HASH TEXT NOT NULL,
                STATUS TEXT NOT NULL DEFAULT 'PENDING' CHECK (STATUS IN ('PENDING','RUNNING','COMPLETED','FAILED')),
                MACRO_COUNT INTEGER NOT NULL DEFAULT 0,
                RUNNABLE_COUNT INTEGER NOT NULL DEFAULT 0,
                ERROR_MESSAGE TEXT,
                CREATED_AT TEXT NOT NULL,
                COMPLETED_AT TEXT,
                UNIQUE(REPOSITORY_ID, SOURCE_FILE_HASH)
            );

            CREATE TABLE IF NOT EXISTS MACRO_DEFINITIONS (
                MACRO_ID TEXT PRIMARY KEY,
                EXTRACTION_RUN_ID TEXT NOT NULL,
                REPOSITORY_ID TEXT NOT NULL,
                MODULE_NAME TEXT NOT NULL,
                PROC_NAME TEXT NOT NULL,
                SOURCE_OBJECT_HASH TEXT NOT NULL,
                SOURCE_LINE_COUNT INTEGER NOT NULL,
                IS_AUTO_EXEC INTEGER NOT NULL DEFAULT 0,
                HAS_PARAMETERS INTEGER NOT NULL DEFAULT 0,
                STATIC_RISK TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (STATIC_RISK IN ('PENDING','RUNNABLE','BLOCKED_EXTERNAL','BLOCKED_UNSUPPORTED')),
                BLOCK_REASONS_JSON TEXT NOT NULL DEFAULT '[]',
                EXECUTION_LANE TEXT CHECK (EXECUTION_LANE IN ('SQL','INTERPRETED')),
                PARSED_AST_OBJECT_HASH TEXT,
                PARSER_VERSION TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL,
                FOREIGN KEY (EXTRACTION_RUN_ID) REFERENCES MACRO_EXTRACTION_RUNS(EXTRACTION_RUN_ID)
            );
            CREATE INDEX IF NOT EXISTS IDX_MACRO_DEFINITIONS_REPO ON MACRO_DEFINITIONS(REPOSITORY_ID, STATIC_RISK);

            CREATE TABLE IF NOT EXISTS MACRO_RUNS (
                RUN_ID TEXT PRIMARY KEY,
                MACRO_ID TEXT NOT NULL,
                REPOSITORY_ID TEXT NOT NULL,
                BRANCH_ID TEXT NOT NULL,
                REQUESTED_BY_USER_ID TEXT NOT NULL,
                STATUS TEXT NOT NULL DEFAULT 'PENDING_CONFIRMATION'
                    CHECK (STATUS IN ('PENDING_CONFIRMATION','CONFIRMED','EXECUTED','FAILED','REJECTED','EXPIRED')),
                EXPECTED_HEAD_COMMIT_ID TEXT NOT NULL,
                PREVIEW_OBJECT_HASH TEXT NOT NULL,
                PREVIEW_CHANGE_COUNT INTEGER NOT NULL,
                EXECUTION_LANE TEXT NOT NULL CHECK (EXECUTION_LANE IN ('SQL','INTERPRETED')),
                STATEMENTS_EXECUTED INTEGER,
                COMMIT_MESSAGE TEXT NOT NULL,
                IDEMPOTENCY_KEY TEXT NOT NULL UNIQUE,
                EXPIRES_AT TEXT NOT NULL,
                CONFIRMED_BY TEXT,
                CONFIRMED_AT TEXT,
                COMMIT_ID TEXT,
                ERROR_CODE TEXT,
                ERROR_MESSAGE TEXT,
                CREATED_AT TEXT NOT NULL,
                FOREIGN KEY (MACRO_ID) REFERENCES MACRO_DEFINITIONS(MACRO_ID)
            );
            CREATE INDEX IF NOT EXISTS IDX_MACRO_RUNS_REPO ON MACRO_RUNS(REPOSITORY_ID, STATUS);
            CREATE INDEX IF NOT EXISTS IDX_MACRO_RUNS_MACRO ON MACRO_RUNS(MACRO_ID, CREATED_AT);

            -- Phase 5: flags when a re-uploaded workbook's macro logic
            -- actually changed (not just that a new extraction ran) --
            -- one row per (macro_id) that had a prior version, computed at
            -- extraction time by run_service.extract().
            CREATE TABLE IF NOT EXISTS MACRO_DRIFT_EVENTS (
                DRIFT_ID TEXT PRIMARY KEY,
                MACRO_ID TEXT NOT NULL,
                PREVIOUS_MACRO_ID TEXT NOT NULL,
                REPOSITORY_ID TEXT NOT NULL,
                MODULE_NAME TEXT NOT NULL,
                PROC_NAME TEXT NOT NULL,
                PREVIOUS_STATIC_RISK TEXT NOT NULL,
                NEW_STATIC_RISK TEXT NOT NULL,
                PREVIOUS_EXECUTION_LANE TEXT,
                NEW_EXECUTION_LANE TEXT,
                RISK_CHANGED INTEGER NOT NULL DEFAULT 0,
                DIFF_JSON TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL,
                FOREIGN KEY (MACRO_ID) REFERENCES MACRO_DEFINITIONS(MACRO_ID)
            );
            CREATE INDEX IF NOT EXISTS IDX_MACRO_DRIFT_MACRO ON MACRO_DRIFT_EVENTS(MACRO_ID);

            -- Phase 5 USP1: AI Macro Explainer's durable cache, mirroring
            -- FORMULA_EXPLANATIONS' shape. Keyed by (repository, module,
            -- proc, source hash) -- not macro_id, since re-extraction mints
            -- a new macro_id for an unchanged proc and re-explaining
            -- unchanged source would be wasted AI cost.
            CREATE TABLE IF NOT EXISTS MACRO_EXPLANATIONS (
                EXPLANATION_ID TEXT PRIMARY KEY,
                REPOSITORY_ID TEXT NOT NULL,
                MODULE_NAME TEXT NOT NULL,
                PROC_NAME TEXT NOT NULL,
                SOURCE_OBJECT_HASH TEXT NOT NULL,
                SUMMARY TEXT NOT NULL,
                STEP_BY_STEP_JSON TEXT NOT NULL DEFAULT '[]',
                RISK_NOTE TEXT,
                CONFIDENCE REAL NOT NULL DEFAULT 0,
                WARNINGS_JSON TEXT NOT NULL DEFAULT '[]',
                AI_REQUEST_ID TEXT,
                CREATED_BY TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL,
                UNIQUE(REPOSITORY_ID, MODULE_NAME, PROC_NAME, SOURCE_OBJECT_HASH)
            );

            -- Phase 5 USP3: a saved, reusable one-click action. Pure
            -- metadata -- running one resolves the CURRENT runnable
            -- MACRO_DEFINITIONS row for (module_name, proc_name) and calls
            -- the unmodified run_service.prepare_run(), reusing every
            -- governance rail rather than a second execution engine.
            -- Keyed by (module_name, proc_name), not macro_id, for the
            -- same reason MACRO_DRIFT_EVENTS is: macro_id isn't stable
            -- across re-extractions.
            CREATE TABLE IF NOT EXISTS MACRO_RECIPES (
                RECIPE_ID TEXT PRIMARY KEY,
                REPOSITORY_ID TEXT NOT NULL,
                MODULE_NAME TEXT NOT NULL,
                PROC_NAME TEXT NOT NULL,
                NAME TEXT NOT NULL,
                DESCRIPTION TEXT,
                CREATED_BY TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL,
                LAST_RUN_AT TEXT,
                RUN_COUNT INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS IDX_MACRO_RECIPES_REPO ON MACRO_RECIPES(REPOSITORY_ID);

            -- Phase 5 USP4: Continuous Macro Assurance -- re-checked after
            -- every merge to main (see app.macros.continuous_assurance),
            -- mirroring app.euc.continuous_assurance.rescore_repository_
            -- after_merge exactly (opt-in only, one row per check).
            CREATE TABLE IF NOT EXISTS MACRO_ASSURANCE_CHECKS (
                CHECK_ID TEXT PRIMARY KEY,
                REPOSITORY_ID TEXT NOT NULL,
                MACRO_ID TEXT NOT NULL,
                MODULE_NAME TEXT NOT NULL,
                PROC_NAME TEXT NOT NULL,
                BRANCH_ID TEXT NOT NULL,
                STATUS TEXT NOT NULL CHECK (STATUS IN ('OK','STALE')),
                STALE_REASON TEXT,
                CHECKED_AT TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS IDX_MACRO_ASSURANCE_REPO ON MACRO_ASSURANCE_CHECKS(REPOSITORY_ID, CHECKED_AT DESC);

            CREATE TABLE IF NOT EXISTS EUC_DEPENDENCY_RUNS (
                DEPENDENCY_RUN_ID TEXT PRIMARY KEY, ANALYSIS_ID TEXT NOT NULL,
                EUC_ID TEXT NOT NULL, SOURCE_COMMIT_ID TEXT,
                ENGINE_VERSION TEXT NOT NULL, STATUS TEXT NOT NULL,
                PROGRESS INTEGER NOT NULL DEFAULT 0, CURRENT_STEP TEXT,
                NODE_COUNT INTEGER NOT NULL DEFAULT 0, EDGE_COUNT INTEGER NOT NULL DEFAULT 0,
                LOGICAL_EDGE_COUNT INTEGER NOT NULL DEFAULT 0,
                CYCLE_COUNT INTEGER NOT NULL DEFAULT 0, UNRESOLVED_COUNT INTEGER NOT NULL DEFAULT 0,
                DYNAMIC_COUNT INTEGER NOT NULL DEFAULT 0, BROKEN_COUNT INTEGER NOT NULL DEFAULT 0,
                GRAPH_MANIFEST_HASH TEXT, UPSTREAM_INDEX_HASH TEXT, DOWNSTREAM_INDEX_HASH TEXT,
                METRICS_JSON TEXT NOT NULL DEFAULT '{}', WARNINGS_JSON TEXT NOT NULL DEFAULT '[]',
                STARTED_AT TEXT NOT NULL, COMPLETED_AT TEXT, DURATION_MS INTEGER
            );

            CREATE TABLE IF NOT EXISTS EUC_FORMULA_ASTS (
                DEPENDENCY_RUN_ID TEXT NOT NULL, PATTERN_ID TEXT NOT NULL,
                NORMALIZED_HASH TEXT NOT NULL, AST_OBJECT_HASH TEXT NOT NULL,
                AST_DEPTH INTEGER NOT NULL DEFAULT 0, FUNCTION_COUNT INTEGER NOT NULL DEFAULT 0,
                REFERENCE_COUNT INTEGER NOT NULL DEFAULT 0, RANGE_COUNT INTEGER NOT NULL DEFAULT 0,
                DYNAMIC_REFERENCE_COUNT INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (DEPENDENCY_RUN_ID, PATTERN_ID)
            );

            CREATE TABLE IF NOT EXISTS EUC_DEPENDENCY_NODES (
                DEPENDENCY_RUN_ID TEXT NOT NULL, NODE_ID TEXT NOT NULL, NODE_TYPE TEXT NOT NULL,
                SHEET_ID TEXT, ROW_ID TEXT, COLUMN_ID TEXT, CELL_ADDRESS TEXT,
                DISPLAY_NAME TEXT, UPSTREAM_COUNT INTEGER NOT NULL DEFAULT 0,
                DOWNSTREAM_COUNT INTEGER NOT NULL DEFAULT 0, DIRECT_UPSTREAM_COUNT INTEGER NOT NULL DEFAULT 0,
                DIRECT_DOWNSTREAM_COUNT INTEGER NOT NULL DEFAULT 0, MAX_UPSTREAM_DEPTH INTEGER NOT NULL DEFAULT 0,
                MAX_DOWNSTREAM_DEPTH INTEGER NOT NULL DEFAULT 0, SHEET_SPREAD INTEGER NOT NULL DEFAULT 0,
                HUB_SCORE REAL NOT NULL DEFAULT 0, TECHNICAL_CRITICALITY REAL NOT NULL DEFAULT 0,
                NODE_ROLE TEXT NOT NULL DEFAULT 'SOURCE', COMPONENT_ID TEXT,
                METADATA_JSON TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (DEPENDENCY_RUN_ID, NODE_ID)
            );

            CREATE TABLE IF NOT EXISTS EUC_DEPENDENCY_EDGES (
                EDGE_ID TEXT PRIMARY KEY, DEPENDENCY_RUN_ID TEXT NOT NULL, ANALYSIS_ID TEXT NOT NULL,
                SOURCE_NODE_ID TEXT NOT NULL, SOURCE_NODE_TYPE TEXT NOT NULL,
                TARGET_NODE_ID TEXT NOT NULL, TARGET_NODE_TYPE TEXT NOT NULL,
                DEPENDENCY_TYPE TEXT NOT NULL, DEPENDENCY_CATEGORY TEXT NOT NULL,
                RESOLUTION_STATUS TEXT NOT NULL, LOGICAL_CARDINALITY INTEGER NOT NULL DEFAULT 1,
                METADATA_JSON TEXT NOT NULL DEFAULT '{}', CREATED_AT TEXT NOT NULL,
                UNIQUE(DEPENDENCY_RUN_ID,SOURCE_NODE_ID,TARGET_NODE_ID,DEPENDENCY_TYPE,DEPENDENCY_CATEGORY)
            );

            CREATE TABLE IF NOT EXISTS EUC_DEPENDENCY_BLOCKS (
                BLOCK_ID TEXT PRIMARY KEY, DEPENDENCY_RUN_ID TEXT NOT NULL,
                SHEET_ID TEXT, DIRECTION TEXT NOT NULL, NODE_COUNT INTEGER NOT NULL,
                EDGE_COUNT INTEGER NOT NULL, OBJECT_HASH TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS EUC_DEPENDENCY_CYCLES (
                CYCLE_ID TEXT PRIMARY KEY, DEPENDENCY_RUN_ID TEXT NOT NULL,
                CYCLE_SIZE INTEGER NOT NULL, SEVERITY TEXT NOT NULL,
                NODE_MANIFEST_HASH TEXT NOT NULL, SHEETS_JSON TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS EUC_SHEET_DEPENDENCIES (
                DEPENDENCY_RUN_ID TEXT NOT NULL, SOURCE_SHEET_ID TEXT NOT NULL,
                TARGET_SHEET_ID TEXT NOT NULL, EDGE_WEIGHT INTEGER NOT NULL,
                SOURCE_ROLE TEXT, TARGET_ROLE TEXT,
                PRIMARY KEY (DEPENDENCY_RUN_ID,SOURCE_SHEET_ID,TARGET_SHEET_ID)
            );

            CREATE TABLE IF NOT EXISTS EUC_DEPENDENCY_ALIASES (
                ALIAS_ID TEXT PRIMARY KEY, SOURCE_EUC_ID TEXT NOT NULL,
                EXTERNAL_REFERENCE TEXT NOT NULL, TARGET_EUC_ID TEXT,
                STATUS TEXT NOT NULL DEFAULT 'UNRESOLVED', CONFIRMED_BY TEXT,
                CREATED_AT TEXT NOT NULL, UPDATED_AT TEXT NOT NULL,
                UNIQUE(SOURCE_EUC_ID,EXTERNAL_REFERENCE)
            );

            CREATE TABLE IF NOT EXISTS EUC_SCORING_PROFILES (
                PROFILE_ID TEXT PRIMARY KEY, PROFILE_NAME TEXT NOT NULL,
                PROFILE_VERSION TEXT NOT NULL, DESCRIPTION TEXT,
                WEIGHTS_JSON TEXT NOT NULL, THRESHOLDS_JSON TEXT NOT NULL,
                STATUS TEXT NOT NULL DEFAULT 'ACTIVE', CREATED_AT TEXT NOT NULL,
                UPDATED_AT TEXT NOT NULL,
                UNIQUE(PROFILE_NAME,PROFILE_VERSION)
            );

            CREATE TABLE IF NOT EXISTS EUC_INTELLIGENCE_RUNS (
                INTELLIGENCE_RUN_ID TEXT PRIMARY KEY, EUC_ID TEXT NOT NULL,
                SOURCE_COMMIT_ID TEXT, INVENTORY_ANALYSIS_ID TEXT NOT NULL,
                DEPENDENCY_RUN_ID TEXT NOT NULL, ENGINE_VERSION TEXT NOT NULL,
                RULESET_VERSION TEXT NOT NULL, SCORING_PROFILE TEXT NOT NULL,
                SCORING_PROFILE_VERSION TEXT NOT NULL, STATUS TEXT NOT NULL,
                PROGRESS INTEGER NOT NULL DEFAULT 0, CURRENT_STEP TEXT,
                COMPLEXITY_SCORE REAL NOT NULL DEFAULT 0,
                INHERENT_RISK_SCORE REAL NOT NULL DEFAULT 0,
                CONTROL_SCORE REAL NOT NULL DEFAULT 0,
                RESIDUAL_RISK_SCORE REAL NOT NULL DEFAULT 0,
                FINDING_COUNT INTEGER NOT NULL DEFAULT 0,
                CRITICAL_FINDING_COUNT INTEGER NOT NULL DEFAULT 0,
                RESULT_MANIFEST_HASH TEXT, FEATURE_MANIFEST_HASH TEXT,
                EXPLANATION_MANIFEST_HASH TEXT, SUMMARY_JSON TEXT NOT NULL DEFAULT '{}',
                WARNINGS_JSON TEXT NOT NULL DEFAULT '[]', STARTED_AT TEXT NOT NULL,
                COMPLETED_AT TEXT, DURATION_MS INTEGER
            );

            CREATE TABLE IF NOT EXISTS EUC_SCORE_COMPONENTS (
                INTELLIGENCE_RUN_ID TEXT NOT NULL, SCORE_TYPE TEXT NOT NULL,
                DIMENSION TEXT NOT NULL, RAW_SCORE REAL NOT NULL,
                WEIGHT REAL NOT NULL, CONTRIBUTION REAL NOT NULL,
                CLASSIFICATION TEXT NOT NULL, EVIDENCE_OBJECT_HASH TEXT,
                EXPLANATION TEXT NOT NULL,
                PRIMARY KEY (INTELLIGENCE_RUN_ID,SCORE_TYPE,DIMENSION)
            );

            CREATE TABLE IF NOT EXISTS EUC_CONTROL_INVENTORY (
                CONTROL_RESULT_ID TEXT PRIMARY KEY, INTELLIGENCE_RUN_ID TEXT NOT NULL,
                CONTROL_CODE TEXT NOT NULL, CONTROL_NAME TEXT NOT NULL,
                CONTROL_CATEGORY TEXT NOT NULL, CONTROL_SOURCE TEXT NOT NULL,
                CONTROL_COUNT INTEGER NOT NULL DEFAULT 0, COVERAGE_SCORE REAL NOT NULL DEFAULT 0,
                WEIGHTED_COVERAGE_SCORE REAL NOT NULL DEFAULT 0,
                EFFECTIVENESS TEXT NOT NULL, EVIDENCE_OBJECT_HASH TEXT,
                UNIQUE(INTELLIGENCE_RUN_ID,CONTROL_CODE)
            );

            CREATE TABLE IF NOT EXISTS EUC_FINDINGS (
                FINDING_ID TEXT PRIMARY KEY, EUC_ID TEXT NOT NULL,
                FINGERPRINT TEXT NOT NULL, RULE_ID TEXT NOT NULL,
                CATEGORY TEXT NOT NULL, TITLE TEXT NOT NULL,
                STATUS TEXT NOT NULL DEFAULT 'OPEN', FIRST_RUN_ID TEXT NOT NULL,
                LAST_RUN_ID TEXT NOT NULL, FIRST_SOURCE_COMMIT_ID TEXT,
                LAST_SOURCE_COMMIT_ID TEXT, RECURRENCE_COUNT INTEGER NOT NULL DEFAULT 0,
                OWNER_USER_ID TEXT, DUE_DATE TEXT, ACCEPTED_UNTIL TEXT,
                CREATED_AT TEXT NOT NULL, UPDATED_AT TEXT NOT NULL,
                UNIQUE(EUC_ID,FINGERPRINT)
            );

            CREATE TABLE IF NOT EXISTS EUC_FINDING_OCCURRENCES (
                INTELLIGENCE_RUN_ID TEXT NOT NULL, FINDING_ID TEXT NOT NULL,
                SEVERITY TEXT NOT NULL, CONFIDENCE REAL NOT NULL,
                NODE_ID TEXT, SHEET_ID TEXT, CELL_ADDRESS TEXT,
                DESCRIPTION TEXT NOT NULL, REMEDIATION_CODE TEXT,
                EVIDENCE_MANIFEST_HASH TEXT NOT NULL,
                DEPENDENCY_IMPACT_JSON TEXT NOT NULL DEFAULT '{}',
                DETECTED_AT TEXT NOT NULL,
                PRIMARY KEY (INTELLIGENCE_RUN_ID,FINDING_ID)
            );

            CREATE TABLE IF NOT EXISTS EUC_FINDING_ACTIONS (
                ACTION_ID TEXT PRIMARY KEY, FINDING_ID TEXT NOT NULL,
                ACTION_TYPE TEXT NOT NULL, PREVIOUS_STATUS TEXT NOT NULL,
                NEW_STATUS TEXT NOT NULL, REASON TEXT NOT NULL,
                ACTOR_USER_ID TEXT NOT NULL, SOURCE_COMMIT_ID TEXT,
                EXPIRES_AT TEXT, CREATED_AT TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS EUC_FINDING_REMEDIATIONS (
                REMEDIATION_ID TEXT PRIMARY KEY, FINDING_ID TEXT NOT NULL, EUC_ID TEXT NOT NULL,
                RECOMMENDED_STATUS TEXT NOT NULL, REASON TEXT NOT NULL, CONFIDENCE REAL NOT NULL,
                RISK_LEVEL TEXT NOT NULL, AI_REQUEST_ID TEXT,
                STATUS TEXT NOT NULL DEFAULT 'PROPOSED', CREATED_BY TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL, APPLIED_AT TEXT
            );

            CREATE TABLE IF NOT EXISTS EUC_ATTESTATIONS (
                ATTESTATION_ID TEXT PRIMARY KEY, EUC_ID TEXT NOT NULL, REPOSITORY_ID TEXT NOT NULL,
                STATEMENT TEXT NOT NULL, OPEN_FINDING_COUNT INTEGER NOT NULL DEFAULT 0,
                OPEN_CRITICAL_HIGH_COUNT INTEGER NOT NULL DEFAULT 0, RESIDUAL_RISK REAL,
                SOURCE_COMMIT_ID TEXT, SUBMITTED_BY TEXT NOT NULL, SUBMITTED_AT TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS EUC_MIGRATION_RUNS (
                MIGRATION_RUN_ID TEXT PRIMARY KEY, EUC_ID TEXT NOT NULL,
                SOURCE_COMMIT_ID TEXT, INVENTORY_ANALYSIS_ID TEXT NOT NULL,
                DEPENDENCY_RUN_ID TEXT NOT NULL, INTELLIGENCE_RUN_ID TEXT NOT NULL,
                ENGINE_VERSION TEXT NOT NULL, RULESET_VERSION TEXT NOT NULL,
                STATUS TEXT NOT NULL, PROGRESS INTEGER NOT NULL DEFAULT 0,
                CURRENT_STEP TEXT, READINESS_SCORE REAL NOT NULL DEFAULT 0,
                READINESS_CLASSIFICATION TEXT, RECOMMENDED_STRATEGY TEXT,
                AUTO_PERCENT REAL NOT NULL DEFAULT 0, ASSISTED_PERCENT REAL NOT NULL DEFAULT 0,
                MANUAL_PERCENT REAL NOT NULL DEFAULT 0, RETAIN_PERCENT REAL NOT NULL DEFAULT 0,
                UNSUPPORTED_PERCENT REAL NOT NULL DEFAULT 0, RETIRE_PERCENT REAL NOT NULL DEFAULT 0,
                EFFORT_CLASS TEXT, UNIT_COUNT INTEGER NOT NULL DEFAULT 0,
                BLOCKER_COUNT INTEGER NOT NULL DEFAULT 0, CRITICAL_BLOCKER_COUNT INTEGER NOT NULL DEFAULT 0,
                BLUEPRINT_MANIFEST_HASH TEXT, FEATURE_MANIFEST_HASH TEXT,
                SUMMARY_JSON TEXT NOT NULL DEFAULT '{}', WARNINGS_JSON TEXT NOT NULL DEFAULT '[]',
                STARTED_AT TEXT NOT NULL, COMPLETED_AT TEXT, DURATION_MS INTEGER
            );

            CREATE TABLE IF NOT EXISTS EUC_MIGRATION_UNITS (
                MIGRATION_RUN_ID TEXT NOT NULL, UNIT_ID TEXT NOT NULL,
                PARENT_UNIT_ID TEXT, SOURCE_TYPE TEXT NOT NULL, SOURCE_ID TEXT NOT NULL,
                SOURCE_NAME TEXT NOT NULL, SHEET_ID TEXT, DOMAIN_ID TEXT,
                ENGINE_MODE TEXT NOT NULL, DIFFICULTY TEXT NOT NULL,
                MIGRATION_WEIGHT REAL NOT NULL, TARGET_TYPE TEXT NOT NULL,
                TARGET_COMPONENT TEXT, WAVE_NUMBER INTEGER,
                RATIONALE TEXT NOT NULL, EVIDENCE_OBJECT_HASH TEXT,
                PRIMARY KEY (MIGRATION_RUN_ID,UNIT_ID)
            );

            CREATE TABLE IF NOT EXISTS EUC_MIGRATION_BLOCKERS (
                MIGRATION_RUN_ID TEXT NOT NULL, BLOCKER_ID TEXT NOT NULL,
                BLOCKER_TYPE TEXT NOT NULL, CATEGORY TEXT NOT NULL,
                SEVERITY TEXT NOT NULL, AFFECTED_UNIT_ID TEXT,
                AFFECTED_COMPONENT TEXT NOT NULL, MIGRATION_EFFECT TEXT NOT NULL,
                REMEDIATION TEXT NOT NULL, EFFORT_CLASS TEXT NOT NULL,
                DEPENDENCY_IMPACT_JSON TEXT NOT NULL DEFAULT '{}',
                EVIDENCE_OBJECT_HASH TEXT, PRIMARY KEY (MIGRATION_RUN_ID,BLOCKER_ID)
            );

            CREATE TABLE IF NOT EXISTS EUC_MIGRATION_WAVES (
                MIGRATION_RUN_ID TEXT NOT NULL, WAVE_NUMBER INTEGER NOT NULL,
                WAVE_NAME TEXT NOT NULL, OBJECTIVE TEXT NOT NULL,
                UNIT_COUNT INTEGER NOT NULL DEFAULT 0, EFFORT_CLASS TEXT NOT NULL,
                DEPENDS_ON_JSON TEXT NOT NULL DEFAULT '[]', UNIT_IDS_OBJECT_HASH TEXT,
                PRIMARY KEY (MIGRATION_RUN_ID,WAVE_NUMBER)
            );

            CREATE TABLE IF NOT EXISTS EUC_TARGET_MAPPINGS (
                MIGRATION_RUN_ID TEXT NOT NULL, MAPPING_ID TEXT NOT NULL,
                UNIT_ID TEXT NOT NULL, SOURCE_TYPE TEXT NOT NULL,
                TARGET_TYPE TEXT NOT NULL, TARGET_COMPONENT TEXT NOT NULL,
                MAPPING_PATTERN TEXT NOT NULL, CONFIG_JSON TEXT NOT NULL DEFAULT '{}',
                EVIDENCE_OBJECT_HASH TEXT, PRIMARY KEY (MIGRATION_RUN_ID,MAPPING_ID)
            );

            CREATE TABLE IF NOT EXISTS EUC_CONTROL_MAPPINGS (
                MIGRATION_RUN_ID TEXT NOT NULL, CONTROL_MAPPING_ID TEXT NOT NULL,
                SOURCE_CONTROL_CODE TEXT NOT NULL, SOURCE_CONTROL_NAME TEXT NOT NULL,
                TARGET_CONTROL TEXT NOT NULL, PRESERVATION_STATUS TEXT NOT NULL,
                RATIONALE TEXT NOT NULL, TEST_REQUIRED INTEGER NOT NULL DEFAULT 1,
                EVIDENCE_OBJECT_HASH TEXT, PRIMARY KEY (MIGRATION_RUN_ID,CONTROL_MAPPING_ID)
            );

            CREATE TABLE IF NOT EXISTS EUC_VALIDATION_PLANS (
                MIGRATION_RUN_ID TEXT NOT NULL, VALIDATION_ID TEXT NOT NULL,
                UNIT_ID TEXT, VALIDATION_TYPE TEXT NOT NULL,
                SOURCE_OUTPUT TEXT NOT NULL, TARGET_OUTPUT TEXT NOT NULL,
                COMPARISON_METHOD TEXT NOT NULL, ABSOLUTE_TOLERANCE REAL,
                RELATIVE_TOLERANCE REAL, PRIORITY TEXT NOT NULL,
                HISTORICAL_REPLAY_COUNT INTEGER NOT NULL DEFAULT 0,
                ACCEPTANCE_CRITERIA TEXT NOT NULL,
                PRIMARY KEY (MIGRATION_RUN_ID,VALIDATION_ID)
            );

            CREATE TABLE IF NOT EXISTS EUC_MIGRATION_OVERRIDES (
                OVERRIDE_ID TEXT PRIMARY KEY, EUC_ID TEXT NOT NULL,
                UNIT_ID TEXT NOT NULL, MIGRATION_RUN_ID TEXT NOT NULL,
                ENGINE_MODE TEXT NOT NULL, MANUAL_MODE TEXT NOT NULL,
                REASON TEXT NOT NULL, ACTOR_USER_ID TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL, REVOKED_AT TEXT
            );

            CREATE TABLE IF NOT EXISTS EUC_APPLICATION_MODELS (
                APPLICATION_MODEL_ID TEXT PRIMARY KEY, EUC_ID TEXT NOT NULL,
                SOURCE_COMMIT_ID TEXT, MIGRATION_RUN_ID TEXT NOT NULL,
                AIR_VERSION TEXT NOT NULL, TARGET_PROFILE TEXT NOT NULL,
                STATUS TEXT NOT NULL, RAW_COVERAGE REAL NOT NULL DEFAULT 0,
                CRITICALITY_WEIGHTED_COVERAGE REAL NOT NULL DEFAULT 0,
                MIGRATION_CONFIDENCE REAL NOT NULL DEFAULT 0,
                REVIEW_REQUIRED_COUNT INTEGER NOT NULL DEFAULT 0,
                VALIDATION_ERROR_COUNT INTEGER NOT NULL DEFAULT 0,
                MANIFEST_HASH TEXT NOT NULL, SUMMARY_JSON TEXT NOT NULL DEFAULT '{}',
                CREATED_BY TEXT NOT NULL, CREATED_AT TEXT NOT NULL,
                APPROVED_BY TEXT, APPROVED_AT TEXT, SUPERSEDED_AT TEXT
            );

            CREATE TABLE IF NOT EXISTS EUC_APPLICATION_COMPONENTS (
                APPLICATION_MODEL_ID TEXT NOT NULL, COMPONENT_ID TEXT NOT NULL,
                COMPONENT_TYPE TEXT NOT NULL, NAME TEXT NOT NULL,
                DOMAIN_ID TEXT, CONFIDENCE REAL NOT NULL,
                GENERATION_POLICY TEXT NOT NULL, REVIEW_STATE TEXT NOT NULL,
                SOURCE_UNIT_ID TEXT, PROVENANCE_JSON TEXT NOT NULL DEFAULT '{}',
                OBJECT_HASH TEXT NOT NULL,
                PRIMARY KEY (APPLICATION_MODEL_ID,COMPONENT_ID)
            );

            CREATE TABLE IF NOT EXISTS EUC_APPLICATION_REVIEWS (
                REVIEW_ID TEXT PRIMARY KEY, APPLICATION_MODEL_ID TEXT NOT NULL,
                COMPONENT_ID TEXT NOT NULL, DECISION TEXT NOT NULL,
                REASON TEXT NOT NULL, ACTOR_USER_ID TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL,
                UNIQUE(APPLICATION_MODEL_ID,COMPONENT_ID)
            );

            CREATE TABLE IF NOT EXISTS EUC_APPLICATION_GENERATION_RUNS (
                GENERATION_RUN_ID TEXT PRIMARY KEY, APPLICATION_MODEL_ID TEXT NOT NULL,
                GENERATOR_VERSION TEXT NOT NULL, TARGET_PROFILE TEXT NOT NULL,
                GENERATION_MODE TEXT NOT NULL, SOURCE_MANIFEST_HASH TEXT NOT NULL,
                OUTPUT_MANIFEST_HASH TEXT, BUNDLE_OBJECT_HASH TEXT,
                APPLICATION_VERSION TEXT, STATUS TEXT NOT NULL,
                FILES_GENERATED INTEGER NOT NULL DEFAULT 0,
                TESTS_GENERATED INTEGER NOT NULL DEFAULT 0,
                STARTED_BY TEXT NOT NULL, STARTED_AT TEXT NOT NULL,
                COMPLETED_AT TEXT, ERROR_MESSAGE TEXT
            );

            CREATE TABLE IF NOT EXISTS EUC_APPLICATION_LINEAGE (
                APPLICATION_MODEL_ID TEXT NOT NULL, LINEAGE_ID TEXT NOT NULL,
                SOURCE_TYPE TEXT NOT NULL, SOURCE_ID TEXT NOT NULL,
                SOURCE_LOCATION TEXT, TARGET_TYPE TEXT NOT NULL,
                TARGET_ID TEXT NOT NULL, TARGET_PATH TEXT,
                RELATIONSHIP_TYPE TEXT NOT NULL, CONFIDENCE REAL NOT NULL,
                PRIMARY KEY (APPLICATION_MODEL_ID,LINEAGE_ID)
            );

            CREATE TABLE IF NOT EXISTS TASKS (
                TASK_ID TEXT PRIMARY KEY,
                REPOSITORY_ID TEXT NOT NULL,
                BRANCH_ID TEXT,
                ASSIGNED_TO_USER_ID TEXT,
                TITLE TEXT NOT NULL,
                DESCRIPTION TEXT,
                STATUS TEXT NOT NULL DEFAULT 'OPEN' CHECK (STATUS IN ('OPEN','IN_PROGRESS','DONE')),
                CREATED_BY TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL,
                DUE_AT TEXT,
                FOREIGN KEY (REPOSITORY_ID) REFERENCES WORKBOOK_REPOSITORIES(REPOSITORY_ID),
                FOREIGN KEY (BRANCH_ID) REFERENCES BRANCHES(BRANCH_ID),
                FOREIGN KEY (ASSIGNED_TO_USER_ID) REFERENCES APP_USERS(USER_ID),
                FOREIGN KEY (CREATED_BY) REFERENCES APP_USERS(USER_ID)
            );

            CREATE TABLE IF NOT EXISTS DEVICE_FINGERPRINTS (
                FINGERPRINT_ID TEXT PRIMARY KEY,
                USER_ID TEXT NOT NULL,
                SESSION_ID TEXT,
                IP_ADDRESS TEXT,
                WIFI_SSID TEXT,
                MACHINE_ID TEXT,
                USER_AGENT TEXT,
                TRUST_STATUS TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (TRUST_STATUS IN ('TRUSTED','UNKNOWN','BLOCKED')),
                FIRST_SEEN_AT TEXT NOT NULL,
                LAST_SEEN_AT TEXT NOT NULL,
                FOREIGN KEY (USER_ID) REFERENCES APP_USERS(USER_ID),
                FOREIGN KEY (SESSION_ID) REFERENCES AUTH_SESSIONS(SESSION_ID)
            );

            CREATE TABLE IF NOT EXISTS USER_DAILY_ACTIVITY (
                USER_ID TEXT NOT NULL,
                ACTIVITY_DATE TEXT NOT NULL,
                FIRST_SEEN_AT TEXT NOT NULL,
                LAST_SEEN_AT TEXT NOT NULL,
                HEARTBEAT_COUNT INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (USER_ID, ACTIVITY_DATE),
                FOREIGN KEY (USER_ID) REFERENCES APP_USERS(USER_ID)
            );

            CREATE TABLE IF NOT EXISTS REPOSITORY_DAILY_ACTIVITY (
                USER_ID TEXT NOT NULL,
                REPOSITORY_ID TEXT NOT NULL,
                ACTIVITY_DATE TEXT NOT NULL,
                FIRST_SEEN_AT TEXT NOT NULL,
                LAST_SEEN_AT TEXT NOT NULL,
                ACTIVE_SECONDS INTEGER NOT NULL DEFAULT 0,
                HEARTBEAT_COUNT INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (USER_ID, REPOSITORY_ID, ACTIVITY_DATE)
            );

            CREATE TABLE IF NOT EXISTS NOTIFICATIONS (
                NOTIFICATION_ID TEXT PRIMARY KEY,
                USER_ID TEXT NOT NULL,
                ORGANIZATION_ID TEXT,
                TYPE TEXT NOT NULL,
                TITLE TEXT NOT NULL,
                BODY TEXT,
                RESOURCE_TYPE TEXT,
                RESOURCE_ID TEXT,
                READ_AT TEXT,
                CREATED_AT TEXT NOT NULL,
                FOREIGN KEY (USER_ID) REFERENCES APP_USERS(USER_ID)
            );

            CREATE TABLE IF NOT EXISTS NOTIFICATION_PREFERENCES (
                USER_ID TEXT NOT NULL,
                TYPE TEXT NOT NULL,
                ENABLED INTEGER NOT NULL DEFAULT 1,
                UPDATED_AT TEXT NOT NULL,
                PRIMARY KEY (USER_ID, TYPE)
            );

            CREATE TABLE IF NOT EXISTS MERGE_RESOLUTION_KNOWLEDGE (
                DOC_ID TEXT PRIMARY KEY,
                CONFLICT_TYPE TEXT NOT NULL,
                TITLE TEXT NOT NULL,
                SCENARIO_TEXT TEXT NOT NULL,
                RECOMMENDED_RESOLUTION TEXT NOT NULL
                    CHECK (RECOMMENDED_RESOLUTION IN ('KEEP_MAIN','ACCEPT_BRANCH','CUSTOM','MANUAL_REVIEW')),
                RATIONALE TEXT NOT NULL,
                RISK_LEVEL TEXT NOT NULL DEFAULT 'LOW' CHECK (RISK_LEVEL IN ('LOW','MEDIUM','HIGH','CRITICAL')),
                SOURCE TEXT NOT NULL DEFAULT 'SYNTHETIC' CHECK (SOURCE IN ('SYNTHETIC','HISTORICAL')),
                REPOSITORY_ID TEXT,
                CREATED_AT TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS MERGE_RESOLUTION_KNOWLEDGE_FTS USING fts5(
                doc_id UNINDEXED, conflict_type, title, scenario_text, rationale
            );

            CREATE TABLE IF NOT EXISTS MERGE_REQUEST_AI_ASSESSMENTS (
                ASSESSMENT_ID TEXT PRIMARY KEY,
                MERGE_REQUEST_ID TEXT NOT NULL,
                RISK_LEVEL TEXT NOT NULL DEFAULT 'MEDIUM' CHECK (RISK_LEVEL IN ('LOW','MEDIUM','HIGH','CRITICAL')),
                RECOMMENDATION TEXT NOT NULL
                    CHECK (RECOMMENDATION IN ('APPROVE','HOLD_FOR_REVIEW','REJECT')),
                SUMMARY TEXT NOT NULL,
                CONFIDENCE REAL NOT NULL DEFAULT 0,
                WARNINGS_JSON TEXT NOT NULL DEFAULT '[]',
                FIELD_INVESTIGATIONS_JSON TEXT NOT NULL DEFAULT '[]',
                RISK_SCORE REAL NOT NULL DEFAULT 0,
                RISK_BREAKDOWN_JSON TEXT NOT NULL DEFAULT '[]',
                AI_REQUEST_ID TEXT,
                CREATED_BY TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL,
                FOREIGN KEY (MERGE_REQUEST_ID) REFERENCES MERGE_REQUESTS(MERGE_REQUEST_ID)
            );

            CREATE TABLE IF NOT EXISTS COMMIT_AI_REVIEWS (
                REVIEW_ID TEXT PRIMARY KEY,
                COMMIT_ID TEXT NOT NULL UNIQUE,
                REPOSITORY_ID TEXT NOT NULL,
                BRANCH_ID TEXT NOT NULL,
                RISK_LEVEL TEXT NOT NULL DEFAULT 'LOW' CHECK (RISK_LEVEL IN ('LOW','MEDIUM','HIGH','CRITICAL')),
                RISK_SCORE REAL NOT NULL DEFAULT 0,
                RISK_BREAKDOWN_JSON TEXT NOT NULL DEFAULT '[]',
                FIELD_INVESTIGATIONS_JSON TEXT NOT NULL DEFAULT '[]',
                RECOMMENDATION TEXT NOT NULL DEFAULT 'LOOKS_GOOD'
                    CHECK (RECOMMENDATION IN ('LOOKS_GOOD','REVIEW_RECOMMENDED')),
                SUMMARY TEXT NOT NULL,
                CONFIDENCE REAL NOT NULL DEFAULT 0,
                WARNINGS_JSON TEXT NOT NULL DEFAULT '[]',
                AI_REQUEST_ID TEXT,
                CREATED_BY TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL,
                FOREIGN KEY (COMMIT_ID) REFERENCES COMMITS(COMMIT_ID)
            );

            CREATE TABLE IF NOT EXISTS MERGE_CONFLICT_AI_SUGGESTIONS (
                SUGGESTION_ID TEXT PRIMARY KEY,
                MERGE_REQUEST_ID TEXT NOT NULL,
                CONFLICT_ID TEXT NOT NULL,
                RESOLUTION_TYPE TEXT NOT NULL
                    CHECK (RESOLUTION_TYPE IN ('KEEP_MAIN','ACCEPT_BRANCH','CUSTOM','MANUAL_REVIEW')),
                CUSTOM_VALUE_JSON TEXT,
                RATIONALE TEXT,
                CONFIDENCE REAL NOT NULL DEFAULT 0,
                RISK_LEVEL TEXT NOT NULL DEFAULT 'LOW' CHECK (RISK_LEVEL IN ('LOW','MEDIUM','HIGH','CRITICAL')),
                PRECEDENTS_JSON TEXT NOT NULL DEFAULT '[]',
                AI_REQUEST_ID TEXT,
                STATUS TEXT NOT NULL DEFAULT 'PROPOSED' CHECK (STATUS IN ('PROPOSED','APPLIED','DISMISSED')),
                CREATED_AT TEXT NOT NULL,
                APPLIED_AT TEXT,
                DISMISSED_AT TEXT,
                FOREIGN KEY (MERGE_REQUEST_ID) REFERENCES MERGE_REQUESTS(MERGE_REQUEST_ID),
                FOREIGN KEY (CONFLICT_ID) REFERENCES MERGE_CONFLICTS(CONFLICT_ID)
            );

            CREATE TABLE IF NOT EXISTS EUC_BRANCH_COMPARISONS (
                COMPARISON_ID TEXT PRIMARY KEY,
                REPOSITORY_ID TEXT NOT NULL,
                BRANCH_ID TEXT NOT NULL,
                MAIN_EUC_ID TEXT NOT NULL,
                BRANCH_EUC_ID TEXT NOT NULL,
                RISK_SCORE_DELTA REAL NOT NULL DEFAULT 0,
                FINDINGS_INTRODUCED_JSON TEXT NOT NULL DEFAULT '[]',
                FINDINGS_RESOLVED_JSON TEXT NOT NULL DEFAULT '[]',
                ATTRIBUTION_JSON TEXT NOT NULL DEFAULT '[]',
                SUMMARY TEXT NOT NULL,
                CONFIDENCE REAL NOT NULL DEFAULT 0,
                WARNINGS_JSON TEXT NOT NULL DEFAULT '[]',
                AI_REQUEST_ID TEXT,
                CREATED_BY TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL,
                FOREIGN KEY (BRANCH_ID) REFERENCES BRANCHES(BRANCH_ID)
            );

            CREATE TABLE IF NOT EXISTS FORMULA_EXPLANATIONS (
                EXPLANATION_ID TEXT PRIMARY KEY,
                REPOSITORY_ID TEXT NOT NULL,
                BRANCH_ID TEXT NOT NULL,
                SHEET_ID TEXT NOT NULL,
                ROW_ID TEXT NOT NULL,
                COLUMN_ID TEXT NOT NULL,
                FORMULA_HASH TEXT NOT NULL,
                FORMULA_TEXT TEXT NOT NULL,
                CELL_ADDRESS TEXT,
                SUMMARY TEXT NOT NULL,
                STEP_BY_STEP_JSON TEXT NOT NULL DEFAULT '[]',
                REFERENCED_CELLS_JSON TEXT NOT NULL DEFAULT '[]',
                RISK_NOTE TEXT,
                CONFIDENCE REAL NOT NULL DEFAULT 0,
                WARNINGS_JSON TEXT NOT NULL DEFAULT '[]',
                AI_REQUEST_ID TEXT,
                CREATED_BY TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL,
                UNIQUE(BRANCH_ID, SHEET_ID, ROW_ID, COLUMN_ID, FORMULA_HASH)
            );

            CREATE TABLE IF NOT EXISTS FORMULA_PATTERN_KNOWLEDGE (
                DOC_ID TEXT PRIMARY KEY,
                FUNCTION_SIGNATURE TEXT NOT NULL,
                TITLE TEXT NOT NULL,
                PATTERN_TEXT TEXT NOT NULL,
                EXPLANATION TEXT NOT NULL,
                SOURCE TEXT NOT NULL DEFAULT 'SYNTHETIC' CHECK (SOURCE IN ('SYNTHETIC','HISTORICAL')),
                CREATED_AT TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS FORMULA_PATTERN_KNOWLEDGE_FTS USING fts5(
                doc_id UNINDEXED, function_signature, title, pattern_text, explanation
            );

            CREATE TABLE IF NOT EXISTS PORTFOLIO_BRIEFINGS (
                BRIEFING_ID TEXT PRIMARY KEY,
                ORGANIZATION_ID TEXT NOT NULL,
                TOTAL_ASSETS INTEGER NOT NULL DEFAULT 0,
                HIGH_RISK_COUNT INTEGER NOT NULL DEFAULT 0,
                CRITICAL_FINDING_TOTAL INTEGER NOT NULL DEFAULT 0,
                AVERAGE_RESIDUAL_RISK REAL NOT NULL DEFAULT 0,
                STALE_COUNT INTEGER NOT NULL DEFAULT 0,
                TOP_RISK_REPOSITORIES_JSON TEXT NOT NULL DEFAULT '[]',
                OBSERVATIONS_JSON TEXT NOT NULL DEFAULT '[]',
                NARRATIVE TEXT,
                CONFIDENCE REAL NOT NULL DEFAULT 0,
                WARNINGS_JSON TEXT NOT NULL DEFAULT '[]',
                AI_REQUEST_ID TEXT,
                CREATED_BY TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS DISCOVERY_RECOMMENDATIONS (
                RECOMMENDATION_ID TEXT PRIMARY KEY,
                REPOSITORY_ID TEXT NOT NULL,
                ASKED_BY TEXT NOT NULL,
                TOPIC TEXT NOT NULL,
                RECOMMENDED_USER_ID TEXT,
                CANDIDATES_JSON TEXT NOT NULL DEFAULT '[]',
                RATIONALE TEXT NOT NULL,
                CONFIDENCE REAL NOT NULL DEFAULT 0,
                WARNINGS_JSON TEXT NOT NULL DEFAULT '[]',
                AI_REQUEST_ID TEXT,
                CREATED_AT TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS RBAC_ANOMALY_FINDINGS (
                FINDING_ID TEXT PRIMARY KEY,
                REPOSITORY_ID TEXT NOT NULL,
                ANOMALY_TYPE TEXT NOT NULL
                    CHECK (ANOMALY_TYPE IN ('DORMANT_GRANT','ROLE_ACTIVITY_MISMATCH','UNTRUSTED_DEVICE_ACTIVE','MULTI_DEVICE_BURST')),
                SUBJECT_USER_ID TEXT,
                SEVERITY TEXT NOT NULL DEFAULT 'MEDIUM' CHECK (SEVERITY IN ('LOW','MEDIUM','HIGH','CRITICAL')),
                EVIDENCE_JSON TEXT NOT NULL DEFAULT '{}',
                EXPLANATION TEXT,
                RECOMMENDED_ACTION TEXT,
                CONFIDENCE REAL NOT NULL DEFAULT 0,
                STATUS TEXT NOT NULL DEFAULT 'OPEN' CHECK (STATUS IN ('OPEN','ACKNOWLEDGED','DISMISSED','RESOLVED')),
                DEDUPLICATION_KEY TEXT NOT NULL,
                AI_REQUEST_ID TEXT,
                DECIDED_BY TEXT,
                DECISION_REASON TEXT,
                FIRST_DETECTED_AT TEXT NOT NULL,
                LAST_DETECTED_AT TEXT NOT NULL,
                DECIDED_AT TEXT,
                UNIQUE(REPOSITORY_ID, DEDUPLICATION_KEY)
            );

            CREATE TABLE IF NOT EXISTS RBAC_ANOMALY_KNOWLEDGE (
                DOC_ID TEXT PRIMARY KEY,
                ANOMALY_TYPE TEXT NOT NULL,
                TITLE TEXT NOT NULL,
                SCENARIO_TEXT TEXT NOT NULL,
                RESOLUTION TEXT NOT NULL,
                RATIONALE TEXT NOT NULL,
                SOURCE TEXT NOT NULL DEFAULT 'SYNTHETIC' CHECK (SOURCE IN ('SYNTHETIC','HISTORICAL')),
                CREATED_AT TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS RBAC_ANOMALY_KNOWLEDGE_FTS USING fts5(
                doc_id UNINDEXED, anomaly_type, title, scenario_text, rationale
            );

            CREATE INDEX IF NOT EXISTS IDX_AUDIT_TABLE_VERSION
                ON AUDIT_COMMITS(TABLE_ID, VERSION DESC);
            CREATE INDEX IF NOT EXISTS IDX_AUDIT_BATCH
                ON AUDIT_COMMITS(BATCH_ID);
            CREATE INDEX IF NOT EXISTS IDX_AUDIT_USER
                ON AUDIT_COMMITS(USER_ID, CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_WORKSPACE_EVENT_TABLE
                ON WORKSPACE_EVENTS(TABLE_ID, REVISION DESC);
            CREATE INDEX IF NOT EXISTS IDX_COMMITS_BRANCH
                ON COMMITS(BRANCH_ID, CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_COMMIT_CHANGES_COMMIT
                ON COMMIT_CHANGES(COMMIT_ID, OPERATION_TYPE);
            CREATE INDEX IF NOT EXISTS IDX_COMMIT_CHANGES_CELL
                ON COMMIT_CHANGES(BRANCH_ID, SHEET_ID, ROW_ID, COLUMN_ID, CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_SHEET_ROWS_POSITION
                ON SHEET_ROWS(BRANCH_ID, SHEET_ID, ROW_POSITION);
            CREATE INDEX IF NOT EXISTS IDX_SHEET_COLUMNS_POSITION
                ON SHEET_COLUMNS(BRANCH_ID, SHEET_ID, COLUMN_POSITION);
            CREATE INDEX IF NOT EXISTS IDX_REPOSITORY_MEMBERS_USER
                ON REPOSITORY_MEMBERS(USER_ID, REPOSITORY_ID);
            CREATE INDEX IF NOT EXISTS IDX_BRANCH_SHEET_TABLES_BRANCH
                ON BRANCH_SHEET_TABLES(BRANCH_ID, IS_PRIMARY DESC);
            CREATE INDEX IF NOT EXISTS IDX_MERGE_REQUEST_REPOSITORY
                ON MERGE_REQUESTS(REPOSITORY_ID, CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_MERGE_CONFLICT_REQUEST
                ON MERGE_CONFLICTS(MERGE_REQUEST_ID, STATUS, CONFLICT_TYPE);
            CREATE INDEX IF NOT EXISTS IDX_VALIDATION_REQUEST
                ON VALIDATION_RUNS(MERGE_REQUEST_ID, STARTED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_AUDIT_EVENTS_REPOSITORY
                ON AUDIT_EVENTS(REPOSITORY_ID, CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_AUDIT_EVENTS_ACTOR
                ON AUDIT_EVENTS(ACTOR_USER_ID, CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_AUDIT_EVENTS_TRACE
                ON AUDIT_EVENTS(TRACE_ID, CREATED_AT);
            CREATE INDEX IF NOT EXISTS IDX_OPERATION_METRICS_NAME
                ON OPERATION_METRICS(METRIC_NAME, CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_SECURITY_EVENTS_CREATED
                ON SECURITY_EVENTS(CREATED_AT DESC, SEVERITY);
            CREATE INDEX IF NOT EXISTS IDX_OBJECTS_TYPE
                ON STORAGE_OBJECTS(OBJECT_TYPE, CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_OBJECT_REFERENCES_CHILD
                ON OBJECT_REFERENCES(CHILD_HASH);
            CREATE INDEX IF NOT EXISTS IDX_COMMIT_MANIFESTS_BRANCH
                ON COMMIT_MANIFESTS(BRANCH_ID, CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_OUTBOX_PENDING
                ON OUTBOX_EVENTS(STATUS, CREATED_AT);
            CREATE INDEX IF NOT EXISTS IDX_EUC_REPOSITORY ON EUC_ASSETS(REPOSITORY_ID, CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_EUC_RUNS ON EUC_ANALYSIS_RUNS(EUC_ID, STARTED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_EUC_SHEETS ON EUC_SHEET_INVENTORY(ANALYSIS_ID, SHEET_POSITION);
            CREATE INDEX IF NOT EXISTS IDX_EUC_PATTERNS ON EUC_FORMULA_PATTERNS(ANALYSIS_ID, OCCURRENCE_COUNT DESC);
            CREATE INDEX IF NOT EXISTS IDX_EUC_OBJECTS ON EUC_OBJECT_INVENTORY(ANALYSIS_ID, OBJECT_TYPE);
            CREATE INDEX IF NOT EXISTS IDX_EUC_FINDING_REMEDIATIONS ON EUC_FINDING_REMEDIATIONS(FINDING_ID, CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_EUC_ATTESTATIONS ON EUC_ATTESTATIONS(EUC_ID, SUBMITTED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_EUC_ATTESTATIONS_REPOSITORY ON EUC_ATTESTATIONS(REPOSITORY_ID, SUBMITTED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_DEP_RUN_EUC ON EUC_DEPENDENCY_RUNS(EUC_ID, STARTED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_DEP_EDGE_SOURCE ON EUC_DEPENDENCY_EDGES(DEPENDENCY_RUN_ID,SOURCE_NODE_ID);
            CREATE INDEX IF NOT EXISTS IDX_DEP_EDGE_TARGET ON EUC_DEPENDENCY_EDGES(DEPENDENCY_RUN_ID,TARGET_NODE_ID);
            CREATE INDEX IF NOT EXISTS IDX_DEP_NODE_CRITICAL ON EUC_DEPENDENCY_NODES(DEPENDENCY_RUN_ID,TECHNICAL_CRITICALITY DESC);
            CREATE INDEX IF NOT EXISTS IDX_DEP_NODE_CELL ON EUC_DEPENDENCY_NODES(DEPENDENCY_RUN_ID,SHEET_ID,CELL_ADDRESS);
            CREATE INDEX IF NOT EXISTS IDX_INTEL_RUN_EUC ON EUC_INTELLIGENCE_RUNS(EUC_ID,STARTED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_FINDING_EUC_STATUS ON EUC_FINDINGS(EUC_ID,STATUS,UPDATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_FINDING_OCC_SEVERITY ON EUC_FINDING_OCCURRENCES(INTELLIGENCE_RUN_ID,SEVERITY);
            CREATE INDEX IF NOT EXISTS IDX_FINDING_ACTION ON EUC_FINDING_ACTIONS(FINDING_ID,CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_MIGRATION_RUN_EUC ON EUC_MIGRATION_RUNS(EUC_ID,STARTED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_MIGRATION_UNIT_MODE ON EUC_MIGRATION_UNITS(MIGRATION_RUN_ID,ENGINE_MODE,DIFFICULTY);
            CREATE INDEX IF NOT EXISTS IDX_MIGRATION_BLOCKER_SEVERITY ON EUC_MIGRATION_BLOCKERS(MIGRATION_RUN_ID,SEVERITY);
            CREATE INDEX IF NOT EXISTS IDX_MIGRATION_OVERRIDE_UNIT ON EUC_MIGRATION_OVERRIDES(EUC_ID,UNIT_ID,CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_AIR_MODEL_EUC ON EUC_APPLICATION_MODELS(EUC_ID,CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_AIR_COMPONENT_TYPE ON EUC_APPLICATION_COMPONENTS(APPLICATION_MODEL_ID,COMPONENT_TYPE,CONFIDENCE DESC);
            CREATE INDEX IF NOT EXISTS IDX_AIR_REVIEW_MODEL ON EUC_APPLICATION_REVIEWS(APPLICATION_MODEL_ID,CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_AIR_GENERATION_MODEL ON EUC_APPLICATION_GENERATION_RUNS(APPLICATION_MODEL_ID,STARTED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_AIR_LINEAGE_SOURCE ON EUC_APPLICATION_LINEAGE(APPLICATION_MODEL_ID,SOURCE_TYPE,SOURCE_ID);
            CREATE INDEX IF NOT EXISTS IDX_TASKS_REPOSITORY ON TASKS(REPOSITORY_ID, STATUS, CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_TASKS_BRANCH ON TASKS(BRANCH_ID, STATUS);
            CREATE INDEX IF NOT EXISTS IDX_TASKS_ASSIGNEE ON TASKS(ASSIGNED_TO_USER_ID, STATUS);
            CREATE INDEX IF NOT EXISTS IDX_DEVICE_FINGERPRINTS_USER ON DEVICE_FINGERPRINTS(USER_ID, LAST_SEEN_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_DEVICE_FINGERPRINTS_MACHINE ON DEVICE_FINGERPRINTS(MACHINE_ID);
            CREATE UNIQUE INDEX IF NOT EXISTS UQ_DEVICE_FINGERPRINTS_USER_MACHINE ON DEVICE_FINGERPRINTS(USER_ID, MACHINE_ID);
            CREATE INDEX IF NOT EXISTS IDX_USER_DAILY_ACTIVITY_DATE ON USER_DAILY_ACTIVITY(ACTIVITY_DATE, USER_ID);
            CREATE INDEX IF NOT EXISTS IDX_NOTIFICATIONS_USER ON NOTIFICATIONS(USER_ID, READ_AT, CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_NOTIFICATIONS_ORGANIZATION ON NOTIFICATIONS(ORGANIZATION_ID, CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_MERGE_KNOWLEDGE_TYPE ON MERGE_RESOLUTION_KNOWLEDGE(CONFLICT_TYPE, SOURCE);
            CREATE INDEX IF NOT EXISTS IDX_FORMULA_EXPLANATION_CELL ON FORMULA_EXPLANATIONS(BRANCH_ID, SHEET_ID, ROW_ID, COLUMN_ID);
            CREATE INDEX IF NOT EXISTS IDX_FORMULA_PATTERN_SIGNATURE ON FORMULA_PATTERN_KNOWLEDGE(FUNCTION_SIGNATURE, SOURCE);
            CREATE INDEX IF NOT EXISTS IDX_PORTFOLIO_BRIEFING_ORG ON PORTFOLIO_BRIEFINGS(ORGANIZATION_ID, CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_DISCOVERY_REPOSITORY ON DISCOVERY_RECOMMENDATIONS(REPOSITORY_ID, CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_RBAC_ANOMALY_REPOSITORY ON RBAC_ANOMALY_FINDINGS(REPOSITORY_ID, STATUS, LAST_DETECTED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_RBAC_ANOMALY_KNOWLEDGE_TYPE ON RBAC_ANOMALY_KNOWLEDGE(ANOMALY_TYPE, SOURCE);
            CREATE INDEX IF NOT EXISTS IDX_MERGE_SUGGESTION_REQUEST ON MERGE_CONFLICT_AI_SUGGESTIONS(MERGE_REQUEST_ID, STATUS);
            CREATE INDEX IF NOT EXISTS IDX_MERGE_SUGGESTION_CONFLICT ON MERGE_CONFLICT_AI_SUGGESTIONS(CONFLICT_ID, STATUS);
            CREATE INDEX IF NOT EXISTS IDX_MERGE_ASSESSMENT_REQUEST ON MERGE_REQUEST_AI_ASSESSMENTS(MERGE_REQUEST_ID, CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_COMMIT_AI_REVIEW_BRANCH ON COMMIT_AI_REVIEWS(BRANCH_ID, CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_COMMIT_AI_REVIEW_REPOSITORY ON COMMIT_AI_REVIEWS(REPOSITORY_ID, CREATED_AT DESC);
            CREATE INDEX IF NOT EXISTS IDX_EUC_BRANCH_COMPARISON_BRANCH ON EUC_BRANCH_COMPARISONS(BRANCH_ID, CREATED_AT DESC);

            CREATE TRIGGER IF NOT EXISTS MERGE_RESOLUTION_KNOWLEDGE_FTS_SYNC
            AFTER INSERT ON MERGE_RESOLUTION_KNOWLEDGE
            BEGIN
                INSERT INTO MERGE_RESOLUTION_KNOWLEDGE_FTS (doc_id, conflict_type, title, scenario_text, rationale)
                VALUES (new.DOC_ID, new.CONFLICT_TYPE, new.TITLE, new.SCENARIO_TEXT, new.RATIONALE);
            END;

            CREATE TRIGGER IF NOT EXISTS FORMULA_PATTERN_KNOWLEDGE_FTS_SYNC
            AFTER INSERT ON FORMULA_PATTERN_KNOWLEDGE
            BEGIN
                INSERT INTO FORMULA_PATTERN_KNOWLEDGE_FTS (doc_id, function_signature, title, pattern_text, explanation)
                VALUES (new.DOC_ID, new.FUNCTION_SIGNATURE, new.TITLE, new.PATTERN_TEXT, new.EXPLANATION);
            END;

            CREATE TRIGGER IF NOT EXISTS RBAC_ANOMALY_KNOWLEDGE_FTS_SYNC
            AFTER INSERT ON RBAC_ANOMALY_KNOWLEDGE
            BEGIN
                INSERT INTO RBAC_ANOMALY_KNOWLEDGE_FTS (doc_id, anomaly_type, title, scenario_text, rationale)
                VALUES (new.DOC_ID, new.ANOMALY_TYPE, new.TITLE, new.SCENARIO_TEXT, new.RATIONALE);
            END;

            CREATE TRIGGER IF NOT EXISTS AUDIT_EVENTS_APPEND_ONLY_UPDATE
            BEFORE UPDATE ON AUDIT_EVENTS
            BEGIN
                SELECT RAISE(ABORT, 'AUDIT_EVENTS is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS AUDIT_EVENTS_APPEND_ONLY_DELETE
            BEFORE DELETE ON AUDIT_EVENTS
            BEGIN
                SELECT RAISE(ABORT, 'AUDIT_EVENTS is append-only');
            END;
            """
        )
        login_code_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info('AUTH_LOGIN_CODES')")
        }
        if "ATTEMPTS" not in login_code_columns:
            conn.execute(
                "ALTER TABLE AUTH_LOGIN_CODES ADD COLUMN ATTEMPTS INTEGER NOT NULL DEFAULT 0"
            )
        if "MAX_ATTEMPTS" not in login_code_columns:
            conn.execute(
                "ALTER TABLE AUTH_LOGIN_CODES ADD COLUMN MAX_ATTEMPTS INTEGER NOT NULL DEFAULT 5"
            )
        user_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info('APP_USERS')")
        }
        if "EMPLOYEE_ID" not in user_columns:
            conn.execute("ALTER TABLE APP_USERS ADD COLUMN EMPLOYEE_ID TEXT")
        repository_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info('WORKBOOK_REPOSITORIES')")
        }
        repository_migrations = {
            "REPOSITORY_SLUG": "TEXT",
            "VISIBILITY": "TEXT NOT NULL DEFAULT 'private'",
            "BUSINESS_OWNER": "TEXT",
            "DATA_CLASSIFICATION": "TEXT NOT NULL DEFAULT 'internal'",
            "RETENTION_POLICY": "TEXT",
            "STORAGE_ENGINE": "TEXT NOT NULL DEFAULT 'semantic_object_v1'",
        }
        for column, definition in repository_migrations.items():
            if column not in repository_columns:
                conn.execute(
                    f"ALTER TABLE WORKBOOK_REPOSITORIES ADD COLUMN {column} {definition}"
                )
        commit_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info('COMMITS')")
        }
        commit_migrations = {
            "ROOT_MANIFEST_HASH": "TEXT",
            "OBJECTS_CREATED": "INTEGER NOT NULL DEFAULT 0",
            "OBJECTS_REUSED": "INTEGER NOT NULL DEFAULT 0",
            "PHYSICAL_BYTES_ADDED": "INTEGER NOT NULL DEFAULT 0",
            "LOGICAL_BYTES_CHANGED": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, definition in commit_migrations.items():
            if column not in commit_columns:
                conn.execute(f"ALTER TABLE COMMITS ADD COLUMN {column} {definition}")
        presence_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info('DATASET_PRESENCE')")
        }
        if "STATUS" not in presence_columns:
            conn.execute("ALTER TABLE DATASET_PRESENCE ADD COLUMN STATUS TEXT NOT NULL DEFAULT 'ONLINE'")
        branch_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info('BRANCHES')")
        }
        if "LOCAL_DOWNLOAD_PATH" not in branch_columns:
            conn.execute("ALTER TABLE BRANCHES ADD COLUMN LOCAL_DOWNLOAD_PATH TEXT")
        working_copy_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info('WORKING_COPIES')")
        }
        if "LOCAL_FILE_PATH" not in working_copy_columns:
            conn.execute("ALTER TABLE WORKING_COPIES ADD COLUMN LOCAL_FILE_PATH TEXT")
        if "BOUND_MACHINE_ID" not in working_copy_columns:
            conn.execute("ALTER TABLE WORKING_COPIES ADD COLUMN BOUND_MACHINE_ID TEXT")
        user_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info('APP_USERS')")
        }
        if "PASSWORD_HASH" not in user_columns:
            conn.execute("ALTER TABLE APP_USERS ADD COLUMN PASSWORD_HASH TEXT")
        assessment_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info('MERGE_REQUEST_AI_ASSESSMENTS')")
        }
        if assessment_columns and "FIELD_INVESTIGATIONS_JSON" not in assessment_columns:
            conn.execute(
                "ALTER TABLE MERGE_REQUEST_AI_ASSESSMENTS ADD COLUMN FIELD_INVESTIGATIONS_JSON TEXT NOT NULL DEFAULT '[]'"
            )
        if assessment_columns and "RISK_SCORE" not in assessment_columns:
            conn.execute("ALTER TABLE MERGE_REQUEST_AI_ASSESSMENTS ADD COLUMN RISK_SCORE REAL NOT NULL DEFAULT 0")
        if assessment_columns and "RISK_BREAKDOWN_JSON" not in assessment_columns:
            conn.execute(
                "ALTER TABLE MERGE_REQUEST_AI_ASSESSMENTS ADD COLUMN RISK_BREAKDOWN_JSON TEXT NOT NULL DEFAULT '[]'"
            )
        now = _utcnow()
        conn.execute("INSERT OR IGNORE INTO CATEGORIES VALUES ('CAT_HOME', NULL, 'Home', 'All workbook repositories', 0, 'ACTIVE', ?, ?)", (now, now))
        conn.execute("INSERT OR IGNORE INTO CATEGORIES VALUES ('CAT_UNSORTED', 'CAT_HOME', 'Unsorted', 'New workbook repositories', 100, 'ACTIVE', ?, ?)", (now, now))
        conn.execute(
            """
            INSERT OR IGNORE INTO APP_USERS
                (USER_ID, EMAIL, DISPLAY_NAME, ROLE, CREATED_AT, LAST_LOGIN_AT)
            VALUES ('USR_SYSTEM', 'system@local', 'System', 'system', ?, ?)
            """,
            (now, now),
        )
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'QUEUE_BOARD_%'"
        ).fetchall()
        for row in tables:
            table_id = SafeIdentifier(row[0])
            row_count = conn.execute(f'SELECT COUNT(*) FROM "{table_id}"').fetchone()[0]
            column_count = len(conn.execute(f'PRAGMA table_info("{table_id}")').fetchall()) - 1
            conn.execute(
                """
                INSERT OR IGNORE INTO DATASET_REGISTRY
                    (TABLE_ID, OWNER_USER_ID, ROW_COUNT, COLUMN_COUNT, CREATED_AT, UPDATED_AT)
                VALUES (?, 'USR_SYSTEM', ?, ?, ?, ?)
                """,
                (table_id, row_count, column_count, now, now),
            )
            current_version = conn.execute(
                "SELECT CURRENT_VERSION FROM DATASET_REGISTRY WHERE TABLE_ID=?",
                (table_id,),
            ).fetchone()[0]
            # Local import: avoids a module-load-time cycle, since
            # sync_store.py imports _get_connection/_utcnow from this module.
            from .sync_store import _store_dataset_version
            _store_dataset_version(
                conn, table_id, int(current_version), None,
                "Imported existing dataset state", "USR_SYSTEM", now,
            )
            repository_exists = conn.execute(
                "SELECT 1 FROM WORKBOOK_REPOSITORIES WHERE TABLE_ID=?", (table_id,)
            ).fetchone()
            if not repository_exists:
                registry = conn.execute(
                    "SELECT OWNER_USER_ID, ORIGINAL_FILENAME FROM DATASET_REGISTRY WHERE TABLE_ID=?",
                    (table_id,),
                ).fetchone()
                repository_id = f"REP_{uuid.uuid4().hex[:12].upper()}"
                main_branch_id = f"BR_{uuid.uuid4().hex[:12].upper()}"
                root_commit = f"CMT_{uuid.uuid4().hex[:12].upper()}"
                filename = registry["ORIGINAL_FILENAME"] or table_id
                conn.execute(
                    """
                    INSERT INTO WORKBOOK_REPOSITORIES
                        (REPOSITORY_ID, TABLE_ID, CATEGORY_ID, REPOSITORY_NAME,
                         DESCRIPTION, DEFAULT_BRANCH_ID, CREATED_BY, CREATED_AT,
                         UPDATED_AT, STATUS, MAIN_PROTECTED)
                    VALUES (?, ?, 'CAT_UNSORTED', ?, ?, ?, ?, ?, ?, 'ACTIVE', 0)
                    """,
                    (
                        repository_id, table_id, Path(filename).stem,
                        f"Migrated repository for {filename}", main_branch_id,
                        registry["OWNER_USER_ID"], now, now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO BRANCHES
                        (BRANCH_ID, REPOSITORY_ID, DATA_TABLE_ID, BRANCH_NAME,
                         BRANCH_TYPE, CREATED_BY, BASE_COMMIT_ID, HEAD_COMMIT_ID,
                         STATUS, CREATED_AT, UPDATED_AT)
                    VALUES (?, ?, ?, 'main', 'MAIN', ?, ?, ?, 'ACTIVE', ?, ?)
                    """,
                    (
                        main_branch_id, repository_id, table_id,
                        registry["OWNER_USER_ID"], root_commit, root_commit, now, now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO WORKBOOK_SHEETS
                        (SHEET_ID, REPOSITORY_ID, SHEET_NAME, SHEET_ORDER, STATUS, CREATED_AT)
                    VALUES (?, ?, 'Sheet1', 0, 'ACTIVE', ?)
                    """,
                    (f"SHEET_{uuid.uuid4().hex[:12].upper()}", repository_id, now),
                )
        semantic_branches = conn.execute(
            """
            SELECT B.*, R.DEFAULT_BRANCH_ID
            FROM BRANCHES B JOIN WORKBOOK_REPOSITORIES R ON R.REPOSITORY_ID=B.REPOSITORY_ID
            WHERE B.STATUS='ACTIVE'
            ORDER BY CASE B.BRANCH_TYPE WHEN 'MAIN' THEN 0 ELSE 1 END
            """
        ).fetchall()
        for branch in semantic_branches:
            conn.execute(
                """
                INSERT INTO REPOSITORY_MEMBERS
                    (REPOSITORY_ID, USER_ID, ROLE, GRANTED_BY, CREATED_AT, UPDATED_AT)
                SELECT R.REPOSITORY_ID, R.CREATED_BY, 'owner', R.CREATED_BY, R.CREATED_AT, ?
                FROM WORKBOOK_REPOSITORIES R WHERE R.REPOSITORY_ID=?
                ON CONFLICT(REPOSITORY_ID, USER_ID) DO UPDATE SET ROLE='owner', UPDATED_AT=excluded.UPDATED_AT
                """,
                (now, branch["REPOSITORY_ID"]),
            )
            source_branch_id = (
                branch["DEFAULT_BRANCH_ID"]
                if branch["BRANCH_TYPE"] == "USER" else None
            )
            ensure_branch_identities(
                conn,
                branch["BRANCH_ID"],
                branch["REPOSITORY_ID"],
                branch["DATA_TABLE_ID"],
                source_branch_id=source_branch_id,
            )
            _ensure_stage2_commit_foundation(conn, branch, now)
        from ..access_control.bootstrap import initialize_access_control
        initialize_access_control(conn, now)
        from ..integrations.schema import initialize_integration_schema
        initialize_integration_schema(conn, now)
        from ..ai.schema import initialize_ai_schema
        initialize_ai_schema(conn, now)
        ai_request_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info('AI_REQUESTS')")
        }
        if ai_request_columns and "AGENT_RUN_ID" not in ai_request_columns:
            conn.execute("ALTER TABLE AI_REQUESTS ADD COLUMN AGENT_RUN_ID TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS IDX_AI_REQUEST_AGENT_RUN ON AI_REQUESTS(AGENT_RUN_ID)")
        macro_run_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info('MACRO_RUNS')")
        }
        if macro_run_columns and "RECIPE_ID" not in macro_run_columns:
            conn.execute("ALTER TABLE MACRO_RUNS ADD COLUMN RECIPE_ID TEXT")
        # GroundedAnswer.insufficient_evidence was computed by the gateway
        # but never persisted alongside the domain result rows -- add it so
        # a low-confidence/ungrounded answer stays flagged after the fact,
        # not just in the in-memory response of the original request.
        for table in ("MERGE_REQUEST_AI_ASSESSMENTS", "COMMIT_AI_REVIEWS", "FORMULA_EXPLANATIONS"):
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info('{table}')")}
            if columns and "INSUFFICIENT_EVIDENCE" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN INSUFFICIENT_EVIDENCE INTEGER NOT NULL DEFAULT 0")
        # Knowledge/precedent tables were shared across every organization --
        # one org's dismissal reasoning could leak into another's RAG
        # evidence. Add the column (existing rows default to NULL/global so
        # nothing already stored is silently discarded) and filter on it
        # going forward in ai.rbac_knowledge / ai.formula_knowledge / ai.merge_knowledge.
        for table in ("RBAC_ANOMALY_KNOWLEDGE", "FORMULA_PATTERN_KNOWLEDGE", "MERGE_RESOLUTION_KNOWLEDGE"):
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info('{table}')")}
            if columns and "ORGANIZATION_ID" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN ORGANIZATION_ID TEXT")
        from ..ai.merge_knowledge import seed_synthetic_corpus
        seed_synthetic_corpus(conn)
        from ..ai.formula_knowledge import seed_synthetic_formula_corpus
        seed_synthetic_formula_corpus(conn)
        from ..ai.rbac_knowledge import seed_synthetic_rbac_corpus
        seed_synthetic_rbac_corpus(conn)
        conn.commit()
    finally:
        conn.close()


def _current_db_path() -> Path:
    """Resolve the active DB_PATH through the `app.database` shim.

    Tests (and any other external code) isolate themselves by reassigning
    `database.DB_PATH` to a temp-file path — a widely used pattern across
    backend/tests/. A plain `from .store.schema import DB_PATH` re-export
    only copies the value at import time, so reassigning the shim's
    attribute would NOT be visible here if this module read its own
    `DB_PATH` global directly. Reading it through the shim module (imported
    lazily to avoid a load-time cycle, since database.py imports this
    module) keeps that reassignment pattern working unchanged.
    """
    from .. import database as _database_shim
    return _database_shim.DB_PATH


def _get_connection() -> sqlite3.Connection:
    """Open (or create) the SQLite database and return a connection."""
    conn = sqlite3.connect(str(_current_db_path()))
    conn.execute("PRAGMA journal_mode=WAL;")  # better concurrent read perf
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_stage2_commit_foundation(
    conn: sqlite3.Connection, branch: sqlite3.Row, now: str
) -> None:
    """Backfill a first-class checkpoint commit for pre-Stage-2 branch state."""
    head_commit_id = branch["HEAD_COMMIT_ID"] or stable_id("CMT")
    if not branch["HEAD_COMMIT_ID"]:
        conn.execute(
            "UPDATE BRANCHES SET HEAD_COMMIT_ID=?, BASE_COMMIT_ID=COALESCE(BASE_COMMIT_ID, ?) WHERE BRANCH_ID=?",
            (head_commit_id, head_commit_id, branch["BRANCH_ID"]),
        )
    existing = conn.execute(
        "SELECT 1 FROM COMMITS WHERE COMMIT_ID=?", (head_commit_id,)
    ).fetchone()
    if existing:
        manifest = conn.execute(
            "SELECT 1 FROM COMMIT_MANIFESTS WHERE COMMIT_ID=?", (head_commit_id,)
        ).fetchone()
        if not manifest:
            from ..services.semantic_ledger_service import ledger_for

            state = semantic_snapshot(conn, branch["DATA_TABLE_ID"])
            ledger_for(_current_db_path()).persist_commit(
                conn, head_commit_id, branch["REPOSITORY_ID"], branch["BRANCH_ID"], state
            )
        return
    author = conn.execute(
        "SELECT EMAIL FROM APP_USERS WHERE USER_ID=?", (branch["CREATED_BY"],)
    ).fetchone()
    snapshot = semantic_snapshot(conn, branch["DATA_TABLE_ID"])
    snapshot["head_commit_id"] = head_commit_id
    snapshot_json = json.dumps(snapshot, default=str, separators=(",", ":"))
    commit_hash = hashlib.sha256(
        f"{branch['REPOSITORY_ID']}:{branch['BRANCH_ID']}:{head_commit_id}:{snapshot_json}".encode()
    ).hexdigest()
    conn.execute(
        """
        INSERT INTO COMMITS
            (COMMIT_ID, REPOSITORY_ID, BRANCH_ID, AUTHOR_USER_ID, AUTHOR_EMAIL,
             MESSAGE, CREATED_AT, CHANGE_COUNT, COMMIT_HASH, STATUS, DATASET_VERSION)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 'CHECKPOINT',
                (SELECT CURRENT_VERSION FROM DATASET_REGISTRY WHERE TABLE_ID=?))
        """,
        (
            head_commit_id, branch["REPOSITORY_ID"], branch["BRANCH_ID"],
            branch["CREATED_BY"], author[0] if author else "system@local",
            "Stage 2 baseline checkpoint", now, commit_hash, branch["DATA_TABLE_ID"],
        ),
    )
    from ..services.semantic_ledger_service import ledger_for

    ledger_for(_current_db_path()).persist_commit(
        conn, head_commit_id, branch["REPOSITORY_ID"], branch["BRANCH_ID"], snapshot
    )
    conn.execute(
        """
        INSERT INTO BRANCH_CHECKPOINTS
            (CHECKPOINT_ID, REPOSITORY_ID, BRANCH_ID, COMMIT_ID,
             SNAPSHOT_JSON, REASON, CREATED_AT)
        VALUES (?, ?, ?, ?, ?, 'STAGE2_BASELINE', ?)
        """,
        (
            stable_id("CP"), branch["REPOSITORY_ID"], branch["BRANCH_ID"],
            head_commit_id, snapshot_json, now,
        ),
    )


# ---------------------------------------------------------------------------
# Column-name sanitizer
# ---------------------------------------------------------------------------
_CLEAN_RE = re.compile(r"[^A-Z0-9_]")


def _sanitize_column_name(raw: str) -> str:
    """
    Sanitize a raw Excel header into a safe SQLite column name.

    Rules:
      1. Strip leading/trailing whitespace
      2. Replace spaces & special characters with underscores
      3. Force UPPERCASE
      4. Cap at 30 characters
      5. Ensure it does not start with a digit (prefix with underscore)
    """
    name = raw.strip().upper().replace(" ", "_")
    name = _CLEAN_RE.sub("_", name)
    # collapse consecutive underscores
    name = re.sub(r"_+", "_", name).strip("_")
    if not name:
        name = "COL"
    if name[0].isdigit():
        name = "_" + name
    return name[:30]


# ---------------------------------------------------------------------------
# Pandas dtype → SQLite type mapping
# ---------------------------------------------------------------------------
def _map_dtype(dtype) -> str:
    """Map a pandas Series dtype to a SQLite column type."""
    kind = dtype.kind
    if kind in ("i", "u"):  # signed/unsigned integer
        return "INTEGER"
    if kind == "f":  # floating point
        return "REAL"
    if kind == "b":  # boolean
        return "INTEGER"
    # everything else (object, string, datetime, etc.) → TEXT
    return "TEXT"


def _sqlite_scalar(value: Any) -> Any:
    """Convert pandas/Excel scalar types to values accepted by sqlite3."""
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat(sep=" ")
    if isinstance(value, pd.Timedelta):
        return str(value)

    item = getattr(value, "item", None)
    if callable(item):
        value = item()

    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bytes)):
        return value
    return str(value)


# ---------------------------------------------------------------------------
def reconcile_orphaned_upload_tables() -> list[str]:
    """Detect and drop primary upload tables left behind by a crash between
    create_sqlite_table_from_df() and register_dataset() (upload
    provisioning uses three separately-committed connections rather than one
    shared transaction — see the Phase 1 code-quality audit). A
    'QUEUE_BOARD_*' table with no matching DATASET_REGISTRY row can only be
    such a partial upload: nothing else in the product creates that prefix
    without registering it in the same request, and the crash means the
    original upload's owner/filename/etc were never persisted, so there is
    nothing to re-register it with — dropping it is the only viable
    remediation. Called on startup (see main.py's lifespan()); safe to call
    at any time since it only touches tables that are provably unreferenced.
    Returns the list of table names it dropped, for logging.
    """
    conn = _get_connection()
    dropped: list[str] = []
    try:
        candidates = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'QUEUE_BOARD_%'"
        ).fetchall()
        for row in candidates:
            table_id = SafeIdentifier(row[0])
            registered = conn.execute(
                "SELECT 1 FROM DATASET_REGISTRY WHERE TABLE_ID=?", (table_id,)
            ).fetchone()
            if not registered:
                conn.execute(f'DROP TABLE "{table_id}"')
                dropped.append(str(table_id))
        if dropped:
            conn.commit()
    finally:
        conn.close()
    return dropped


# Public API
# ---------------------------------------------------------------------------
def table_exists(table_name: str) -> bool:
    """Check whether a table exists in the database."""
    conn = _get_connection()
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?;",
            (table_name,),
        )
        return cursor.fetchone() is not None
    finally:
        conn.close()


def column_exists(table_name: str, column_name: str) -> bool:
    """Check whether a column exists in the given table."""
    table_name = SafeIdentifier(table_name)
    conn = _get_connection()
    try:
        cursor = conn.execute(f'PRAGMA table_info("{table_name}");')
        columns = [row["name"].upper() for row in cursor.fetchall()]
        return column_name.upper() in columns
    finally:
        conn.close()


def get_system_setting(key: str, default: str | None = None) -> str | None:
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT SETTING_VALUE FROM SYSTEM_SETTINGS WHERE SETTING_KEY=?",
            (key,),
        ).fetchone()
        return row[0] if row else default
    finally:
        conn.close()


def set_system_setting(key: str, value: str, user_id: str = "USR_SYSTEM") -> None:
    conn = _get_connection()
    now = _utcnow()
    try:
        conn.execute(
            """
            INSERT INTO SYSTEM_SETTINGS (SETTING_KEY, SETTING_VALUE, UPDATED_AT, UPDATED_BY)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(SETTING_KEY) DO UPDATE SET
                SETTING_VALUE=excluded.SETTING_VALUE,
                UPDATED_AT=excluded.UPDATED_AT,
                UPDATED_BY=excluded.UPDATED_BY
            """,
            (key, value, now, user_id),
        )
        conn.commit()
    finally:
        conn.close()


