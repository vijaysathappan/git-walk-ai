"""Stage 4 audit, observability, analytics, and workbook-lineage APIs."""

from fastapi import APIRouter, Depends, HTTPException, Query

from ..database import user_can_access_branch, user_can_access_table
from ..repositories.governance_store import (
    cell_traceability,
    list_audit_events,
    operational_metrics,
    operational_metrics_trend,
    repository_id_for_table,
    repository_insights,
    row_history,
    verify_audit_integrity,
    workbook_blame,
    workbook_change_activity,
)
from ..repositories.merge_store import branch_context, repository_role
from ..security import Principal, current_principal
from ..config import settings
from ..observability import record_audit_event


router = APIRouter(prefix="/api/v1", tags=["governance"])


def _branch_access(branch_id: str, principal: Principal) -> dict:
    branch = branch_context(branch_id)
    if not branch or not user_can_access_branch(branch_id, principal.user_id):
        raise HTTPException(status_code=403, detail="Branch access is required")
    return branch


@router.get("/audit/events")
async def audit_events(
    table_id: str | None = None,
    event_type: str | None = None,
    status: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    since: str | None = None,
    until: str | None = None,
    principal: Principal = Depends(current_principal),
):
    repository_id = None
    actor_user_id = None
    if table_id:
        normalized = table_id.strip().upper()
        if not user_can_access_table(normalized, principal.user_id):
            raise HTTPException(status_code=403, detail="Repository access is required")
        repository_id = repository_id_for_table(normalized)
    elif principal.user_id != "USR_SYSTEM":
        actor_user_id = principal.user_id
    return {
        "events": list_audit_events(
            repository_id=repository_id, actor_user_id=actor_user_id,
            event_type=event_type, status=status, limit=limit,
            since=since, until=until,
        ),
        "integrity": verify_audit_integrity(),
    }


@router.get("/observability/metrics")
async def metrics(
    hours: int = Query(default=24, ge=1, le=2160),
    _principal: Principal = Depends(current_principal),
):
    return operational_metrics(hours)


@router.get("/observability/metrics/trend")
async def metrics_trend(
    hours: int = Query(default=24 * 14, ge=1, le=24 * 180),
    _principal: Principal = Depends(current_principal),
):
    return {"trend": operational_metrics_trend(hours)}


@router.get("/security/posture")
async def security_posture(_principal: Principal = Depends(current_principal)):
    return {
        "environment": settings.app_env,
        "controls": {
            "authentication_required": settings.auth_required,
            "otp_single_use": True,
            "session_revocation": True,
            "signed_working_copies": True,
            "protected_main": True,
            "maker_checker": True,
            "append_only_audit": True,
            "encrypted_ai_keys": True,
            "bounded_xlsx_processing": True,
        },
        "upload_limits": {
            "bytes": settings.max_upload_bytes,
            "zip_entries": settings.max_xlsx_entries,
            "expanded_bytes": settings.max_xlsx_uncompressed_bytes,
            "sheets": settings.max_workbook_sheets,
            "rows": settings.max_workbook_rows,
            "columns": settings.max_workbook_columns,
            "timeout_seconds": settings.upload_processing_timeout_seconds,
        },
    }


@router.get("/repositories/{table_id}/insights")
async def insights(table_id: str, principal: Principal = Depends(current_principal)):
    normalized = table_id.strip().upper()
    if not user_can_access_table(normalized, principal.user_id):
        raise HTTPException(status_code=403, detail="Repository access is required")
    repository_id = repository_id_for_table(normalized)
    if not repository_id:
        raise HTTPException(status_code=404, detail="Repository does not exist")
    return repository_insights(repository_id)


@router.get("/branches/{branch_id}/blame")
async def blame(
    branch_id: str,
    sheet_id: str | None = None,
    limit: int = Query(default=5000, ge=1, le=20000),
    principal: Principal = Depends(current_principal),
):
    _branch_access(branch_id, principal)
    return workbook_blame(branch_id, sheet_id, limit)


@router.get("/branches/{branch_id}/change-activity")
async def change_activity(
    branch_id: str,
    sheet_id: str | None = None,
    operation: str | None = None,
    sort: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=1000, ge=1, le=5000),
    principal: Principal = Depends(current_principal),
):
    _branch_access(branch_id, principal)
    return workbook_change_activity(branch_id, sheet_id, operation, sort, limit)


@router.get("/branches/{branch_id}/rows/{sheet_id}/{row_id}/history")
async def branch_row_history(
    branch_id: str, sheet_id: str, row_id: str,
    principal: Principal = Depends(current_principal),
):
    _branch_access(branch_id, principal)
    return {"history": row_history(branch_id, sheet_id, row_id)}


@router.get("/branches/{branch_id}/cells/{sheet_id}/{row_id}/{column_id}/traceability")
async def trace_cell(
    branch_id: str, sheet_id: str, row_id: str, column_id: str,
    principal: Principal = Depends(current_principal),
):
    branch = _branch_access(branch_id, principal)
    try:
        result = cell_traceability(branch_id, sheet_id, row_id, column_id)
        record_audit_event(
            "CELL_READ", actor_user_id=principal.user_id,
            repository_id=branch["repository_id"], branch_id=branch_id,
            commit_id=result.get("last_commit_id"),
            payload={
                "sheet_id": sheet_id, "sheet_name": result.get("sheet_name"),
                "row_id": row_id, "column_id": column_id,
                "column_name": result.get("column_name"),
                "row_position": result.get("row_position"),
                "column_position": result.get("column_position"),
                "value": result.get("formula") or result.get("value"),
            },
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
