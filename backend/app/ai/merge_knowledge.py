"""RAG knowledge base for merge-conflict resolution.

No vector/embeddings dependency exists in this project and the free NVIDIA
OpenRouter catalogue (``ai.catalog``) has no embeddings model, so retrieval
here is SQLite FTS5 keyword/BM25 search over a structured corpus — the
``MERGE_RESOLUTION_KNOWLEDGE`` table (mirrored into the
``MERGE_RESOLUTION_KNOWLEDGE_FTS`` virtual table by an append-only trigger,
see ``database.py``), consistent with this codebase's zero-heavy-dependency
style.

The corpus starts from a deterministic synthetic dataset (``SYNTHETIC_PRECEDENTS``,
one Python data module, no LLM calls) covering every ``conflict_type`` the
three-way merge engine (``excel.merge_engine``) can produce, and grows with
every real resolution via :func:`record_resolution_outcome` — every conflict
a human or the AI agent resolves is appended back as a ``HISTORICAL``
precedent, so retrieval quality compounds with product usage.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .. import database
from ..excel.identity import stable_id

# ---------------------------------------------------------------------------
# Synthetic precedent corpus — one deterministic Python data module, no LLM
# calls. Seeded idempotently via INSERT OR IGNORE keyed by stable doc ids.
# ---------------------------------------------------------------------------

SYNTHETIC_PRECEDENTS: list[dict[str, str]] = [
    # CELL_VALUE_CONFLICT
    {
        "doc_id": "SYN_CELL_VALUE_CONFLICT_001", "conflict_type": "CELL_VALUE_CONFLICT",
        "title": "Both sides overwrote the same total with different numbers",
        "scenario_text": "Main changed a subtotal cell to a new number while the personal branch independently typed a different number into the same cell; there is no shared formula driving either value.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "Two independently typed numeric overrides on the same cell cannot be reconciled automatically without knowing which figure is current; a human should confirm the source of truth.",
        "risk_level": "HIGH",
    },
    {
        "doc_id": "SYN_CELL_VALUE_CONFLICT_002", "conflict_type": "CELL_VALUE_CONFLICT",
        "title": "Branch corrected an obvious typo main left untouched",
        "scenario_text": "The branch changed a clearly malformed value (e.g. a stray decimal or transposed digits) while main's value for the same cell is unchanged from the shared base.",
        "recommended_resolution": "ACCEPT_BRANCH",
        "rationale": "Main made no change from base, so the branch's correction is the only intentional edit and is safe to accept.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_CELL_VALUE_CONFLICT_003", "conflict_type": "CELL_VALUE_CONFLICT",
        "title": "Main applied a period-close adjustment after the branch was cut",
        "scenario_text": "Main's value reflects a later, authoritative period-close adjustment posted by finance; the branch's value predates that adjustment and was not aware of it.",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "Main represents the more recent authoritative state; the branch's stale value should not overwrite a deliberate downstream correction.",
        "risk_level": "MEDIUM",
    },
    {
        "doc_id": "SYN_CELL_VALUE_CONFLICT_004", "conflict_type": "CELL_VALUE_CONFLICT",
        "title": "Both branches rounded the same figure differently",
        "scenario_text": "Main and branch both changed a cell from the same base value to numerically close but not identical figures, consistent with independent rounding of the same underlying computation.",
        "recommended_resolution": "CUSTOM",
        "rationale": "Neither side is clearly wrong; average or re-derive the figure from the shared source calculation rather than blindly picking one side.",
        "risk_level": "MEDIUM",
    },
    {
        "doc_id": "SYN_CELL_VALUE_CONFLICT_005", "conflict_type": "CELL_VALUE_CONFLICT",
        "title": "Branch value matches an external system of record",
        "scenario_text": "The branch's new value for the cell matches a value that is independently verifiable against an external system referenced elsewhere in the workbook or its comments.",
        "recommended_resolution": "ACCEPT_BRANCH",
        "rationale": "External corroboration outweighs an unverified main-branch edit.",
        "risk_level": "LOW",
    },
    # FORMULA_CONFLICT
    {
        "doc_id": "SYN_FORMULA_CONFLICT_001", "conflict_type": "FORMULA_CONFLICT",
        "title": "Branch simplified a SUM formula while main extended its range",
        "scenario_text": "Main widened a SUM range to include a newly inserted line item; the branch independently rewrote the same formula to simplify it without knowledge of the new line item.",
        "recommended_resolution": "CUSTOM",
        "rationale": "Preserve both intents: apply the branch's simplification style to main's wider, more complete range rather than picking one side wholesale.",
        "risk_level": "MEDIUM",
    },
    {
        "doc_id": "SYN_FORMULA_CONFLICT_002", "conflict_type": "FORMULA_CONFLICT",
        "title": "Branch fixed a broken formula reference main never touched",
        "scenario_text": "The branch corrected a formula that referenced a since-deleted range, while main's formula for the same cell is unchanged from base and would still error out.",
        "recommended_resolution": "ACCEPT_BRANCH",
        "rationale": "Main made no intentional formula change, so the branch's fix is the only deliberate edit and resolves a live error.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_FORMULA_CONFLICT_003", "conflict_type": "FORMULA_CONFLICT",
        "title": "Main introduced a new calculation model the branch predates",
        "scenario_text": "Main replaced a simple formula with a more sophisticated model (e.g. weighted average instead of a flat average) as part of a coordinated methodology change; the branch's formula reflects the old methodology.",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "A deliberate, wider methodology change on main should not be silently reverted by an unrelated branch edit.",
        "risk_level": "HIGH",
    },
    {
        "doc_id": "SYN_FORMULA_CONFLICT_004", "conflict_type": "FORMULA_CONFLICT",
        "title": "Both sides changed the same formula to fix the same bug",
        "scenario_text": "Main and branch both rewrote the same formula, converging on functionally equivalent expressions that fix the same underlying calculation error.",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "Both edits achieve the same outcome, so keeping either side is safe; defaulting to main avoids an unnecessary formula-text churn.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_FORMULA_CONFLICT_005", "conflict_type": "FORMULA_CONFLICT",
        "title": "Branch formula depends on a cell with high downstream fan-out",
        "scenario_text": "The branch's replacement formula references a cell that many other formulas across the workbook depend on, per the dependency graph's technical-criticality score.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "High-fan-out formula changes can ripple across the workbook; a human should confirm the change before it propagates.",
        "risk_level": "CRITICAL",
    },
    # CELL_FORMAT_CONFLICT
    {
        "doc_id": "SYN_CELL_FORMAT_CONFLICT_001", "conflict_type": "CELL_FORMAT_CONFLICT",
        "title": "Branch only changed cell formatting, main changed the value",
        "scenario_text": "The branch applied a purely cosmetic style change (e.g. currency formatting, bold) to a cell whose underlying value main independently changed.",
        "recommended_resolution": "ACCEPT_BRANCH",
        "rationale": "Formatting conflicts are cosmetic and independent of the value conflict handled separately; accepting the branch's formatting is safe.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_CELL_FORMAT_CONFLICT_002", "conflict_type": "CELL_FORMAT_CONFLICT",
        "title": "Main applied a workbook-wide style guide update",
        "scenario_text": "Main's style change is part of a broader, template-driven formatting pass applied consistently across many cells; the branch's formatting predates that pass.",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "A coordinated style-guide rollout on main should not be reverted by one branch's stale local formatting.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_CELL_FORMAT_CONFLICT_003", "conflict_type": "CELL_FORMAT_CONFLICT",
        "title": "Branch highlighted a cell to flag it for review",
        "scenario_text": "The branch added conditional highlighting to draw attention to an anomaly, while main's formatting for the same cell is unchanged from base.",
        "recommended_resolution": "ACCEPT_BRANCH",
        "rationale": "Main made no intentional formatting change, so the branch's review flag is the only deliberate edit.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_CELL_FORMAT_CONFLICT_004", "conflict_type": "CELL_FORMAT_CONFLICT",
        "title": "Both sides changed number format to different currencies",
        "scenario_text": "Main formatted a cell as USD while the branch independently formatted the same cell as EUR, reflecting a genuine disagreement about the reporting currency.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "A currency-format disagreement usually signals an unresolved business decision, not a cosmetic conflict.",
        "risk_level": "MEDIUM",
    },
    {
        "doc_id": "SYN_CELL_FORMAT_CONFLICT_005", "conflict_type": "CELL_FORMAT_CONFLICT",
        "title": "Branch formatting is redundant with main's",
        "scenario_text": "Main and branch applied visually equivalent formatting (e.g. both bold the same cell) starting from the same base style.",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "The two edits are equivalent in effect, so keeping main avoids unnecessary churn.",
        "risk_level": "LOW",
    },
    # CELL_COMMENT_CONFLICT
    {
        "doc_id": "SYN_CELL_COMMENT_CONFLICT_001", "conflict_type": "CELL_COMMENT_CONFLICT",
        "title": "Both sides left different review notes on the same cell",
        "scenario_text": "Main and branch each added a different comment to the same cell, both providing useful but distinct context for reviewers.",
        "recommended_resolution": "CUSTOM",
        "rationale": "Comments are additive documentation, not competing facts; concatenate both notes rather than discarding either.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_CELL_COMMENT_CONFLICT_002", "conflict_type": "CELL_COMMENT_CONFLICT",
        "title": "Branch resolved a review comment main left open",
        "scenario_text": "The branch's comment marks a prior open question as resolved with an explanation; main's comment for the same cell is unchanged from base.",
        "recommended_resolution": "ACCEPT_BRANCH",
        "rationale": "Main made no intentional comment change, so the branch's resolution note is the only deliberate edit.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_CELL_COMMENT_CONFLICT_003", "conflict_type": "CELL_COMMENT_CONFLICT",
        "title": "Main's comment reflects a later approval decision",
        "scenario_text": "Main's comment documents a sign-off from a reviewer dated after the branch was created; the branch's comment predates that approval.",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "The more recent approval note on main is authoritative context that should not be lost.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_CELL_COMMENT_CONFLICT_004", "conflict_type": "CELL_COMMENT_CONFLICT",
        "title": "Branch comment duplicates main's wording",
        "scenario_text": "The branch and main both added near-identical comments to the same cell starting from an empty base, suggesting parallel documentation of the same fact.",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "The comments are redundant, so keeping either is equivalent; defaulting to main avoids unnecessary text churn.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_CELL_COMMENT_CONFLICT_005", "conflict_type": "CELL_COMMENT_CONFLICT",
        "title": "Branch comment flags a data-quality concern",
        "scenario_text": "The branch's comment raises a specific, evidenced concern about the cell's value (e.g. references a mismatched source document); main's comment is unrelated or empty.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "A data-quality flag should be surfaced to a human reviewer rather than silently dropped or silently accepted.",
        "risk_level": "MEDIUM",
    },
    # STRUCTURAL_CONFLICT
    {
        "doc_id": "SYN_STRUCTURAL_CONFLICT_001", "conflict_type": "STRUCTURAL_CONFLICT",
        "title": "Both sides restructured overlapping parts of the same sheet",
        "scenario_text": "Main and branch each made multiple structural edits (inserted rows, moved columns) to overlapping regions of the same sheet, making automatic reconciliation ambiguous.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "Overlapping structural rewrites are the highest-risk conflict category and should always be reviewed by a human before merging.",
        "risk_level": "CRITICAL",
    },
    {
        "doc_id": "SYN_STRUCTURAL_CONFLICT_002", "conflict_type": "STRUCTURAL_CONFLICT",
        "title": "Branch's structural change is isolated to an unrelated region",
        "scenario_text": "The branch's structural edits touch a different area of the sheet than main's, and the merge engine still reports a structural conflict due to shared positional dependencies.",
        "recommended_resolution": "ACCEPT_BRANCH",
        "rationale": "When the two structural changes don't semantically overlap, the branch's edit can usually be safely layered onto main.",
        "risk_level": "MEDIUM",
    },
    {
        "doc_id": "SYN_STRUCTURAL_CONFLICT_003", "conflict_type": "STRUCTURAL_CONFLICT",
        "title": "Main's structural change is part of a coordinated template migration",
        "scenario_text": "Main's structural edits are part of a broader, deliberate workbook restructuring (e.g. new standard template) that the branch predates.",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "A coordinated template migration on main should take precedence over a stale branch's structure.",
        "risk_level": "HIGH",
    },
    {
        "doc_id": "SYN_STRUCTURAL_CONFLICT_004", "conflict_type": "STRUCTURAL_CONFLICT",
        "title": "Structural conflict touches a sheet with active downstream integrations",
        "scenario_text": "The conflicting sheet feeds one or more Information Fabric integration connectors, so a structural mismatch risks breaking downstream sync.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "Structural changes on integration-connected sheets have blast radius beyond the workbook and need explicit sign-off.",
        "risk_level": "CRITICAL",
    },
    {
        "doc_id": "SYN_STRUCTURAL_CONFLICT_005", "conflict_type": "STRUCTURAL_CONFLICT",
        "title": "Both sides reordered the same block for readability, no data change",
        "scenario_text": "Main and branch each reordered the same set of rows/columns for readability, with no underlying value or formula differences once reordered.",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "When the net effect is cosmetic reordering with no data divergence, keeping main avoids unnecessary rework.",
        "risk_level": "LOW",
    },
    # SHEET_RENAME_CONFLICT
    {
        "doc_id": "SYN_SHEET_RENAME_CONFLICT_001", "conflict_type": "SHEET_RENAME_CONFLICT",
        "title": "Branch renamed a sheet to match a new naming convention",
        "scenario_text": "The branch renamed a sheet to align with a recently adopted naming convention; main's sheet name is unchanged from base.",
        "recommended_resolution": "ACCEPT_BRANCH",
        "rationale": "Main made no intentional rename, so the branch's convention update is the only deliberate edit.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_SHEET_RENAME_CONFLICT_002", "conflict_type": "SHEET_RENAME_CONFLICT",
        "title": "Both sides renamed the sheet to different names",
        "scenario_text": "Main and branch each independently renamed the same sheet to different, non-equivalent names, reflecting a genuine naming disagreement.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "Divergent renames usually reflect an unresolved naming decision that should be settled by a human, not guessed at.",
        "risk_level": "MEDIUM",
    },
    {
        "doc_id": "SYN_SHEET_RENAME_CONFLICT_003", "conflict_type": "SHEET_RENAME_CONFLICT",
        "title": "Main's rename corrects a typo the branch never saw",
        "scenario_text": "Main renamed the sheet to fix an obvious spelling error; the branch's name is the original, unchanged, typo'd name.",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "A typo correction on main is a safe, low-risk improvement that should not be reverted.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_SHEET_RENAME_CONFLICT_004", "conflict_type": "SHEET_RENAME_CONFLICT",
        "title": "Sheet is referenced by external links using its old name",
        "scenario_text": "The sheet being renamed is referenced by external workbook links or integration connectors that still expect the original name.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "Renaming a sheet with external dependents risks breaking links outside the workbook; confirm before applying either rename.",
        "risk_level": "HIGH",
    },
    {
        "doc_id": "SYN_SHEET_RENAME_CONFLICT_005", "conflict_type": "SHEET_RENAME_CONFLICT",
        "title": "Branch's rename is a superset clarification of main's",
        "scenario_text": "Main shortened a sheet name for brevity while the branch expanded it for clarity; both changes started from the same base name and neither is objectively wrong.",
        "recommended_resolution": "CUSTOM",
        "rationale": "Neither name is clearly correct; propose a name that keeps main's brevity and the branch's clarifying detail.",
        "risk_level": "LOW",
    },
    # SHEET_MOVE_CONFLICT
    {
        "doc_id": "SYN_SHEET_MOVE_CONFLICT_001", "conflict_type": "SHEET_MOVE_CONFLICT",
        "title": "Branch reordered sheets for a presentation, main didn't move it",
        "scenario_text": "The branch moved a sheet to a new tab position to improve presentation order; main's sheet position is unchanged from base.",
        "recommended_resolution": "ACCEPT_BRANCH",
        "rationale": "Main made no intentional move, so the branch's reordering is the only deliberate edit and is low risk.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_SHEET_MOVE_CONFLICT_002", "conflict_type": "SHEET_MOVE_CONFLICT",
        "title": "Main reorganized the whole workbook's tab order",
        "scenario_text": "Main's sheet position change is part of a broader, deliberate reorganization of every tab in the workbook; the branch moved only this one sheet independently.",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "A workbook-wide reorganization on main should take precedence over one branch's isolated move.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_SHEET_MOVE_CONFLICT_003", "conflict_type": "SHEET_MOVE_CONFLICT",
        "title": "Both sides moved the sheet to the same logical grouping",
        "scenario_text": "Main and branch moved the sheet to adjacent positions within the same logical section (e.g. both moved it near other summary sheets).",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "The intents are equivalent, so keeping main avoids unnecessary tab-order churn.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_SHEET_MOVE_CONFLICT_004", "conflict_type": "SHEET_MOVE_CONFLICT",
        "title": "Sheet order affects a printed report's page sequence",
        "scenario_text": "The workbook's tab order is known to drive a printed or exported report's section sequence, and the two moves would produce different report orderings.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "When tab order has a downstream reporting effect, a human should decide the correct sequence rather than defaulting silently.",
        "risk_level": "MEDIUM",
    },
    {
        "doc_id": "SYN_SHEET_MOVE_CONFLICT_005", "conflict_type": "SHEET_MOVE_CONFLICT",
        "title": "Branch move is purely cosmetic with no reporting dependency",
        "scenario_text": "The branch moved a scratch or working sheet with no downstream report or integration dependency, while main's position is unchanged from base.",
        "recommended_resolution": "ACCEPT_BRANCH",
        "rationale": "Low-stakes cosmetic moves on unreferenced sheets are safe to accept from the only side that made an intentional change.",
        "risk_level": "LOW",
    },
    # SHEET_DELETE_MODIFY_CONFLICT
    {
        "doc_id": "SYN_SHEET_DELETE_MODIFY_CONFLICT_001", "conflict_type": "SHEET_DELETE_MODIFY_CONFLICT",
        "title": "Main deleted a sheet the branch was actively editing",
        "scenario_text": "Main removed a sheet as part of workbook cleanup, while the branch independently made substantive edits to that same sheet, unaware it had been deleted.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "Deleting a sheet with concurrent unmerged edits risks silently losing work; a human must decide whether to restore or discard it.",
        "risk_level": "CRITICAL",
    },
    {
        "doc_id": "SYN_SHEET_DELETE_MODIFY_CONFLICT_002", "conflict_type": "SHEET_DELETE_MODIFY_CONFLICT",
        "title": "Branch's edits were trivial formatting on a sheet main retired",
        "scenario_text": "Main deleted a deprecated sheet as part of a planned retirement; the branch's only edits to that sheet were minor formatting tweaks with no data significance.",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "A deliberate sheet retirement should proceed when the concurrent edits are low-value, but this should still be confirmed given the destructive nature of deletion.",
        "risk_level": "HIGH",
    },
    {
        "doc_id": "SYN_SHEET_DELETE_MODIFY_CONFLICT_003", "conflict_type": "SHEET_DELETE_MODIFY_CONFLICT",
        "title": "Branch deleted a sheet main was still updating",
        "scenario_text": "The branch removed a sheet it considered obsolete, while main independently continued to update data on that same sheet after the branch diverged.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "Main's continued activity suggests the sheet is still in use; deleting it would discard active work without confirmation.",
        "risk_level": "CRITICAL",
    },
    {
        "doc_id": "SYN_SHEET_DELETE_MODIFY_CONFLICT_004", "conflict_type": "SHEET_DELETE_MODIFY_CONFLICT",
        "title": "Deleted sheet feeds an active integration connector",
        "scenario_text": "The sheet being deleted by one side is a source for an active Information Fabric sync connector, and deleting it would break that integration.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "Deleting an integration-connected sheet has effects outside the workbook and must be explicitly confirmed.",
        "risk_level": "CRITICAL",
    },
    {
        "doc_id": "SYN_SHEET_DELETE_MODIFY_CONFLICT_005", "conflict_type": "SHEET_DELETE_MODIFY_CONFLICT",
        "title": "Sheet was an empty scratch pad with no references",
        "scenario_text": "The sheet in conflict is an empty or near-empty scratch sheet with no formulas referencing it from other sheets, and one side deleted it while the other made trivial edits.",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "An unreferenced scratch sheet carries low risk either way, but deletions should still default toward the more deliberate side (main) rather than silently restoring.",
        "risk_level": "MEDIUM",
    },
    # COLUMN_RENAME_CONFLICT
    {
        "doc_id": "SYN_COLUMN_RENAME_CONFLICT_001", "conflict_type": "COLUMN_RENAME_CONFLICT",
        "title": "Branch renamed a column header to fix a typo",
        "scenario_text": "The branch corrected a misspelled column header; main's header for the same column is unchanged from base.",
        "recommended_resolution": "ACCEPT_BRANCH",
        "rationale": "Main made no intentional rename, so the branch's typo fix is the only deliberate edit.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_COLUMN_RENAME_CONFLICT_002", "conflict_type": "COLUMN_RENAME_CONFLICT",
        "title": "Both sides renamed the column to different, non-equivalent labels",
        "scenario_text": "Main and branch renamed the same column header to genuinely different labels, suggesting disagreement about what the column represents.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "A column-meaning disagreement should be resolved by a human rather than guessed.",
        "risk_level": "MEDIUM",
    },
    {
        "doc_id": "SYN_COLUMN_RENAME_CONFLICT_003", "conflict_type": "COLUMN_RENAME_CONFLICT",
        "title": "Main applied a standard column-naming convention",
        "scenario_text": "Main's rename aligns the column header with an organization-wide naming standard applied across many workbooks; the branch's header predates that standard.",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "A standardization effort on main should not be reverted by a stale branch header.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_COLUMN_RENAME_CONFLICT_004", "conflict_type": "COLUMN_RENAME_CONFLICT",
        "title": "Column header is referenced by an integration field mapping",
        "scenario_text": "The column header being renamed is used as a field-mapping key by an active integration connector, and the two proposed names would map differently downstream.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "Renaming a mapped integration field can silently break downstream sync; confirm before choosing either name.",
        "risk_level": "HIGH",
    },
    {
        "doc_id": "SYN_COLUMN_RENAME_CONFLICT_005", "conflict_type": "COLUMN_RENAME_CONFLICT",
        "title": "Branch's header adds a unit suffix main's lacks",
        "scenario_text": "The branch renamed a column to add a unit suffix (e.g. 'Amount' to 'Amount (USD)') for clarity; main's header is the original, unchanged name.",
        "recommended_resolution": "ACCEPT_BRANCH",
        "rationale": "Adding unit clarity is a low-risk, purely additive improvement and main made no competing change.",
        "risk_level": "LOW",
    },
    # COLUMN_MOVE_CONFLICT
    {
        "doc_id": "SYN_COLUMN_MOVE_CONFLICT_001", "conflict_type": "COLUMN_MOVE_CONFLICT",
        "title": "Branch reordered columns for a cleaner layout",
        "scenario_text": "The branch moved a column to a new position to improve layout; main's column position is unchanged from base.",
        "recommended_resolution": "ACCEPT_BRANCH",
        "rationale": "Main made no intentional move, so the branch's layout change is the only deliberate edit.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_COLUMN_MOVE_CONFLICT_002", "conflict_type": "COLUMN_MOVE_CONFLICT",
        "title": "Main reordered the whole sheet's columns as part of a template update",
        "scenario_text": "Main's column position change is part of a broader, deliberate column reorganization applied across the sheet; the branch moved only this one column independently.",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "A sheet-wide reorganization on main should take precedence over one branch's isolated move.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_COLUMN_MOVE_CONFLICT_003", "conflict_type": "COLUMN_MOVE_CONFLICT",
        "title": "Column order affects an exported CSV's column sequence",
        "scenario_text": "The sheet's column order is known to drive an exported file's column sequence consumed by a downstream system, and the two moves would produce different sequences.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "When column order has a downstream export dependency, a human should decide the correct sequence.",
        "risk_level": "MEDIUM",
    },
    {
        "doc_id": "SYN_COLUMN_MOVE_CONFLICT_004", "conflict_type": "COLUMN_MOVE_CONFLICT",
        "title": "Both sides moved the column to the same logical grouping",
        "scenario_text": "Main and branch moved the column to adjacent positions within the same logical group of related columns (e.g. both moved it next to other cost columns).",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "The intents are equivalent, so keeping main avoids unnecessary column-order churn.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_COLUMN_MOVE_CONFLICT_005", "conflict_type": "COLUMN_MOVE_CONFLICT",
        "title": "Branch move is on a rarely-used helper column",
        "scenario_text": "The branch moved a helper or scratch column with no downstream export or formula dependency, while main's position is unchanged from base.",
        "recommended_resolution": "ACCEPT_BRANCH",
        "rationale": "Low-stakes moves on unreferenced helper columns are safe to accept from the only side that made an intentional change.",
        "risk_level": "LOW",
    },
    # COLUMN_DELETE_MODIFY_CONFLICT
    {
        "doc_id": "SYN_COLUMN_DELETE_MODIFY_CONFLICT_001", "conflict_type": "COLUMN_DELETE_MODIFY_CONFLICT",
        "title": "Main deleted a column the branch was actively populating",
        "scenario_text": "Main removed a column as part of a schema cleanup, while the branch independently added or edited values in that same column, unaware it had been deleted.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "Deleting a column with concurrent unmerged data risks silently losing work; a human must decide whether to restore or discard it.",
        "risk_level": "CRITICAL",
    },
    {
        "doc_id": "SYN_COLUMN_DELETE_MODIFY_CONFLICT_002", "conflict_type": "COLUMN_DELETE_MODIFY_CONFLICT",
        "title": "Branch's edits were trivial on a column main retired",
        "scenario_text": "Main deleted a deprecated column as part of a planned cleanup; the branch's only edits to that column were minor formatting tweaks with no data significance.",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "A deliberate column retirement should proceed when concurrent edits are low-value, though deletion should still be confirmed given its destructive nature.",
        "risk_level": "HIGH",
    },
    {
        "doc_id": "SYN_COLUMN_DELETE_MODIFY_CONFLICT_003", "conflict_type": "COLUMN_DELETE_MODIFY_CONFLICT",
        "title": "Deleted column is referenced by formulas elsewhere",
        "scenario_text": "The column being deleted by one side is referenced by formulas in other parts of the workbook, per the dependency graph, and deleting it would break those formulas.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "A referenced column's deletion has ripple effects that must be confirmed before merging.",
        "risk_level": "CRITICAL",
    },
    {
        "doc_id": "SYN_COLUMN_DELETE_MODIFY_CONFLICT_004", "conflict_type": "COLUMN_DELETE_MODIFY_CONFLICT",
        "title": "Column is mapped by an active integration connector",
        "scenario_text": "The column being deleted by one side is used as a field-mapping source by an active integration connector, and deleting it would break that sync.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "Deleting an integration-mapped column has effects outside the workbook and must be explicitly confirmed.",
        "risk_level": "CRITICAL",
    },
    {
        "doc_id": "SYN_COLUMN_DELETE_MODIFY_CONFLICT_005", "conflict_type": "COLUMN_DELETE_MODIFY_CONFLICT",
        "title": "Column was an empty helper column with no references",
        "scenario_text": "The column in conflict is empty or near-empty with no formulas referencing it, and one side deleted it while the other made trivial edits.",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "An unreferenced helper column carries low risk either way, but deletions should still default toward the more deliberate side (main).",
        "risk_level": "MEDIUM",
    },
    # ROW_MOVE_CONFLICT
    {
        "doc_id": "SYN_ROW_MOVE_CONFLICT_001", "conflict_type": "ROW_MOVE_CONFLICT",
        "title": "Branch reordered rows alphabetically",
        "scenario_text": "The branch moved a row to keep the sheet alphabetically sorted after adding new entries; main's row position is unchanged from base.",
        "recommended_resolution": "ACCEPT_BRANCH",
        "rationale": "Main made no intentional move, so the branch's sort-order maintenance is the only deliberate edit.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_ROW_MOVE_CONFLICT_002", "conflict_type": "ROW_MOVE_CONFLICT",
        "title": "Main resorted the whole table by a new priority column",
        "scenario_text": "Main's row position change is part of a broader, deliberate re-sort of the entire table by a newly added priority column; the branch moved only this one row independently.",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "A table-wide re-sort on main should take precedence over one branch's isolated move.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_ROW_MOVE_CONFLICT_003", "conflict_type": "ROW_MOVE_CONFLICT",
        "title": "Row order affects a printed report's line sequence",
        "scenario_text": "The table's row order is known to drive a printed or exported report's line-item sequence, and the two moves would produce different orderings.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "When row order has a downstream reporting effect, a human should decide the correct sequence.",
        "risk_level": "MEDIUM",
    },
    {
        "doc_id": "SYN_ROW_MOVE_CONFLICT_004", "conflict_type": "ROW_MOVE_CONFLICT",
        "title": "Both sides moved the row to the same logical position",
        "scenario_text": "Main and branch moved the row to adjacent positions within the same logical grouping (e.g. both moved it near related line items).",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "The intents are equivalent, so keeping main avoids unnecessary row-order churn.",
        "risk_level": "LOW",
    },
    {
        "doc_id": "SYN_ROW_MOVE_CONFLICT_005", "conflict_type": "ROW_MOVE_CONFLICT",
        "title": "Row is a scratch entry with no downstream dependency",
        "scenario_text": "The row in conflict is a scratch or working entry with no formulas referencing its position, and one side moved it while the other left it in place.",
        "recommended_resolution": "ACCEPT_BRANCH",
        "rationale": "Low-stakes moves on unreferenced rows are safe to accept from the only side that made an intentional change.",
        "risk_level": "LOW",
    },
    # ROW_DELETE_MODIFY_CONFLICT
    {
        "doc_id": "SYN_ROW_DELETE_MODIFY_CONFLICT_001", "conflict_type": "ROW_DELETE_MODIFY_CONFLICT",
        "title": "Main deleted a row the branch was actively editing",
        "scenario_text": "Main removed a row as part of data cleanup, while the branch independently made substantive edits to that same row, unaware it had been deleted.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "Deleting a row with concurrent unmerged edits risks silently losing work; a human must decide whether to restore or discard it.",
        "risk_level": "CRITICAL",
    },
    {
        "doc_id": "SYN_ROW_DELETE_MODIFY_CONFLICT_002", "conflict_type": "ROW_DELETE_MODIFY_CONFLICT",
        "title": "Branch's edits were trivial on a row main retired",
        "scenario_text": "Main deleted a duplicate or obsolete row as part of planned cleanup; the branch's only edits to that row were minor formatting tweaks with no data significance.",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "A deliberate row retirement should proceed when concurrent edits are low-value, though deletion should still be confirmed given its destructive nature.",
        "risk_level": "HIGH",
    },
    {
        "doc_id": "SYN_ROW_DELETE_MODIFY_CONFLICT_003", "conflict_type": "ROW_DELETE_MODIFY_CONFLICT",
        "title": "Branch deleted a row main was still updating with new figures",
        "scenario_text": "The branch removed a row it considered obsolete, while main independently continued to update figures in that same row after the branch diverged.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "Main's continued activity suggests the row is still in use; deleting it would discard active work without confirmation.",
        "risk_level": "CRITICAL",
    },
    {
        "doc_id": "SYN_ROW_DELETE_MODIFY_CONFLICT_004", "conflict_type": "ROW_DELETE_MODIFY_CONFLICT",
        "title": "Deleted row is referenced by a downstream SUM or lookup",
        "scenario_text": "The row being deleted by one side is included in a SUM range or is a lookup target referenced elsewhere in the workbook, per the dependency graph.",
        "recommended_resolution": "MANUAL_REVIEW",
        "rationale": "A referenced row's deletion changes downstream totals and lookups; confirm before merging.",
        "risk_level": "CRITICAL",
    },
    {
        "doc_id": "SYN_ROW_DELETE_MODIFY_CONFLICT_005", "conflict_type": "ROW_DELETE_MODIFY_CONFLICT",
        "title": "Row was an empty placeholder with no references",
        "scenario_text": "The row in conflict is empty or near-empty with no formulas referencing it, and one side deleted it while the other made trivial edits.",
        "recommended_resolution": "KEEP_MAIN",
        "rationale": "An unreferenced placeholder row carries low risk either way, but deletions should still default toward the more deliberate side (main).",
        "risk_level": "MEDIUM",
    },
]


def seed_synthetic_corpus(conn: sqlite3.Connection | None = None) -> int:
    """Idempotently seed the synthetic precedent corpus. Safe to call on
    every startup — ``INSERT OR IGNORE`` keyed by the stable ``doc_id``s
    above means re-running never duplicates rows (and, since the FTS mirror
    is only populated by an ``AFTER INSERT`` trigger, an ignored insert
    never touches the FTS table either)."""
    owns_connection = conn is None
    conn = conn or database._get_connection()
    now = database._utcnow()
    inserted = 0
    try:
        for doc in SYNTHETIC_PRECEDENTS:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO MERGE_RESOLUTION_KNOWLEDGE
                    (DOC_ID, CONFLICT_TYPE, TITLE, SCENARIO_TEXT, RECOMMENDED_RESOLUTION,
                     RATIONALE, RISK_LEVEL, SOURCE, REPOSITORY_ID, CREATED_AT)
                VALUES (?,?,?,?,?,?,?,'SYNTHETIC',NULL,?)
                """,
                (
                    doc["doc_id"], doc["conflict_type"], doc["title"], doc["scenario_text"],
                    doc["recommended_resolution"], doc["rationale"], doc["risk_level"], now,
                ),
            )
            inserted += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        if owns_connection:
            conn.commit()
        return inserted
    finally:
        if owns_connection:
            conn.close()


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_\-]+")


def _fts_query_terms(conflict: dict[str, Any]) -> list[str]:
    parts = [
        str(conflict.get("conflict_type") or conflict.get("CONFLICT_TYPE") or ""),
        str((conflict.get("base_state") or conflict.get("BASE_STATE") or {}).get("property", "")),
        str(conflict.get("sheet_id") or conflict.get("SHEET_ID") or ""),
    ]
    terms = [term for term in _TOKEN_PATTERN.findall(" ".join(parts).lower()) if len(term) > 2]
    seen: list[str] = []
    for term in terms:
        if term not in seen:
            seen.append(term)
    return seen[:8]


def retrieve_precedents(conflict: dict[str, Any], limit: int = 5, organization_id: str | None = None) -> list[dict[str, Any]]:
    """Return the most relevant resolution precedents for one conflict.

    Scoped first to the conflict's own ``conflict_type`` — a precedent for a
    different conflict type is never useful regardless of keyword overlap —
    then ranked within that type by SQLite FTS5 BM25 relevance against the
    conflict's property/cell-address terms. Falls back to a plain
    conflict-type match (no ranking) if FTS5 is ever unavailable at runtime
    (defensive only). Also scoped to `organization_id` when given: a real
    resolution (SOURCE='HISTORICAL') is specific to the organization it
    happened in and must never leak into another org's evidence; the
    generic SYNTHETIC seed rows (ORGANIZATION_ID IS NULL) stay shared."""
    conflict_type = str(conflict.get("conflict_type") or conflict.get("CONFLICT_TYPE") or "")
    terms = [term for term in _fts_query_terms(conflict) if term != conflict_type.lower()]
    limit = max(1, min(limit, 20))
    org_filter = " AND (K.ORGANIZATION_ID IS NULL OR K.ORGANIZATION_ID=?)" if organization_id else " AND K.ORGANIZATION_ID IS NULL"
    org_params = (organization_id,) if organization_id else ()
    conn = database._get_connection()
    try:
        if terms:
            match_query = " OR ".join(f'"{term}"' for term in terms)
            try:
                rows = conn.execute(
                    f"""
                    SELECT K.* FROM MERGE_RESOLUTION_KNOWLEDGE_FTS F
                    JOIN MERGE_RESOLUTION_KNOWLEDGE K ON K.DOC_ID = F.doc_id
                    WHERE K.CONFLICT_TYPE = ? AND MERGE_RESOLUTION_KNOWLEDGE_FTS MATCH ?{org_filter}
                    ORDER BY bm25(MERGE_RESOLUTION_KNOWLEDGE_FTS) LIMIT ?
                    """,
                    (conflict_type, match_query, *org_params, limit),
                ).fetchall()
                if rows:
                    return [{key.lower(): row[key] for key in row.keys()} for row in rows]
            except sqlite3.OperationalError:
                pass
        rows = conn.execute(
            f"SELECT * FROM MERGE_RESOLUTION_KNOWLEDGE K WHERE K.CONFLICT_TYPE=?{org_filter} ORDER BY SOURCE DESC, CREATED_AT DESC LIMIT ?",
            (conflict_type, *org_params, limit),
        ).fetchall()
        return [{key.lower(): row[key] for key in row.keys()} for row in rows]
    finally:
        conn.close()


def record_resolution_outcome(
    conflict: dict[str, Any], resolution_type: str, resolved_state: Any, repository_id: str | None = None,
    organization_id: str | None = None,
) -> None:
    """Append one HISTORICAL precedent from a real resolution (human or
    AI-applied). Called from ``MergeService.resolve_conflict`` right after
    the existing resolution write; must never block or fail that call, so
    callers wrap this in a try/except."""
    conflict_type = str(conflict.get("conflict_type") or conflict.get("CONFLICT_TYPE") or "UNKNOWN")
    sheet_id = conflict.get("sheet_id") or conflict.get("SHEET_ID")
    base_state = conflict.get("base_state") or conflict.get("BASE_STATE") or {}
    main_state = conflict.get("main_state") or conflict.get("MAIN_STATE") or {}
    branch_state = conflict.get("branch_state") or conflict.get("BRANCH_STATE") or {}
    scenario_text = (
        f"Real conflict on sheet {sheet_id}: base={base_state.get('value')!r}, "
        f"main={main_state.get('value')!r}, branch={branch_state.get('value')!r}."
    )
    doc_id = stable_id("HST")
    conn = database._get_connection()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO MERGE_RESOLUTION_KNOWLEDGE
                (DOC_ID, CONFLICT_TYPE, TITLE, SCENARIO_TEXT, RECOMMENDED_RESOLUTION,
                 RATIONALE, RISK_LEVEL, SOURCE, REPOSITORY_ID, CREATED_AT, ORGANIZATION_ID)
            VALUES (?,?,?,?,?,?,'LOW','HISTORICAL',?,?,?)
            """,
            (
                doc_id, conflict_type, f"Resolved {conflict_type.lower().replace('_', ' ')}", scenario_text,
                resolution_type, f"Resolved as {resolution_type} with final value {resolved_state!r}.",
                repository_id, database._utcnow(), organization_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
