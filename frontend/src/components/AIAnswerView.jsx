import React, { useMemo } from "react";

/**
 * Renders an AI text answer as a real, scannable "chat" output instead of
 * a raw pre-formatted blob: a bold lede line, the rest as bulleted points
 * with numbers/percentages highlighted, recommended actions as cards, and
 * warnings as chips. Free small models occasionally echo the whole
 * structured GroundedAnswer object as their "answer" text instead of prose
 * — this also detects that case and unpacks the JSON into the same clean
 * layout, so a model hiccup degrades gracefully instead of dumping JSON.
 */
function fromParsed(parsed) {
  return {
    answer: parsed.answer,
    confidence: typeof parsed.confidence === "number" ? parsed.confidence : null,
    insufficientEvidence: Boolean(parsed.insufficient_evidence),
    evidence: Array.isArray(parsed.evidence) ? parsed.evidence : [],
    recommendedActions: Array.isArray(parsed.recommended_actions) ? parsed.recommended_actions : [],
    warnings: Array.isArray(parsed.warnings) ? parsed.warnings.filter(Boolean) : [],
  };
}

// Best-effort recovery for a model that echoed its structured answer as
// text but mangled the JSON (doubled braces, a trailing comma, an unclosed
// bracket) — pulls the "answer" field out with a regex instead of giving up
// and showing the raw blob.
function recoverAnswerField(text) {
  const match = text.match(/"answer"\s*:\s*"((?:[^"\\]|\\.)*)"/);
  if (!match) return null;
  try {
    return JSON.parse(`"${match[1]}"`);
  } catch {
    return match[1].replace(/\\"/g, '"').replace(/\\n/g, " ");
  }
}

function parseAnswer(raw) {
  const empty = { answer: "", confidence: null, insufficientEvidence: false, evidence: [], recommendedActions: [], warnings: [] };
  // Some callers (e.g. a saved AI_MESSAGES row) hand back the already-parsed
  // GroundedAnswer object rather than its JSON-serialized text.
  if (raw && typeof raw === "object" && typeof raw.answer === "string") return fromParsed(raw);
  if (typeof raw !== "string" || !raw.trim()) return empty;
  const trimmed = raw.trim();
  if (trimmed.includes("{")) {
    for (const candidate of [trimmed, trimmed.replace(/^\{+/, "{"), trimmed.slice(trimmed.indexOf("{"))]) {
      try {
        const parsed = JSON.parse(candidate);
        if (parsed && typeof parsed === "object" && typeof parsed.answer === "string") return fromParsed(parsed);
      } catch {
        // try the next candidate
      }
    }
    const recovered = recoverAnswerField(trimmed);
    if (recovered) return { ...empty, answer: recovered };
  }
  return { ...empty, answer: trimmed };
}

// Some models (especially free ones) return "<br>" as a literal line-break
// marker and "**bold**" markdown instead of real HTML/structured output --
// shown as-is this reads as broken text. Converted to real newlines /
// stripped before splitting, so those answers render as clean bullets
// exactly like a model that answered with real sentences.
function normalizeMarkup(text) {
  return text
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/\*\*(.*?)\*\*/g, "$1")
    .replace(/^#{1,6}\s*/gm, "");
}

function splitSentences(text) {
  const normalized = normalizeMarkup(text);
  const lines = normalized.split(/\n+/).map((line) => line.trim()).filter(Boolean);
  if (lines.length > 1) {
    // The model already delimited discrete items (one per line) -- respect
    // that structure directly instead of re-running sentence-punctuation
    // splitting, which would wrongly merge separate "- Label: value" lines
    // into one run-on paragraph.
    return lines.map((line) => line.replace(/^[-*•]\s*/, ""));
  }
  return normalized
    .replace(/\n+/g, " ")
    .split(/(?<=[.!?])\s+(?=[A-Z0-9])/)
    .map((part) => part.trim())
    .filter(Boolean);
}

export function highlightMetrics(sentence, keyIndex) {
  const parts = sentence.split(/(\$?\b\d[\d,]*(?:\.\d+)?%?\b)/g);
  return parts.map((part, index) => (
    /^\$?\d/.test(part)
      ? <mark key={`${keyIndex}-${index}`} className="ai-answer-metric">{part}</mark>
      : <React.Fragment key={`${keyIndex}-${index}`}>{part}</React.Fragment>
  ));
}

const RISK_TONE = { LOW: "low", MEDIUM: "medium", HIGH: "high", CRITICAL: "critical" };

export default function AIAnswerView({ text, confidence: confidenceProp, warnings: warningsProp }) {
  const parsed = useMemo(() => parseAnswer(text), [text]);
  const confidence = confidenceProp ?? parsed.confidence;
  const warnings = warningsProp?.length ? warningsProp : parsed.warnings;
  const sentences = useMemo(() => splitSentences(parsed.answer), [parsed.answer]);
  const [lede, ...points] = sentences;

  if (!parsed.answer) return null;

  return (
    <div className="ai-answer-view">
      {confidence != null ? (
        <div className="ai-answer-confidence" style={{ "--confidence": `${Math.round(confidence * 100)}%` }}>
          <span>{Math.round(confidence * 100)}% grounded</span>
        </div>
      ) : null}
      {parsed.insufficientEvidence ? <p className="ai-answer-flag">Evidence was insufficient for a fully confident answer.</p> : null}
      {lede ? <p className="ai-answer-lede">{highlightMetrics(lede, "lede")}</p> : null}
      {points.length ? (
        <ul className="ai-answer-points">
          {points.map((point, index) => <li key={index}><i /><span>{highlightMetrics(point, index)}</span></li>)}
        </ul>
      ) : null}

      {parsed.recommendedActions.length ? (
        <div className="ai-answer-actions">
          {parsed.recommendedActions.map((action, index) => (
            <article key={index} className={`ai-answer-action-card risk-${RISK_TONE[action.risk_level] || "medium"}`}>
              <header><strong>{action.title}</strong>{action.risk_level ? <b>{action.risk_level}</b> : null}</header>
              <p>{action.rationale}</p>
            </article>
          ))}
        </div>
      ) : null}

      {parsed.evidence.length ? (
        <div className="ai-answer-evidence">
          {parsed.evidence.map((item, index) => <span key={index}>{item.type}<code>{item.id}</code></span>)}
        </div>
      ) : null}

      {warnings?.length ? (
        <div className="ai-answer-warnings">
          {warnings.map((warning, index) => <span key={index}>{warning}</span>)}
        </div>
      ) : null}
    </div>
  );
}
