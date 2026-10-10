import React from "react";

// Matches "fieldName": "value with \" escapes handled" without requiring
// the surrounding text to be valid JSON at all — some free models return a
// response that's a nearly-complete JSON object but missing a final closing
// brace/bracket (or with a stray duplicated one), which fails JSON.parse
// outright even though the field we actually want is perfectly intact.
function regexRecoverField(text) {
  for (const field of ["rationale", "answer"]) {
    const pattern = new RegExp(`"${field}"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"`, "g");
    const matches = [...text.matchAll(pattern)];
    if (matches.length) {
      return matches[matches.length - 1][1].replace(/\\"/g, '"').replace(/\\n/g, " ").replace(/\\t/g, " ").trim();
    }
  }
  return null;
}

// Safety net for AI summaries stored before the backend started recovering
// a clean rationale itself (see backend/app/ai/response_parsing.py): if a
// summary already saved to the database is still a raw JSON blob (from a
// free model's response that failed strict structured-output validation),
// recover the real prose from it here too, so a stale historical record
// never has to be re-run just to stop showing raw JSON to a user.
export function cleanAssessmentSummary(text) {
  const raw = String(text || "").trim();
  const looksLikeJson = raw.startsWith("{") && (raw.includes('"answer"') || raw.includes('"rationale"'));
  if (!looksLikeJson) return raw;

  if (raw.endsWith("}")) {
    try {
      const parsed = JSON.parse(raw);
      const actions = parsed.recommended_actions || [];
      const overall = actions.find((item) => item?.rationale) || actions[0];
      const recovered = overall?.rationale || parsed.answer;
      if (recovered) return recovered;
    } catch {
      // Not valid JSON after all (truncated / malformed) — fall through to
      // the regex-based recovery below instead of giving up.
    }
  }

  return regexRecoverField(raw) || raw;
}

export function summaryToBullets(text) {
  const clean = cleanAssessmentSummary(text);
  return clean
    .split(/(?<=[.!?])\s+(?=[A-Z(])/)
    .map((sentence) => sentence.trim())
    .filter(Boolean);
}

const AI_HIGHLIGHT_PATTERN = new RegExp(
  [
    "\\b(LOW|MEDIUM|HIGH|CRITICAL|VERY_LOW|VERY_HIGH)\\b",
    "\\b(APPROVE|HOLD FOR REVIEW|REJECT|LOOKS GOOD|REVIEW RECOMMENDED|KEEP MAIN|ACCEPT BRANCH|MANUAL REVIEW|ACKNOWLEDGED|DISMISSED|REMEDIATION PLANNED|ACCEPTED RISK)\\b",
    "\\d+(\\.\\d+)?(/100|%)",
    // Cell/range references (B7, Sheet1!C3, AA12:AB20) and common Excel
    // function names -- the specific technical vocabulary a Formula
    // Explainer or Risk Radar answer is actually built from, so a reader
    // can spot exactly what's being talked about at a glance.
    "\\b[A-Za-z_][A-Za-z0-9_ ]*![A-Z]{1,3}\\$?\\d{1,7}(:\\$?[A-Z]{1,3}\\$?\\d{1,7})?\\b",
    "\\b\\$?[A-Z]{1,3}\\$?\\d{1,7}(:\\$?[A-Z]{1,3}\\$?\\d{1,7})?\\b",
    "\\b(VLOOKUP|HLOOKUP|XLOOKUP|INDEX|MATCH|SUMIFS?|COUNTIFS?|IFERROR|IFS|SUMPRODUCT|OFFSET|INDIRECT)\\b",
  ].join("|"),
  "gi",
);

export function highlightAssessmentText(text) {
  if (!text) return null;
  const parts = [];
  let lastIndex = 0;
  let match;
  let key = 0;
  AI_HIGHLIGHT_PATTERN.lastIndex = 0;
  while ((match = AI_HIGHLIGHT_PATTERN.exec(text)) !== null) {
    if (match.index > lastIndex) parts.push(text.slice(lastIndex, match.index));
    parts.push(<mark key={key++} className="ai-highlight-term">{match[0]}</mark>);
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) parts.push(text.slice(lastIndex));
  return parts;
}
