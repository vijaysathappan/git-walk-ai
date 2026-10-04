"""Virtual Run: authenticated macro registration, extraction, viewing, and
SQL-fast-lane execution (prepare/preview/confirm) endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from ..config import settings
from ..euc.validation import EUCValidationError
from ..macros import macro_explainer, run_service
from ..repositories.commit_store import BranchHeadChangedError
from ..security import Principal, current_principal

router = APIRouter(prefix="/api/v1/repositories/{table_id}/macros", tags=["virtual-run"])


class PrepareRunRequest(BaseModel):
    branch_id: str = Field(..., min_length=1)


class ConfirmRunRequest(BaseModel):
    commit_message: str | None = Field(default=None, max_length=300)


class SaveRecipeRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)


class RunRecipeRequest(BaseModel):
    branch_id: str = Field(..., min_length=1)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, BranchHeadChangedError):
        return HTTPException(status_code=409, detail={
            "code": "BRANCH_HEAD_CHANGED",
            "message": "The branch advanced since this macro run was prepared. Run it again.",
            "expected_head_commit_id": exc.expected_head,
            "current_head_commit_id": exc.current_head,
        })
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail={"code": "ACCESS_DENIED", "message": str(exc)})
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": str(exc).strip("'")})
    if isinstance(exc, EUCValidationError):
        return HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)})
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail={"code": "INVALID_MACRO_RUN", "message": str(exc)})
    return HTTPException(status_code=500, detail={"code": "MACRO_OPERATION_FAILED", "message": str(exc)})


@router.post("/source", status_code=201)
async def register_macro_source(
    table_id: str,
    file: UploadFile = File(...),
    principal: Principal = Depends(current_principal),
):
    if not file.filename or not file.filename.lower().endswith(".xlsm"):
        raise HTTPException(status_code=400, detail="Only .xlsm files carry a macro project.")
    try:
        payload = await file.read(settings.euc_max_file_bytes + 1)
        return run_service.register_source(table_id, file.filename, payload, principal.user_id)
    except Exception as exc:
        raise _error(exc) from exc
    finally:
        await file.close()


@router.post("/extract")
async def extract_macros_route(table_id: str, principal: Principal = Depends(current_principal)):
    try:
        return run_service.extract(table_id, principal.user_id)
    except Exception as exc:
        raise _error(exc) from exc


@router.get("")
async def list_macros_route(table_id: str, principal: Principal = Depends(current_principal)):
    try:
        result = run_service.list_macros(table_id, principal.user_id)
    except Exception as exc:
        raise _error(exc) from exc
    if result["has_source"] and result["extraction_status"] == "NOT_STARTED":
        try:
            run_service.extract(table_id, principal.user_id)
            result = run_service.list_macros(table_id, principal.user_id)
        except Exception as exc:
            raise _error(exc) from exc
    return result


@router.get("/{macro_id}/source")
async def macro_source_route(table_id: str, macro_id: str, principal: Principal = Depends(current_principal)):
    try:
        return run_service.get_macro_source(table_id, macro_id, principal.user_id)
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/{macro_id}/runs", status_code=201)
async def prepare_run_route(
    table_id: str, macro_id: str, payload: PrepareRunRequest, principal: Principal = Depends(current_principal),
):
    try:
        return run_service.prepare_run(table_id, macro_id, payload.branch_id, principal.user_id)
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/runs/{run_id}")
async def get_run_route(table_id: str, run_id: str, principal: Principal = Depends(current_principal)):
    try:
        return run_service.get_run(table_id, run_id, principal.user_id)
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/runs/{run_id}/confirm")
async def confirm_run_route(
    table_id: str, run_id: str, payload: ConfirmRunRequest, principal: Principal = Depends(current_principal),
):
    try:
        return run_service.confirm_run(table_id, run_id, principal.user_id, payload.commit_message)
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/{macro_id}/explain")
async def start_macro_explanation_route(table_id: str, macro_id: str, principal: Principal = Depends(current_principal)):
    try:
        return macro_explainer.start_macro_explanation(principal.user_id, table_id, macro_id)
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/{macro_id}/explanation")
async def get_macro_explanation_route(table_id: str, macro_id: str, principal: Principal = Depends(current_principal)):
    try:
        cached = run_service.get_cached_macro_explanation(table_id, macro_id, principal.user_id)
    except Exception as exc:
        raise _error(exc) from exc
    if not cached:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "No explanation is cached for this macro yet."})
    return cached


@router.post("/{macro_id}/recipes", status_code=201)
async def save_recipe_route(
    table_id: str, macro_id: str, payload: SaveRecipeRequest, principal: Principal = Depends(current_principal),
):
    try:
        return run_service.save_recipe(table_id, macro_id, principal.user_id, payload.name, payload.description)
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/recipes")
async def list_recipes_route(table_id: str, principal: Principal = Depends(current_principal)):
    try:
        return run_service.list_recipes(table_id, principal.user_id)
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/recipes/{recipe_id}/run", status_code=201)
async def run_recipe_route(
    table_id: str, recipe_id: str, payload: RunRecipeRequest, principal: Principal = Depends(current_principal),
):
    try:
        return run_service.prepare_run_from_recipe(table_id, recipe_id, payload.branch_id, principal.user_id)
    except Exception as exc:
        raise _error(exc) from exc


@router.delete("/recipes/{recipe_id}")
async def delete_recipe_route(table_id: str, recipe_id: str, principal: Principal = Depends(current_principal)):
    try:
        return run_service.delete_recipe(table_id, recipe_id, principal.user_id)
    except Exception as exc:
        raise _error(exc) from exc
