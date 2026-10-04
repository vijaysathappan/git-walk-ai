"""RAG knowledge base for the Formula Explainer agent.

Same reasoning as ``ai.merge_knowledge``: no vector/embeddings dependency
exists in this project and the free NVIDIA OpenRouter catalogue has no
embeddings model, so retrieval here is SQLite FTS5 keyword/BM25 search over
a structured corpus (``FORMULA_PATTERN_KNOWLEDGE`` / ``..._FTS``), not
vector search.

The corpus starts from a small, deterministic synthetic dataset covering
the function families that actually show up in real spreadsheets (lookup
chains, conditional aggregation, nested logic, error handling, cross-sheet
references) and grows with real usage: every formula this agent explains
appends a ``HISTORICAL`` precedent keyed by the *set* of functions it uses
(not the literal formula text, which would never repeat), so the next
formula built from the same function combination retrieves a real,
product-specific precedent instead of only a generic synthetic example.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .. import database

_FUNCTION_PATTERN = re.compile(r"\b([A-Z][A-Z0-9.]{1,30})\s*\(")
_CELL_REF_PATTERN = re.compile(
    r"(?:'[^']+'|[A-Za-z_][A-Za-z0-9_]*)?!?\$?[A-Za-z]{1,3}\$?[0-9]{1,7}(?::\$?[A-Za-z]{1,3}\$?[0-9]{1,7})?"
)

SYNTHETIC_PATTERNS: list[dict[str, str]] = [
    {
        "doc_id": "SYN_FORMULA_VLOOKUP_001", "function_signature": "VLOOKUP",
        "title": "VLOOKUP exact-match lookup",
        "pattern_text": "VLOOKUP(lookup_value, table_array, col_index, FALSE)",
        "explanation": "Looks up lookup_value in the first column of table_array and returns the value col_index columns to the right, on the first row where it finds an EXACT match (the final FALSE/0 argument). If no exact match exists it returns #N/A rather than a wrong approximate row.",
    },
    {
        "doc_id": "SYN_FORMULA_VLOOKUP_002", "function_signature": "VLOOKUP",
        "title": "VLOOKUP approximate-match lookup (sorted table)",
        "pattern_text": "VLOOKUP(lookup_value, table_array, col_index, TRUE)",
        "explanation": "With TRUE/omitted as the last argument, VLOOKUP finds the largest value less than or equal to lookup_value in a table that MUST be sorted ascending by its first column -- commonly used for tiered lookups like tax brackets or commission tiers. If the table isn't actually sorted this silently returns the wrong row instead of erroring.",
    },
    {
        "doc_id": "SYN_FORMULA_INDEX_MATCH_001", "function_signature": "INDEX,MATCH",
        "title": "INDEX/MATCH two-way or reverse lookup",
        "pattern_text": "INDEX(return_range, MATCH(lookup_value, lookup_range, 0))",
        "explanation": "MATCH first finds the row position of lookup_value within lookup_range (0 forces exact match), then INDEX returns the value at that position from return_range. Functionally similar to VLOOKUP but not limited to looking rightward, and doesn't break if columns are inserted between the lookup and return ranges.",
    },
    {
        "doc_id": "SYN_FORMULA_XLOOKUP_001", "function_signature": "XLOOKUP",
        "title": "XLOOKUP with explicit not-found fallback",
        "pattern_text": "XLOOKUP(lookup_value, lookup_array, return_array, if_not_found)",
        "explanation": "Modern replacement for VLOOKUP/INDEX-MATCH: looks up lookup_value in lookup_array and returns the matching entry from return_array, returning if_not_found (instead of #N/A) when no match exists -- the 4th argument is what usually distinguishes intentional XLOOKUP use from a copy-pasted VLOOKUP.",
    },
    {
        "doc_id": "SYN_FORMULA_SUMIFS_001", "function_signature": "SUMIFS",
        "title": "Multi-condition conditional sum",
        "pattern_text": "SUMIFS(sum_range, criteria_range1, criteria1, criteria_range2, criteria2, ...)",
        "explanation": "Sums sum_range only for rows where EVERY criteria pair matches (an implicit AND across all conditions, not OR). Commonly used to total a metric filtered by multiple dimensions at once, e.g. amount where region=X AND month=Y.",
    },
    {
        "doc_id": "SYN_FORMULA_COUNTIFS_001", "function_signature": "COUNTIFS",
        "title": "Multi-condition conditional count",
        "pattern_text": "COUNTIFS(criteria_range1, criteria1, criteria_range2, criteria2, ...)",
        "explanation": "Counts rows where every criteria pair matches simultaneously (AND, not OR) -- the counting equivalent of SUMIFS, typically used to tally records meeting several filters at once.",
    },
    {
        "doc_id": "SYN_FORMULA_NESTED_IF_001", "function_signature": "IF",
        "title": "Nested IF acting as a tiered classifier",
        "pattern_text": "IF(cond1, result1, IF(cond2, result2, IF(cond3, result3, default)))",
        "explanation": "A chain of nested IFs evaluates conditions in order and returns the result for the FIRST one that's true, falling through to the innermost default if none match -- functionally a manual if/elif/else chain, commonly used for tiering, grading, or bucketing a value.",
    },
    {
        "doc_id": "SYN_FORMULA_IFS_001", "function_signature": "IFS",
        "title": "IFS as a flatter alternative to nested IF",
        "pattern_text": "IFS(cond1, result1, cond2, result2, TRUE, default)",
        "explanation": "Evaluates each condition in order and returns the result for the first TRUE one -- the same behavior as a nested IF chain but without the pyramid of parentheses; a trailing TRUE, default pair is the conventional way to express a catch-all default.",
    },
    {
        "doc_id": "SYN_FORMULA_IFERROR_001", "function_signature": "IFERROR",
        "title": "IFERROR masking a potential error",
        "pattern_text": "IFERROR(risky_expression, fallback_value)",
        "explanation": "Evaluates risky_expression and returns fallback_value instead of showing an error (#DIV/0!, #N/A, #REF!, etc.) if it fails. Useful for graceful degradation, but it also silently hides the ROOT CAUSE of a broken reference or a lookup miss -- worth flagging if the fallback is a plain number rather than something like \"Not found\".",
    },
    {
        "doc_id": "SYN_FORMULA_CROSS_SHEET_001", "function_signature": "CROSS_SHEET_REFERENCE",
        "title": "Cross-sheet reference",
        "pattern_text": "'Sheet Name'!A1 or SheetName!A1",
        "explanation": "Pulls a value directly from another worksheet in the same workbook. This creates a dependency: if the referenced sheet's structure (inserted/deleted rows or columns) changes without updating this formula, the reference can silently point at the wrong cell.",
    },
    {
        "doc_id": "SYN_FORMULA_SUMPRODUCT_001", "function_signature": "SUMPRODUCT",
        "title": "SUMPRODUCT used as an array-multiply-and-sum",
        "pattern_text": "SUMPRODUCT((range1=criteria)*(range2=criteria2)*value_range)",
        "explanation": "Multiplies arrays element-wise and sums the result; when combined with comparison expressions like (range=criteria), each TRUE/FALSE becomes 1/0, making this a common (if less readable) alternative to SUMIFS for conditions that SUMIFS itself can't express, such as OR logic or non-equality comparisons.",
    },
    {
        "doc_id": "SYN_FORMULA_CONCAT_001", "function_signature": "CONCATENATE,TEXTJOIN,&",
        "title": "Text concatenation",
        "pattern_text": "CONCATENATE(a,b) / TEXTJOIN(delim, ignore_empty, range) / a & b",
        "explanation": "Joins text values together. TEXTJOIN additionally supports a delimiter and an option to skip empty cells across an entire range, which CONCATENATE and the & operator can't do without spelling out every cell.",
    },
    {
        "doc_id": "SYN_FORMULA_OFFSET_001", "function_signature": "OFFSET",
        "title": "OFFSET building a dynamic range",
        "pattern_text": "OFFSET(reference, rows, cols, [height], [width])",
        "explanation": "Returns a reference shifted rows/cols away from the starting reference, optionally resized -- powerful for dynamic ranges (e.g. a rolling N-period window) but volatile: it recalculates on every workbook change, which can slow down large workbooks, and refactoring tools can't trace it as a normal cell reference.",
    },
    {
        "doc_id": "SYN_FORMULA_INDIRECT_001", "function_signature": "INDIRECT",
        "title": "INDIRECT building a reference from text",
        "pattern_text": "INDIRECT(text_that_looks_like_a_reference)",
        "explanation": "Turns a text string into an actual cell reference at calculation time. Flexible for building references dynamically (e.g. from a sheet-name cell), but the resulting dependency is invisible to Excel's own dependency tracing and to this product's static dependency graph, since the real target isn't known until the formula runs.",
    },
    {
        "doc_id": "SYN_FORMULA_CIRCULAR_001", "function_signature": "CIRCULAR_PATTERN",
        "title": "Formula that references its own cell, directly or through a cycle",
        "pattern_text": "A1 = ... A1 ... (directly or via a chain back to itself)",
        "explanation": "A circular reference means the formula's value depends, directly or through a chain of other formulas, on itself. Excel either refuses to calculate it (showing a warning) or, with iterative calculation enabled, converges to an answer through repeated passes -- which is rarely what the author intended and is usually a sign of an accidental self-reference.",
    },
]


def seed_synthetic_formula_corpus(conn=None) -> int:
    owns_connection = conn is None
    conn = conn or database._get_connection()
    now = database._utcnow()
    inserted = 0
    try:
        for doc in SYNTHETIC_PATTERNS:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO FORMULA_PATTERN_KNOWLEDGE
                    (DOC_ID, FUNCTION_SIGNATURE, TITLE, PATTERN_TEXT, EXPLANATION, SOURCE, CREATED_AT)
                VALUES (?,?,?,?,?,'SYNTHETIC',?)
                """,
                (doc["doc_id"], doc["function_signature"], doc["title"], doc["pattern_text"], doc["explanation"], now),
            )
            inserted += 1 if cursor.rowcount and cursor.rowcount > 0 else 0
        if owns_connection:
            conn.commit()
    finally:
        if owns_connection:
            conn.close()
    return inserted


def extract_functions(formula: str) -> list[str]:
    """Deterministic parse of every function NAME used in a formula (not
    its arguments) -- the same information a human would read off the
    formula bar, used both to ground the AI's explanation in real
    structure and to key precedent retrieval/growth."""
    if not formula:
        return []
    text = formula[1:] if formula.startswith("=") else formula
    return sorted(dict.fromkeys(match.upper() for match in _FUNCTION_PATTERN.findall(text)))


def extract_cell_references(formula: str) -> list[str]:
    """Deterministic parse of every cell/range reference token in a
    formula, used to drive dependency/history lookups -- never fabricated,
    since a wrong reference here would misdirect the whole investigation."""
    if not formula:
        return []
    text = formula[1:] if formula.startswith("=") else formula
    candidates = _CELL_REF_PATTERN.findall(text)
    # A bare function name like SUM without a following digit can match the
    # pattern's optional-sheet-prefix branch; only keep tokens that actually
    # end in a run of digits (a real row number).
    return sorted(dict.fromkeys(token for token in candidates if re.search(r"\d$", token)))[:25]


def retrieve_formula_patterns(functions: list[str], limit: int = 5, organization_id: str | None = None) -> list[dict[str, Any]]:
    """FTS5 BM25 retrieval scoped to the function names actually present in
    the formula being explained -- a precedent for an unrelated function
    family is never useful regardless of incidental keyword overlap. Also
    scoped to `organization_id` when given, so a real admin's recorded
    explanation (SOURCE='HISTORICAL') never leaks across organizations;
    the generic SYNTHETIC seed rows (ORGANIZATION_ID IS NULL) stay shared."""
    if not functions:
        return []
    limit = max(1, min(limit, 20))
    match_query = " OR ".join(f'"{name}"' for name in functions)
    org_filter = " AND (K.ORGANIZATION_ID IS NULL OR K.ORGANIZATION_ID=?)" if organization_id else " AND K.ORGANIZATION_ID IS NULL"
    org_params = (organization_id,) if organization_id else ()
    conn = database._get_connection()
    try:
        try:
            rows = conn.execute(
                f"""
                SELECT K.* FROM FORMULA_PATTERN_KNOWLEDGE_FTS F
                JOIN FORMULA_PATTERN_KNOWLEDGE K ON K.DOC_ID = F.doc_id
                WHERE FORMULA_PATTERN_KNOWLEDGE_FTS MATCH ?{org_filter}
                ORDER BY bm25(FORMULA_PATTERN_KNOWLEDGE_FTS) LIMIT ?
                """,
                (match_query, *org_params, limit),
            ).fetchall()
        except Exception:
            # Defensive fallback only -- FTS5 is confirmed available in
            # this environment, but a plain LIKE scan degrades gracefully
            # rather than breaking the agent if it were ever unavailable.
            like_clauses = " OR ".join("FUNCTION_SIGNATURE LIKE ?" for _ in functions)
            rows = conn.execute(
                f"SELECT * FROM FORMULA_PATTERN_KNOWLEDGE K WHERE ({like_clauses}){org_filter} LIMIT ?",
                [f"%{name}%" for name in functions] + list(org_params) + [limit],
            ).fetchall()
        return [{key.lower(): row[key] for key in row.keys()} for row in rows]
    finally:
        conn.close()


def record_formula_precedent(functions: list[str], summary: str, organization_id: str | None = None) -> None:
    """Append one self-grown HISTORICAL precedent keyed by the SET of
    functions used (not the literal formula, which would almost never
    repeat) -- so the next formula built from the same function
    combination in this product retrieves a real, previously-explained
    example instead of only a generic synthetic one. Never blocks or fails
    the explanation itself; callers wrap this in try/except."""
    if not functions:
        return
    signature = ",".join(functions)
    doc_id = f"HST_FORMULA_{hashlib.sha256(signature.encode()).hexdigest()[:20].upper()}"
    conn = database._get_connection()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO FORMULA_PATTERN_KNOWLEDGE
                (DOC_ID, FUNCTION_SIGNATURE, TITLE, PATTERN_TEXT, EXPLANATION, SOURCE, CREATED_AT, ORGANIZATION_ID)
            VALUES (?,?,?,?,?,'HISTORICAL',?,?)
            """,
            (doc_id, signature, f"Previously explained: {signature}", signature, summary[:2000], database._utcnow(), organization_id),
        )
        conn.commit()
    finally:
        conn.close()
