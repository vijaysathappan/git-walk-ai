"""Notification center: reads/read-state for the NOTIFICATIONS table.

Unifies signals from across the product into one glanceable feed — merge
conflicts, AI merge-risk flags (ai/merge_agent.py), device blocks
(database.py::set_device_trust_status), and EUC Risk Drift Radar findings
(ai/euc_narrative.py) — all written through the single
database.create_notification() helper. Permission here is simply "read your
own notifications," no admin check needed.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..database import (
    get_notification_preferences,
    list_notifications,
    mark_all_notifications_read,
    mark_notification_read,
    set_notification_preference,
)
from ..security import Principal, current_principal

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


class NotificationPreferenceRequest(BaseModel):
    type: str
    enabled: bool


@router.get("/preferences")
async def notification_preferences(principal: Principal = Depends(current_principal)):
    return {"preferences": get_notification_preferences(principal.user_id)}


@router.put("/preferences")
async def update_notification_preference(payload: NotificationPreferenceRequest, principal: Principal = Depends(current_principal)):
    set_notification_preference(principal.user_id, payload.type, payload.enabled)
    return {"preferences": get_notification_preferences(principal.user_id)}


@router.get("")
async def notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(current_principal),
):
    items = list_notifications(principal.user_id, unread_only, limit)
    unread_count = len(list_notifications(principal.user_id, True, 200))
    return {"notifications": items, "unread_count": unread_count}


@router.post("/{notification_id}/read")
async def read_notification(notification_id: str, principal: Principal = Depends(current_principal)):
    try:
        mark_notification_read(notification_id, principal.user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Notification does not exist") from exc
    return {"status": "READ"}


@router.post("/read-all")
async def read_all_notifications(principal: Principal = Depends(current_principal)):
    count = mark_all_notifications_read(principal.user_id)
    return {"marked_read": count}
