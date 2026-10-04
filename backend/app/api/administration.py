"""Stage 3.1 enterprise security administration API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..access_control.service import (
    add_group_member,
    add_organization_member,
    assign_role,
    create_group,
    create_policy,
    create_service_account,
    list_organization_devices,
    primary_organization,
    revoke_assignment,
    revoke_organization_session,
    security_overview,
    set_organization_device_trust,
    set_user_status,
)
from ..security import Principal, current_principal


router = APIRouter(prefix="/api/v1/admin", tags=["enterprise-security"])


class GroupRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=500)


class OrganizationMemberRequest(BaseModel):
    email: str = Field(min_length=5, max_length=320)


class GroupMemberRequest(BaseModel):
    user_id: str = Field(min_length=4, max_length=80)


class RoleAssignmentRequest(BaseModel):
    role_key: str = Field(min_length=2, max_length=80)
    scope_type: str = Field(min_length=2, max_length=40)
    scope_id: str = Field(min_length=2, max_length=120)
    user_id: str | None = Field(default=None, max_length=80)
    group_id: str | None = Field(default=None, max_length=80)


class PolicyRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    effect: str = Field(min_length=4, max_length=5)
    permission_key: str = Field(min_length=1, max_length=120)
    subject_type: str = Field(min_length=3, max_length=30)
    subject_id: str | None = Field(default=None, max_length=120)
    resource_type: str = Field(min_length=1, max_length=40)
    resource_id: str | None = Field(default=None, max_length=120)
    conditions: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=100, ge=1, le=10000)


class UserStatusRequest(BaseModel):
    status: str = Field(min_length=6, max_length=10)


class ServiceAccountRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    role_key: str = Field(min_length=2, max_length=80)
    scope_type: str = Field(min_length=2, max_length=40)
    scope_id: str = Field(min_length=2, max_length=120)


class DeviceTrustRequest(BaseModel):
    trust_status: str = Field(pattern="^(TRUSTED|BLOCKED|UNKNOWN)$")


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
    raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/overview")
async def overview(
    organization_id: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    try: return security_overview(_organization(organization_id, principal), principal.user_id)
    except Exception as exc: _raise(exc)


@router.post("/members", status_code=201)
async def members(payload: OrganizationMemberRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return add_organization_member(_organization(organization_id, principal), payload.email, principal.user_id)
    except Exception as exc: _raise(exc)


@router.post("/groups", status_code=201)
async def groups(payload: GroupRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return create_group(_organization(organization_id, principal), payload.name, payload.description, principal.user_id)
    except Exception as exc: _raise(exc)


@router.post("/groups/{group_id}/members", status_code=201)
async def group_members(group_id: str, payload: GroupMemberRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return add_group_member(_organization(organization_id, principal), group_id, payload.user_id, principal.user_id)
    except Exception as exc: _raise(exc)


@router.post("/role-assignments", status_code=201)
async def role_assignments(payload: RoleAssignmentRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try:
        return assign_role(_organization(organization_id, principal), payload.role_key, payload.scope_type,
                           payload.scope_id, principal.user_id, user_id=payload.user_id, group_id=payload.group_id)
    except Exception as exc: _raise(exc)


@router.delete("/role-assignments/{assignment_id}", status_code=204)
async def delete_role_assignment(assignment_id: str, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: revoke_assignment(_organization(organization_id, principal), assignment_id, principal.user_id)
    except Exception as exc: _raise(exc)


@router.post("/policies", status_code=201)
async def policies(payload: PolicyRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return create_policy(_organization(organization_id, principal), payload.model_dump(), principal.user_id)
    except Exception as exc: _raise(exc)


@router.patch("/users/{user_id}/status")
async def user_status(user_id: str, payload: UserStatusRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return set_user_status(_organization(organization_id, principal), user_id, payload.status, principal.user_id)
    except Exception as exc: _raise(exc)


@router.post("/service-accounts", status_code=201)
async def service_accounts(payload: ServiceAccountRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try:
        return create_service_account(_organization(organization_id, principal), payload.name, payload.role_key,
                                      payload.scope_type, payload.scope_id, principal.user_id)
    except Exception as exc: _raise(exc)


@router.get("/devices")
async def devices(organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return {"devices": list_organization_devices(_organization(organization_id, principal), principal.user_id)}
    except Exception as exc: _raise(exc)


@router.post("/devices/{fingerprint_id}/trust")
async def device_trust(fingerprint_id: str, payload: DeviceTrustRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return set_organization_device_trust(_organization(organization_id, principal), principal.user_id, fingerprint_id, payload.trust_status)
    except Exception as exc: _raise(exc)


@router.post("/sessions/{session_id}/revoke", status_code=204)
async def revoke_session_route(session_id: str, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: revoke_organization_session(_organization(organization_id, principal), principal.user_id, session_id)
    except Exception as exc: _raise(exc)
