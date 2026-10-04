"""Stage 1 semantic-ledger storage, migration, and bounded state APIs."""

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import database
from ..database import get_repository, user_can_access_branch
from ..excel.identity import semantic_snapshot
from ..repositories.commit_store import get_commit
from ..security import Principal, current_principal
from ..services.semantic_ledger_service import ledger_for
from ..storage.serializer import canonical_serialize


router = APIRouter(prefix="/api/v1", tags=["semantic-storage"])


def _repository(table_id: str, principal: Principal) -> dict:
    repository = get_repository(table_id.strip().upper(), principal.user_id)
    if not repository:
        raise HTTPException(status_code=404, detail={
            "code": "REPOSITORY_NOT_FOUND", "message": "Repository does not exist or is not accessible",
        })
    return repository


@router.get("/repositories/{table_id}/storage")
async def repository_storage(
    table_id: str,
    principal: Principal = Depends(current_principal),
):
    repository = _repository(table_id, principal)
    conn = database._get_connection()
    try:
        ledger = ledger_for(database.DB_PATH)
        metrics = ledger.storage_metrics(conn, repository["repository_id"])
        branches = conn.execute(
            "SELECT BRANCH_ID,BRANCH_NAME,HEAD_COMMIT_ID FROM BRANCHES WHERE REPOSITORY_ID=? AND STATUS='ACTIVE' ORDER BY BRANCH_TYPE,BRANCH_NAME",
            (repository["repository_id"],),
        ).fetchall()
        branch_costs = []
        for branch in branches:
            root = conn.execute(
                "SELECT ROOT_MANIFEST_HASH FROM COMMIT_MANIFESTS WHERE COMMIT_ID=?",
                (branch["HEAD_COMMIT_ID"],),
            ).fetchone()
            reachable = ledger.reachable_hashes(conn, [root[0]]) if root else set()
            physical = 0
            if reachable:
                placeholders = ",".join("?" for _ in reachable)
                physical = conn.execute(
                    f"SELECT COALESCE(SUM(COMPRESSED_SIZE),0) FROM STORAGE_OBJECTS WHERE OBJECT_HASH IN ({placeholders})",
                    tuple(reachable),
                ).fetchone()[0]
            branch_costs.append({
                "branch_id": branch["BRANCH_ID"], "branch_name": branch["BRANCH_NAME"],
                "head_commit_id": branch["HEAD_COMMIT_ID"], "reachable_objects": len(reachable),
                "reachable_bytes": int(physical or 0),
            })
        integrity = conn.execute(
            "SELECT STATUS,COUNT(*) FROM STORAGE_OBJECTS GROUP BY STATUS"
        ).fetchall()
        return {
            **metrics, "repository_id": repository["repository_id"],
            "repository_name": repository["repository_name"],
            "branches": branch_costs,
            "object_health": {row[0].lower(): row[1] for row in integrity},
        }
    finally:
        conn.close()


@router.post("/repositories/{table_id}/storage/migrate")
async def migrate_repository_storage(
    table_id: str,
    principal: Principal = Depends(current_principal),
):
    repository = _repository(table_id, principal)
    if not repository.get("capabilities", {}).get("manage_access"):
        raise HTTPException(status_code=403, detail={
            "code": "OWNER_REQUIRED", "message": "Only the repository owner can migrate storage",
        })
    conn = database._get_connection()
    try:
        ledger = ledger_for(database.DB_PATH)
        branches = conn.execute(
            "SELECT * FROM BRANCHES WHERE REPOSITORY_ID=? AND STATUS='ACTIVE'",
            (repository["repository_id"],),
        ).fetchall()
        results = []
        for branch in branches:
            state = semantic_snapshot(conn, branch["DATA_TABLE_ID"])
            result = ledger.migrate_branch(conn, branch, state)
            rebuilt = ledger.reconstruct(conn, branch["HEAD_COMMIT_ID"])
            equivalent = canonical_serialize({"sheets": state["sheets"]}) == canonical_serialize({"sheets": rebuilt["sheets"]})
            if not equivalent:
                conn.rollback()
                raise HTTPException(status_code=500, detail={
                    "code": "STATE_EQUIVALENCE_FAILED", "message": f"Migration validation failed for {branch['BRANCH_NAME']}",
                })
            results.append({"branch_id": branch["BRANCH_ID"], "branch_name": branch["BRANCH_NAME"], **result})
        conn.commit()
        return {"status": "MIGRATED", "storage_engine": "semantic_object_v1", "branches": results}
    finally:
        conn.close()


@router.get("/commits/{commit_id}/state")
async def commit_state(
    commit_id: str,
    sheet_id: str | None = None,
    row_start: int = Query(default=0, ge=0),
    row_limit: int = Query(default=500, ge=1, le=5000),
    principal: Principal = Depends(current_principal),
):
    commit = get_commit(commit_id)
    if not commit or not user_can_access_branch(commit["branch_id"], principal.user_id):
        raise HTTPException(status_code=404, detail={
            "code": "COMMIT_NOT_FOUND", "message": "Commit does not exist or is not accessible",
        })
    conn = database._get_connection()
    try:
        state = ledger_for(database.DB_PATH).reconstruct(conn, commit_id)
        if state is None:
            raise HTTPException(status_code=409, detail={
                "code": "MANIFEST_NOT_AVAILABLE", "message": "This legacy commit has not been migrated",
            })
        sheets = state["sheets"]
        if sheet_id:
            sheets = [sheet for sheet in sheets if sheet["sheet_id"] == sheet_id]
            if not sheets:
                raise HTTPException(status_code=404, detail={
                    "code": "SHEET_NOT_FOUND", "message": "Worksheet does not exist in this commit",
                })
        output = []
        for sheet in sheets:
            rows = sheet["rows"]
            output.append({**sheet, "rows": rows[row_start:row_start + row_limit],
                           "total_rows": len(rows), "row_start": row_start})
        return {**state, "sheets": output}
    finally:
        conn.close()


@router.post("/storage/gc")
async def storage_gc(
    table_id: str,
    dry_run: bool = True,
    principal: Principal = Depends(current_principal),
):
    repository = _repository(table_id, principal)
    if not repository.get("capabilities", {}).get("manage_access"):
        raise HTTPException(status_code=403, detail={"code": "OWNER_REQUIRED", "message": "Only an owner can run storage GC"})
    conn = database._get_connection()
    try:
        result = ledger_for(database.DB_PATH).collect_garbage(conn, dry_run=dry_run)
        conn.commit()
        return result
    except PermissionError as exc:
        conn.rollback()
        raise HTTPException(status_code=409, detail={"code": "LEGAL_HOLD_ACTIVE", "message": str(exc)}) from exc
    finally:
        conn.close()
