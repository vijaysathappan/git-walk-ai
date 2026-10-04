"""Reviewer Request workflow: a requester asks a SPECIFIC person (usually
picked from the existing "suggest a reviewer" ranking) to approve or reject
a merge request. The reviewer gets an email + in-app notification; clicking
either opens a full-screen response view. The merge stays blocked until
that person responds, same as any other pending review today.

`respond()` skips `merge_service.review()`'s per-decision RBAC check
(`merge_request.approve`/`.review`) for the invited reviewer: creating the
request itself already required the REQUESTER to hold `merge_request.review`
(checked in `request_reviewer` below), and a suggested reviewer is often a
collaborator without a standing review/approve role. Being explicitly,
individually invited by someone who already had that authority is treated as
delegated authorization for this one merge request -- every other gate in
`review()` (head-changed, open conflicts, validation status) still applies
untouched.

If the reviewer hasn't responded within `settings.reviewer_response_hours`
(default 6h), the ORIGINAL REQUESTER (not an unattended background job) can
choose to have the existing AI risk-assessment agent (`ai.merge_agent`)
recommend a decision instead of waiting indefinitely. That recommendation
is applied through the exact same `merge_service.review()` call a human
reviewer's decision goes through -- run under the REQUESTER's own identity,
so it is gated by the requester actually holding `merge_request.approve`
(APPROVED) or `merge_request.review` (REJECTED) themselves. This is a
deliberate, existing-permission reuse, not a new bypass: a requester who
isn't authorized to approve merges can't use "AI fallback" to approve one
either -- `review()`'s own check still applies untouched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..access_control.engine import authorization_engine, repository_resource
from ..ai.merge_agent import assess_merge_request
from ..config import settings
from ..database import create_notification, get_repository_organization_id, get_user
from ..observability import record_audit_event
from ..repositories.merge_store import (
    create_reviewer_request,
    find_pending_reviewer_request,
    get_reviewer_request as _get_reviewer_request_row,
    list_reviewer_requests as _list_reviewer_requests,
    update_reviewer_request_response,
)
from ..security import send_email, smtp_configured
from .merge_service import MergeActor, merge_service

_RECOMMENDATION_TO_DECISION = {"APPROVE": "APPROVED", "REJECT": "REJECTED"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def request_reviewer(merge_request_id: str, reviewer_user_id: str, actor: MergeActor) -> dict[str, Any]:
    request = merge_service.get_request(merge_request_id, actor)
    authorization_engine.require(actor.user_id, "merge_request.review", repository_resource(request["repository_id"]))

    reviewer = get_user(reviewer_user_id)
    if not reviewer:
        raise ValueError("The selected reviewer no longer exists")
    if reviewer_user_id == actor.user_id:
        raise ValueError("You can't request a review from yourself")
    if find_pending_reviewer_request(merge_request_id, reviewer_user_id):
        raise ValueError("A review request is already pending for this person")

    expires_at = (_utcnow() + timedelta(hours=settings.reviewer_response_hours)).isoformat()
    created = create_reviewer_request(
        merge_request_id=merge_request_id, repository_id=request["repository_id"],
        requested_by_user_id=actor.user_id, reviewer_user_id=reviewer_user_id,
        reviewer_email=reviewer["email"], expires_at=expires_at,
    )

    organization_id = get_repository_organization_id(request["repository_id"])
    try:
        create_notification(
            reviewer_user_id, "REVIEWER_REQUESTED",
            title=f"{actor.email} asked you to review: {request['title']}",
            body=f"Respond within {settings.reviewer_response_hours} hours, or the requester may ask AI to decide instead.",
            resource_type="REVIEWER_REQUEST", resource_id=created["request_id"],
            organization_id=organization_id,
        )
    except Exception:
        pass  # a notification failing to write must never block the request itself
    if smtp_configured():
        try:
            send_email(
                reviewer["email"], f"Review requested: {request['title']}",
                f"{actor.email} asked you to review the merge request \"{request['title']}\" on Git Walk.\n\n"
                f"Please sign in and respond within {settings.reviewer_response_hours} hours. "
                "If you don't respond in time, the requester may use AI to decide instead.",
            )
        except Exception:
            pass  # best-effort, same convention as create_notification above

    record_audit_event(
        "REVIEWER_REQUEST_CREATED", actor_user_id=actor.user_id, repository_id=request["repository_id"],
        branch_id=request["source_branch_id"], merge_request_id=merge_request_id,
        payload={"request_id": created["request_id"], "reviewer_user_id": reviewer_user_id},
    )
    return created


def list_reviewer_requests(merge_request_id: str, actor: MergeActor) -> list[dict[str, Any]]:
    merge_service.get_request(merge_request_id, actor)  # access-checked read
    return _list_reviewer_requests(merge_request_id)


def get_reviewer_request(request_id: str, actor: MergeActor) -> dict[str, Any]:
    row = _get_reviewer_request_row(request_id)
    if not row:
        raise PermissionError("Review request does not exist or is not accessible")
    request = merge_service.get_request(row["merge_request_id"], actor)  # access-checked read
    return {**row, "merge_request": request}


def respond(request_id: str, decision: str, comment: str | None, actor: MergeActor) -> dict[str, Any]:
    row = _get_reviewer_request_row(request_id)
    if not row:
        raise PermissionError("Review request does not exist or is not accessible")
    if row["status"] != "PENDING":
        raise ValueError("This review request has already been responded to")
    if row["reviewer_user_id"] != actor.user_id:
        raise PermissionError("Only the requested reviewer can respond to this request")

    # The invited reviewer may not hold a standing merge_request.review/.approve
    # role -- being hand-picked and explicitly requested by someone who already
    # held merge_request.review (checked in request_reviewer above) is itself
    # the authorization for THIS specific request, so the per-decision RBAC
    # check is intentionally skipped here.
    result = merge_service.review(
        merge_request_id=row["merge_request_id"], decision=decision, comment=comment, actor=actor,
        skip_permission_check=True,
    )
    update_reviewer_request_response(request_id, "RESPONDED", decision, comment)

    try:
        create_notification(
            row["requested_by_user_id"], "REVIEWER_RESPONDED",
            title=f"{actor.email} {decision.lower()} your merge request",
            body=comment or "", resource_type="MERGE_REQUEST", resource_id=row["merge_request_id"],
            organization_id=get_repository_organization_id(row["repository_id"]),
        )
    except Exception:
        pass

    record_audit_event(
        "REVIEWER_REQUEST_RESPONDED", actor_user_id=actor.user_id, repository_id=row["repository_id"],
        merge_request_id=row["merge_request_id"],
        payload={"request_id": request_id, "decision": decision},
    )
    return result


async def use_ai_fallback(request_id: str, actor: MergeActor) -> dict[str, Any]:
    row = _get_reviewer_request_row(request_id)
    if not row:
        raise PermissionError("Review request does not exist or is not accessible")
    if row["status"] != "PENDING":
        raise ValueError("This review request has already been responded to")
    if row["requested_by_user_id"] != actor.user_id:
        raise PermissionError("Only the person who requested this review can use the AI fallback")
    if _utcnow() < datetime.fromisoformat(row["expires_at"]):
        raise ValueError(f"The {settings.reviewer_response_hours}-hour review window hasn't elapsed yet")

    organization_id = get_repository_organization_id(row["repository_id"])
    assessment = await assess_merge_request(organization_id, actor.user_id, row["merge_request_id"], actor)
    recommendation = assessment.get("recommendation")
    decision = _RECOMMENDATION_TO_DECISION.get(recommendation)
    if not decision:
        raise ValueError(
            "The AI assessment could not confidently recommend approving or rejecting this merge "
            f"(recommendation: {recommendation or 'unknown'}) -- it still needs a human reviewer."
        )
    comment = (
        f"AI fallback decision after a {settings.reviewer_response_hours}-hour non-response window "
        f"(risk level {assessment.get('risk_level', 'unknown')}). {assessment.get('summary', '')}"
    ).strip()

    result = merge_service.review(
        merge_request_id=row["merge_request_id"], decision=decision, comment=comment, actor=actor,
    )
    update_reviewer_request_response(request_id, "AI_FALLBACK", decision, comment)

    record_audit_event(
        "REVIEWER_REQUEST_AI_FALLBACK", actor_user_id=actor.user_id, repository_id=row["repository_id"],
        merge_request_id=row["merge_request_id"],
        payload={"request_id": request_id, "decision": decision, "ai_recommendation": recommendation,
                 "risk_level": assessment.get("risk_level")},
    )
    return result
