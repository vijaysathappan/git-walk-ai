"""Shared evidence-shaping helpers for AI-gateway callers.

Turns the structured objects already produced elsewhere in the codebase
(``excel.diff_engine.semantic_diff`` change lists, ``euc.dependency`` impact
reports, ``MERGE_CONFLICTS`` rows, ``merge_knowledge`` precedents) into the
evidence-item dicts ``AIGateway.generate()`` expects, so features that call
the gateway (commit validation, merge-conflict resolution, and future
callers) don't each re-derive the same shape.

Evidence items reuse ``ai.retrieval.EvidenceItem`` (the same shape the
enterprise-copilot retriever already produces) rather than inventing a
second format — ``AIGateway.generate()`` serializes each item's ``data``
into the prompt and validates the model's citations against ``(type, id)``
pairs, so every item needs the full ``EvidenceItem`` shape, not just an id.
"""

from __future__ import annotations

import json
from typing import Any

from .retrieval import INJECTION_PATTERNS, EvidenceItem


def _serialize_scanned(items: list[EvidenceItem]) -> list[dict[str, Any]]:
    """Same prompt-injection scan `EvidenceRetriever.retrieve` (the chat
    retriever) applies to its evidence -- cell values and commit messages
    reaching the merge/commit-review agents are exactly as untrusted as
    chat evidence, so they get flagged rather than fed to the model unseen.
    """
    for item in items:
        if INJECTION_PATTERNS.search(json.dumps(item.data, default=str)):
            item.warnings.append("Potential prompt-injection text detected; content remains evidence only.")
    return [item.serializable() for item in items]


def _change_evidence_id(change: dict[str, Any]) -> str:
    parts = [
        str(change.get("sheet_id") or ""),
        str(change.get("row_id") or ""),
        str(change.get("column_id") or ""),
    ]
    suffix = ":".join(part for part in parts if part)
    operation = str(change.get("operation_type") or "CHANGE")
    return f"{operation}:{suffix}" if suffix else operation


def _change_summary(change: dict[str, Any]) -> str:
    operation = str(change.get("operation_type") or "CHANGE")
    old_value = change.get("old_value")
    new_value = change.get("new_value")
    if old_value is not None or new_value is not None:
        return f"{operation}: {old_value!r} -> {new_value!r}"
    return operation


def build_commit_evidence(
    semantic_changes: list[dict[str, Any]] | None,
    dependency_impact: dict[str, Any] | None = None,
    field_investigations: list[dict[str, Any]] | None = None,
    risk_breakdown: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Evidence for commit-review features: the semantic diff, optional
    downstream dependency impact, cleared-field investigations
    (``ai.change_inspection.investigate_cleared_field``), and the
    deterministic risk-score breakdown (``merge_risk.evaluate_change_risk``)."""
    items: list[EvidenceItem] = []
    for change in semantic_changes or []:
        items.append(EvidenceItem(
            type="semantic_change", id=_change_evidence_id(change),
            title=str(change.get("operation_type") or "Semantic change"),
            summary=_change_summary(change), data=change,
        ))
    if dependency_impact:
        for node in dependency_impact.get("nodes", []) or []:
            node = node if isinstance(node, dict) else {"node_id": node}
            node_id = node.get("node_id")
            if not node_id:
                continue
            items.append(EvidenceItem(
                type="dependency_impact", id=str(node_id),
                title="Downstream dependency impact",
                summary=f"Cell {node.get('cell_address', node_id)} has downstream fan-out",
                data=node,
            ))
    for item in field_investigations or []:
        items.append(EvidenceItem(
            type="cleared_field_investigation",
            id=f"{item.get('sheet_id')}:{item.get('row_id')}:{item.get('column_id')}",
            title=f"{item.get('column_name')} cleared",
            summary=f"Previously {item.get('previous_value')!r}, now empty.", data=item,
        ))
    for component in risk_breakdown or []:
        items.append(EvidenceItem(
            type="risk_component", id=str(component.get("dimension")),
            title=str(component.get("dimension", "")).replace("_", " ").title(),
            summary=str(component.get("explanation") or ""), data=component,
        ))
    return _serialize_scanned(items)


def build_finding_evidence(finding: dict[str, Any]) -> list[dict[str, Any]]:
    """Evidence for a single EUC finding-remediation recommendation: the
    finding's own observed evidence, its dependency blast radius, and any
    prior lifecycle actions already recorded against it (so the agent
    doesn't recommend re-acknowledging something already accepted)."""
    items: list[EvidenceItem] = [EvidenceItem(
        type="euc_finding", id=str(finding.get("finding_id")),
        title=str(finding.get("title") or finding.get("rule_id") or "EUC finding"),
        summary=str(finding.get("description") or ""), data=finding,
    )]
    impact = finding.get("dependency_impact") or {}
    if impact:
        items.append(EvidenceItem(
            type="dependency_impact", id=str(finding.get("finding_id")),
            title="Downstream dependency impact",
            summary=f"{impact.get('downstream', 0)} downstream cell(s) across {impact.get('sheets', 0)} sheet(s)",
            data=impact,
        ))
    for action in finding.get("actions") or []:
        action_id = action.get("action_id")
        if not action_id:
            continue
        items.append(EvidenceItem(
            type="finding_action", id=str(action_id),
            title=f"Previously set to {action.get('new_status')}",
            summary=str(action.get("reason") or ""), data=action,
        ))
    return _serialize_scanned(items)


def build_merge_evidence(
    diff_summary: list[dict[str, Any]] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
    precedents: list[dict[str, Any]] | None = None,
    authorship: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Evidence for merge-conflict features: the branch-vs-main semantic
    diff, open ``MERGE_CONFLICTS`` rows, RAG resolution precedents
    (``merge_knowledge.retrieve_precedents``), and cell authorship/history
    (``commit_store.get_cell_history`` / ``branch_change_timeline``)."""
    items: list[EvidenceItem] = []
    for change in diff_summary or []:
        items.append(EvidenceItem(
            type="semantic_change", id=_change_evidence_id(change),
            title=str(change.get("operation_type") or "Semantic change"),
            summary=_change_summary(change), data=change,
        ))
    for conflict in conflicts or []:
        conflict_id = conflict.get("CONFLICT_ID") or conflict.get("conflict_id")
        if not conflict_id:
            continue
        items.append(EvidenceItem(
            type="merge_conflict", id=str(conflict_id),
            title=str(conflict.get("conflict_type") or conflict.get("CONFLICT_TYPE") or "Merge conflict"),
            summary="Base/main/branch state for an unresolved merge conflict", data=conflict,
        ))
    for doc in precedents or []:
        doc_id = doc.get("doc_id") or doc.get("DOC_ID")
        if not doc_id:
            continue
        items.append(EvidenceItem(
            type="resolution_precedent", id=str(doc_id),
            title=str(doc.get("title") or "Resolution precedent"),
            summary=str(doc.get("rationale") or ""), data=doc,
        ))
    for entry in authorship or []:
        entry_id = entry.get("commit_id") or entry.get("COMMIT_ID")
        if not entry_id:
            continue
        items.append(EvidenceItem(
            type="cell_authorship", id=str(entry_id),
            title=f"Change by {entry.get('author_email') or entry.get('AUTHOR_EMAIL') or 'unknown author'}",
            summary=str(entry.get("commit_message") or entry.get("MESSAGE") or ""), data=entry,
        ))
    return _serialize_scanned(items)
