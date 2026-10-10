import React, { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { addDatasetMember, getDatasetMembers, revokeDatasetMember } from "../services/api";

const STATUS_META = {
  ONLINE: { label: "Online", color: "#3fb950", bg: "rgba(63,185,80,.12)" },
  NEED_HELP: { label: "Need help", color: "#d29922", bg: "rgba(210,153,34,.14)" },
  OFFLINE: { label: "Offline", color: "#6e7781", bg: "rgba(110,119,129,.1)" },
};

function timeAgo(iso) {
  if (!iso) return "never";
  const diffMs = Date.now() - new Date(iso).getTime();
  const minutes = Math.round(diffMs / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

function UserDetailModal({ member, activityEntry, memberDevices, isOwner, busyFingerprintId, onSetDeviceTrust, onRevoke, busy, onClose }) {
  const meta = STATUS_META[activityEntry?.status] || STATUS_META.OFFLINE;
  return createPortal(
    <div className="checkout-backdrop" onClick={onClose}>
      <div className="checkout-dialog user-detail-dialog" onClick={(event) => event.stopPropagation()}>
        <button type="button" className="checkout-close" onClick={onClose}>Close</button>
        <div className="user-detail-header">
          <span className="user-avatar" style={{ boxShadow: `0 0 0 3px ${meta.color}`, background: `linear-gradient(135deg, ${meta.color}, #0d1117)` }}>
            {(member.display_name || member.email || "?").slice(0, 2).toUpperCase()}
          </span>
          <div>
            <strong>{member.display_name || member.email}</strong>
            <small>{member.email}</small>
          </div>
          <span className={`pill role-tree-role-pill role-tone-${member.role}`}>{member.role}</span>
        </div>

        <div className="team-roster-kpis user-detail-kpis">
          <div className="team-roster-kpi" style={{ "--kpi-color": meta.color, "--kpi-bg": meta.bg }}>
            <span>Status</span>
            <strong>{meta.label}</strong>
          </div>
          <div className="team-roster-kpi" style={{ "--kpi-color": "#0969da", "--kpi-bg": "rgba(9,105,218,.1)" }}>
            <span>Active today</span>
            <strong>{activityEntry?.hours_active_today != null ? `${activityEntry.hours_active_today}h` : "0h"}</strong>
          </div>
          <div className="team-roster-kpi" style={{ "--kpi-color": "#8250df", "--kpi-bg": "rgba(130,80,223,.1)" }}>
            <span>Last seen</span>
            <strong>{timeAgo(activityEntry?.last_seen_at)}</strong>
          </div>
          <div className="team-roster-kpi" style={{ "--kpi-color": "#cf222e", "--kpi-bg": "rgba(207,34,46,.1)" }}>
            <span>Devices</span>
            <strong>{memberDevices.length}</strong>
          </div>
        </div>

        <div className="user-detail-devices">
          <h4>Known devices</h4>
          {memberDevices.length ? (
            <ul>
              {memberDevices.map((device) => (
                <li key={device.fingerprint_id}>
                  <div>
                    <code>{device.ip_address || "unknown"}</code>
                    <small title={device.machine_id}>{device.machine_id}</small>
                  </div>
                  <span className={`pill device-trust-${device.trust_status?.toLowerCase()}`}>{device.trust_status}</span>
                  {isOwner ? (
                    device.trust_status !== "BLOCKED" ? (
                      <button className="trace-button" disabled={busyFingerprintId === device.fingerprint_id} onClick={() => onSetDeviceTrust(device.fingerprint_id, "block")}>Block</button>
                    ) : (
                      <button className="trace-button" disabled={busyFingerprintId === device.fingerprint_id} onClick={() => onSetDeviceTrust(device.fingerprint_id, "trust")}>Trust</button>
                    )
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <div className="empty-state compact">No devices recorded for this member yet.</div>
          )}
        </div>

        {isOwner && member.role !== "owner" ? (
          <div className="user-detail-actions">
            <button className="trace-button danger" disabled={busy} onClick={() => onRevoke(member.email)}>Revoke access</button>
          </div>
        ) : null}
      </div>
    </div>,
    document.body,
  );
}

/**
 * Repository-scoped RBAC tree: Repository -> Role -> User leaves. Readable
 * by every member of the repository; onboard/revoke controls only render
 * for the owner (the server already enforces this on add/revoke, this is
 * just not showing controls that would 403 anyway). Refetches whenever the
 * selected repository changes. Tree stays lean for large rosters — every
 * per-person detail (status, active hours, devices, revoke) lives behind a
 * click-through detail modal instead of being crammed into the node itself.
 */
export default function RepositoryAccessTree({ tableId, repositoryName, isOwner, onError, activity, devices, busyFingerprintId, onSetDeviceTrust }) {
  const [members, setMembers] = useState([]);
  const [loading, setLoading] = useState(false);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("editor");
  const [busy, setBusy] = useState(false);
  const [selectedMember, setSelectedMember] = useState(null);

  useEffect(() => {
    if (!tableId) { setMembers([]); return; }
    setLoading(true);
    getDatasetMembers(tableId)
      .then((result) => setMembers(result?.members || []))
      .catch((err) => onError?.(err.message))
      .finally(() => setLoading(false));
  }, [tableId]);

  const refresh = () => getDatasetMembers(tableId)
    .then((result) => setMembers(result?.members || []))
    .catch((err) => onError?.(err.message));

  const roles = useMemo(() => {
    const byRole = new Map();
    members.forEach((member) => {
      if (!byRole.has(member.role)) byRole.set(member.role, []);
      byRole.get(member.role).push(member);
    });
    const order = ["owner", "editor", "viewer"];
    return [...byRole.entries()].sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]));
  }, [members]);

  const onboard = async (e) => {
    e.preventDefault();
    if (!email.trim()) return;
    setBusy(true);
    try {
      await addDatasetMember(tableId, email.trim(), role);
      setEmail("");
      await refresh();
    } catch (err) {
      onError?.(err.message);
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (memberEmail) => {
    setBusy(true);
    try {
      await revokeDatasetMember(tableId, memberEmail);
      setSelectedMember(null);
      await refresh();
    } catch (err) {
      onError?.(err.message);
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <div className="empty-state compact">Loading access tree...</div>;
  if (!members.length) return <div className="empty-state compact">No members onboarded to this repository yet.</div>;

  const activityFor = (member) => (activity || []).find((entry) => entry.user_id === member.user_id || entry.email === member.email) || null;
  const devicesFor = (member) => (devices || []).filter((device) => device.user_id === member.user_id || device.email === member.email);

  return (
    <div className="role-tree-wrap">
      <ul className="role-tree">
        <li>
          <div className="role-tree-node role-tree-root"><strong>{repositoryName || "This repository"}</strong><small>{members.length} member(s)</small></div>
          <ul>
            {roles.map(([roleKey, roleMembers]) => (
              <li key={roleKey}>
                <div className="role-tree-node role-tree-role"><strong>{roleKey}</strong><small>{roleMembers.length} member(s)</small></div>
                <ul>
                  {roleMembers.map((member) => {
                    const entry = activityFor(member);
                    const meta = STATUS_META[entry?.status] || STATUS_META.OFFLINE;
                    return (
                      <li key={member.user_id}>
                        <button type="button" className="role-tree-node role-tree-user" onClick={() => setSelectedMember(member)}>
                          <span className="user-avatar" style={{ boxShadow: `0 0 0 2px ${meta.color}` }}>
                            {(member.email || member.user_id).slice(0, 2).toUpperCase()}
                          </span>
                          <strong>{member.display_name || member.email}</strong>
                          <small>{member.email}</small>
                          <i className="role-tree-status-dot" style={{ background: meta.color }} title={meta.label} />
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </li>
            ))}
          </ul>
        </li>
      </ul>
      {isOwner ? (
        <form className="role-tree-onboard" onSubmit={onboard}>
          <input type="email" placeholder="name@company.com" value={email} onChange={(e) => setEmail(e.target.value)} disabled={busy} />
          <select value={role} onChange={(e) => setRole(e.target.value)} disabled={busy}>
            <option value="editor">Editor</option>
            <option value="viewer">Viewer</option>
          </select>
          <button type="submit" disabled={busy || !email.trim()}>Onboard user</button>
        </form>
      ) : null}

      {selectedMember ? (
        <UserDetailModal
          member={selectedMember}
          activityEntry={activityFor(selectedMember)}
          memberDevices={devicesFor(selectedMember)}
          isOwner={isOwner}
          busyFingerprintId={busyFingerprintId}
          onSetDeviceTrust={onSetDeviceTrust}
          onRevoke={revoke}
          busy={busy}
          onClose={() => setSelectedMember(null)}
        />
      ) : null}
    </div>
  );
}
