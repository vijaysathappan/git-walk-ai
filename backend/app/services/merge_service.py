"""Authoritative Stage 3 branch sync, review, merge, and revert workflows."""

import logging
from dataclasses import dataclass
from typing import Any

from ..database import get_table_snapshot
from ..excel.diff_engine import semantic_diff
from ..excel.merge_engine import resolution_key, three_way_merge
from ..excel.validation import validate_workbook
from ..repositories.commit_store import (
    branch_change_timeline,
    commit_semantic_delta,
    get_commit,
    reconstruct_branch,
)
from ..repositories.merge_store import (
    ahead_behind,
    branch_context,
    create_merge_request_record,
    get_merge_request,
    list_merge_requests,
    mark_merged,
    merge_base,
    repository_role,
    resolve_conflict_record,
    save_review,
    save_validation_run,
    update_branch_base,
    workbook_filename,
)
from ..observability import metric_timer, record_audit_event, record_metric, structured_log
from ..access_control.engine import authorization_engine, branch_resource, repository_resource
from .branch_lifecycle_manager import BranchLifecycleManager
from ..euc.continuous_assurance import rescore_repository_after_merge
from ..macros.continuous_assurance import rescore_macros_after_merge


class MergeConflictError(RuntimeError):
    def __init__(self, conflicts: list[dict[str, Any]]):
        self.conflicts = conflicts
        super().__init__(f"Merge has {len(conflicts)} conflict(s)")


class MergeHeadChangedError(RuntimeError):
    pass


@dataclass(frozen=True)
class MergeActor:
    user_id: str
    email: str


def _state(branch_id: str, commit_id: str | None = None) -> dict[str, Any]:
    return reconstruct_branch(branch_id, commit_id)


def _annotate_changes(items: list[dict[str, Any]], base_state: dict[str, Any], branch_state: dict[str, Any]) -> list[dict[str, Any]]:
    """Add human-readable `sheet_name`/`column_name`/`row_label` fields to raw
    diff/timeline entries, which otherwise only carry internal stable IDs
    (SHEET_xxx/COL_xxx/ROW_xxx) -- meaningless to a reviewer deciding whether
    to approve a change. Additive only: every existing field is left as-is.
    """
    sheet_names: dict[str, str] = {}
    column_names: dict[str, str] = {}
    row_positions: dict[tuple[str, str], int] = {}
    for state in (branch_state, base_state):
        for sheet in state.get("sheets", []):
            sheet_names.setdefault(sheet["sheet_id"], sheet.get("name"))
            for column in sheet.get("columns", []):
                column_names.setdefault(column["column_id"], column.get("name"))
            for row in sheet.get("rows", []):
                row_positions.setdefault((sheet["sheet_id"], row["row_id"]), row.get("position"))
    for item in items:
        operation = item.get("operation_type")
        sheet_id = item.get("sheet_id")
        if sheet_id:
            item["sheet_name"] = (
                (item.get("new_value") if operation in ("SHEET_CREATE", "SHEET_RENAME") else None)
                or sheet_names.get(sheet_id) or sheet_id
            )
        column_id = item.get("column_id")
        if column_id:
            item["column_name"] = (
                (item.get("new_value") if operation in ("COLUMN_INSERT", "COLUMN_RENAME") else None)
                or column_names.get(column_id) or column_id
            )
        row_id = item.get("row_id")
        if row_id:
            position = row_positions.get((sheet_id, row_id))
            item["row_label"] = f"Row {position + 1}" if position is not None else "Row"
    return items


def _resolution_map(request: dict[str, Any]) -> dict[tuple, Any]:
    output = {}
    for conflict in request.get("conflicts", []):
        if conflict["status"] == "RESOLVED":
            output[resolution_key(conflict)] = conflict.get("resolved_state")
    return output


class MergeService:
    def branch_divergence(
        self, source_branch_id: str, target_branch_id: str, actor: MergeActor
    ) -> dict[str, Any]:
        source = branch_context(source_branch_id)
        if not source:
            raise PermissionError("Repository access is required")
        authorization_engine.require(actor.user_id, "branch.read", branch_resource(source["repository_id"], source_branch_id))
        return ahead_behind(source_branch_id, target_branch_id)

    def create_request(
        self,
        *,
        source_branch_id: str,
        target_branch_id: str,
        title: str,
        description: str | None,
        actor: MergeActor,
    ) -> dict[str, Any]:
        source = branch_context(source_branch_id)
        target = branch_context(target_branch_id)
        if not source or not target or source["repository_id"] != target["repository_id"]:
            raise ValueError("Source and target branches must exist in one repository")
        role = repository_role(source["repository_id"], actor.user_id)
        authorization_engine.require(actor.user_id, "merge_request.create", repository_resource(source["repository_id"]))
        if source["branch_type"] != "USER" or source["status"] != "ACTIVE":
            raise ValueError("Source must be an active personal branch")
        if target["branch_type"] != "MAIN" or target["status"] != "ACTIVE":
            raise ValueError("Target must be the active protected main branch")
        if source["created_by"] != actor.user_id and role != "owner":
            raise PermissionError("Only the branch owner or repository owner can open this request")
        base_commit = merge_base(source["head_commit_id"], target["head_commit_id"])
        base = _state(source_branch_id, base_commit)
        main = _state(target_branch_id, target["head_commit_id"])
        branch = _state(source_branch_id, source["head_commit_id"])
        merge_result = three_way_merge(base, main, branch)
        request_id = create_merge_request_record(
            repository_id=source["repository_id"], source_branch_id=source_branch_id,
            target_branch_id=target_branch_id, source_head=source["head_commit_id"],
            target_head=target["head_commit_id"], base_commit=base_commit,
            created_by=actor.user_id, title=title, description=description,
            conflicts=merge_result["conflicts"],
        )
        record_audit_event(
            "MERGE_REQUEST_CREATED", actor_user_id=actor.user_id,
            repository_id=source["repository_id"], branch_id=source_branch_id,
            merge_request_id=request_id,
            payload={"title": title, "target_branch_id": target_branch_id,
                     "conflict_count": len(merge_result["conflicts"])},
        )
        if merge_result["conflicts"]:
            record_audit_event(
                "MERGE_CONFLICT_CREATED", actor_user_id=actor.user_id,
                repository_id=source["repository_id"], branch_id=source_branch_id,
                merge_request_id=request_id,
                payload={"count": len(merge_result["conflicts"]),
                         "types": sorted({item["conflict_type"] for item in merge_result["conflicts"]})},
            )
            try:
                owner_id = source.get("repository_owner_id")
                if owner_id and owner_id != actor.user_id:
                    from .. import database
                    database.create_notification(
                        owner_id, "MERGE_REQUEST_CONFLICTED", "New merge request has conflicts",
                        f"'{title}' has {len(merge_result['conflicts'])} conflict(s) — run AI analysis or resolve manually.",
                        resource_type="MERGE_REQUEST", resource_id=request_id,
                    )
            except Exception as exc:
                structured_log(
                    logging.WARNING, "merge_conflict_notification_failed",
                    merge_request_id=request_id, error=str(exc), error_type=type(exc).__name__,
                )
        record_metric(
            "merge_conflict", 1 if merge_result["conflicts"] else 0, "boolean",
            user_id=actor.user_id, repository_id=source["repository_id"],
            branch_id=source_branch_id,
        )
        record_audit_event(
            "VALIDATION_STARTED", actor_user_id="USR_SYSTEM", actor_type="SYSTEM",
            repository_id=source["repository_id"], branch_id=source_branch_id,
            merge_request_id=request_id,
        )
        with metric_timer(
            "validation_latency", repository_id=source["repository_id"],
            branch_id=source_branch_id,
        ):
            validation = validate_workbook(merge_result["merged"], main)
        save_validation_run(
            repository_id=source["repository_id"], branch_id=source_branch_id,
            merge_request_id=request_id, validation=validation,
        )
        record_audit_event(
            "VALIDATION_COMPLETED" if validation["status"] == "PASSED" else "VALIDATION_FAILED",
            actor_user_id="USR_SYSTEM", actor_type="SYSTEM",
            repository_id=source["repository_id"], branch_id=source_branch_id,
            merge_request_id=request_id, status=validation["status"],
            payload={"errors": validation["error_count"], "warnings": validation["warning_count"]},
        )
        return self.get_request(request_id, actor)

    def list_requests(self, repository_id: str, actor: MergeActor) -> list[dict[str, Any]]:
        authorization_engine.require(actor.user_id, "merge_request.read", repository_resource(repository_id))
        return list_merge_requests(repository_id)

    def get_request(self, merge_request_id: str, actor: MergeActor) -> dict[str, Any]:
        request = get_merge_request(merge_request_id)
        if not request:
            raise PermissionError("Merge request does not exist or is not accessible")
        authorization_engine.require(actor.user_id, "merge_request.read", repository_resource(request["repository_id"]))
        source = branch_context(request["source_branch_id"])
        target = branch_context(request["target_branch_id"])
        request["heads_current"] = bool(
            source and target
            and source["head_commit_id"] == request["source_head_commit_id"]
            and target["head_commit_id"] == request["target_head_commit_id"]
        )
        request["divergence"] = ahead_behind(
            request["source_branch_id"], request["target_branch_id"]
        )
        base_state = _state(
            request["source_branch_id"], request["merge_base_commit_id"]
        )
        branch_state = _state(
            request["source_branch_id"], request["source_head_commit_id"]
        )
        changes = _annotate_changes(semantic_diff(base_state, branch_state), base_state, branch_state)
        request["repository_name"] = (source or {}).get("repository_name")
        request["workbook_filename"] = workbook_filename((source or {}).get("main_table_id"))
        request["change_summary"] = {
            "total": len(changes),
            "cells": sum(item["operation_type"].startswith("CELL_") for item in changes),
            "formulas": sum(item["operation_type"] == "CELL_FORMULA_UPDATE" for item in changes),
            "rows_added": sum(item["operation_type"] == "ROW_INSERT" for item in changes),
            "rows_deleted": sum(item["operation_type"] == "ROW_DELETE" for item in changes),
            "columns": sum(item["operation_type"].startswith("COLUMN_") for item in changes),
            "sheets": len({item.get("sheet_id") for item in changes}),
        }
        request["changes"] = changes[:500]
        request["timeline"] = _annotate_changes(
            branch_change_timeline(
                request["source_branch_id"],
                request["source_head_commit_id"],
                request["merge_base_commit_id"],
            ),
            base_state, branch_state,
        )[:1000]
        return request

    def resolve_conflict(
        self,
        *,
        merge_request_id: str,
        conflict_id: str,
        resolution_type: str,
        custom_value: Any,
        actor: MergeActor,
    ) -> dict[str, Any]:
        request = self.get_request(merge_request_id, actor)
        authorization_engine.require(actor.user_id, "merge_request.resolve", repository_resource(request["repository_id"]))
        if request["status"] in {"MERGED", "CLOSED"}:
            raise ValueError("Merge request is closed")
        conflict = next(
            (item for item in request["conflicts"] if item["conflict_id"] == conflict_id), None
        )
        if not conflict:
            raise ValueError("Conflict does not belong to this merge request")
        if conflict["status"] != "OPEN":
            raise ValueError("Conflict has already been resolved")
        if resolution_type == "KEEP_MAIN":
            selected = conflict["main_state"].get("value")
        elif resolution_type == "ACCEPT_BRANCH":
            selected = conflict["branch_state"].get("value")
        elif resolution_type == "CUSTOM":
            selected = custom_value
        else:
            raise ValueError("Unsupported conflict resolution")
        resolve_conflict_record(conflict_id, actor.user_id, resolution_type, selected)
        try:
            from ..ai.merge_knowledge import record_resolution_outcome
            from ..database import get_repository_organization_id
            record_resolution_outcome(
                conflict, resolution_type, selected, request["repository_id"],
                organization_id=get_repository_organization_id(request["repository_id"]),
            )
        except Exception:
            pass
        record_audit_event(
            "MERGE_CONFLICT_RESOLVED", actor_user_id=actor.user_id,
            repository_id=request["repository_id"], branch_id=request["source_branch_id"],
            merge_request_id=merge_request_id,
            payload={"conflict_id": conflict_id, "resolution_type": resolution_type},
        )
        updated = self.get_request(merge_request_id, actor)
        if not any(item["status"] == "OPEN" for item in updated["conflicts"]):
            self._validate_request(updated)
        return self.get_request(merge_request_id, actor)

    def _validate_request(self, request: dict[str, Any]) -> dict[str, Any]:
        source = branch_context(request["source_branch_id"])
        target = branch_context(request["target_branch_id"])
        if not source or not target:
            raise ValueError("Merge request branch no longer exists")
        base = _state(request["source_branch_id"], request["merge_base_commit_id"])
        main = _state(request["target_branch_id"], request["target_head_commit_id"])
        branch = _state(request["source_branch_id"], request["source_head_commit_id"])
        result = three_way_merge(base, main, branch, _resolution_map(request))
        unresolved = [
            conflict for conflict in result["conflicts"]
            if resolution_key(conflict) not in _resolution_map(request)
        ]
        if unresolved:
            raise MergeConflictError(unresolved)
        validation = validate_workbook(result["merged"], main)
        save_validation_run(
            repository_id=request["repository_id"], branch_id=request["source_branch_id"],
            merge_request_id=request["merge_request_id"], validation=validation,
        )
        return {"state": result["merged"], "validation": validation}

    def review(
        self,
        *,
        merge_request_id: str,
        decision: str,
        comment: str | None,
        actor: MergeActor,
        skip_permission_check: bool = False,
    ) -> dict[str, Any]:
        request = self.get_request(merge_request_id, actor)
        if not skip_permission_check:
            permission = "merge_request.approve" if decision == "APPROVED" else "merge_request.review"
            authorization_engine.require(actor.user_id, permission, repository_resource(request["repository_id"]))
        if decision == "APPROVED":
            if not request["heads_current"]:
                raise MergeHeadChangedError("Source or target HEAD changed after the request was opened")
            if any(item["status"] == "OPEN" for item in request["conflicts"]):
                raise ValueError("Resolve every merge conflict before approval")
            if request.get("validation_status") != "PASSED":
                raise ValueError("Validation must pass before approval")
        if decision not in {"APPROVED", "REJECTED"}:
            raise ValueError("Decision must be APPROVED or REJECTED")
        save_review(merge_request_id, actor.user_id, decision, comment)
        record_audit_event(
            f"MERGE_REQUEST_{decision}", actor_user_id=actor.user_id,
            repository_id=request["repository_id"], branch_id=request["source_branch_id"],
            merge_request_id=merge_request_id, payload={"comment": comment},
        )
        return self.get_request(merge_request_id, actor)

    def merge(self, merge_request_id: str, actor: MergeActor, delete_source_branch: bool = False) -> dict[str, Any]:
        request = self.get_request(merge_request_id, actor)
        authorization_engine.require(actor.user_id, "merge_request.merge", repository_resource(request["repository_id"]))
        if request["status"] != "APPROVED":
            raise ValueError("Merge request must be approved before merge")
        if not request["heads_current"]:
            raise MergeHeadChangedError("Source or target HEAD changed after review")
        owner_approved = any(
            review["decision"] == "APPROVED" for review in request["reviews"]
        )
        if not owner_approved:
            raise PermissionError("The repository owner must approve this merge request")
        merged = self._validate_request(request)
        if merged["validation"]["status"] != "PASSED":
            raise ValueError("Validation failed; merge is blocked")
        source = branch_context(request["source_branch_id"])
        target = branch_context(request["target_branch_id"])
        main = _state(target["branch_id"], target["head_commit_id"])
        changes = semantic_diff(main, merged["state"])
        if not changes:
            raise ValueError("Branch has no changes to merge")
        snapshot = get_table_snapshot(target["data_table_id"])
        with metric_timer(
            "merge_latency", user_id=actor.user_id,
            repository_id=target["repository_id"], branch_id=target["branch_id"],
        ):
            result = commit_semantic_delta(
                table_id=target["data_table_id"], repository_id=target["repository_id"],
                branch_id=target["branch_id"], expected_head_commit_id=target["head_commit_id"],
                base_version=snapshot["version"], changes=changes, user_id=actor.user_id,
                user_email=actor.email, message=f"Merge {source['branch_name']}: {request['title']}",
                additional_parent_commit_id=source["head_commit_id"], commit_status="MERGE",
            )
        mark_merged(
            merge_request_id, source["branch_id"], result["commit_id"], actor.user_id
        )
        cleanup_result = None
        if delete_source_branch:
            cleanup_result = BranchLifecycleManager.cleanup_merged_branch(
                branch_id=source["branch_id"], actor_user_id=actor.user_id
            )
        record_audit_event(
            "MERGE_COMPLETED", actor_user_id=actor.user_id,
            repository_id=target["repository_id"], branch_id=target["branch_id"],
            commit_id=result["commit_id"], merge_request_id=merge_request_id,
            payload={"source_branch_id": source["branch_id"], "change_count": len(changes), "cleanup": cleanup_result},
        )
        record_audit_event(
            "WORKING_COPY_REVOKED", actor_user_id="USR_SYSTEM", actor_type="SYSTEM",
            repository_id=target["repository_id"], branch_id=source["branch_id"],
            merge_request_id=merge_request_id,
        )
        try:
            rescore_repository_after_merge(
                target["repository_id"], target["data_table_id"], target["branch_id"],
                target["branch_name"], actor.user_id,
            )
        except Exception as exc:
            structured_log(
                logging.WARNING, "post_merge_repository_rescore_failed",
                repository_id=target["repository_id"], merge_request_id=merge_request_id,
                error=str(exc), error_type=type(exc).__name__,
            )
        try:
            rescore_macros_after_merge(
                target["repository_id"], target["data_table_id"], target["branch_id"], actor.user_id,
            )
        except Exception as exc:
            structured_log(
                logging.WARNING, "post_merge_macro_rescore_failed",
                repository_id=target["repository_id"], merge_request_id=merge_request_id,
                error=str(exc), error_type=type(exc).__name__,
            )
        return {
            **result,
            "merge_request_id": merge_request_id,
            "source_branch_id": source["branch_id"],
            "source_branch_status": "DELETED" if delete_source_branch else "MERGED",
            "working_copy_status": "REVOKED",
            "cleanup": cleanup_result,
        }

    def sync_branch(self, branch_id: str, actor: MergeActor) -> dict[str, Any]:
        source = branch_context(branch_id)
        if not source or source["branch_type"] != "USER" or source["status"] != "ACTIVE":
            raise ValueError("An active personal branch is required")
        role = repository_role(source["repository_id"], actor.user_id)
        authorization_engine.require(actor.user_id, "branch.sync", branch_resource(source["repository_id"], branch_id))
        if source["created_by"] != actor.user_id and role != "owner":
            raise PermissionError("Only the branch owner can sync this branch")
        target = branch_context(source["default_branch_id"])
        base_commit = merge_base(source["head_commit_id"], target["head_commit_id"])
        base = _state(branch_id, base_commit)
        main = _state(target["branch_id"], target["head_commit_id"])
        branch = _state(branch_id, source["head_commit_id"])
        merge_result = three_way_merge(base, main, branch)
        if merge_result["conflicts"]:
            raise MergeConflictError(merge_result["conflicts"])
        changes = semantic_diff(branch, merge_result["merged"])
        if not changes:
            update_branch_base(branch_id, target["head_commit_id"])
            return {"status": "UP_TO_DATE", **ahead_behind(branch_id, target["branch_id"])}
        snapshot = get_table_snapshot(source["data_table_id"])
        result = commit_semantic_delta(
            table_id=source["data_table_id"], repository_id=source["repository_id"],
            branch_id=branch_id, expected_head_commit_id=source["head_commit_id"],
            base_version=snapshot["version"], changes=changes, user_id=actor.user_id,
            user_email=actor.email, message=f"Sync main into {source['branch_name']}",
            additional_parent_commit_id=target["head_commit_id"], commit_status="SYNC",
        )
        update_branch_base(branch_id, target["head_commit_id"])
        record_audit_event(
            "BRANCH_SYNCED", actor_user_id=actor.user_id,
            repository_id=source["repository_id"], branch_id=branch_id,
            commit_id=result["commit_id"],
            payload={"target_head_commit_id": target["head_commit_id"]},
        )
        return {**result, "status": "SYNCED", "target_head_commit_id": target["head_commit_id"]}

    def revert_commit(self, commit_id: str, actor: MergeActor) -> dict[str, Any]:
        commit = get_commit(commit_id)
        if not commit:
            raise ValueError("Commit does not exist")
        branch = branch_context(commit["branch_id"])
        if not branch or branch["status"] != "ACTIVE":
            raise PermissionError("This commit cannot be reverted on its branch")
        authorization_engine.require(actor.user_id, "commit.revert", branch_resource(commit["repository_id"], branch["branch_id"]))
        current_state = _state(branch["branch_id"], branch["head_commit_id"])
        sheets = {sheet["sheet_id"]: sheet for sheet in current_state.get("sheets", [])}

        def current_change_value(change: dict[str, Any]) -> Any:
            sheet = sheets.get(change.get("sheet_id"))
            if not sheet:
                return None
            operation = change["operation_type"]
            if operation.startswith("CELL_"):
                row = next((item for item in sheet.get("rows", []) if item["row_id"] == change.get("row_id")), None)
                if not row:
                    return None
                map_name = {
                    "CELL_VALUE_UPDATE": "values", "CELL_FORMULA_UPDATE": "formulas",
                    "CELL_FORMAT_UPDATE": "styles", "CELL_COMMENT_UPDATE": "comments",
                }[operation]
                return row.get(map_name, {}).get(change.get("column_id"))
            if operation == "ROW_MOVE":
                row = next((item for item in sheet.get("rows", []) if item["row_id"] == change.get("row_id")), None)
                return row.get("position") if row else None
            if operation in {"COLUMN_MOVE", "COLUMN_RENAME"}:
                column = next((item for item in sheet.get("columns", []) if item["column_id"] == change.get("column_id")), None)
                return column.get("position" if operation == "COLUMN_MOVE" else "name") if column else None
            if operation in {"SHEET_MOVE", "SHEET_RENAME"}:
                return sheet.get("position" if operation == "SHEET_MOVE" else "name")
            return "STRUCTURAL"

        inverse = []
        for change in reversed(commit["changes"]):
            operation = change["operation_type"]
            common = {
                "sheet_id": change.get("sheet_id"), "row_id": change.get("row_id"),
                "column_id": change.get("column_id"),
            }
            expected_current = {
                "CELL_VALUE_UPDATE": change.get("new_value"),
                "CELL_FORMULA_UPDATE": change.get("new_formula"),
                "CELL_FORMAT_UPDATE": change.get("new_style_hash"),
                "CELL_COMMENT_UPDATE": change.get("new_comment"),
                "ROW_MOVE": change.get("new_row_position"),
                "COLUMN_MOVE": change.get("new_column_position"),
                "COLUMN_RENAME": change.get("new_value"),
                "SHEET_MOVE": change.get("new_row_position"),
                "SHEET_RENAME": change.get("new_value"),
            }.get(operation, "STRUCTURAL")
            if expected_current != "STRUCTURAL" and current_change_value(change) != expected_current:
                raise ValueError(
                    f"Cannot safely revert {commit_id}: {operation} was modified by a later commit"
                )
            if operation == "CELL_VALUE_UPDATE":
                inverse.append({**common, "operation_type": operation, "new_value": change.get("old_value")})
            elif operation == "CELL_FORMULA_UPDATE":
                inverse.append({**common, "operation_type": operation, "new_formula": change.get("old_formula")})
            elif operation == "CELL_FORMAT_UPDATE":
                inverse.append({**common, "operation_type": operation, "new_style_hash": change.get("old_style_hash")})
            elif operation == "CELL_COMMENT_UPDATE":
                inverse.append({**common, "operation_type": operation, "new_comment": change.get("old_comment")})
            elif operation == "ROW_INSERT":
                inverse.append({**common, "operation_type": "ROW_DELETE"})
            elif operation == "ROW_DELETE":
                inverse.append({**common, "operation_type": "ROW_INSERT", "new_row_position": change.get("previous_row_position"), "new_value": change.get("old_value") or {}, "metadata": change.get("metadata") or {}})
            elif operation == "ROW_MOVE":
                inverse.append({**common, "operation_type": "ROW_MOVE", "new_row_position": change.get("previous_row_position")})
            elif operation == "COLUMN_INSERT":
                inverse.append({**common, "operation_type": "COLUMN_DELETE"})
            elif operation == "COLUMN_DELETE":
                metadata = change.get("metadata") or {}
                column_name = metadata.get("column_name")
                if not column_name:
                    raise ValueError("This historical column deletion lacks safe revert metadata")
                inverse.append({
                    **common, "operation_type": "COLUMN_INSERT",
                    "new_column_position": change.get("previous_column_position"),
                    "new_value": column_name,
                    "new_data_type": metadata.get("data_type") or "TEXT",
                })
                inverse.extend(
                    {
                        **common, "row_id": row_id,
                        "operation_type": "CELL_VALUE_UPDATE", "new_value": value,
                    }
                    for row_id, value in (change.get("old_value") or {}).items()
                )
            elif operation == "COLUMN_RENAME":
                inverse.append({**common, "operation_type": "COLUMN_RENAME", "new_value": change.get("old_value")})
            elif operation == "COLUMN_MOVE":
                inverse.append({**common, "operation_type": "COLUMN_MOVE", "new_column_position": change.get("previous_column_position")})
            elif operation == "SHEET_RENAME":
                inverse.append({**common, "operation_type": "SHEET_RENAME", "new_value": change.get("old_value")})
            elif operation == "SHEET_MOVE":
                inverse.append({**common, "operation_type": "SHEET_MOVE", "new_row_position": change.get("previous_row_position")})
            elif operation == "SHEET_CREATE":
                inverse.append({**common, "operation_type": "SHEET_DELETE"})
            else:
                raise ValueError(f"Revert is not yet safe for {operation}")
        snapshot = get_table_snapshot(branch["data_table_id"])
        result = commit_semantic_delta(
            table_id=branch["data_table_id"], repository_id=branch["repository_id"],
            branch_id=branch["branch_id"], expected_head_commit_id=branch["head_commit_id"],
            base_version=snapshot["version"], changes=inverse, user_id=actor.user_id,
            user_email=actor.email, message=f"Revert '{commit['message']}'",
            reverts_commit_id=commit_id, commit_status="REVERT",
        )
        record_audit_event(
            "COMMIT_REVERTED", actor_user_id=actor.user_id,
            repository_id=branch["repository_id"], branch_id=branch["branch_id"],
            commit_id=result["commit_id"],
            payload={"reverts_commit_id": commit_id, "change_count": len(inverse)},
        )
        return result


merge_service = MergeService()
