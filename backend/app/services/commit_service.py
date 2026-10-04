"""Authoritative Stage 2 semantic commit workflow."""

from dataclasses import dataclass
from typing import Any

from ..database import (
    user_can_edit_table,
    validate_working_copy,
    working_copy_required,
)
from ..repositories.commit_store import commit_semantic_delta
from ..observability import metric_timer, record_audit_event, record_metric
from ..access_control.engine import authorization_engine, branch_resource


@dataclass(frozen=True)
class CommitActor:
    user_id: str
    email: str


class CommitService:
    def commit(self, payload: Any, actor: CommitActor) -> dict[str, Any]:
        table_id = payload.table_id.strip().upper()
        authorization_engine.require(
            actor.user_id, "commit.create",
            branch_resource(payload.repository_id, payload.branch_id),
        )
        if not user_can_edit_table(table_id, actor.user_id):
            raise PermissionError("Editor access is required for this branch")
        if not working_copy_required(table_id):
            raise PermissionError("Semantic commits are accepted only on personal branches")
        validate_working_copy(
            table_id=table_id,
            user_id=actor.user_id,
            repository_id=payload.repository_id,
            branch_id=payload.branch_id,
            working_copy_id=payload.working_copy_id,
            base_commit_id=payload.base_commit_id,
            issued_at=payload.issued_at,
            signature=payload.signature,
        )
        if not payload.expected_head_commit_id:
            raise ValueError("expected_head_commit_id is required")
        changes = [
            change.model_dump(exclude_none=True) for change in payload.semantic_changes
        ]
        if not changes:
            raise ValueError("Review Changes must produce at least one semantic operation")
        try:
            with metric_timer(
                "commit_latency", user_id=actor.user_id,
                repository_id=payload.repository_id, branch_id=payload.branch_id,
            ):
                result = commit_semantic_delta(
                    table_id=table_id,
                    repository_id=payload.repository_id,
                    branch_id=payload.branch_id,
                    expected_head_commit_id=payload.expected_head_commit_id,
                    base_version=payload.base_version,
                    changes=changes,
                    user_id=actor.user_id,
                    user_email=actor.email,
                    message=payload.commit_message,
                )
        except Exception as exc:
            record_metric(
                "commit_rejection", 1, "count", status="FAILED",
                user_id=actor.user_id, repository_id=payload.repository_id,
                branch_id=payload.branch_id, tags={"reason": type(exc).__name__},
            )
            record_audit_event(
                "COMMIT_REJECTED", actor_user_id=actor.user_id,
                repository_id=payload.repository_id, branch_id=payload.branch_id,
                working_copy_id=payload.working_copy_id,
                payload={"change_count": len(changes), "message": payload.commit_message},
                status="FAILED", failure_reason=str(exc),
            )
            raise
        record_audit_event(
            "COMMIT_CREATED", actor_user_id=actor.user_id,
            repository_id=payload.repository_id, branch_id=payload.branch_id,
            working_copy_id=payload.working_copy_id, commit_id=result["commit_id"],
            payload={"change_count": len(changes), "message": payload.commit_message,
                     "risk_score": result.get("risk_score", 0)},
        )
        return result


commit_service = CommitService()
