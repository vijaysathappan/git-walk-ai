"""Stage 3 merge request, review, validation, sync, and revert APIs."""

from fastapi import APIRouter, Depends, HTTPException, Query

from ..access_control.service import primary_organization
from ..ai.gateway import AIProviderError
from ..ai.merge_agent import (
    analyze_merge_request,
    assess_merge_request,
    get_latest_assessment,
    list_suggestions,
    prepare_apply_action,
    start_conflict_analysis,
    start_merge_assessment,
)
from ..database import get_repository
from ..services.expertise_service import suggest_reviewers
from ..services import reviewer_request_service
from ..schemas import (
    ConflictResolutionRequest,
    MergeRequestCreate,
    MergeReviewRequest,
    ReviewerRequestCreate,
    ReviewerResponseRequest,
)
from ..security import Principal, current_principal
from ..services.merge_service import (
    MergeActor,
    MergeConflictError,
    MergeHeadChangedError,
    merge_service,
)


router = APIRouter(prefix="/api/v1", tags=["merge-requests"])


def _actor(principal: Principal) -> MergeActor:
    return MergeActor(principal.user_id, principal.email)


def _organization(requested: str | None, principal: Principal) -> str:
    organization_id = requested or primary_organization(principal.user_id)
    if not organization_id:
        raise HTTPException(status_code=404, detail="No organization is available for this account")
    return organization_id


def _raise_ai(exc: Exception) -> None:
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail={"code": "AI_POLICY_DENIED", "message": str(exc)}) from exc
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    if isinstance(exc, AIProviderError):
        status = 503 if exc.transient or exc.code in {"AI_PROVIDER_NOT_CONFIGURED", "AI_PROVIDER_UNAVAILABLE"} else 422
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc), "transient": exc.transient}) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


def _raise(exc: Exception) -> None:
    if isinstance(exc, MergeConflictError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MERGE_CONFLICTS",
                "message": str(exc),
                "conflicts": exc.conflicts,
            },
        ) from exc
    if isinstance(exc, MergeHeadChangedError):
        raise HTTPException(
            status_code=409,
            detail={"code": "MERGE_HEAD_CHANGED", "message": str(exc)},
        ) from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/branches/{branch_id}/divergence")
async def branch_divergence(
    branch_id: str,
    principal: Principal = Depends(current_principal),
):
    from ..repositories.merge_store import branch_context

    branch = branch_context(branch_id)
    if not branch:
        raise HTTPException(status_code=404, detail="Branch does not exist")
    try:
        return merge_service.branch_divergence(
            branch_id, branch["default_branch_id"], _actor(principal)
        )
    except (PermissionError, ValueError, MergeHeadChangedError) as exc:
        _raise(exc)


@router.post("/branches/{branch_id}/sync")
async def sync_branch(
    branch_id: str,
    principal: Principal = Depends(current_principal),
):
    try:
        return merge_service.sync_branch(branch_id, _actor(principal))
    except (PermissionError, ValueError, MergeConflictError) as exc:
        _raise(exc)


@router.get("/repositories/{table_id}/merge-requests")
async def repository_merge_requests(
    table_id: str,
    principal: Principal = Depends(current_principal),
):
    repository = get_repository(table_id.strip().upper(), principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository does not exist")
    try:
        return {
            "merge_requests": merge_service.list_requests(
                repository["repository_id"], _actor(principal)
            )
        }
    except PermissionError as exc:
        _raise(exc)


@router.post("/merge-requests")
async def create_merge_request(
    payload: MergeRequestCreate,
    principal: Principal = Depends(current_principal),
):
    try:
        return merge_service.create_request(
            source_branch_id=payload.source_branch_id,
            target_branch_id=payload.target_branch_id,
            title=payload.title,
            description=payload.description,
            actor=_actor(principal),
        )
    except (PermissionError, ValueError, MergeConflictError) as exc:
        _raise(exc)


@router.get("/merge-requests/{merge_request_id}")
async def merge_request_detail(
    merge_request_id: str,
    principal: Principal = Depends(current_principal),
):
    try:
        return merge_service.get_request(merge_request_id, _actor(principal))
    except (PermissionError, ValueError) as exc:
        _raise(exc)


@router.get("/merge-requests/{merge_request_id}/suggest-reviewer")
async def merge_request_suggest_reviewer(
    merge_request_id: str,
    organization_id: str | None = Query(default=None),
    use_ai: bool = Query(default=True),
    principal: Principal = Depends(current_principal),
):
    try:
        # Access-checked read: raises if the caller cannot see this merge
        # request, and its semantic diff already carries the sheet_ids the
        # ranking is based on — never re-derived or re-authorized here.
        request = merge_service.get_request(merge_request_id, _actor(principal))
    except (PermissionError, ValueError) as exc:
        _raise(exc)
    return await suggest_reviewers(
        _organization(organization_id, principal), principal.user_id, request, use_ai=use_ai,
    )


@router.post("/merge-requests/{merge_request_id}/conflicts/{conflict_id}/resolve")
async def resolve_merge_conflict(
    merge_request_id: str,
    conflict_id: str,
    payload: ConflictResolutionRequest,
    principal: Principal = Depends(current_principal),
):
    try:
        return merge_service.resolve_conflict(
            merge_request_id=merge_request_id,
            conflict_id=conflict_id,
            resolution_type=payload.resolution_type,
            custom_value=payload.custom_value,
            actor=_actor(principal),
        )
    except (PermissionError, ValueError, MergeConflictError) as exc:
        _raise(exc)


@router.post("/merge-requests/{merge_request_id}/ai-analyze")
async def analyze_merge_request_conflicts(
    merge_request_id: str,
    organization_id: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    try:
        return await analyze_merge_request(
            _organization(organization_id, principal), principal.user_id, merge_request_id, _actor(principal)
        )
    except (PermissionError, ValueError, KeyError, AIProviderError) as exc:
        _raise_ai(exc)


@router.post("/merge-requests/{merge_request_id}/ai-analyze/start")
async def start_analyze_merge_request_conflicts(
    merge_request_id: str,
    organization_id: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    """Background-launching twin of ``ai-analyze``: returns immediately so
    the frontend can drive the live-progress timeline instead of blocking
    on the full multi-conflict analysis."""
    try:
        return start_conflict_analysis(
            _organization(organization_id, principal), principal.user_id, merge_request_id, _actor(principal)
        )
    except (PermissionError, ValueError, KeyError, AIProviderError) as exc:
        _raise_ai(exc)


@router.get("/merge-requests/{merge_request_id}/ai-suggestions")
async def merge_request_ai_suggestions(
    merge_request_id: str,
    principal: Principal = Depends(current_principal),
):
    try:
        # Access-checked read: raises if the caller cannot see this merge request.
        merge_service.get_request(merge_request_id, _actor(principal))
    except (PermissionError, ValueError) as exc:
        _raise(exc)
    return {"suggestions": list_suggestions(merge_request_id)}


@router.post("/merge-requests/{merge_request_id}/conflicts/{conflict_id}/ai-apply")
async def prepare_ai_conflict_apply(
    merge_request_id: str,
    conflict_id: str,
    organization_id: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    try:
        return prepare_apply_action(
            _organization(organization_id, principal), principal.user_id,
            merge_request_id, conflict_id, _actor(principal),
        )
    except (PermissionError, ValueError, KeyError) as exc:
        _raise_ai(exc)


@router.post("/merge-requests/{merge_request_id}/ai-assessment")
async def run_merge_request_ai_assessment(
    merge_request_id: str,
    organization_id: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    try:
        return await assess_merge_request(
            _organization(organization_id, principal), principal.user_id, merge_request_id, _actor(principal)
        )
    except (PermissionError, ValueError, KeyError, AIProviderError) as exc:
        _raise_ai(exc)


@router.post("/merge-requests/{merge_request_id}/ai-assessment/start")
async def start_merge_request_ai_assessment(
    merge_request_id: str,
    organization_id: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    """Background-launching twin of ``ai-assessment``."""
    try:
        return start_merge_assessment(
            _organization(organization_id, principal), principal.user_id, merge_request_id, _actor(principal)
        )
    except (PermissionError, ValueError, KeyError, AIProviderError) as exc:
        _raise_ai(exc)


@router.get("/merge-requests/{merge_request_id}/ai-assessment")
async def merge_request_ai_assessment(
    merge_request_id: str,
    principal: Principal = Depends(current_principal),
):
    try:
        # Access-checked read: raises if the caller cannot see this merge request.
        merge_service.get_request(merge_request_id, _actor(principal))
    except (PermissionError, ValueError) as exc:
        _raise(exc)
    return {"assessment": get_latest_assessment(merge_request_id)}


@router.post("/merge-requests/{merge_request_id}/review")
async def review_merge_request(
    merge_request_id: str,
    payload: MergeReviewRequest,
    principal: Principal = Depends(current_principal),
):
    try:
        return merge_service.review(
            merge_request_id=merge_request_id,
            decision=payload.decision,
            comment=payload.comment,
            actor=_actor(principal),
        )
    except (PermissionError, ValueError, MergeHeadChangedError) as exc:
        _raise(exc)


@router.post("/merge-requests/{merge_request_id}/reviewer-requests", status_code=201)
async def create_reviewer_request_route(
    merge_request_id: str, payload: ReviewerRequestCreate, principal: Principal = Depends(current_principal),
):
    try:
        return reviewer_request_service.request_reviewer(merge_request_id, payload.reviewer_user_id, _actor(principal))
    except (PermissionError, ValueError) as exc:
        _raise(exc)


@router.get("/merge-requests/{merge_request_id}/reviewer-requests")
async def list_reviewer_requests_route(
    merge_request_id: str, principal: Principal = Depends(current_principal),
):
    try:
        return {"reviewer_requests": reviewer_request_service.list_reviewer_requests(merge_request_id, _actor(principal))}
    except (PermissionError, ValueError) as exc:
        _raise(exc)


@router.get("/reviewer-requests/{request_id}")
async def get_reviewer_request_route(
    request_id: str, principal: Principal = Depends(current_principal),
):
    try:
        return reviewer_request_service.get_reviewer_request(request_id, _actor(principal))
    except (PermissionError, ValueError) as exc:
        _raise(exc)


@router.post("/reviewer-requests/{request_id}/respond")
async def respond_reviewer_request_route(
    request_id: str, payload: ReviewerResponseRequest, principal: Principal = Depends(current_principal),
):
    try:
        return reviewer_request_service.respond(request_id, payload.decision, payload.comment, _actor(principal))
    except (PermissionError, ValueError, MergeHeadChangedError) as exc:
        _raise(exc)


@router.post("/reviewer-requests/{request_id}/ai-fallback")
async def reviewer_request_ai_fallback_route(
    request_id: str, principal: Principal = Depends(current_principal),
):
    try:
        return await reviewer_request_service.use_ai_fallback(request_id, _actor(principal))
    except (PermissionError, ValueError, KeyError, MergeHeadChangedError, AIProviderError) as exc:
        _raise_ai(exc)


@router.post("/merge-requests/{merge_request_id}/merge")
async def merge_request_merge(
    merge_request_id: str,
    principal: Principal = Depends(current_principal),
):
    try:
        return merge_service.merge(merge_request_id, _actor(principal), delete_source_branch=True)
    except (
        PermissionError, ValueError, MergeConflictError, MergeHeadChangedError
    ) as exc:
        _raise(exc)


@router.post("/commits/{commit_id}/revert")
async def revert_commit(
    commit_id: str,
    principal: Principal = Depends(current_principal),
):
    try:
        return merge_service.revert_commit(commit_id, _actor(principal))
    except (PermissionError, ValueError) as exc:
        _raise(exc)
