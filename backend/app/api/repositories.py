"""Stage 1 category, repository, branch, and working-copy routes."""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from ..database import (
    create_category,
    create_semantic_branch,
    delete_branch,
    delete_repository,
    get_branch_protection,
    get_branch_sheet_page,
    get_repository,
    get_repository_organization_id,
    list_categories,
    list_repository_branches,
    list_repository_devices,
    list_user_working_copies,
    move_repository,
    repository_activity_matrix,
    repository_activity_overview,
    repository_name_available,
    set_device_trust_status,
    update_branch_protection,
    working_copy_checkout_options,
)
from ..access_control.service import primary_organization
from ..ai.discovery_agent import start_discovery
from ..ai.formula_explainer import get_cached_explanation, start_formula_explanation
from ..ai.insights_agent import get_result_by_id as get_data_insight_result, start_data_insight
from ..ai.rbac_anomaly import list_findings as list_rbac_anomaly_findings, resolve_finding as resolve_rbac_anomaly_finding, start_rbac_anomaly_scan
from ..services.expertise_service import repository_expertise
from ..services.team_signals import (
    check_team_signals,
    detect_behavioral_anomalies,
    onboarding_ramp,
    team_health_digest,
)
from .ai_platform import _raise
from ..database import (
    user_can_access_branch,
    user_can_work_on_repository,
    get_system_setting,
    set_system_setting,
    update_branch_local_path,
)
from ..schemas import BranchCreateRequest, CategoryCreateRequest, RepositoryCategoryRequest, WorkingCopyRequest, EucStorageSettingsRequest


class BranchProtectionRequest(BaseModel):
    allow_direct_commits: bool = False
    required_approvals: int = Field(default=1, ge=0, le=10)
    require_validation: bool = True


class FormulaExplainRequest(BaseModel):
    branch_id: str = Field(min_length=1, max_length=100)
    # Only the branch is truly required -- sheet/cell/formula are all
    # optional overrides for someone who already knows exactly which
    # formula they want explained. Left blank, the agent picks the first
    # real formula it finds in the branch for you (see _discover_formula).
    sheet_name: str | None = Field(default=None, max_length=200)
    cell_address: str | None = Field(default=None, max_length=40)
    formula: str | None = Field(default=None, max_length=8000)


class DiscoveryRequest(BaseModel):
    # Optional: left blank, the agent ranks by overall repository expertise
    # instead of a specific topic (see discovery_agent._rank_candidates's
    # existing no-sheet-matched fallback).
    topic: str = Field(default="", max_length=200)


class DataInsightRequest(BaseModel):
    branch_id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=3, max_length=1000)


class RbacFindingDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(ACKNOWLEDGED|DISMISSED|RESOLVED)$")
    reason: str = Field(min_length=2, max_length=1000)
from ..security import Principal, current_principal
from ..repositories.merge_store import branch_context
from ..services.workbook_service import export_branch_workbook, issue_branch_workbook
from ..services.branch_lifecycle_manager import default_gitwalk_directory, validate_storage_drive
from ..observability import record_audit_event


router = APIRouter(prefix="/api/v1", tags=["repositories"])


@router.get("/categories")
async def categories(_principal: Principal = Depends(current_principal)):
    return {"categories": list_categories()}


@router.get("/repositories/name-availability")
async def repository_availability(
    name: str = Query(min_length=1, max_length=120),
    _principal: Principal = Depends(current_principal),
):
    return repository_name_available(name)


@router.post("/categories")
async def add_category(
    payload: CategoryCreateRequest,
    principal: Principal = Depends(current_principal),
):
    try:
        result = create_category(payload.name, payload.description, payload.parent_category_id)
        record_audit_event(
            "CATEGORY_CREATED", actor_user_id=principal.user_id,
            payload={"category_id": result["category_id"], "name": result["name"]},
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/repositories/{table_id}")
async def repository_detail(
    table_id: str,
    principal: Principal = Depends(current_principal),
):
    repository = get_repository(table_id.strip().upper(), principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository does not exist or is not accessible")
    return repository


def _require_repository_owner(table_id: str, principal: Principal):
    repository = get_repository(table_id.strip().upper(), principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository does not exist or is not accessible")
    if repository.get("repository_role") != "owner":
        raise HTTPException(status_code=403, detail="Only the repository owner can view this")
    return repository


@router.get("/repositories/{table_id}/branch-protection")
async def branch_protection(table_id: str, principal: Principal = Depends(current_principal)):
    repository = _require_repository_owner(table_id, principal)
    default_branch_id = repository.get("default_branch_id")
    if not default_branch_id:
        raise HTTPException(status_code=404, detail="Repository has no default branch")
    rule = get_branch_protection(repository["repository_id"], default_branch_id)
    return {"branch_id": default_branch_id, "rule": rule or {
        "allow_direct_commits": False, "required_approvals": 1, "require_validation": True,
    }}


@router.put("/repositories/{table_id}/branch-protection")
async def update_branch_protection_route(
    table_id: str, payload: BranchProtectionRequest, principal: Principal = Depends(current_principal),
):
    repository = _require_repository_owner(table_id, principal)
    default_branch_id = repository.get("default_branch_id")
    if not default_branch_id:
        raise HTTPException(status_code=404, detail="Repository has no default branch")
    organization_id = get_repository_organization_id(repository["repository_id"])
    rule = update_branch_protection(
        repository["repository_id"], default_branch_id, organization_id,
        payload.allow_direct_commits, payload.required_approvals, payload.require_validation, principal.user_id,
    )
    record_audit_event(
        "BRANCH_PROTECTION_UPDATED", actor_user_id=principal.user_id,
        repository_id=repository["repository_id"], branch_id=default_branch_id,
        payload={"allow_direct_commits": payload.allow_direct_commits, "required_approvals": payload.required_approvals,
                 "require_validation": payload.require_validation},
    )
    return {"branch_id": default_branch_id, "rule": rule}


@router.get("/repositories/{table_id}/devices")
async def repository_devices(
    table_id: str,
    principal: Principal = Depends(current_principal),
):
    repository = _require_repository_owner(table_id, principal)
    return {"devices": list_repository_devices(repository["repository_id"])}


@router.post("/repositories/{table_id}/devices/{fingerprint_id}/trust")
async def trust_repository_device(
    table_id: str,
    fingerprint_id: str,
    principal: Principal = Depends(current_principal),
):
    _require_repository_owner(table_id, principal)
    try:
        device = set_device_trust_status(fingerprint_id, "TRUSTED")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    record_audit_event(
        "DEVICE_TRUSTED", actor_user_id=principal.user_id,
        payload={"fingerprint_id": fingerprint_id, "device_user_id": device["user_id"]},
    )
    return device


@router.post("/repositories/{table_id}/devices/{fingerprint_id}/block")
async def block_repository_device(
    table_id: str,
    fingerprint_id: str,
    principal: Principal = Depends(current_principal),
):
    _require_repository_owner(table_id, principal)
    try:
        device = set_device_trust_status(fingerprint_id, "BLOCKED")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    record_audit_event(
        "DEVICE_BLOCKED", actor_user_id=principal.user_id,
        payload={"fingerprint_id": fingerprint_id, "device_user_id": device["user_id"]},
    )
    return device


@router.get("/repositories/{table_id}/activity")
async def repository_activity(
    table_id: str,
    principal: Principal = Depends(current_principal),
):
    repository = get_repository(table_id.strip().upper(), principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository does not exist or is not accessible")
    try:
        overview = repository_activity_overview(repository["repository_id"])
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # Reactive, best-effort side-check (this product has no background
    # scheduler by design) — piggybacks on a read every viewer already
    # triggers every ~20s, so real team-health problems get surfaced as
    # notifications without needing a dedicated poller. Never raises.
    # Runs in the threadpool, not inline in this async handler -- it does
    # three synchronous sqlite scans, which would otherwise block the
    # event loop (and every other in-flight request) for their duration.
    if repository.get("owner_user_id"):
        await run_in_threadpool(
            check_team_signals, repository["repository_id"], repository["owner_user_id"],
            get_repository_organization_id(repository["repository_id"]),
        )
    return {"activity": overview}


@router.get("/repositories/{table_id}/activity/matrix")
async def repository_activity_matrix_route(
    table_id: str,
    days: int = Query(default=14, ge=1, le=60),
    principal: Principal = Depends(current_principal),
):
    repository = get_repository(table_id.strip().upper(), principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository does not exist or is not accessible")
    try:
        return repository_activity_matrix(repository["repository_id"], days)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/repositories/{table_id}/expertise")
async def repository_expertise_route(
    table_id: str,
    days: int = Query(default=90, ge=1, le=365),
    principal: Principal = Depends(current_principal),
):
    repository = get_repository(table_id.strip().upper(), principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository does not exist or is not accessible")
    return repository_expertise(repository["repository_id"], days)


@router.get("/repositories/{table_id}/onboarding-ramp")
async def repository_onboarding_ramp_route(
    table_id: str,
    principal: Principal = Depends(current_principal),
):
    repository = get_repository(table_id.strip().upper(), principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository does not exist or is not accessible")
    return onboarding_ramp(repository["repository_id"])


@router.get("/repositories/{table_id}/anomalies")
async def repository_anomalies_route(
    table_id: str,
    days: int = Query(default=30, ge=7, le=180),
    principal: Principal = Depends(current_principal),
):
    repository = get_repository(table_id.strip().upper(), principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository does not exist or is not accessible")
    return detect_behavioral_anomalies(repository["repository_id"], days)


@router.get("/repositories/{table_id}/team-health-digest")
async def repository_team_health_digest_route(
    table_id: str,
    organization_id: str | None = Query(default=None),
    use_ai: bool = Query(default=True),
    principal: Principal = Depends(current_principal),
):
    repository = get_repository(table_id.strip().upper(), principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository does not exist or is not accessible")
    org_id = organization_id or primary_organization(principal.user_id)
    if not org_id:
        raise HTTPException(status_code=404, detail="No organization is available for this account")
    return await team_health_digest(org_id, principal.user_id, repository["repository_id"], use_ai=use_ai)


@router.get("/repositories/{table_id}/members/{user_id}/profile")
async def repository_member_profile_route(
    table_id: str,
    user_id: str,
    principal: Principal = Depends(current_principal),
):
    repository = get_repository(table_id.strip().upper(), principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository does not exist or is not accessible")
    repository_id = repository["repository_id"]
    is_owner = repository.get("repository_role") == "owner"

    activity = repository_activity_overview(repository_id)
    member_activity = next((item for item in activity if item["user_id"] == user_id), None)
    if not member_activity:
        raise HTTPException(status_code=404, detail="This user is not a member of this repository")

    expertise = repository_expertise(repository_id)
    member_expertise = next((item for item in expertise["experts"] if item["user_id"] == user_id), None)

    ramp = onboarding_ramp(repository_id)
    member_ramp = next((item for item in ramp["members"] if item["user_id"] == user_id), None)

    profile = {
        "user_id": user_id,
        "email": member_activity.get("email"),
        "display_name": member_activity.get("display_name"),
        "role": member_activity.get("role"),
        "status": member_activity.get("status"),
        "last_seen_at": member_activity.get("last_seen_at"),
        "hours_active_today": member_activity.get("hours_active_today"),
        "commits": member_expertise["commits"] if member_expertise else 0,
        "last_commit_at": member_expertise["last_commit_at"] if member_expertise else None,
        "open_merge_requests": member_expertise["open_merge_requests"] if member_expertise else 0,
        "open_workload_days": member_expertise["open_workload_days"] if member_expertise else 0.0,
        "top_sheets": member_expertise["top_sheets"] if member_expertise else [],
        "onboarding": member_ramp,
    }
    if is_owner:
        profile["devices"] = [
            device for device in list_repository_devices(repository_id) if device.get("user_id") == user_id
        ]
    return profile


def _discover_formula(branch_state: dict) -> tuple[str, str, str] | None:
    """Scan a branch's sheets, in order, for the first cell that actually
    has a formula -- so the Formula Explainer can work from just a branch,
    without the caller already needing to know which cell to ask about.
    Returns ``(sheet_name, cell_address, formula)`` or ``None`` if this
    branch has no formulas anywhere."""
    from ..excel.identity import column_letter

    for sheet in branch_state.get("sheets", []):
        columns_by_id = {column["column_id"]: column for column in sheet.get("columns", [])}
        for row in sorted(sheet.get("rows", []), key=lambda item: item["position"]):
            for column_id, formula in (row.get("formulas") or {}).items():
                if not formula:
                    continue
                column = columns_by_id.get(column_id)
                if not column:
                    continue
                cell_address = f"{column_letter(column['position'])}{row['position'] + 2}"
                return sheet["name"], cell_address, formula
    return None


def _resolve_formula_cell(
    branch_id: str, sheet_name: str | None, cell_address: str | None, formula: str | None,
) -> tuple[str, str, str, str, str, str]:
    """The taskpane/store only ever knows Excel-native (sheet name, A1
    address) -- resolve that back to this product's stable (sheet_id,
    row_id, column_id) here, server-side. When sheet/cell/formula are
    omitted, auto-discovers the first real formula in the branch instead of
    requiring the caller to already know one (see _discover_formula).
    Returns (sheet_id, row_id, column_id, resolved_sheet_name,
    resolved_cell_address, resolved_formula) so the caller can report back
    exactly what was picked."""
    from ..euc.branch_comparison import resolve_cell_identity
    from ..repositories.commit_store import reconstruct_branch

    branch_state = reconstruct_branch(branch_id)
    if not sheet_name or not cell_address or not formula:
        discovered = _discover_formula(branch_state)
        if not discovered:
            raise HTTPException(status_code=404, detail="This branch has no formulas yet — nothing to explain.")
        sheet_name, cell_address, formula = discovered
    sheet = next((item for item in branch_state.get("sheets", []) if item.get("name") == sheet_name), None)
    if not sheet:
        raise HTTPException(status_code=404, detail="Sheet not found on this branch")
    identity = resolve_cell_identity(branch_state, sheet["sheet_id"], cell_address.replace("$", ""))
    if not identity:
        raise HTTPException(status_code=404, detail="Cell address is outside the sheet's data region")
    return identity["sheet_id"], identity["row_id"], identity["column_id"], sheet_name, cell_address, formula


@router.post("/repositories/{table_id}/formula-explanation")
async def start_formula_explanation_route(
    table_id: str, payload: FormulaExplainRequest,
    organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal),
):
    repository = get_repository(table_id.strip().upper(), principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository does not exist or is not accessible")
    org_id = organization_id or primary_organization(principal.user_id)
    if not org_id:
        raise HTTPException(status_code=404, detail="No organization is available for this account")
    sheet_id, row_id, column_id, sheet_name, cell_address, formula = _resolve_formula_cell(
        payload.branch_id, payload.sheet_name, payload.cell_address, payload.formula,
    )
    try:
        result = start_formula_explanation(
            org_id, principal.user_id, repository["repository_id"], payload.branch_id, sheet_id,
            row_id, column_id, formula, cell_address,
        )
        return {**result, "sheet_name": sheet_name, "cell_address": cell_address, "formula": formula}
    except Exception as exc: _raise(exc)


@router.get("/repositories/{table_id}/formula-explanation")
async def get_formula_explanation_route(
    table_id: str, branch_id: str = Query(...), sheet_name: str = Query(...), cell_address: str = Query(...),
    formula: str = Query(...), principal: Principal = Depends(current_principal),
):
    import hashlib, json
    repository = get_repository(table_id.strip().upper(), principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository does not exist or is not accessible")
    sheet_id, row_id, column_id, _, _, _ = _resolve_formula_cell(branch_id, sheet_name, cell_address, formula)
    formula_hash = hashlib.sha256(json.dumps(formula, sort_keys=True).encode()).hexdigest()
    cached = get_cached_explanation(branch_id, sheet_id, row_id, column_id, formula_hash)
    if not cached:
        raise HTTPException(status_code=404, detail="No explanation is cached for this formula yet")
    return cached


@router.post("/repositories/{table_id}/insights/ask")
async def start_data_insight_route(
    table_id: str, payload: DataInsightRequest, principal: Principal = Depends(current_principal),
):
    repository = get_repository(table_id.strip().upper(), principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository does not exist or is not accessible")
    org_id = get_repository_organization_id(repository["repository_id"])
    if not org_id:
        raise HTTPException(status_code=404, detail="No organization is available for this repository")
    try:
        return start_data_insight(
            org_id, principal.user_id, repository["repository_id"], table_id.strip().upper(),
            payload.branch_id, payload.question.strip(),
        )
    except Exception as exc: _raise(exc)


@router.get("/repositories/{table_id}/insights/results/{insight_result_id}")
async def get_data_insight_result_route(
    table_id: str, insight_result_id: str, principal: Principal = Depends(current_principal),
):
    repository = get_repository(table_id.strip().upper(), principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository does not exist or is not accessible")
    result = get_data_insight_result(insight_result_id)
    if not result or result.get("repository_id") != repository["repository_id"]:
        raise HTTPException(status_code=404, detail="No insight result found with this id")
    return result


@router.post("/repositories/{table_id}/discovery")
async def start_discovery_route(
    table_id: str, payload: DiscoveryRequest,
    organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal),
):
    repository = get_repository(table_id.strip().upper(), principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository does not exist or is not accessible")
    org_id = organization_id or primary_organization(principal.user_id)
    if not org_id:
        raise HTTPException(status_code=404, detail="No organization is available for this account")
    try:
        return start_discovery(org_id, principal.user_id, repository["repository_id"], payload.topic.strip())
    except Exception as exc: _raise(exc)


@router.post("/repositories/{table_id}/rbac-anomalies/scan")
async def start_rbac_anomaly_scan_route(
    table_id: str, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal),
):
    repository = get_repository(table_id.strip().upper(), principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository does not exist or is not accessible")
    if repository.get("repository_role") != "owner":
        raise HTTPException(status_code=403, detail="Only the repository owner can run an RBAC anomaly scan")
    org_id = organization_id or primary_organization(principal.user_id)
    if not org_id:
        raise HTTPException(status_code=404, detail="No organization is available for this account")
    try:
        return start_rbac_anomaly_scan(org_id, principal.user_id, repository["repository_id"])
    except Exception as exc: _raise(exc)


@router.get("/repositories/{table_id}/rbac-anomalies")
async def list_rbac_anomalies_route(
    table_id: str, status: str | None = Query(default=None), principal: Principal = Depends(current_principal),
):
    repository = get_repository(table_id.strip().upper(), principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository does not exist or is not accessible")
    if repository.get("repository_role") != "owner":
        raise HTTPException(status_code=403, detail="Only the repository owner can view RBAC anomaly findings")
    return {"findings": list_rbac_anomaly_findings(repository["repository_id"], status)}


@router.post("/repositories/{table_id}/rbac-anomalies/{finding_id}/decision")
async def decide_rbac_anomaly_route(
    table_id: str, finding_id: str, payload: RbacFindingDecisionRequest, principal: Principal = Depends(current_principal),
):
    repository = get_repository(table_id.strip().upper(), principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository does not exist or is not accessible")
    if repository.get("repository_role") != "owner":
        raise HTTPException(status_code=403, detail="Only the repository owner can decide on an RBAC anomaly finding")
    try:
        return resolve_rbac_anomaly_finding(repository["repository_id"], finding_id, principal.user_id, payload.decision, payload.reason)
    except Exception as exc: _raise(exc)


@router.patch("/repositories/{table_id}/category")
async def repository_category(
    table_id: str,
    payload: RepositoryCategoryRequest,
    principal: Principal = Depends(current_principal),
):
    try:
        result = move_repository(
            table_id.strip().upper(), payload.category_id, principal.user_id
        )
        record_audit_event(
            "REPOSITORY_UPDATED", actor_user_id=principal.user_id,
            repository_id=result["repository_id"],
            payload={"category_id": payload.category_id},
        )
        return result
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/repositories/{table_id}/branches")
async def repository_branches(
    table_id: str,
    principal: Principal = Depends(current_principal),
):
    repository = get_repository(table_id.strip().upper(), principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository does not exist or is not accessible")
    return {"branches": list_repository_branches(table_id.strip().upper(), principal.user_id)}


@router.post("/repositories/{table_id}/branches", status_code=201)
async def create_branch_pointer(
    table_id: str,
    payload: BranchCreateRequest,
    principal: Principal = Depends(current_principal),
):
    try:
        result = create_semantic_branch(
            table_id.strip().upper(), payload.name, payload.from_commit_id, principal.user_id
        )
        record_audit_event(
            "BRANCH_CREATED", actor_user_id=principal.user_id,
            repository_id=get_repository(table_id.strip().upper(), principal.user_id)["repository_id"],
            branch_id=result["branch_id"], payload={"base_commit_id": result["base_commit_id"],
                                                     "storage_bytes_added": 0},
        )
        return result
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/repositories/{table_id}/branches/{branch_id}/sheets/{sheet_id}/records")
async def repository_sheet_records(
    table_id: str,
    branch_id: str,
    sheet_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(current_principal),
):
    try:
        return get_branch_sheet_page(
            table_id.strip().upper(), branch_id, sheet_id,
            principal.user_id, limit, offset,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/repositories/{table_id}/branches/{branch_id}/download")
async def download_repository_branch(
    table_id: str,
    branch_id: str,
    principal: Principal = Depends(current_principal),
):
    normalized = table_id.strip().upper()
    repository = get_repository(normalized, principal.user_id)
    branch = branch_context(branch_id)
    if (
        not repository or not branch
        or branch["repository_id"] != repository["repository_id"]
        or not user_can_access_branch(branch_id, principal.user_id)
    ):
        raise HTTPException(status_code=404, detail="Branch does not exist or is not accessible")
    result = export_branch_workbook(
        branch["data_table_id"], branch_id, branch["branch_name"]
    )
    record_audit_event(
        "WORKBOOK_DOWNLOADED", actor_user_id=principal.user_id,
        repository_id=branch["repository_id"], branch_id=branch_id,
        payload={"purpose": "complete_branch_export"},
    )
    return FileResponse(
        result["path"],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{branch['branch_name'].replace('/', '_')}.xlsx",
        background=BackgroundTask(shutil.rmtree, Path(result["path"]).parent, True),
    )


@router.delete("/repositories/{table_id}")
async def remove_repository(
    table_id: str,
    principal: Principal = Depends(current_principal),
):
    try:
        # delete_repository() now runs the full permanent-deletion pipeline
        # (export -> email -> notify -> hard delete) and already records
        # its own REPOSITORY_PURGED audit event — see
        # services/repository_purge_service.py — so no separate audit call
        # is needed here.
        result = delete_repository(table_id.strip().upper(), principal.user_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return result


@router.delete("/branches/{branch_id}")
async def remove_branch(
    branch_id: str,
    principal: Principal = Depends(current_principal),
):
    context = branch_context(branch_id)
    try:
        result = delete_branch(branch_id, principal.user_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    record_audit_event(
        "BRANCH_DELETED", actor_user_id=principal.user_id,
        repository_id=context["repository_id"] if context else None,
        branch_id=branch_id, payload={"working_copy_status": "REVOKED"},
    )
    return result


@router.get("/settings/euc-storage")
async def get_euc_storage_setting(_principal: Principal = Depends(current_principal)):
    stored_dir = get_system_setting("euc_download_dir", "")
    exists = False
    if stored_dir:
        try:
            p = validate_storage_drive(stored_dir)
            exists = p.exists() and p.is_dir()
        except Exception:
            exists = False
    return {
        "local_download_dir": stored_dir or "",
        "exists": exists,
    }


@router.post("/settings/euc-storage")
async def save_euc_storage_setting(
    payload: EucStorageSettingsRequest,
    principal: Principal = Depends(current_principal),
):
    try:
        valid_dir = validate_storage_drive(payload.local_download_dir)
        valid_dir.mkdir(parents=True, exist_ok=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed creating directory: {exc}") from exc

    set_system_setting("euc_download_dir", str(valid_dir), principal.user_id)
    return {
        "status": "SAVED",
        "local_download_dir": str(valid_dir),
        "exists": True,
        "message": f"EUC download folder set to: {valid_dir}",
    }


@router.post("/repositories/{table_id}/work-on-workbook")
async def work_on_workbook(
    table_id: str,
    payload: WorkingCopyRequest,
    principal: Principal = Depends(current_principal),
):
    normalized = table_id.strip().upper()
    if not user_can_work_on_repository(normalized, principal.user_id):
        raise HTTPException(status_code=403, detail="Editor access is required to create a branch")

    repository = get_repository(normalized, principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository does not exist or is not accessible")

    # Always the predictable per-repository folder under the user's home
    # directory (<home>/gitwalk/<repo>) unless THIS request explicitly asks
    # for a different one -- the old remembered "euc_download_dir" system
    # setting is intentionally no longer consulted here, so a stale/custom
    # value configured earlier can't silently redirect every future save.
    target_dir_str = payload.local_download_dir or str(
        default_gitwalk_directory(repository["repository_name"])
    )
    try:
        local_target_dir = validate_storage_drive(target_dir_str)
        local_target_dir.mkdir(parents=True, exist_ok=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed accessing local directory: {exc}") from exc

    try:
        result = issue_branch_workbook(
            normalized, principal.user_id, principal.email,
            branch_mode=payload.mode, branch_id=payload.branch_id,
            local_target_dir=str(local_target_dir),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    filename = f"gitwalk_{result['branch_name'].replace('/', '_')}.xlsm"
    local_saved_file = Path(result.get("local_file_path") or (local_target_dir / filename))
    shutil.copy2(result["path"], local_saved_file)
    update_branch_local_path(result["branch_id"], str(local_saved_file))
    shutil.rmtree(Path(result["path"]).parent, ignore_errors=True)

    record_audit_event(
        "WORKING_COPY_CREATED", actor_user_id=principal.user_id,
        repository_id=result["repository_id"], branch_id=result["branch_id"],
        working_copy_id=result["working_copy_id"],
        payload={"branch_name": result["branch_name"], "local_path": str(local_saved_file)},
    )
    record_audit_event(
        "WORKBOOK_DOWNLOADED", actor_user_id=principal.user_id,
        repository_id=result["repository_id"], branch_id=result["branch_id"],
        working_copy_id=result["working_copy_id"],
        payload={"purpose": "working_copy", "local_path": str(local_saved_file)},
    )
    return {
        "table_id": normalized,
        "branch_table_id": result["table_id"],
        "repository_id": result["repository_id"],
        "branch_id": result["branch_id"],
        "branch_name": result["branch_name"],
        "working_copy_id": result["working_copy_id"],
        "local_path": str(local_saved_file),
    }


@router.get("/repositories/{table_id}/checkout-options")
async def checkout_options(
    table_id: str,
    principal: Principal = Depends(current_principal),
):
    try:
        return working_copy_checkout_options(
            table_id.strip().upper(), principal.user_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/working-copies/mine")
async def my_working_copies(principal: Principal = Depends(current_principal)):
    return {"working_copies": list_user_working_copies(principal.user_id)}
