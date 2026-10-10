import React, { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { getReviewerRequest, respondToReviewerRequest } from "../services/api";

/**
 * Full-screen response view opened from a "please review this" notification
 * or email. Shows the merge request's real detail (change summary, conflict
 * state, validation) and lets the requested reviewer Approve/Reject with a
 * comment -- submitting calls the exact same `merge_service.review()` path
 * the normal in-app review button uses, so every existing permission and
 * validation rule still applies untouched.
 */
export default function ReviewerResponseModal({ requestId, onClose, onError, onResponded }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [decision, setDecision] = useState(null);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getReviewerRequest(requestId)
      .then((result) => { if (!cancelled) setData(result); })
      .catch((err) => onError?.(err.message))
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestId]);

  const submit = async () => {
    if (!decision) return;
    setBusy(true);
    try {
      await respondToReviewerRequest(requestId, decision, comment.trim());
      onResponded?.();
    } catch (err) {
      onError?.(err.message);
    } finally {
      setBusy(false);
    }
  };

  const request = data?.merge_request;
  const alreadyResponded = data?.status && data.status !== "PENDING";

  return createPortal(
    <div className="checkout-backdrop" role="presentation" onClick={busy ? undefined : onClose}>
      <section className="checkout-dialog working-copies-dialog-full reviewer-response-dialog" role="dialog" aria-modal="true" onClick={(event) => event.stopPropagation()}>
        <button type="button" className="checkout-close" onClick={onClose} disabled={busy}>Close</button>

        {loading ? <div className="loading-state">Loading merge request...</div> : null}

        {!loading && request ? (
          <>
            <div className="panel-header">
              <div>
                <p className="eyebrow">REVIEW REQUESTED</p>
                <h2>{request.title}</h2>
                <p className="muted">{request.description || "No description provided."}</p>
              </div>
              <span className={`pill ${request.status === "MERGED" ? "ready" : ""}`}>{request.status.replaceAll("_", " ")}</span>
            </div>

            <div className="reviewer-response-context">
              <article><span>Repository</span><strong>{request.repository_name || "-"}</strong></article>
              <article><span>Workbook</span><strong>{request.workbook_filename || "-"}</strong></article>
              <article><span>Requested by</span><strong>{request.created_by_email || "-"}</strong></article>
              <article><span>Branch</span><strong>{request.source_branch_name} &rarr; {request.target_branch_name}</strong></article>
            </div>

            <div className="reviewer-response-stats">
              <article><span>Changes</span><strong>{request.change_summary?.total ?? 0}</strong></article>
              <article><span>Cell edits</span><strong>{request.change_summary?.cells ?? 0}</strong></article>
              <article><span>Formula edits</span><strong>{request.change_summary?.formulas ?? 0}</strong></article>
              <article><span>Sheets touched</span><strong>{request.change_summary?.sheets ?? 0}</strong></article>
              <article><span>Open conflicts</span><strong>{(request.conflicts || []).filter((item) => item.status === "OPEN").length}</strong></article>
              <article><span>Validation</span><strong>{request.validation_status || "PENDING"}</strong></article>
            </div>

            {request.changes?.length ? (
              <div className="semantic-diff-table reviewer-response-diff">
                {request.changes.slice(0, 30).map((change, index) => (
                  <article key={index}>
                    <b>{change.operation_type.replaceAll("_", " ")}</b>
                    <code>{change.sheet_name ? `Sheet "${change.sheet_name}"` : ""}{change.column_name ? ` / column "${change.column_name}"` : ""}{change.row_label ? ` / ${change.row_label}` : ""}</code>
                    <span>
                      <del>{change.old_value == null || change.old_value === "" ? <i className="diff-empty">empty</i> : String(change.old_value)}</del>
                      <i>&rarr;</i>
                      <ins>{change.new_value == null || change.new_value === "" ? <i className="diff-empty">empty</i> : String(change.new_value)}</ins>
                    </span>
                  </article>
                ))}
                {request.changes.length > 30 ? <p className="muted">...and {request.changes.length - 30} more change(s).</p> : null}
              </div>
            ) : <div className="empty-state compact">No changes to show.</div>}

            {alreadyResponded ? (
              <div className="reviewer-response-already">
                <strong>You already responded to this request.</strong>
                <p>Decision: {data.decision} {data.comment_text ? `— "${data.comment_text}"` : ""}</p>
              </div>
            ) : (
              <div className="reviewer-response-form">
                <p className="eyebrow">YOUR DECISION</p>
                <div className="reviewer-response-decision-buttons">
                  <button type="button" className={`secondary-button ${decision === "APPROVED" ? "active" : ""}`} onClick={() => setDecision("APPROVED")} disabled={busy}>Approve</button>
                  <button type="button" className={`secondary-button ${decision === "REJECTED" ? "active" : ""}`} onClick={() => setDecision("REJECTED")} disabled={busy}>Reject</button>
                </div>
                <textarea
                  value={comment} onChange={(event) => setComment(event.target.value)}
                  placeholder="Add a comment (recommended)..." maxLength={1000} disabled={busy}
                />
                <button type="button" className="primary-button" onClick={submit} disabled={busy || !decision}>
                  {busy ? "Submitting..." : "Submit decision"}
                </button>
              </div>
            )}
          </>
        ) : null}
      </section>
    </div>,
    document.body,
  );
}
