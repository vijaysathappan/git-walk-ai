"""Stage 2 commit graph, reconstructed state, metrics, and lineage APIs."""

from fastapi import APIRouter, Depends, HTTPException, Query

from ..access_control.service import primary_organization
from ..ai.commit_review import get_latest_review, review_commit, start_commit_review
from ..ai.gateway import AIProviderError
from ..database import get_repository, user_can_access_branch
from ..repositories.commit_store import (
    branch_metrics,
    get_cell_history,
    get_commit,
    list_branch_commits,
    list_repository_commits,
    reconstruct_branch,
)
from ..security import Principal, current_principal


router = APIRouter(prefix="/api/v1", tags=["semantic-versioning"])


def _branch_access(branch_id: str, principal: Principal) -> None:
    if not user_can_access_branch(branch_id, principal.user_id):
        raise HTTPException(status_code=404, detail="Branch does not exist or is not accessible")


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


@router.get("/branches/{branch_id}/state")
async def branch_state(
    branch_id: str,
    commit_id: str | None = None,
    principal: Principal = Depends(current_principal),
):
    _branch_access(branch_id, principal)
    try:
        return reconstruct_branch(branch_id, commit_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/repositories/{table_id}/commit-graph")
async def commit_graph(
    table_id: str,
    limit: int = Query(default=300, ge=1, le=1000),
    principal: Principal = Depends(current_principal),
):
    repository = get_repository(table_id, principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository does not exist or is not accessible")
    return {"commits": list_repository_commits(repository["repository_id"], limit)}


@router.get("/branches/{branch_id}/commits")
async def branch_commits(
    branch_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(current_principal),
):
    _branch_access(branch_id, principal)
    return {"commits": list_branch_commits(branch_id, limit)}


@router.get("/branches/{branch_id}/metrics")
async def metrics(
    branch_id: str,
    principal: Principal = Depends(current_principal),
):
    _branch_access(branch_id, principal)
    return branch_metrics(branch_id)


@router.get("/commits/{commit_id}")
async def commit_detail(
    commit_id: str,
    principal: Principal = Depends(current_principal),
):
    commit = get_commit(commit_id)
    if not commit:
        raise HTTPException(status_code=404, detail="Commit does not exist")
    _branch_access(commit["branch_id"], principal)
    return commit


@router.post("/commits/{commit_id}/ai-review")
async def run_commit_ai_review(
    commit_id: str,
    organization_id: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    commit = get_commit(commit_id)
    if not commit:
        raise HTTPException(status_code=404, detail="Commit does not exist")
    _branch_access(commit["branch_id"], principal)
    try:
        return await review_commit(_organization(organization_id, principal), principal.user_id, commit_id)
    except (PermissionError, ValueError, KeyError, AIProviderError) as exc:
        _raise_ai(exc)


@router.post("/commits/{commit_id}/ai-review/start")
async def start_commit_ai_review(
    commit_id: str,
    organization_id: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    """Background-launching twin of ``ai-review``."""
    commit = get_commit(commit_id)
    if not commit:
        raise HTTPException(status_code=404, detail="Commit does not exist")
    _branch_access(commit["branch_id"], principal)
    try:
        return start_commit_review(_organization(organization_id, principal), principal.user_id, commit_id)
    except (PermissionError, ValueError, KeyError, AIProviderError) as exc:
        _raise_ai(exc)


@router.get("/commits/{commit_id}/ai-review")
async def commit_ai_review(
    commit_id: str,
    principal: Principal = Depends(current_principal),
):
    commit = get_commit(commit_id)
    if not commit:
        raise HTTPException(status_code=404, detail="Commit does not exist")
    _branch_access(commit["branch_id"], principal)
    return {"review": get_latest_review(commit_id)}


@router.get("/branches/{branch_id}/cells/{sheet_id}/{row_id}/{column_id}/history")
async def cell_history(
    branch_id: str,
    sheet_id: str,
    row_id: str,
    column_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(current_principal),
):
    _branch_access(branch_id, principal)
    return {
        "history": get_cell_history(
            branch_id, sheet_id, row_id, column_id, limit
        )
    }
