import React from "react";

const STATUS_META = {
  ONLINE: { label: "Online", color: "#3fb950", bg: "rgba(63,185,80,.12)" },
  NEED_HELP: { label: "Need help", color: "#d29922", bg: "rgba(210,153,34,.14)" },
  OFFLINE: { label: "Offline", color: "#6e7781", bg: "rgba(110,119,129,.1)" },
};

/**
 * Team Activity roster — one landscape card per member, no scrolling and
 * no calendar grid: just the three things that matter at a glance, each as
 * its own KPI-style number box — live status (Online / Need Help /
 * Offline, from the Excel taskpane's own heartbeat), and real accumulated
 * active hours today (REPOSITORY_DAILY_ACTIVITY, incremented
 * heartbeat-to-heartbeat so idle gaps are never counted).
 */
export default function TeamActivityMatrix({ activity, onSelect }) {
  const users = activity || [];

  if (!users.length) return <div className="empty-state compact">No members with access to this repository yet.</div>;

  return (
    <div className="team-roster">
      {users.map((entry) => {
        const meta = STATUS_META[entry.status] || STATUS_META.OFFLINE;
        return (
          <button type="button" key={entry.user_id} className="team-roster-card" onClick={() => onSelect?.(entry)}>
            <div className="team-roster-person">
              <i className="user-avatar" style={{ boxShadow: `0 0 0 2px ${meta.color}` }}>
                {(entry.display_name || entry.email || "?").slice(0, 2).toUpperCase()}
              </i>
              <div>
                <strong>{entry.display_name || entry.email}</strong>
                <small>{entry.role}</small>
              </div>
            </div>
            <div className="team-roster-kpis">
              <div className="team-roster-kpi" style={{ "--kpi-color": meta.color, "--kpi-bg": meta.bg }}>
                <span>Status</span>
                <strong>{meta.label}</strong>
              </div>
              <div className="team-roster-kpi" style={{ "--kpi-color": "#0969da", "--kpi-bg": "rgba(9,105,218,.1)" }}>
                <span>Active today</span>
                <strong>{entry.hours_active_today != null ? `${entry.hours_active_today}h` : "0h"}</strong>
              </div>
            </div>
          </button>
        );
      })}
    </div>
  );
}
