"""Environment-backed application settings."""

import os

from .ai.catalog import NVIDIA_FREE_MODEL_DEFAULTS, is_free_nvidia_model_id
import secrets
from pathlib import Path


def _load_local_env() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()


def _local_auth_secret() -> str:
    configured = os.getenv("AUTH_SECRET", "").strip()
    if configured:
        return configured
    if os.getenv("APP_ENV", "development").strip().lower() == "production":
        raise RuntimeError("AUTH_SECRET is required when APP_ENV=production")
    secret_path = Path(__file__).resolve().parent.parent / ".auth-secret"
    if secret_path.exists():
        return secret_path.read_text(encoding="utf-8").strip()
    generated = secrets.token_urlsafe(48)
    secret_path.write_text(generated, encoding="utf-8")
    return generated


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    app_env = os.getenv("APP_ENV", "development").strip().lower()
    app_url = os.getenv("APP_URL", "https://localhost:3000").rstrip("/")
    api_base_url = os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/")
    office_addin_url = os.getenv("OFFICE_ADDIN_URL", app_url).rstrip("/")
    database_url = os.getenv("DATABASE_URL", "").strip()
    storage_engine = os.getenv("STORAGE_ENGINE", "semantic_object_v1").strip()
    object_store_provider = os.getenv("OBJECT_STORE_PROVIDER", "local").strip().lower()
    object_store_path = os.getenv("OBJECT_STORE_PATH", "").strip()
    object_store_bucket = os.getenv("OBJECT_STORE_BUCKET", "gitwalk-objects").strip()
    object_store_endpoint = os.getenv("OBJECT_STORE_ENDPOINT", "").strip()
    object_store_access_key = os.getenv("OBJECT_STORE_ACCESS_KEY", "").strip()
    object_store_secret_key = os.getenv("OBJECT_STORE_SECRET_KEY", "").strip()
    object_compression = os.getenv("OBJECT_COMPRESSION", "zstd").strip().lower()
    object_hash_algorithm = os.getenv("OBJECT_HASH_ALGORITHM", "sha256").strip().lower()
    semantic_block_rows = max(16, int(os.getenv("SEMANTIC_BLOCK_ROWS", "256")))
    idempotency_ttl_hours = max(1, int(os.getenv("IDEMPOTENCY_TTL_HOURS", "24")))
    cors_origins = [
        item.strip().rstrip("/")
        for item in os.getenv("CORS_ORIGINS", app_url).split(",")
        if item.strip()
    ]
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    smtp_from = os.getenv("SMTP_FROM", smtp_user)
    auth_secret = _local_auth_secret()
    auth_required = env_bool("AUTH_REQUIRED", True)
    dev_show_otp = env_bool("DEV_SHOW_OTP", app_env == "development")
    session_hours = int(os.getenv("SESSION_HOURS", "24"))
    otp_minutes = int(os.getenv("OTP_MINUTES", "10"))
    otp_request_cooldown_seconds = int(os.getenv("OTP_REQUEST_COOLDOWN_SECONDS", "12"))
    otp_requests_per_hour = int(os.getenv("OTP_REQUESTS_PER_HOUR", "25"))
    reviewer_response_hours = int(os.getenv("REVIEWER_RESPONSE_HOURS", "6"))
    # Reference mid-tier commercial LLM pricing, used only to compute a
    # "what this would have cost" figure for the AI token ledger's
    # cost-avoidance KPI. Git Walk routes exclusively to free-tier models,
    # so actual spend is always $0 — this is informational, not a real bill.
    ai_reference_cost_per_1k_input_tokens = float(os.getenv("AI_REFERENCE_COST_PER_1K_INPUT_TOKENS", "0.003"))
    ai_reference_cost_per_1k_output_tokens = float(os.getenv("AI_REFERENCE_COST_PER_1K_OUTPUT_TOKENS", "0.015"))
    max_upload_bytes = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
    max_xlsx_entries = int(os.getenv("MAX_XLSX_ENTRIES", "2500"))
    max_xlsx_uncompressed_bytes = int(
        os.getenv("MAX_XLSX_UNCOMPRESSED_BYTES", str(250 * 1024 * 1024))
    )
    max_workbook_sheets = int(os.getenv("MAX_WORKBOOK_SHEETS", "100"))
    max_workbook_rows = int(os.getenv("MAX_WORKBOOK_ROWS", "250000"))
    max_workbook_columns = int(os.getenv("MAX_WORKBOOK_COLUMNS", "1000"))
    upload_processing_timeout_seconds = int(
        os.getenv("UPLOAD_PROCESSING_TIMEOUT_SECONDS", "60")
    )
    temp_file_max_age_hours = int(os.getenv("TEMP_FILE_MAX_AGE_HOURS", "24"))
    repository_purge_sweep_enabled = os.getenv("REPOSITORY_PURGE_SWEEP_ENABLED", "true").strip().lower() not in ("0", "false", "no")
    repository_purge_inactivity_days = max(1, int(os.getenv("REPOSITORY_PURGE_INACTIVITY_DAYS", "30")))
    repository_purge_sweep_interval_seconds = max(300, int(os.getenv("REPOSITORY_PURGE_SWEEP_INTERVAL_SECONDS", str(24 * 3600))))
    repository_purge_export_dir = os.getenv("REPOSITORY_PURGE_EXPORT_DIR", "").strip()
    repository_purge_max_physical_rows_per_table = max(100, int(os.getenv("REPOSITORY_PURGE_MAX_PHYSICAL_ROWS_PER_TABLE", "20000")))
    euc_analysis_version = os.getenv("EUC_ANALYSIS_VERSION", "2.1.0").strip()
    euc_max_file_bytes = int(os.getenv("EUC_MAX_FILE_SIZE", str(50 * 1024 * 1024)))
    euc_max_decompressed_bytes = int(
        os.getenv("EUC_MAX_DECOMPRESSED_SIZE", str(500 * 1024 * 1024))
    )
    euc_max_zip_entries = int(os.getenv("EUC_MAX_ZIP_ENTRIES", "5000"))
    euc_max_xml_bytes = int(os.getenv("EUC_MAX_XML_SIZE", str(100 * 1024 * 1024)))
    euc_max_cells = int(os.getenv("EUC_MAX_CELLS", "5000000"))
    euc_max_formulas = int(os.getenv("EUC_MAX_FORMULAS", "1000000"))
    euc_analysis_timeout_seconds = int(
        os.getenv("EUC_ANALYSIS_TIMEOUT_SECONDS", "300")
    )
    dependency_engine_version = os.getenv("DEPENDENCY_ENGINE_VERSION", "2.2.0").strip()
    dependency_range_expansion_limit = max(
        1, int(os.getenv("DEPENDENCY_RANGE_EXPANSION_LIMIT", "1000"))
    )
    dependency_max_depth = max(1, int(os.getenv("MAX_DEPENDENCY_DEPTH", "20")))
    dependency_max_response_nodes = max(
        50, int(os.getenv("MAX_GRAPH_RESPONSE_NODES", "5000"))
    )
    dependency_sqlite_edge_limit = max(
        1000, int(os.getenv("DEPENDENCY_SQLITE_EDGE_LIMIT", "100000"))
    )
    dependency_analysis_timeout_seconds = max(
        30, int(os.getenv("DEPENDENCY_ANALYSIS_TIMEOUT_SECONDS", "300"))
    )
    intelligence_engine_version = os.getenv("INTELLIGENCE_ENGINE_VERSION", "2.3.0").strip()
    intelligence_ruleset_version = os.getenv("INTELLIGENCE_RULESET_VERSION", "ruleset@2.3.0").strip()
    intelligence_default_profile = os.getenv("INTELLIGENCE_DEFAULT_PROFILE", "DEFAULT").strip().upper()
    intelligence_analysis_timeout_seconds = max(
        30, int(os.getenv("INTELLIGENCE_ANALYSIS_TIMEOUT_SECONDS", "300"))
    )
    intelligence_criticality_threshold = min(
        100.0, max(0.0, float(os.getenv("INTELLIGENCE_CRITICALITY_THRESHOLD", "70")))
    )
    intelligence_finding_limit = max(
        100, int(os.getenv("INTELLIGENCE_FINDING_LIMIT", "5000"))
    )
    migration_engine_version = os.getenv("MIGRATION_ENGINE_VERSION", "2.4.0").strip()
    migration_ruleset_version = os.getenv("MIGRATION_RULESET_VERSION", "migration-rules@2.4.0").strip()
    migration_analysis_timeout_seconds = max(
        30, int(os.getenv("MIGRATION_ANALYSIS_TIMEOUT_SECONDS", "300"))
    )
    migration_unit_limit = max(1000, int(os.getenv("MIGRATION_UNIT_LIMIT", "100000")))
    migration_replay_commit_limit = max(
        1, min(200, int(os.getenv("MIGRATION_REPLAY_COMMIT_LIMIT", "50")))
    )
    migration_absolute_tolerance = max(
        0.0, float(os.getenv("MIGRATION_ABSOLUTE_TOLERANCE", "0.000001"))
    )
    migration_relative_tolerance = max(
        0.0, float(os.getenv("MIGRATION_RELATIVE_TOLERANCE", "0.000001"))
    )
    air_engine_version = os.getenv("AIR_ENGINE_VERSION", "2.5.0").strip()
    air_generator_version = os.getenv("AIR_GENERATOR_VERSION", "2.5.0").strip()
    air_default_target_profile = os.getenv(
        "AIR_DEFAULT_TARGET_PROFILE", "WEB_POSTGRES_FASTAPI_REACT"
    ).strip().upper()
    air_auto_generate_confidence = min(
        1.0, max(0.0, float(os.getenv("AIR_AUTO_GENERATE_CONFIDENCE", "0.95")))
    )
    air_review_confidence = min(
        1.0, max(0.0, float(os.getenv("AIR_REVIEW_CONFIDENCE", "0.80")))
    )
    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_model = os.getenv(
        "OPENROUTER_MODEL", NVIDIA_FREE_MODEL_DEFAULTS[0]
    ).strip()
    openrouter_models = [
        item.strip()
        for item in os.getenv(
            "OPENROUTER_MODELS", ",".join(NVIDIA_FREE_MODEL_DEFAULTS)
        ).split(",")
        if is_free_nvidia_model_id(item)
    ]
    if not is_free_nvidia_model_id(openrouter_model):
        openrouter_model = NVIDIA_FREE_MODEL_DEFAULTS[0]
    if openrouter_model not in openrouter_models:
        openrouter_models.insert(0, openrouter_model)


settings = Settings()
