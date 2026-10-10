import React, { useEffect, useRef, useState } from "react";
import { getAgentRunProgress, getCommitAIReview, startCommitAIReview } from "../services/api";
import { highlightAssessmentText, summaryToBullets } from "../utils/aiText";

const cardStyle = {
  padding: 14, border: "1px solid #3c3c3c", borderRadius: 6,
  background: "#252526", boxShadow: "0 8px 24px rgba(0,0,0,.16)",
};
const labelStyle = {
  color: "#8399a5", fontSize: 10, fontWeight: 800, letterSpacing: ".12em",
  textTransform: "uppercase", marginBottom: 7,
};
const inputStyle = {
  boxSizing: "border-box", width: "100%", padding: "10px 11px", color: "#edf8f7",
  background: "#1e1e1e", border: "1px solid #4c4c4c", borderRadius: 4, outline: "none",
};
const buttonStyle = {
  width: "100%", padding: "11px 13px", border: "1px solid #2ea043", borderRadius: 4, color: "#fff",
  background: "#238636", fontSize: 12, fontWeight: 800, cursor: "pointer",
};

const RISK_COLORS = {
  LOW: "#3fb950", MEDIUM: "#d4a72c", HIGH: "#e0722f", CRITICAL: "#f85149",
  VERY_LOW: "#3fb950", VERY_HIGH: "#f85149",
};

function riskColor(level) {
  return RISK_COLORS[String(level || "MEDIUM").toUpperCase()] || "#d4a72c";
}

function RiskBadge({ review }) {
  if (!review) return null;
  const color = riskColor(review.risk_level);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
      <span style={{ padding: "3px 9px", borderRadius: 999, background: "#2d2340", color: "#d2a8ff", fontSize: 9, fontWeight: 900, letterSpacing: ".02em" }}>
        {review.recommendation.replaceAll("_", " ")}
      </span>
      <b style={{ fontSize: 10, color: "#8399a5" }}>{Math.round((review.confidence || 0) * 100)}% confidence</b>
      <i style={{ fontStyle: "normal", padding: "3px 8px", borderRadius: 5, fontSize: 9, fontWeight: 800, background: "#1e1e1e", color }}>
        {review.risk_level} risk &middot; {Math.round(review.risk_score ?? 0)}/100
      </i>
    </div>
  );
}

function RiskBreakdown({ components }) {
  if (!components?.length) return null;
  return (
    <div style={{ marginTop: 12, paddingTop: 10, borderTop: "1px dashed #3c3c3c" }}>
      <div style={labelStyle}>Risk breakdown</div>
      {components.map((component) => (
        <div key={component.dimension} style={{ display: "grid", gridTemplateColumns: "110px 1fr 28px", alignItems: "center", gap: 8, marginBottom: 5 }}>
          <span style={{ fontSize: 8, fontWeight: 800, color: "#8399a5", textTransform: "uppercase" }}>{component.dimension.replaceAll("_", " ")}</span>
          <div style={{ height: 7, borderRadius: 999, background: "#1e1e1e", overflow: "hidden" }}>
            <div style={{ height: "100%", borderRadius: 999, width: `${Math.min(100, component.score)}%`, background: riskColor(component.classification) }} />
          </div>
          <span style={{ fontSize: 9, fontWeight: 800, color: "#c6d6db", textAlign: "right" }}>{Math.round(component.score)}</span>
        </div>
      ))}
    </div>
  );
}

function FieldExplanations({ fields }) {
  if (!fields?.length) return null;
  return (
    <div style={{ marginTop: 12, paddingTop: 10, borderTop: "1px dashed #3c3c3c" }}>
      <div style={labelStyle}>Why these fields changed</div>
      {fields.map((field, index) => (
        <div key={index} style={{ padding: "9px 10px", marginBottom: 7, border: "1px solid #3c3c3c", borderRadius: 7, background: "#1e1e1e" }}>
          <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
            <strong style={{ fontSize: 10, color: "#edf8f7" }}>{field.column_name}</strong>
            <span style={{ fontSize: 8, color: "#70858f" }}>row {field.row_id}</span>
          </div>
          <p style={{ margin: "5px 0 0", fontSize: 9, color: "#8399a5" }}>
            Previously: {field.previous_value == null || field.previous_value === "" ? "(empty)" : String(field.previous_value)} &rarr; now empty
          </p>
          <p style={{ margin: "5px 0 0", fontSize: 10, color: "#c6d6db" }}>{field.explanation}</p>
          <small style={{ display: "block", marginTop: 5, color: "#a684e8", fontSize: 8 }}>
            Last touched by {field.changed_by || "unknown"}{field.changed_at ? ` on ${new Date(field.changed_at).toLocaleString()}` : ""} &middot; {field.prior_edit_count} prior edit(s)
          </small>
        </div>
      ))}
    </div>
  );
}

/**
 * Unified commit review: semantic diff + commit message + commit button,
 * then (once a commit_id exists) the deterministic risk breakdown and AI
 * explanation update in place — no navigation, no separate screen. AI
 * review runs strictly AFTER the commit already succeeded; it never gates
 * or blocks the commit itself.
 */
export default function CommitReviewPanel({
  summary, commitMessage, setCommitMessage, busy, workspaceClosed, onCommit, lastCommitId,
  recoveredDraft, onReviewRecoveredDraft, onDiscardDraft,
  pendingCommit, pendingCommitBusy, onRetryPendingCommit, onDiscardPendingCommit,
}) {
  const [review, setReview] = useState(null);
  const [reviewBusy, setReviewBusy] = useState(false);
  const [reviewError, setReviewError] = useState("");
  const [reviewSteps, setReviewSteps] = useState([]);
  const reviewedCommitRef = useRef(null);

  useEffect(() => {
    if (!lastCommitId || reviewedCommitRef.current === lastCommitId) return;
    reviewedCommitRef.current = lastCommitId;
    setReview(null); setReviewError(""); setReviewSteps([]); setReviewBusy(true);
    let cancelled = false;

    const poll = async (runId) => {
      try {
        const progress = await getAgentRunProgress(runId);
        if (cancelled) return;
        setReviewSteps(progress.steps || []);
        if (progress.run.status === "RUNNING") { setTimeout(() => poll(runId), 900); return; }
        setReviewBusy(false);
        if (progress.run.status === "COMPLETED") {
          const { review: latest } = await getCommitAIReview(lastCommitId);
          if (!cancelled) setReview(latest);
        } else {
          setReviewError("AI review failed — the AI provider may be unavailable right now.");
        }
      } catch (err) {
        if (!cancelled) { setReviewBusy(false); setReviewError(err.message || "AI review is unavailable right now."); }
      }
    };

    startCommitAIReview(lastCommitId)
      .then((started) => poll(started.agent_run_id))
      .catch((err) => { if (!cancelled) { setReviewBusy(false); setReviewError(err.message || "AI review is unavailable right now."); } });

    return () => { cancelled = true; };
  }, [lastCommitId]);

  return (
    <div style={cardStyle}>
      <div style={labelStyle}>Commit review</div>

      {pendingCommit ? (
        <div style={{ marginBottom: 12, padding: 11, border: "1px solid #b9862d", borderRadius: 6, background: "#2a2114" }}>
          <div style={{ color: "#f4ba62", fontSize: 10, fontWeight: 800, marginBottom: 5 }}>PENDING COMMIT (network failure)</div>
          <p style={{ margin: 0, fontSize: 10, color: "#e9d7b5", lineHeight: 1.5 }}>
            A commit saved at {new Date(pendingCommit.saved_at).toLocaleTimeString()} failed to reach the server —
            "{pendingCommit.commit_message}" ({pendingCommit.semantic_changes?.length || 0} change(s)). It's saved locally.
          </p>
          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <button style={{ ...buttonStyle, width: "auto", flex: 1, padding: "8px 10px", fontSize: 10 }} disabled={pendingCommitBusy} onClick={onRetryPendingCommit}>
              {pendingCommitBusy ? "Retrying..." : "Retry now"}
            </button>
            <button style={{ ...buttonStyle, width: "auto", flex: 1, padding: "8px 10px", fontSize: 10, background: "#3a2a1c", border: "1px solid #b9862d" }} disabled={pendingCommitBusy} onClick={onDiscardPendingCommit}>
              Discard
            </button>
          </div>
        </div>
      ) : null}

      {recoveredDraft ? (
        <div style={{ marginBottom: 12, padding: 11, border: "1px solid #3c3c3c", borderRadius: 6, background: "#1e1e1e" }}>
          <div style={{ color: "#66c4ff", fontSize: 10, fontWeight: 800, marginBottom: 5 }}>RECOVERED UNSAVED DRAFT</div>
          <p style={{ margin: 0, fontSize: 10, color: "#a8bbc3", lineHeight: 1.5 }}>
            {recoveredDraft.semantic_changes?.length || 0} change(s) staged earlier
            {recoveredDraft.commit_message ? ` ("${recoveredDraft.commit_message}")` : ""} were never committed.
            Click Review to check whether they're still in this workbook.
          </p>
          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <button style={{ ...buttonStyle, width: "auto", flex: 1, padding: "8px 10px", fontSize: 10, background: "#172632", border: "1px solid #3c3c3c" }} onClick={onReviewRecoveredDraft}>
              Review now
            </button>
            <button style={{ ...buttonStyle, width: "auto", flex: 1, padding: "8px 10px", fontSize: 10, background: "#2a191c", border: "1px solid #6e3a3a" }} onClick={onDiscardDraft}>
              Discard
            </button>
          </div>
        </div>
      ) : null}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(2,1fr)", gap: 8, textAlign: "center", marginBottom: 12 }}>
        <div><div style={labelStyle}>Cells / formulas</div><strong style={{ color: "#75ead0" }}>{summary.counts.cells}/{summary.counts.formulas}</strong></div>
        <div><div style={labelStyle}>Row operations</div><strong style={{ color: "#66c4ff" }}>{summary.counts.rows}</strong></div>
        <div><div style={labelStyle}>Column operations</div><strong style={{ color: "#f4ba62" }}>{summary.counts.columns}</strong></div>
        <div><div style={labelStyle}>Sheet operations</div><strong style={{ color: "#d2a8ff" }}>{summary.counts.sheets || 0}</strong></div>
      </div>

      {summary.preview?.length ? (
        <div style={{ marginBottom: 12 }}>
          <div style={labelStyle}>Staged diff</div>
          <div style={{ maxHeight: 140, overflowY: "auto", border: "1px solid #3c3c3c", borderRadius: 4, background: "#1e1e1e", padding: "0 8px" }}>
            {summary.preview.slice(0, 30).map((line, index) => (
              <div key={index} style={{ color: "#a8bbc3", borderBottom: "1px solid #2a2a2a", padding: "5px 0", font: "10px Consolas,monospace" }}>{line}</div>
            ))}
          </div>
        </div>
      ) : null}

      <div style={{ marginBottom: 10 }}>
        <div style={labelStyle}>Commit message</div>
        <input
          style={inputStyle} value={commitMessage} onChange={(e) => setCommitMessage(e.target.value)}
          placeholder="Describe this data change" maxLength={300}
        />
      </div>
      <button
        style={{ ...buttonStyle, opacity: !commitMessage.trim() || busy || workspaceClosed ? 0.55 : 1 }}
        disabled={!commitMessage.trim() || busy || workspaceClosed}
        onClick={onCommit}
      >
        {busy ? "Working..." : `Commit ${summary.changeCount === "?" ? "changes" : `${summary.changeCount} change(s)`}`}
      </button>

      {lastCommitId ? (
        <div style={{ marginTop: 14, paddingTop: 12, borderTop: "1px solid #3c3c3c" }}>
          <div style={labelStyle}>AI commit review</div>
          {reviewBusy ? (
            <div>
              <style>{"@keyframes gitwalk-spin { to { transform: rotate(360deg); } }"}</style>
              {reviewSteps.map((step) => (
                <div key={step.step_id} style={{ display: "flex", gap: 8, alignItems: "flex-start", marginBottom: 6 }}>
                  <span style={{ width: 16, height: 16, flexShrink: 0, borderRadius: "50%", background: "#3fb950", color: "#fff", fontSize: 9, display: "grid", placeItems: "center" }}>&#10003;</span>
                  <div><strong style={{ display: "block", fontSize: 9.5, color: "#edf8f7" }}>{step.step_type.replace(/_/g, " ")}</strong><span style={{ fontSize: 9, color: "#8399a5" }}>{step.description}</span></div>
                </div>
              ))}
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <span style={{ width: 16, height: 16, flexShrink: 0, borderRadius: "50%", border: "2px solid #8250df", borderTopColor: "transparent", animation: "gitwalk-spin 0.8s linear infinite" }} />
                <span style={{ fontSize: 9, color: "#c297ff" }}>{reviewSteps.length ? "Working on the next step..." : "Starting the review agent..."}</span>
              </div>
            </div>
          ) : reviewError ? (
            <div style={{ color: "#ffaaa3", fontSize: 10 }}>{reviewError}</div>
          ) : review ? (
            <>
              <RiskBadge review={review} />
              <ul style={{ margin: "9px 0 0", paddingLeft: 16, fontSize: 11, color: "#c6d6db", lineHeight: 1.6 }}>
                {summaryToBullets(review.summary).map((sentence, index) => (
                  <li key={index} style={{ marginBottom: 3 }}>{highlightAssessmentText(sentence)}</li>
                ))}
              </ul>
              <RiskBreakdown components={review.risk_breakdown} />
              <FieldExplanations fields={review.field_investigations} />
              {(review.warnings || []).length ? (
                <ul style={{ margin: "8px 0 0", paddingLeft: 16, color: "#e9d7b5", fontSize: 9 }}>
                  {review.warnings.map((warning, index) => <li key={index}>{warning}</li>)}
                </ul>
              ) : null}
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
