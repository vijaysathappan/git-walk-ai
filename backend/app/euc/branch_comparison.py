"""Branch-vs-main EUC risk snapshot, diff, and commit attribution.

Stage 2.1-2.3 EUC analysis (``euc.service``, ``euc.dependency``,
``euc.intelligence``) operates on a static uploaded ``.xlsx`` snapshot and
has no concept of a branch. This module builds a branch-aware comparison on
top of that unmodified pipeline: materialize main's and a branch's current
state as real, ingestable workbook bytes (reusing
``services.workbook_service.export_branch_workbook``, already used by
"Download full branch"), run the existing analysis pipeline against each,
and diff the resulting ``EUC_FINDINGS``.

The genuinely new step is attribution: every EUC_FINDING_OCCURRENCES row
carries a ``sheet_id``/``cell_address`` pair. Because sheet identity
(``WORKBOOK_SHEETS.SHEET_ID``) is scoped to the repository -- not to any one
EUC asset -- and shared by every branch's ``BRANCH_SHEETS`` row, a finding's
``sheet_id`` is directly comparable across main's and a branch's separately
ingested snapshots, and can be resolved back to this product's stable
``(sheet_id, row_id, column_id)`` cell identity to look up real per-cell
commit authorship via ``repositories.commit_store.get_cell_history`` --
already used by the merge and commit-review agents.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from openpyxl.utils import coordinate_to_tuple

from ..excel.identity import semantic_snapshot
from ..database import _get_connection
from ..repositories.commit_store import get_cell_history
from ..repositories.merge_store import branch_context
from ..services.workbook_service import export_branch_workbook
from .dependency.services import DependencyService
from .intelligence import IntelligenceService
from .service import analyze_euc, ingest_euc

_dependency_service = DependencyService()
_intelligence_service = IntelligenceService()


def ingest_branch_snapshot(repository_id: str, branch_id: str, branch_name: str, table_id: str, user_id: str) -> dict[str, Any]:
    """Materialize a branch's current state and run just Stage 2.1
    (ingest + analyze) against it -- the same scope as the existing manual
    "Register and analyze" upload flow, letting a user pick a branch to
    inventory instead of exporting and re-uploading a file by hand. Shares
    the export/ingest primitives with ``snapshot_branch_for_comparison``
    above but stops before the heavier dependency/intelligence build,
    which the EUC workspace's own "Build dependency intelligence" /
    "Run control intelligence" actions already trigger on demand."""
    export = export_branch_workbook(table_id, branch_id, branch_name)
    try:
        payload = Path(export["path"]).read_bytes()
    finally:
        shutil.rmtree(Path(export["path"]).parent, ignore_errors=True)
    asset = ingest_euc(repository_id, f"{branch_name.replace('/', '_')}.xlsx", payload, user_id)
    analyze_euc(asset["euc_id"], user_id)
    return asset


def snapshot_branch_for_comparison(
    repository_id: str, branch_id: str, branch_name: str, table_id: str, user_id: str,
) -> dict[str, Any]:
    """Materialize ``branch_id``'s current state and run the full, unmodified
    Stage 2.1 -> 2.2 -> 2.3 EUC pipeline against it. Ingestion dedupes by
    content hash, so re-comparing an unchanged branch reuses the same
    ``EUC_ID`` at no extra analysis cost."""
    export = export_branch_workbook(table_id, branch_id, branch_name)
    try:
        payload = Path(export["path"]).read_bytes()
    finally:
        shutil.rmtree(Path(export["path"]).parent, ignore_errors=True)
    asset = ingest_euc(repository_id, f"{branch_name.replace('/', '_')}.xlsx", payload, user_id)
    euc_id = asset["euc_id"]
    analyze_euc(euc_id, user_id)
    _dependency_service.build(euc_id, user_id)
    _intelligence_service.build(euc_id, user_id)
    overview = _intelligence_service.overview(euc_id, user_id)
    findings = _intelligence_service.findings(euc_id, user_id, limit=500)["items"]
    return {
        "euc_id": euc_id,
        "intelligence_run_id": overview["intelligence_run_id"],
        "scores": overview["scores"],
        "findings": findings,
    }


def _finding_key(finding: dict[str, Any]) -> tuple:
    return (finding.get("rule_id"), finding.get("sheet_id"), finding.get("cell_address"), finding.get("node_id"))


def diff_findings(main_findings: list[dict[str, Any]], branch_findings: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify findings as introduced (present on the branch, absent on
    main), resolved (present on main, absent on the branch), or unchanged."""
    main_by_key = {_finding_key(item): item for item in main_findings}
    branch_by_key = {_finding_key(item): item for item in branch_findings}
    introduced = [item for key, item in branch_by_key.items() if key not in main_by_key]
    resolved = [item for key, item in main_by_key.items() if key not in branch_by_key]
    return {"introduced": introduced, "resolved": resolved}


def resolve_cell_identity(branch_state: dict[str, Any], sheet_id: str, cell_address: str) -> dict[str, Any] | None:
    """Resolve a plain Excel ``cell_address`` (e.g. ``"C5"``) on one sheet of
    a branch's semantic snapshot (``excel.identity.semantic_snapshot``) back
    to this product's stable ``(sheet_id, row_id, column_id)`` cell
    identity. Returns ``None`` when the address falls outside the sheet's
    data region (e.g. the header row) or the sheet can't be found."""
    sheet = next((item for item in branch_state.get("sheets", []) if item.get("sheet_id") == sheet_id), None)
    if not sheet:
        return None
    try:
        row_index, col_index = coordinate_to_tuple(cell_address)
    except ValueError:
        return None
    if row_index < 2:  # row 1 is the header row written by _write_branch_xlsx
        return None
    columns = sorted(sheet.get("columns", []), key=lambda item: item["position"])
    rows = sorted(sheet.get("rows", []), key=lambda item: item["position"])
    col_pos, row_pos = col_index - 1, row_index - 2
    if col_pos < 0 or col_pos >= len(columns) or row_pos < 0 or row_pos >= len(rows):
        return None
    return {"sheet_id": sheet_id, "row_id": rows[row_pos]["row_id"], "column_id": columns[col_pos]["column_id"]}


def attribute_finding(branch_id: str, branch_state: dict[str, Any], finding: dict[str, Any]) -> dict[str, Any] | None:
    """Attribute one introduced finding to the exact commit/author that
    caused it, when it maps to a single cell. Returns ``None`` (not an
    error) for structural findings with no single addressable cell -- e.g.
    a whole-workbook VBA finding or a multi-node circular dependency --
    rather than guessing."""
    sheet_id, cell_address = finding.get("sheet_id"), finding.get("cell_address")
    if not sheet_id or not cell_address:
        return None
    identity = resolve_cell_identity(branch_state, sheet_id, cell_address)
    if not identity:
        return None
    history = get_cell_history(branch_id, identity["sheet_id"], identity["row_id"], identity["column_id"], limit=50)
    if not history:
        return None
    latest = history[0]
    return {
        "finding_id": finding.get("finding_id"),
        "rule_id": finding.get("rule_id"),
        "sheet_id": sheet_id,
        "cell_address": cell_address,
        "author_email": latest.get("author_email"),
        "commit_id": latest.get("commit_id"),
        "commit_message": latest.get("message"),
        "occurred_at": latest.get("created_at"),
        "prior_edit_count": len(history),
    }


def resolve_main_branch(repository_id: str) -> dict[str, Any]:
    """Look up the repository's default (main) branch context, in the same
    shape ``branch_context()`` returns for any other branch."""
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT DEFAULT_BRANCH_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (repository_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["DEFAULT_BRANCH_ID"]:
        raise KeyError("Repository has no default branch")
    main = branch_context(row["DEFAULT_BRANCH_ID"])
    if not main:
        raise KeyError("Repository's default branch does not exist")
    return main
