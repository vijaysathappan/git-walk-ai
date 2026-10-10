import React, { useEffect, useRef, useState } from "react";
import { getAgentRunProgress } from "../services/api";

const STEP_META = {
  PLAN: { label: "Planning", icon: "◈" },
  INVESTIGATE: { label: "Investigating", icon: "◉" },
  RETRIEVE: { label: "Retrieving precedents", icon: "◉" },
  GATHER: { label: "Gathering evidence", icon: "◉" },
  DETECT: { label: "Detecting patterns", icon: "◉" },
  RANK: { label: "Ranking candidates", icon: "◉" },
  SCORE: { label: "Scoring", icon: "■" },
  OBSERVE: { label: "Observing", icon: "◉" },
  PROPOSE: { label: "Proposing", icon: "▲" },
  EXPLAIN: { label: "Explaining", icon: "◆" },
  PREPARE_ACTION: { label: "Preparing action", icon: "■" },
  QUERY_PLANNED: { label: "Planning the query", icon: "◈" },
  EXECUTED: { label: "Running the query", icon: "■" },
  NARRATED: { label: "Explaining the result", icon: "◆" },
};

function stepMeta(stepType) {
  return STEP_META[stepType] || { label: stepType?.replace(/_/g, " ") || "Working", icon: "●" };
}

function elapsedLabel(startIso) {
  if (!startIso) return "";
  const seconds = Math.max(0, Math.round((Date.now() - new Date(startIso).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

/**
 * The live "what's actually happening in the backend right now" view every
 * agent in this product shares -- polls the generic
 * GET /ai-platform/agent-runs/{id} endpoint (real AI_AGENT_STEPS rows,
 * written as the background agent body genuinely executes them) instead of
 * a plain spinner, so an AI call in flight reads as a transparent system
 * doing legible work rather than an opaque black box. Every line rendered
 * here is real backend state -- nothing is simulated; the only invented
 * element is the pulsing "still working" placeholder shown for the gap
 * before the NEXT real step lands, and even that reports genuine elapsed
 * time rather than fabricated progress.
 *
 * `onDone`/`onError` are read through a ref rather than being effect
 * dependencies -- callers pass fresh closures on every render, and putting
 * them in the polling effect's dependency array would restart polling (and
 * flash the whole timeline back to a blank "running" state) on every
 * parent re-render, even long after the run had already finished. The
 * effect below depends on `runId` alone, so it starts once per run and
 * stops for good the moment that run reaches a terminal status.
 */
export default function AgentRunTimeline({ runId, onDone, onError, compact = false }) {
  const [run, setRun] = useState(null);
  const [steps, setSteps] = useState([]);
  const [collapsed, setCollapsed] = useState(false);
  const doneRef = useRef(false);
  const callbacksRef = useRef({ onDone, onError });
  callbacksRef.current = { onDone, onError };

  useEffect(() => {
    doneRef.current = false;
    setRun(null);
    setSteps([]);
    setCollapsed(false);
    if (!runId) return undefined;

    let cancelled = false;
    const poll = async () => {
      try {
        const result = await getAgentRunProgress(runId);
        if (cancelled) return;
        setRun(result.run);
        setSteps(result.steps || []);
        if (result.run.status !== "RUNNING" && !doneRef.current) {
          doneRef.current = true;
          setCollapsed(true); // save space once it's done; one click re-expands
          callbacksRef.current.onDone?.(result);
        }
      } catch (err) {
        if (!cancelled) callbacksRef.current.onError?.(err.message);
      }
    };

    poll();
    const interval = setInterval(() => {
      if (doneRef.current) { clearInterval(interval); return; }
      poll();
    }, 900);
    return () => { cancelled = true; clearInterval(interval); };
  }, [runId]);

  if (!runId) return null;
  const status = run?.status || "RUNNING";
  const isRunning = status === "RUNNING";
  const isTerminal = status !== "RUNNING";

  if (isTerminal && collapsed) {
    return (
      <button
        type="button"
        className={`agent-run-timeline agent-run-summary status-${status.toLowerCase()}`}
        onClick={() => setCollapsed(false)}
      >
        <span className={`agent-run-summary-dot ${status === "COMPLETED" ? "done" : "failed"}`}>
          {status === "COMPLETED" ? "✓" : "!"}
        </span>
        <span className="agent-run-summary-text">
          {status === "COMPLETED" ? `Done — ${steps.length} step(s)` : "This run failed"}
        </span>
        <span className="agent-run-summary-toggle">View steps</span>
      </button>
    );
  }

  return (
    <div className={`agent-run-timeline ${compact ? "compact" : ""} status-${status.toLowerCase()}`}>
      {isTerminal ? (
        <button type="button" className="agent-run-collapse-toggle" onClick={() => setCollapsed(true)}>Collapse</button>
      ) : null}
      <div className="agent-run-timeline-rail">
        {steps.map((step, index) => {
          const meta = stepMeta(step.step_type);
          return (
            <div key={step.step_id} className={`agent-run-step status-${step.status.toLowerCase()}`}>
              <div className="agent-run-step-dot"><span>{meta.icon}</span></div>
              {index < steps.length - 1 || isRunning ? <i className="agent-run-step-connector" /> : null}
              <div className="agent-run-step-body">
                <strong>{meta.label}</strong>
                <p>{step.description}</p>
              </div>
            </div>
          );
        })}
        {isRunning ? (
          <div className="agent-run-step status-pending pulse">
            <div className="agent-run-step-dot"><span className="agent-run-pulse-dot" /></div>
            <div className="agent-run-step-body">
              <strong>Working on the next step...</strong>
              <p className="muted">{elapsedLabel(run?.created_at)} elapsed{steps.length ? ` since "${stepMeta(steps[steps.length - 1].step_type).label.toLowerCase()}" finished` : ""}</p>
            </div>
          </div>
        ) : null}
        {status === "COMPLETED" ? (
          <div className="agent-run-step status-completed final">
            <div className="agent-run-step-dot done"><span>&#10003;</span></div>
            <div className="agent-run-step-body"><strong>Done</strong></div>
          </div>
        ) : null}
        {status === "FAILED" ? (
          <div className="agent-run-step status-failed final">
            <div className="agent-run-step-dot failed"><span>&#33;</span></div>
            <div className="agent-run-step-body"><strong>This run failed</strong><p className="muted">{run?.error_code || "The AI provider was unavailable. Nothing else was affected."}</p></div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
