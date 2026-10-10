import React, { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { getPersonalActivity } from "../services/api";

const RANGE_OPTIONS = [
  { key: "all", label: "All" },
  { key: "30d", label: "30d" },
  { key: "7d", label: "7d" },
];

function intensityClass(count, max) {
  if (!count) return 0;
  if (max <= 0) return 1;
  const ratio = count / max;
  if (ratio > 0.75) return 4;
  if (ratio > 0.5) return 3;
  if (ratio > 0.25) return 2;
  return 1;
}

function buildWeeks(heatmap) {
  if (!heatmap.length) return [];
  const weeks = [];
  const firstDay = new Date(`${heatmap[0].date}T00:00:00Z`).getUTCDay();
  let week = new Array(firstDay).fill(null);
  heatmap.forEach((day) => {
    week.push(day);
    if (week.length === 7) {
      weeks.push(week);
      week = [];
    }
  });
  if (week.length) {
    while (week.length < 7) week.push(null);
    weeks.push(week);
  }
  return weeks;
}

function monthLabelForWeek(week) {
  const firstReal = week.find((day) => day);
  if (!firstReal) return "";
  return new Date(`${firstReal.date}T00:00:00Z`).toLocaleDateString(undefined, { month: "short" });
}

const TABS = [
  ["overview", "Overview"],
  ["contributions", "Contributions"],
  ["reviews", "Reviews"],
  ["automation", "Automation"],
  ["models", "Models"],
  ["log", "Activity log"],
];

const FAMILY_META = {
  commit: { label: "Commits", tone: "blue" },
  merge: { label: "Merges", tone: "green" },
  device: { label: "Devices & sessions", tone: "amber" },
  ai: { label: "AI & agents", tone: "purple" },
  security: { label: "Security & access", tone: "red" },
  general: { label: "Other", tone: "muted" },
};

/**
 * Personal Activity — a GitHub-style "your footprint in Git Walk" popup.
 * Every number comes from GET /ai-platform/personal-activity: repository
 * access, commits, branches, merge requests opened/reviewed, AI actions
 * confirmed, EUC attestations submitted, autonomous agent runs triggered,
 * and a full audit-event breakdown — all read straight from their own rows
 * server side, nothing invented client-side. Six tabs, one productivity
 * lens each, so a user's whole footprint in the product is visible in one
 * place, not just their commit count.
 */
export default function PersonalActivityModal({ onClose }) {
  const [range, setRange] = useState("30d");
  const [view, setView] = useState("overview");
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    getPersonalActivity(range)
      .then((result) => { if (!cancelled) setData(result); })
      .catch((err) => { if (!cancelled) setError(err.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [range]);

  const weeks = useMemo(() => buildWeeks(data?.heatmap || []), [data]);
  const maxCount = useMemo(() => Math.max(0, ...(data?.heatmap || []).map((day) => day.count)), [data]);

  const overview = data?.overview || {};

  return createPortal(
    <div className="checkout-backdrop" onClick={onClose}>
      <div className="checkout-dialog personal-activity-dialog" onClick={(event) => event.stopPropagation()}>
        <button type="button" className="checkout-close" onClick={onClose}>Close</button>
        <p className="eyebrow">YOUR FOOTPRINT</p>
        <h2>Personal Activity</h2>

        <div className="personal-activity-toolbar">
          <div className="personal-activity-tabs">
            {TABS.map(([key, label]) => (
              <button key={key} type="button" className={view === key ? "active" : ""} onClick={() => setView(key)}>{label}</button>
            ))}
          </div>
          <div className="personal-activity-range">
            {RANGE_OPTIONS.map((option) => (
              <button key={option.key} type="button" className={range === option.key ? "active" : ""} onClick={() => setRange(option.key)}>{option.label}</button>
            ))}
          </div>
        </div>

        {error ? <div className="empty-state compact">{error}</div> : null}
        {loading && !data ? <div className="empty-state compact">Loading your activity...</div> : null}

        {data && view === "overview" ? (
          <>
            <div className="personal-activity-kpis">
              <article><span>Repositories</span><strong>{overview.repositories ?? 0}</strong></article>
              <article><span>Commits</span><strong>{overview.commits ?? 0}</strong></article>
              <article><span>Cells changed</span><strong>{Number(overview.cells_changed || 0).toLocaleString()}</strong></article>
              <article><span>Total tokens</span><strong>{Number(overview.total_tokens || 0).toLocaleString()}</strong></article>
              <article><span>Active days</span><strong>{overview.active_days ?? 0}</strong></article>
              <article><span>Longest streak</span><strong>{overview.longest_streak ?? 0}<small className="personal-activity-unit">{overview.longest_streak === 1 ? " day" : " days"}</small></strong></article>
              <article><span>Busiest day</span><strong className="personal-activity-favorite">{overview.busiest_day || "—"}</strong></article>
              <article><span>Peak hour</span><strong>{overview.peak_hour || "—"}</strong></article>
              <article><span>Merge requests</span><strong>{data.contributions?.merge_requests_opened ?? 0}</strong></article>
              <article><span>Reviews given</span><strong>{data.reviews?.reviews_given_total ?? 0}</strong></article>
            </div>

            <div className="personal-activity-heatmap-wrap">
              {weeks.length ? (
                <div className="personal-activity-heatmap" style={{ gridTemplateColumns: `repeat(${weeks.length}, 1fr)` }}>
                  {weeks.map((week, weekIndex) => (
                    <div key={weekIndex} className="personal-activity-week">
                      {weekIndex === 0 || monthLabelForWeek(week) !== monthLabelForWeek(weeks[weekIndex - 1]) ? (
                        <span className="personal-activity-month">{monthLabelForWeek(week)}</span>
                      ) : null}
                      {week.map((day, dayIndex) => (
                        <div
                          key={dayIndex}
                          className={`personal-activity-cell level-${day ? intensityClass(day.count, maxCount) : "empty"}`}
                          title={day ? `${day.count} event${day.count === 1 ? "" : "s"} on ${day.date}` : ""}
                        />
                      ))}
                    </div>
                  ))}
                </div>
              ) : <div className="empty-state compact">No activity recorded yet.</div>}
            </div>

            {data.comparison?.message ? <p className="personal-activity-comparison muted">{data.comparison.message}</p> : null}
          </>
        ) : null}

        {data && view === "contributions" ? (
          <div className="personal-activity-section">
            <div className="personal-activity-kpis compact">
              <article><span>Branches created</span><strong>{data.contributions.branches_created}</strong></article>
              <article><span>Merge requests opened</span><strong>{data.contributions.merge_requests_opened}</strong></article>
              <article><span>Merge requests merged</span><strong>{data.contributions.merge_requests_merged}</strong></article>
              <article><span>Commits</span><strong>{overview.commits ?? 0}</strong></article>
            </div>
            <p className="personal-activity-subhead">By repository</p>
            <div className="personal-activity-bars">
              {data.contributions.repository_breakdown.length ? data.contributions.repository_breakdown.map((row) => {
                const max = data.contributions.repository_breakdown[0]?.commits || 1;
                return (
                  <article key={row.repository_id} className="personal-activity-bar-row">
                    <div><strong>{row.repository_name}</strong><small>{row.commits} commit{row.commits === 1 ? "" : "s"} · {Number(row.cells_changed).toLocaleString()} cells changed</small></div>
                    <div className="personal-activity-model-bar-track"><div className="personal-activity-model-bar-fill" style={{ width: `${Math.round((row.commits / max) * 100)}%` }} /></div>
                  </article>
                );
              }) : <div className="empty-state compact">No commits in this period yet.</div>}
            </div>
          </div>
        ) : null}

        {data && view === "reviews" ? (
          <div className="personal-activity-section">
            <div className="personal-activity-kpis compact">
              <article><span>Reviews given</span><strong>{data.reviews.reviews_given_total}</strong></article>
              <article><span>Approved</span><strong>{data.reviews.reviews_given.APPROVED ?? 0}</strong></article>
              <article><span>Rejected</span><strong>{data.reviews.reviews_given.REJECTED ?? 0}</strong></article>
              <article><span>AI actions confirmed</span><strong>{data.reviews.ai_actions_confirmed}</strong></article>
              <article><span>Attestations submitted</span><strong>{data.reviews.attestations_submitted}</strong></article>
            </div>
            <p className="personal-activity-note muted">Reviews are decisions you made as a checker on someone else's merge request. AI actions confirmed counts every governed AI recommendation — a conflict resolution, a finding remediation — you personally signed off on before it executed.</p>
          </div>
        ) : null}

        {data && view === "automation" ? (
          <div className="personal-activity-section">
            <div className="personal-activity-kpis compact">
              <article><span>Agent runs triggered</span><strong>{data.automation.agent_runs_total}</strong></article>
            </div>
            <p className="personal-activity-subhead">By agent</p>
            <div className="personal-activity-bars">
              {data.automation.agent_runs.length ? data.automation.agent_runs.map((row) => {
                const max = data.automation.agent_runs[0]?.runs || 1;
                return (
                  <article key={row.agent_name} className="personal-activity-bar-row">
                    <div><strong>{row.agent_name}</strong><small>{row.runs} run{row.runs === 1 ? "" : "s"}</small></div>
                    <div className="personal-activity-model-bar-track"><div className="personal-activity-model-bar-fill purple" style={{ width: `${Math.round((row.runs / max) * 100)}%` }} /></div>
                  </article>
                );
              }) : <div className="empty-state compact">You haven't run an AI agent in this period yet.</div>}
            </div>
          </div>
        ) : null}

        {data && view === "models" ? (
          <div className="personal-activity-models">
            {data.models.length ? data.models.map((model) => (
              <article key={model.model_id}>
                <div><strong>{model.display_name}</strong><small>{model.requests} request{model.requests === 1 ? "" : "s"}</small></div>
                <div className="personal-activity-model-bar-track"><div className="personal-activity-model-bar-fill" style={{ width: `${Math.round((model.tokens / (data.models[0]?.tokens || 1)) * 100)}%` }} /></div>
                <div className="personal-activity-model-stats">
                  <span>{Number(model.tokens).toLocaleString()} tokens</span>
                  <span>{Math.round(model.success_rate * 100)}% success</span>
                  <span>{model.avg_latency_ms ? `${Math.round(model.avg_latency_ms)}ms avg` : "—"}</span>
                </div>
              </article>
            )) : <div className="empty-state compact">No AI usage in this period yet.</div>}
          </div>
        ) : null}

        {data && view === "log" ? (
          <div className="personal-activity-section">
            <p className="personal-activity-subhead">Every action, by category</p>
            <div className="personal-activity-family-grid">
              {Object.entries(FAMILY_META).map(([key, meta]) => (
                <article key={key} className={`personal-activity-family-card tone-${meta.tone}`}>
                  <span>{meta.label}</span>
                  <strong>{data.action_log.by_family[key] ?? 0}</strong>
                </article>
              ))}
            </div>
            <p className="personal-activity-subhead">Most frequent actions</p>
            <div className="personal-activity-bars">
              {data.action_log.top_actions.length ? data.action_log.top_actions.map((row) => {
                const max = data.action_log.top_actions[0]?.count || 1;
                return (
                  <article key={row.event_type} className="personal-activity-bar-row">
                    <div><strong>{row.event_type.replaceAll("_", " ")}</strong><small>{row.count}</small></div>
                    <div className="personal-activity-model-bar-track"><div className="personal-activity-model-bar-fill" style={{ width: `${Math.round((row.count / max) * 100)}%` }} /></div>
                  </article>
                );
              }) : <div className="empty-state compact">Nothing recorded in this period yet.</div>}
            </div>
          </div>
        ) : null}
      </div>
    </div>,
    document.body
  );
}
