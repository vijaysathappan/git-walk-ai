import React, { useState } from "react";
import { getDataInsightResult, startDataInsight } from "../services/api";
import AgentRunTimeline from "./AgentRunTimeline";
import InsightsReportModal from "./InsightsReportModal";

const INSIGHT_PROMPTS = [
  ["Top category", "Which category or region has the highest total, and how far ahead is it of the rest?"],
  ["Trend over time", "How has the total changed over time, and where is it heading next?"],
  ["Distribution", "How is the data distributed across categories? Show it as a chart."],
  ["Outliers", "Are there any categories or rows that stand out as unusually high or low?"],
  ["Merge health", "What percentage of our merge requests succeed, and how many run into conflicts?"],
  ["Commit activity", "How much of our recent activity is formula changes versus plain cell changes?"],
];

export default function DataInsightsPanel({ tableId, branchId, onError }) {
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [runId, setRunId] = useState(null);
  const [result, setResult] = useState(null);
  const [modalOpen, setModalOpen] = useState(false);

  const ask = async (text) => {
    const finalQuestion = (text ?? question).trim();
    if (finalQuestion.length < 3 || !tableId || !branchId) return;
    setBusy(true);
    setResult(null);
    try {
      const started = await startDataInsight(tableId, branchId, finalQuestion);
      if (started.status === "COMPLETED") {
        setResult(started);
        setModalOpen(true);
        setBusy(false);
      } else {
        setRunId(started.agent_run_id);
      }
    } catch (err) {
      onError?.(err.message);
      setBusy(false);
    }
  };

  const onRunDone = async (progress) => {
    setBusy(false);
    setRunId(null);
    if (progress.run.status !== "COMPLETED") return;
    try {
      const insightResultId = progress.result?.insight_result_id;
      if (!insightResultId) return;
      const fetched = await getDataInsightResult(tableId, insightResultId);
      setResult(fetched);
      setModalOpen(true);
    } catch (err) {
      onError?.(err.message);
    }
  };

  const disabled = busy || !tableId || !branchId;

  return (
    <div className="data-insights-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">DATA INSIGHTS</p>
          <h3>Ask this repository's data anything</h3>
          <p className="muted">
            Understands your question, writes and runs a validated query against the live data, and answers with a
            chart, a table, and a plain-English explanation — grounded in real computed numbers, never invented.
          </p>
        </div>
      </div>

      {!tableId || !branchId ? (
        <div className="empty-state compact">Select a repository and branch to ask about its data.</div>
      ) : null}

      <div className="insight-prompt-chips">
        {INSIGHT_PROMPTS.map(([label, text]) => (
          <button key={label} type="button" className="insight-prompt-chip" onClick={() => { setQuestion(text); ask(text); }} disabled={disabled}>
            {label}
          </button>
        ))}
      </div>

      <div className="insight-ask-bar">
        <textarea
          value={question} onChange={(event) => setQuestion(event.target.value)}
          placeholder="e.g. Which region has the highest total sales amount?"
          disabled={disabled}
        />
        <button type="button" className="primary-button" onClick={() => ask()} disabled={disabled || question.trim().length < 3}>
          {busy ? "Generating insights..." : "Generate insights"}
        </button>
      </div>

      {runId ? (
        <div className="insight-run-progress">
          <AgentRunTimeline runId={runId} onDone={onRunDone} onError={onError} />
        </div>
      ) : null}

      {modalOpen && result ? (
        <InsightsReportModal result={result} onClose={() => setModalOpen(false)} />
      ) : null}
    </div>
  );
}
