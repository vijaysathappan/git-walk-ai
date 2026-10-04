"""Backward-compatible re-export shim.

database.py used to be a single ~4,900-line module; it is now split into
backend/app/store/ by concern (schema/DDL, auth, dataset/repository CRUD,
presence/notifications, cell/table sync, KPIs). Every existing
`from .database import X` import elsewhere in the codebase keeps working
unmodified because this module re-exports every public name from its new
home.
"""

from .store.schema import (
    DB_PATH,
    VersionConflictError,
    _CLEAN_RE,
    _DB_DIR,
    _ensure_stage2_commit_foundation,
    _get_connection,
    _map_dtype,
    _sqlite_scalar,
    _utcnow,
    column_exists,
    get_system_setting,
    initialize_product_schema,
    reconcile_orphaned_upload_tables,
    set_system_setting,
    table_exists,
)

from .store.identifiers import (
    SafeIdentifier,
    validate_identifier,
)

from .store.auth_store import (
    auth_session_is_active,
    bind_or_verify_device,
    consume_login_code,
    create_auth_session,
    create_login_code,
    delete_user_ai_settings,
    device_trust_status,
    get_or_create_user,
    get_user,
    get_user_ai_settings,
    hash_password,
    list_organization_devices,
    list_repository_devices,
    record_device_fingerprint,
    revoke_auth_session,
    revoke_session,
    save_user_ai_settings,
    set_device_trust_status,
    set_user_password,
    verify_password,
    verify_user_credentials,
)

from .store.dataset_store import (
    _accept_pending_invitations,
    _clone_table,
    _materialize_branch_projection,
    _normalize_path,
    _workspace_event,
    add_dataset_member,
    advance_branch_head,
    authenticate_working_copy_identity,
    create_category,
    create_semantic_branch,
    create_sqlite_table_from_df,
    create_working_copy,
    delete_branch,
    delete_repository,
    get_branch_protection,
    get_branch_sheet_page,
    get_dataset_members,
    get_repository,
    get_repository_organization_id,
    get_table_page,
    get_workspace_snapshot,
    list_categories,
    list_datasets,
    list_repository_branches,
    list_user_working_copies,
    move_repository,
    register_dataset,
    remove_dataset_member,
    repository_name_available,
    resolve_repository_owner,
    store_initial_formula_metadata,
    update_branch_local_path,
    update_branch_protection,
    update_working_copy_local_path,
    user_can_access_branch,
    user_can_access_table,
    user_can_edit_table,
    user_can_work_on_repository,
    validate_working_copy,
    verify_workbook_access,
    working_copy_checkout_options,
    working_copy_required,
)

from .store.presence_store import (
    _PRESENCE_TTL_SECONDS,
    _notification_enabled,
    _seconds_since,
    _touch_daily_activity,
    create_notification,
    get_notification_preferences,
    list_active_presence,
    list_notifications,
    mark_all_notifications_read,
    mark_notification_read,
    remove_dataset_presence,
    repository_activity_matrix,
    repository_activity_overview,
    set_notification_preference,
    touch_dataset_presence,
)

from .store.sync_store import (
    _risk_score,
    _snapshot_from_connection,
    _store_dataset_version,
    _values_match,
    apply_bulk_updates,
    apply_workbook_commit,
    get_audit_history,
    get_table_snapshot,
    read_cell,
    rollback_batch,
    update_cell,
)

from .store.kpi_store import (
    get_kpis,
)

