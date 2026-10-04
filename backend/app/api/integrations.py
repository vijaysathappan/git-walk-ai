"""Stage 4 enterprise integration and information-fabric API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from .. import database
from ..access_control.service import primary_organization
from ..integrations.canonical import canonical_object, create_mapping, create_schema
from ..integrations.connectors.base import ConnectorError
from ..integrations.search import operations_dashboard, search_catalogue
from ..integrations.service import integration_service
from ..integrations.sync import create_sync_job, resolve_conflict
from ..integrations.thread import impact, node_timeline, thread_graph
from ..security import Principal, current_principal


router = APIRouter(prefix="/api/v1/information-fabric", tags=["information-fabric"])


class ConnectionRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    connector_type: str = Field(min_length=2, max_length=40)
    environment: str = Field(default="DEVELOPMENT", max_length=40)
    configuration: dict[str, Any] = Field(default_factory=dict)
    credentials: dict[str, Any] = Field(default_factory=dict)


class RunRequest(BaseModel):
    mapping_id: str | None = Field(default=None, max_length=80)
    idempotency_key: str | None = Field(default=None, max_length=160)
    batch_size: int = Field(default=1000, ge=1, le=10000)
    source_object_id: str = Field(default="", max_length=100)


class CanonicalSchemaRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    schema_definition: dict[str, Any]
    business_keys: list[str] = Field(min_length=1, max_length=20)


class CanonicalMappingRequest(BaseModel):
    connection_id: str
    source_object_type: str = Field(min_length=1, max_length=120)
    schema_id: str
    mapping: dict[str, Any]


class SyncJobRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    source_connection_id: str
    target_connection_id: str | None = None
    mapping_id: str | None = None
    direction: str = "INBOUND"
    mode: str = "INCREMENTAL"
    deletion_policy: str = "IGNORE"
    conflict_strategy: str = "MANUAL"


class ConflictResolutionRequest(BaseModel):
    strategy: str


def _organization(requested: str | None, principal: Principal) -> str:
    organization_id = requested or primary_organization(principal.user_id)
    if not organization_id:
        raise HTTPException(status_code=404, detail="No organization is available for this account")
    return organization_id


def _raise(exc: Exception) -> None:
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail={"code": "ACCESS_DENIED", "message": str(exc)}) from exc
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    if isinstance(exc, ConnectorError):
        status = 503 if exc.transient else 422
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc), "transient": exc.transient}) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/connector-types")
async def connector_types(organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return integration_service.connector_types(principal.user_id, _organization(organization_id, principal))
    except Exception as exc: _raise(exc)


@router.get("/connections")
async def connections(organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return integration_service.list_connections(_organization(organization_id, principal), principal.user_id)
    except Exception as exc: _raise(exc)


@router.post("/connections", status_code=201)
async def create_connection_endpoint(payload: ConnectionRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return integration_service.create_connection(_organization(organization_id, principal), payload.model_dump(), principal.user_id)
    except Exception as exc: _raise(exc)


@router.get("/connections/{connection_id}")
async def connection(connection_id: str, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return integration_service.get_connection(_organization(organization_id, principal), connection_id, principal.user_id)
    except Exception as exc: _raise(exc)


@router.post("/connections/{connection_id}/source-file")
async def upload_source_file(connection_id: str, file: UploadFile = File(...), organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try:
        contents = await file.read(50 * 1024 * 1024 + 1)
        if len(contents) > 50 * 1024 * 1024: raise ValueError("Integration source file exceeds 50 MB")
        return integration_service.upload_source(_organization(organization_id, principal), connection_id, file.filename or "source.bin", contents, principal.user_id)
    except Exception as exc: _raise(exc)


@router.post("/connections/{connection_id}/test")
async def test_connection_endpoint(connection_id: str, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return await integration_service.test_connection(_organization(organization_id, principal), connection_id, principal.user_id)
    except Exception as exc: _raise(exc)


@router.post("/connections/{connection_id}/discover")
async def discover_connection(connection_id: str, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return await integration_service.discover(_organization(organization_id, principal), connection_id, principal.user_id)
    except Exception as exc: _raise(exc)


@router.post("/connections/{connection_id}/runs", status_code=202)
async def execute_connection(connection_id: str, payload: RunRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try:
        return await integration_service.execute(_organization(organization_id, principal), connection_id, principal.user_id,
                                                 mapping_id=payload.mapping_id, idempotency_key=payload.idempotency_key,
                                                 batch_size=payload.batch_size, source_object_id=payload.source_object_id)
    except Exception as exc: _raise(exc)


@router.get("/runs")
async def runs(connection_id: str | None = Query(default=None), limit: int = Query(default=100, ge=1, le=500), organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return integration_service.list_runs(_organization(organization_id, principal), principal.user_id, connection_id, limit)
    except Exception as exc: _raise(exc)


@router.get("/dead-letters")
async def dead_letters(organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return integration_service.dead_letters(_organization(organization_id, principal), principal.user_id)
    except Exception as exc: _raise(exc)


@router.post("/dead-letters/{dead_letter_id}/replay", status_code=202)
async def replay_dead_letter(dead_letter_id: str, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return await integration_service.replay_dead_letter(_organization(organization_id, principal), dead_letter_id, principal.user_id)
    except Exception as exc: _raise(exc)


@router.post("/canonical/schemas", status_code=201)
async def canonical_schemas(payload: CanonicalSchemaRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    organization = _organization(organization_id, principal)
    try:
        integration_service._require(principal.user_id, "canonical.manage", organization)
        return create_schema(organization, payload.name, payload.schema_definition, payload.business_keys, principal.user_id)
    except Exception as exc: _raise(exc)


@router.post("/canonical/mappings", status_code=201)
async def canonical_mappings(payload: CanonicalMappingRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    organization = _organization(organization_id, principal)
    try:
        integration_service._require(principal.user_id, "canonical.map", organization)
        return create_mapping(organization, payload.connection_id, payload.source_object_type, payload.schema_id, payload.mapping, principal.user_id)
    except Exception as exc: _raise(exc)


@router.get("/canonical")
async def canonical_list(object_type: str | None = Query(default=None), limit: int = Query(default=100, ge=1, le=500), organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    organization = _organization(organization_id, principal)
    try:
        integration_service._require(principal.user_id, "canonical.read", organization); conn = database._get_connection()
        try:
            sql = "SELECT * FROM CANONICAL_OBJECTS WHERE ORGANIZATION_ID=? AND STATUS='ACTIVE'"; params: list[Any] = [organization]
            if object_type: sql += " AND OBJECT_TYPE=?"; params.append(object_type)
            sql += " ORDER BY UPDATED_AT DESC LIMIT ?"; params.append(limit)
            return [{key.lower(): row[key] for key in row.keys()} for row in conn.execute(sql, params)]
        finally: conn.close()
    except Exception as exc: _raise(exc)


@router.get("/canonical/{canonical_id}")
async def canonical_detail(canonical_id: str, version: int | None = Query(default=None, ge=1), organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    organization = _organization(organization_id, principal)
    try:
        integration_service._require(principal.user_id, "canonical.read", organization)
        return canonical_object(organization, canonical_id, version)
    except Exception as exc: _raise(exc)


@router.get("/thread/{node_id}")
async def digital_thread(node_id: str, depth: int = Query(default=3, ge=0, le=8), organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    organization = _organization(organization_id, principal)
    try:
        integration_service._require(principal.user_id, "thread.read", organization)
        return thread_graph(organization, node_id, depth)
    except Exception as exc: _raise(exc)


@router.get("/thread/{node_id}/timeline")
async def digital_thread_timeline(node_id: str, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    organization = _organization(organization_id, principal)
    try:
        integration_service._require(principal.user_id, "thread.read", organization)
        return node_timeline(organization, node_id)
    except Exception as exc: _raise(exc)


@router.get("/thread/{node_id}/impact")
async def digital_thread_impact(node_id: str, depth: int = Query(default=5, ge=1, le=8), organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    organization = _organization(organization_id, principal)
    try:
        integration_service._require(principal.user_id, "thread.read", organization)
        return impact(organization, node_id, depth)
    except Exception as exc: _raise(exc)


@router.post("/sync-jobs", status_code=201)
async def sync_jobs(payload: SyncJobRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    organization = _organization(organization_id, principal)
    try:
        integration_service._require(principal.user_id, "synchronization.manage", organization)
        return create_sync_job(organization, payload.model_dump(), principal.user_id)
    except Exception as exc: _raise(exc)


@router.post("/conflicts/{conflict_id}/resolve")
async def conflicts(conflict_id: str, payload: ConflictResolutionRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    organization = _organization(organization_id, principal)
    try:
        integration_service._require(principal.user_id, "synchronization.manage", organization)
        return resolve_conflict(organization, conflict_id, payload.strategy.upper(), principal.user_id)
    except Exception as exc: _raise(exc)


@router.get("/search")
async def information_search(q: str = Query(default="", max_length=200), item_type: str | None = Query(default=None), organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return search_catalogue(_organization(organization_id, principal), principal.user_id, q, item_type)
    except Exception as exc: _raise(exc)


@router.get("/operations")
async def operations(organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return operations_dashboard(_organization(organization_id, principal), principal.user_id)
    except Exception as exc: _raise(exc)
