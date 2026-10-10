import React, { useState } from "react";
import { createPortal } from "react-dom";
import {
  Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart, Pie, PieChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { highlightMetrics } from "./AIAnswerView";

const PIE_COLORS = ["#3fb950", "#58a6ff", "#f0883e", "#8250df", "#f85149", "#56d364", "#79c0ff"];

function isChartable(rows) {
  return Array.isArray(rows) && rows.length > 0 && "group_key" in rows[0] && "value" in rows[0];
}

function ChartView({ chartType, rows }) {
  if (chartType === "PIE") {
    return (
      <ResponsiveContainer width="100%" height={320}>
        <PieChart>
          <Pie data={rows} dataKey="value" nameKey="group_key" cx="50%" cy="50%" outerRadius={110} label={(entry) => entry.group_key}>
            {rows.map((_, index) => <Cell key={index} fill={PIE_COLORS[index % PIE_COLORS.length]} />)}
          </Pie>
          <Tooltip />
          <Legend />
        </PieChart>
      </ResponsiveContainer>
    );
  }
  if (chartType === "LINE") {
    return (
      <ResponsiveContainer width="100%" height={320}>
        <LineChart data={rows}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e8edf1" />
          <XAxis dataKey="group_key" tick={{ fontSize: 11 }} />
          <YAxis tick={{ fontSize: 11 }} />
          <Tooltip />
          <Line type="monotone" dataKey="value" stroke="#58a6ff" strokeWidth={2.5} dot={{ r: 3 }} />
        </LineChart>
      </ResponsiveContainer>
    );
  }
  return (
    <ResponsiveContainer width="100%" height={320}>
      <BarChart data={rows}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e8edf1" />
        <XAxis dataKey="group_key" tick={{ fontSize: 11 }} />
        <YAxis tick={{ fontSize: 11 }} />
        <Tooltip />
        <Bar dataKey="value" fill="#3fb950" radius={[6, 6, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function TableView({ rows }) {
  if (!rows.length) return <div className="empty-state compact">No rows were returned.</div>;
  const columns = Object.keys(rows[0]);
  return (
    <div className="insight-table-wrap">
      <table className="insight-table">
        <thead><tr>{columns.map((column) => <th key={column}>{column === "group_key" ? "Group" : column === "value" ? "Value" : column}</th>)}</tr></thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>{columns.map((column) => <td key={column}>{String(row[column] ?? "")}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function InsightsReportModal({ result, onClose }) {
  const chartable = isChartable(result.result_data);
  const effectiveChartType = chartable ? result.chart_type : "TABLE_ONLY";
  const [view, setView] = useState(effectiveChartType === "TABLE_ONLY" ? "table" : "chart");

  return createPortal(
    <div className="checkout-backdrop" role="presentation" onClick={onClose}>
      <section className="checkout-dialog working-copies-dialog-full insights-report-dialog" role="dialog" aria-modal="true" onClick={(event) => event.stopPropagation()}>
        <button type="button" className="checkout-close" onClick={onClose}>Close</button>

        <div className="panel-header">
          <div>
            <p className="eyebrow">DATA INSIGHTS / REPORT</p>
            <h2>{result.headline}</h2>
            <p className="muted">
              "{result.question}" &middot; computed live from{" "}
              {result.result_data?.length ?? 0} row{result.result_data?.length === 1 ? "" : "s"} as of commit{" "}
              {String(result.source_commit_id || "").slice(0, 12)}
              {result.cache_hit ? " (cached — same question, unchanged data)" : ""}
            </p>
          </div>
          <div className="insight-confidence-badge" style={{ "--confidence": `${Math.round((result.confidence || 0) * 100)}%` }}>
            <span>{Math.round((result.confidence || 0) * 100)}% grounded</span>
          </div>
        </div>

        {chartable ? (
          <div className="insight-view-toggle">
            <button type="button" className={view === "chart" ? "active" : ""} onClick={() => setView("chart")}>Chart</button>
            <button type="button" className={view === "table" ? "active" : ""} onClick={() => setView("table")}>Table</button>
          </div>
        ) : null}

        <div className="insight-report-body">
          {view === "chart" && chartable ? <ChartView chartType={effectiveChartType} rows={result.result_data} /> : <TableView rows={result.result_data || []} />}
        </div>

        {result.forecast ? (
          <div className="insight-forecast-callout">
            <div className={`insight-forecast-arrow direction-${result.forecast.direction}`}>
              {result.forecast.direction === "up" ? "↑" : result.forecast.direction === "down" ? "↓" : "→"}
            </div>
            <div>
              <strong>Trend estimate: {result.forecast.direction === "up" ? "trending up" : result.forecast.direction === "down" ? "trending down" : "flat"}</strong>
              <p className="muted">{result.forecast.disclaimer}</p>
            </div>
          </div>
        ) : null}

        {result.bullets?.length ? (
          <div className="insight-explainability">
            <p className="eyebrow">EXPLAINABILITY</p>
            <ul className="ai-answer-points">
              {result.bullets.map((bullet, index) => (
                <li key={index}><i /><span>{highlightMetrics(bullet, index)}</span></li>
              ))}
            </ul>
            {result.key_terms?.length ? (
              <div className="insight-key-terms">
                {result.key_terms.map((term, index) => <span key={index} className="insight-key-term">{term}</span>)}
              </div>
            ) : null}
          </div>
        ) : null}

        {result.recommended_actions?.length ? (
          <div className="ai-answer-actions">
            {result.recommended_actions.map((action, index) => (
              <article key={index} className="ai-answer-action-card risk-medium">
                <header><strong>{action.title}</strong></header>
                <p>{action.rationale}</p>
              </article>
            ))}
          </div>
        ) : null}

        {result.warnings?.length ? (
          <div className="ai-answer-warnings">
            {result.warnings.map((warning, index) => <span key={index}>{warning}</span>)}
          </div>
        ) : null}
      </section>
    </div>,
    document.body,
  );
}
