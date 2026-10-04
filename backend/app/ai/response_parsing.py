"""Shared, defensive extraction of the "overall recommendation" action and a
clean summary string from a gateway ``GroundedAnswer`` result.

Both ``merge_agent._parse_assessment`` and ``commit_review._parse_commit_review``
need this. Normally the gateway's structured-output validation already
guarantees ``result["recommended_actions"]`` is well-formed. But when a free
model's raw response fails that validation even after the gateway's own
repair attempts, ``AIGateway.generate()`` falls back to treating the model's
entire raw text as ``result["answer"]`` (with ``insufficient_evidence=True``
and empty ``recommended_actions``) rather than silently inventing structure
that was never actually returned. Some free models' "failed" raw text is
still itself a JSON object shaped like the schema they were asked for (just
rejected on a technicality — an extra field, a stray control character) —
in that case this recovers the real rationale from it instead of ever
handing a user raw JSON to read as if it were prose.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Matches "fieldName": "value with \" escapes and \n handled" — deliberately
# NOT a full JSON parser, so it still finds the field even when the
# surrounding text is truncated or otherwise not valid JSON (a real,
# observed failure mode for some free models: a response that is a nearly-
# complete JSON object but is missing a final closing brace/bracket, or has
# a stray duplicated brace, which makes json.loads() reject the whole thing
# even though the specific field we want is perfectly intact).
_QUOTED_FIELD_RE = {
    field: re.compile(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"')
    for field in ("rationale", "answer")
}


def _unescape(raw: str) -> str:
    return raw.replace('\\"', '"').replace("\\n", " ").replace("\\t", " ").strip()


def _lower_keys(value: Any) -> Any:
    """Recursively lowercase dict keys in a JSON-decoded value. A nested
    JSON-as-text blob embedded in an ``answer``/``rationale`` string was
    never validated by the gateway's own case-normalized Pydantic parsing
    (that only covers the top-level response), so a free model's Title-Case
    keys (``"Answer"``, ``"Recommended_actions"``) would otherwise defeat
    every ``.get("answer")``/``.get("recommended_actions")`` lookup below."""
    if isinstance(value, dict):
        return {str(key).lower(): _lower_keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_lower_keys(item) for item in value]
    return value


def _regex_recover(text: str) -> str | None:
    """Last-resort recovery when the text isn't valid JSON at all: pull the
    last quoted "rationale" (preferred — it's the more specific, per-action
    explanation) or "answer" field straight out with a regex, regardless of
    whether the rest of the structure around it parses."""
    for field in ("rationale", "answer"):
        matches = _QUOTED_FIELD_RE[field].findall(text)
        if matches:
            return _unescape(matches[-1])
    return None


def extract_recommendation_and_summary(
    result: dict[str, Any], allowed_recommendations: set[str],
) -> tuple[dict[str, Any] | None, str]:
    actions = result.get("recommended_actions") or []
    overall = next(
        (item for item in actions if str(item.get("action_type", "")).upper() in allowed_recommendations), None,
    )
    summary = (overall or {}).get("rationale") or ""
    if summary:
        return overall, summary

    raw_answer = str(result.get("answer") or "").strip()
    lowered_answer = raw_answer.lower()
    looks_like_json = lowered_answer.startswith("{") and ('"answer"' in lowered_answer or '"rationale"' in lowered_answer)
    if not looks_like_json:
        return overall, raw_answer

    if raw_answer.endswith("}"):
        try:
            nested = _lower_keys(json.loads(raw_answer))
        except json.JSONDecodeError:
            nested = None
        if isinstance(nested, dict):
            nested_actions = nested.get("recommended_actions") or []
            nested_overall = next(
                (item for item in nested_actions if str(item.get("action_type", "")).upper() in allowed_recommendations),
                None,
            )
            recovered = (nested_overall or {}).get("rationale") or nested.get("answer") or ""
            if recovered:
                return (nested_overall or overall), recovered

    # Either the JSON wasn't well-formed enough for a strict parse, or it
    # parsed but didn't contain a usable field (e.g. an empty rationale) —
    # fall back to pulling the field out directly with a regex before ever
    # giving up and showing the raw JSON-shaped text to a user.
    recovered = _regex_recover(raw_answer)
    return overall, recovered if recovered else raw_answer


def _looks_like_json_echo(text: str) -> bool:
    # Case-insensitive: a free model's malformed JSON echo sometimes uses
    # inconsistent field-name casing (e.g. "Recommended_actions") that a
    # literal-case substring check would miss entirely.
    lowered = text.lower()
    return lowered.startswith("{") and (
        '"recommended_actions"' in lowered or ('"answer"' in lowered and '"evidence"' in lowered) or '"rationale"' in lowered
    )


def sanitize_free_text(raw: str, *, fallback: str = "") -> str:
    """Defensive net for ANY single AI free-text field (an ``answer``,
    ``rationale``, ``reason``, or ``narrative``) that a free/small model
    occasionally corrupts by echoing its ENTIRE structured response back as
    text instead of writing prose -- the same failure mode
    ``extract_recommendation_and_summary`` guards against for the
    merge/commit-review agents, generalized here for every other agent
    (formula explainer, portfolio briefing, discovery, EUC narrative,
    finding remediation, merge-conflict resolution) so none of them can
    ever hand a user raw JSON to read as if it were prose. Returns
    ``fallback`` (default: empty string) rather than the corrupted text
    when no clean prose can be recovered -- silence is better than
    garbage, and callers already have the real structured content
    elsewhere (e.g. a separately-parsed step list)."""
    text = str(raw or "").strip()
    if not _looks_like_json_echo(text):
        return text
    if text.endswith("}"):
        try:
            nested = _lower_keys(json.loads(text))
        except json.JSONDecodeError:
            nested = None
        if isinstance(nested, dict):
            inner = str(nested.get("answer") or "").strip()
            if inner and not _looks_like_json_echo(inner):
                return inner
            nested_actions = nested.get("recommended_actions") or []
            for action in nested_actions:
                candidate = str(action.get("rationale") or "").strip()
                if candidate and not _looks_like_json_echo(candidate):
                    return candidate
            return fallback
    recovered = _regex_recover(text)
    return recovered if recovered and not _looks_like_json_echo(recovered) else fallback
