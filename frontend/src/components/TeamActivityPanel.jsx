import React, { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { highlightAssessmentText, summaryToBullets } from "../utils/aiText";
import {
  blockRepositoryDevice,
  decideRbacAnomaly,
  getRbacAnomalies,
  getRepositoryActivity,
  getRepositoryActivityMatrix,
  getRepositoryAnomalies,
  getRepositoryDevices,
  getRepositoryExpertise,
  getRepositoryMemberProfile,
  getRepositoryOnboardingRamp,
  getRepositoryTeamHealthDigest,
  startDiscovery,
  startRbacAnomalyScan,
  trustRepositoryDevice,
} from "../services/api";
import AgentRunTimeline from "./AgentRunTimeline";
import TeamActivityMatrix from "./TeamActivityMatrix";
import RepositoryAccessTree from "./RepositoryAccessTree";

function pulseIntensity(seconds, max) {
  if (!seconds) return 0;
  if (max <= 0) return 1;
  const ratio = seconds / max;
  if (ratio > 0.75) return 4;
  if (ratio > 0.5) return 3;
  if (ratio > 0.25) return 2;
  return 1;
}

function MemberProfileModal({ tableId, member, onError, onClose }) {
  const [profile, setProfile] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getRepositoryMemberProfile(tableId, member.user_id)
      .then((result) => { if (!cancelled) setProfile(result); })
      .catch((err) => onError?.(err.message))
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [tableId, member.user_id, onError]);

  return createPortal(
    <div className="checkout-backdrop" onClick={onClose}>
      <div className="checkout-dialog user-detail-dialog" onClick={(event) => event.stopPropagation()}>
        <button type="button" className="checkout-close" onClick={onClose}>Close</button>
        <div className="user-detail-header">
          <span className="user-avatar">{(member.display_name || member.email || "?").slice(0, 2).toUpperCase()}</span>
          <div><strong>{member.display_name || member.email}</strong><small>{member.email}</small></div>
          {profile ? <span className={`pill role-tree-role-pill role-tone-${profile.role}`}>{profile.role}</span> : null}
        </div>
        {loading ? <div className="empty-state compact">Loading profile...</div> : null}
        {profile ? (
          <>
            <div className="team-roster-kpis user-detail-kpis">
              <div className="team-roster-kpi" style={{ "--kpi-color": "#3fb950", "--kpi-bg": "rgba(63,185,80,.1)" }}><span>Status</span><strong>{profile.status === "ONLINE" ? "Online" : profile.status === "NEED_HELP" ? "Need help" : "Offline"}</strong></div>
              <div className="team-roster-kpi" style={{ "--kpi-color": "#0969da", "--kpi-bg": "rgba(9,105,218,.1)" }}><span>Active today</span><strong>{profile.hours_active_today != null ? `${profile.hours_active_today}h` : "0h"}</strong></div>
              <div className="team-roster-kpi" style={{ "--kpi-color": "#8250df", "--kpi-bg": "rgba(130,80,223,.1)" }}><span>Commits</span><strong>{profile.commits}</strong></div>
              <div className="team-roster-kpi" style={{ "--kpi-color": "#cf222e", "--kpi-bg": "rgba(207,34,46,.1)" }}><span>Open review load</span><strong>{profile.open_workload_days}d</strong></div>
            </div>

            {profile.onboarding ? (
              <div className="user-detail-section">
                <h4>Onboarding</h4>
                {profile.onboarding.stalled ? (
                  <p className="muted">Onboarded {profile.onboarding.days_since_joined}d ago with no first commit yet.</p>
                ) : profile.onboarding.ramp_days != null ? (
                  <p className="muted">Ramped up in {profile.onboarding.ramp_days} day(s) after joining.</p>
                ) : (
                  <p className="muted">Still ramping — joined {profile.onboarding.days_since_joined ?? 0}d ago.</p>
                )}
              </div>
            ) : null}

            <div className="user-detail-section">
              <h4>Top sheets by expertise</h4>
              {profile.top_sheets.length ? (
                <ul className="user-detail-sheet-list">
                  {profile.top_sheets.map((sheet) => (<li key={sheet.sheet_id}><strong>{sheet.sheet_name}</strong><small>{sheet.touches} touch(es)</small></li>))}
                </ul>
              ) : <p className="muted">No commit history on this repository yet.</p>}
            </div>

            {profile.devices ? (
              <div className="user-detail-devices">
                <h4>Known devices</h4>
                {profile.devices.length ? (
                  <ul>
                    {profile.devices.map((device) => (
                      <li key={device.fingerprint_id}>
                        <div><code>{device.ip_address || "unknown"}</code><small title={device.machine_id}>{device.machine_id}</small></div>
                        <span className={`pill device-trust-${device.trust_status?.toLowerCase()}`}>{device.trust_status}</span>
                      </li>
                    ))}
                  </ul>
                ) : <div className="empty-state compact">No devices recorded for this member yet.</div>}
              </div>
            ) : null}
          </>
        ) : null}
      </div>
    </div>,
    document.body,
  );
}

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

/**
 * Live view of who has this repository open, their RBAC role, device/IP,
 * and activity recency — visible to every member with access. Backed by
 * GET /repositories/{table_id}/activity (any-access, since this session's
 * 14F change) and /devices (still owner-only — a 403/404 there just means
 * the viewer isn't the owner, and the devices panel/trust controls are
 * hidden rather than gating the whole view).
 */
export default function TeamActivityPanel({ tableId, repositoryName, isOwner, onError }) {
  const [activity, setActivity] = useState([]);
  const [devices, setDevices] = useState([]);
  const [loading, setLoading] = useState(false);
  const [busyFingerprintId, setBusyFingerprintId] = useState(null);
  const [notAllowed, setNotAllowed] = useState(false);
  const [devicesAllowed, setDevicesAllowed] = useState(true);
  const [selectedMember, setSelectedMember] = useState(null);
  const [performersExpanded, setPerformersExpanded] = useState(false);
  const [devicesExpanded, setDevicesExpanded] = useState(false);
  const [pulseExpanded, setPulseExpanded] = useState(false);
  const [pulse, setPulse] = useState(null);
  const [pulseLoading, setPulseLoading] = useState(false);
  const [expertiseExpanded, setExpertiseExpanded] = useState(false);
  const [expertise, setExpertise] = useState(null);
  const [expertiseLoading, setExpertiseLoading] = useState(false);
  const [queueExpanded, setQueueExpanded] = useState(false);
  const [onboardingExpanded, setOnboardingExpanded] = useState(false);
  const [onboarding, setOnboarding] = useState(null);
  const [onboardingLoading, setOnboardingLoading] = useState(false);
  const [anomalyExpanded, setAnomalyExpanded] = useState(false);
  const [anomalies, setAnomalies] = useState(null);
  const [anomaliesLoading, setAnomaliesLoading] = useState(false);
  const [digest, setDigest] = useState(null);
  const [digestLoading, setDigestLoading] = useState(false);
  const [discoveryExpanded, setDiscoveryExpanded] = useState(false);
  const [discoveryTopic, setDiscoveryTopic] = useState("");
  const [discoveryRunId, setDiscoveryRunId] = useState(null);
  const [discoveryResult, setDiscoveryResult] = useState(null);
  const [discoveryBusy, setDiscoveryBusy] = useState(false);
  const [rbacExpanded, setRbacExpanded] = useState(false);
  const [rbacFindings, setRbacFindings] = useState(null);
  const [rbacRunId, setRbacRunId] = useState(null);
  const [rbacScanning, setRbacScanning] = useState(false);
  const [rbacBusyFindingId, setRbacBusyFindingId] = useState(null);

  const load = useCallback(async () => {
    if (!tableId) return;
    setLoading(true); setNotAllowed(false);
    try {
      const activityResult = await getRepositoryActivity(tableId);
      setActivity(activityResult.activity || []);
    } catch (err) {
      if (err.status === 403 || err.status === 404) setNotAllowed(true);
      else onError?.(err.message);
    } finally {
      setLoading(false);
    }
    try {
      const devicesResult = await getRepositoryDevices(tableId);
      setDevices(devicesResult.devices || []);
      setDevicesAllowed(true);
    } catch (err) {
      if (err.status === 403 || err.status === 404) setDevicesAllowed(false);
      else onError?.(err.message);
    }
  }, [tableId, onError]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    setPulse(null); setExpertise(null); setOnboarding(null); setAnomalies(null); setDigest(null);
    setDiscoveryResult(null); setDiscoveryRunId(null); setRbacFindings(null); setRbacRunId(null);
  }, [tableId]);

  useEffect(() => {
    if (!pulseExpanded || pulse || !tableId) return;
    setPulseLoading(true);
    getRepositoryActivityMatrix(tableId, 14)
      .then(setPulse)
      .catch((err) => onError?.(err.message))
      .finally(() => setPulseLoading(false));
  }, [pulseExpanded, pulse, tableId, onError]);

  useEffect(() => {
    if ((!expertiseExpanded && !queueExpanded) || expertise || !tableId) return;
    setExpertiseLoading(true);
    getRepositoryExpertise(tableId)
      .then(setExpertise)
      .catch((err) => onError?.(err.message))
      .finally(() => setExpertiseLoading(false));
  }, [expertiseExpanded, queueExpanded, expertise, tableId, onError]);

  useEffect(() => {
    if (!onboardingExpanded || onboarding || !tableId) return;
    setOnboardingLoading(true);
    getRepositoryOnboardingRamp(tableId)
      .then(setOnboarding)
      .catch((err) => onError?.(err.message))
      .finally(() => setOnboardingLoading(false));
  }, [onboardingExpanded, onboarding, tableId, onError]);

  useEffect(() => {
    if (!anomalyExpanded || anomalies || !tableId) return;
    setAnomaliesLoading(true);
    getRepositoryAnomalies(tableId)
      .then(setAnomalies)
      .catch((err) => onError?.(err.message))
      .finally(() => setAnomaliesLoading(false));
  }, [anomalyExpanded, anomalies, tableId, onError]);

  const generateDigest = async () => {
    setDigestLoading(true);
    try {
      const result = await getRepositoryTeamHealthDigest(tableId, true);
      setDigest(result);
    } catch (err) {
      onError?.(err.message);
    } finally {
      setDigestLoading(false);
    }
  };

  const askDiscovery = async (event) => {
    event.preventDefault();
    if (!discoveryTopic.trim()) return;
    setDiscoveryBusy(true); setDiscoveryResult(null); setDiscoveryRunId(null);
    try {
      const started = await startDiscovery(tableId, discoveryTopic.trim());
      if (started.status === "COMPLETED" || started.agent_run_id == null) setDiscoveryResult(started);
      else setDiscoveryRunId(started.agent_run_id);
    } catch (err) {
      onError?.(err.message);
      setDiscoveryBusy(false);
    }
  };

  const onDiscoveryRunDone = useCallback((progress) => {
    setDiscoveryBusy(false);
    if (progress.result) setDiscoveryResult(progress.result);
  }, []);

  useEffect(() => {
    if (!rbacExpanded || rbacFindings || !tableId || !isOwner) return;
    getRbacAnomalies(tableId)
      .then((result) => setRbacFindings(result.findings || []))
      .catch((err) => onError?.(err.message));
  }, [rbacExpanded, rbacFindings, tableId, isOwner, onError]);

  const runRbacScan = async () => {
    setRbacScanning(true); setRbacRunId(null);
    try {
      const started = await startRbacAnomalyScan(tableId);
      setRbacRunId(started.agent_run_id);
    } catch (err) {
      onError?.(err.message);
      setRbacScanning(false);
    }
  };

  const onRbacRunDone = useCallback(() => {
    setRbacScanning(false);
    getRbacAnomalies(tableId).then((result) => setRbacFindings(result.findings || [])).catch((err) => onError?.(err.message));
  }, [tableId, onError]);

  const decideFinding = async (findingId, decision) => {
    const reason = window.prompt(decision === "DISMISSED" ? "Why dismiss this finding?" : "Optional note:") || "";
    if (decision === "DISMISSED" && !reason.trim()) return;
    setRbacBusyFindingId(findingId);
    try {
      await decideRbacAnomaly(tableId, findingId, decision, reason.trim() || "Reviewed by owner.");
      setRbacFindings((prev) => prev.filter((item) => item.finding_id !== findingId));
    } catch (err) {
      onError?.(err.message);
    } finally {
      setRbacBusyFindingId(null);
    }
  };

  useEffect(() => {
    if (!tableId) return undefined;
    const interval = setInterval(load, 20000);
    return () => clearInterval(interval);
  }, [tableId, load]);

  const setDeviceTrust = async (fingerprintId, action) => {
    setBusyFingerprintId(fingerprintId);
    try {
      if (action === "block") await blockRepositoryDevice(tableId, fingerprintId);
      else await trustRepositoryDevice(tableId, fingerprintId);
      await load();
    } catch (err) {
      onError?.(err.message);
    } finally {
      setBusyFingerprintId(null);
    }
  };

  if (notAllowed) {
    return (
      <section className="panel">
        <div className="panel-header"><div><p className="eyebrow">TEAM / LIVE ACTIVITY</p><h2>Team activity</h2><p className="muted">Only the repository owner can view this.</p></div></div>
      </section>
    );
  }

  const onlineCount = activity.filter((item) => item.status === "ONLINE").length;
  const needHelpCount = activity.filter((item) => item.status === "NEED_HELP").length;
  const blockedDeviceCount = devices.filter((item) => item.trust_status === "BLOCKED").length;
  const showDevices = isOwner && devicesAllowed;

  const rank = (item) => (item.status === "NEED_HELP" ? 2 : item.status === "ONLINE" ? 1 : 0);
  const topPerformers = [...activity]
    .sort((a, b) => rank(b) - rank(a) || (b.hours_active_today || 0) - (a.hours_active_today || 0))
    .slice(0, 5);

  return (
    <div className="team-activity-grid">
      <section className="kpi-grid team-kpi-grid team-activity-wide">
        <article className="kpi-card tone-green"><span>Online now</span><strong>{onlineCount}</strong><small>Excel taskpane open right now</small></article>
        <article className={`kpi-card tone-amber ${needHelpCount ? "kpi-card-pulse" : ""}`}><span>Need help</span><strong>{needHelpCount}</strong><small>Flagged for owner attention</small></article>
        <article className="kpi-card tone-ink"><span>Team members</span><strong>{activity.length}</strong><small>With access to this repository</small></article>
        {showDevices ? (
          <article className="kpi-card tone-red"><span>Devices blocked</span><strong>{blockedDeviceCount}</strong><small>Of {devices.length} known device(s)</small></article>
        ) : null}
      </section>

      <section className={`panel team-activity-wide collapsible-panel ${pulseExpanded ? "expanded" : "collapsed"}`}>
        <button type="button" className="panel-header collapsible-panel-trigger" onClick={() => setPulseExpanded((value) => !value)}>
          <div><p className="eyebrow">TEAM / TREND</p><h2>Team pulse</h2><p className="muted">Fourteen days of real accumulated Excel time per person — spot who's drifting quiet or unusually busy before it becomes a problem.</p></div>
          <i className="collapsible-panel-chevron" aria-hidden="true">⌄</i>
        </button>
        {pulseExpanded ? (
          <div className="collapsible-panel-body">
            {pulseLoading && !pulse ? <div className="empty-state compact">Loading team pulse...</div> : null}
            {pulse && pulse.users.length ? (
              (() => {
                const maxSeconds = Math.max(1, ...pulse.users.flatMap((user) => user.cells.map((cell) => cell.seconds)));
                return (
                  <div className="team-pulse-grid">
                    <div className="team-pulse-row team-pulse-header-row">
                      <span className="team-pulse-name" />
                      <div className="team-pulse-cells">
                        {pulse.dates.map((date) => (
                          <span key={date} className="team-pulse-date" title={date}>{new Date(`${date}T00:00:00Z`).getUTCDate()}</span>
                        ))}
                      </div>
                      <span className="team-pulse-total" />
                    </div>
                    {pulse.users.map((user) => (
                      <div key={user.user_id} className="team-pulse-row">
                        <div className="team-pulse-name">
                          <i className="user-avatar">{(user.display_name || user.email || "?").slice(0, 2).toUpperCase()}</i>
                          <span>{user.display_name || user.email}</span>
                        </div>
                        <div className="team-pulse-cells">
                          {user.cells.map((cell, index) => (
                            <span
                              key={index}
                              className={`team-pulse-cell level-${pulseIntensity(cell.seconds, maxSeconds)} ${cell.seconds > 0 && cell.focus < 0.4 ? "fragmented" : ""}`}
                              title={`${Math.round(cell.seconds / 60)}m on ${pulse.dates[index]}${cell.seconds > 0 ? ` — focus ${Math.round(cell.focus * 100)}%` : ""}`}
                            />
                          ))}
                        </div>
                        <strong className="team-pulse-total" title={user.average_focus != null ? `Average focus ${Math.round(user.average_focus * 100)}%` : "No active days"}>
                          {Math.round((user.total_seconds / 3600) * 10) / 10}h
                          {user.average_focus != null ? <small className="team-pulse-focus"> · {Math.round(user.average_focus * 100)}% focus</small> : null}
                        </strong>
                      </div>
                    ))}
                  </div>
                );
              })()
            ) : (pulse && !pulse.users.length ? <div className="empty-state compact">No members with access to this repository yet.</div> : null)}
            <p className="muted team-pulse-legend">Focus = how continuous the active time was, not just how much. A cell with a dashed ring means the day's time was fragmented (pop-in-pop-out) rather than one continuous stretch.</p>
          </div>
        ) : null}
      </section>

      <section className="panel team-activity-wide access-tree-spotlight">
        <div className="panel-header">
          <div><p className="eyebrow">RBAC / REPOSITORY ACCESS</p><h2>Access tree</h2><p className="muted">Who's onboarded to this repository, their role, and their access. Click a person for their full status, hours, and device controls.</p></div>
        </div>
        <RepositoryAccessTree
          tableId={tableId}
          repositoryName={repositoryName}
          isOwner={isOwner}
          onError={onError}
          activity={activity}
          devices={devices}
          busyFingerprintId={busyFingerprintId}
          onSetDeviceTrust={setDeviceTrust}
        />
      </section>

      <section className="panel team-digest-panel">
        <div className="panel-header">
          <div><p className="eyebrow">TEAM / HEALTH DIGEST</p><h2>Team health digest</h2><p className="muted">A deterministic read of onboarding, review backlog, and unusual activity — with an optional AI narrative layered on top.</p></div>
          <button type="button" className="trace-button" disabled={digestLoading} onClick={generateDigest}>{digestLoading ? "Generating..." : "Generate digest"}</button>
        </div>
        {digest ? (
          <div className="collapsible-panel-body team-digest-body">
            <div className="team-roster-kpis">
              <div className="team-roster-kpi" style={{ "--kpi-color": "#3fb950", "--kpi-bg": "rgba(63,185,80,.1)" }}><span>Online now</span><strong>{digest.online_count}/{digest.team_size}</strong></div>
              <div className="team-roster-kpi" style={{ "--kpi-color": "#d29922", "--kpi-bg": "rgba(210,153,34,.1)" }}><span>Stalled onboarding</span><strong>{digest.stalled_onboarding}</strong></div>
              <div className="team-roster-kpi" style={{ "--kpi-color": "#cf222e", "--kpi-bg": "rgba(207,34,46,.1)" }}><span>Bus-factor sheets</span><strong>{digest.bus_factor_sheets}</strong></div>
              <div className="team-roster-kpi" style={{ "--kpi-color": "#0969da", "--kpi-bg": "rgba(9,105,218,.1)" }}><span>Anomalies flagged</span><strong>{digest.anomaly_count}</strong></div>
            </div>
            {digest.narrative ? <ul className="team-digest-observations">{summaryToBullets(digest.narrative).map((sentence, index) => (<li key={index}>{highlightAssessmentText(sentence)}</li>))}</ul> : <p className="muted">No AI narrative available — deterministic observations below still stand on their own.</p>}
            <ul className="team-digest-observations">
              {digest.observations.map((observation, index) => (<li key={index}>{observation}</li>))}
            </ul>
          </div>
        ) : null}
      </section>

      <section className={`panel collapsible-panel ${queueExpanded ? "expanded" : "collapsed"}`}>
        <button type="button" className="panel-header collapsible-panel-trigger" onClick={() => setQueueExpanded((value) => !value)}>
          <div><p className="eyebrow">REVIEW QUEUE / DEPTH</p><h2>Review queue depth</h2><p className="muted">Open merge requests weighted by staleness — a request open for a week counts for more than one opened an hour ago.</p></div>
          <i className="collapsible-panel-chevron" aria-hidden="true">⌄</i>
        </button>
        {queueExpanded ? (
          <div className="collapsible-panel-body">
            {expertiseLoading && !expertise ? <div className="empty-state compact">Loading review queue...</div> : null}
            {expertise && expertise.experts.length ? (
              (() => {
                const topWorkload = Math.max(1, ...expertise.experts.map((expert) => expert.open_workload_days));
                const ranked = [...expertise.experts].sort((a, b) => b.open_workload_days - a.open_workload_days);
                return (
                  <div className="expertise-sheet-card">
                    {ranked.map((expert) => (
                      <div key={expert.user_id} className="expertise-expert-row">
                        <i className="user-avatar">{(expert.display_name || expert.email || "?").slice(0, 2).toUpperCase()}</i>
                        <span className="expertise-name">{expert.display_name}</span>
                        <div className="expertise-bar-track"><div className="expertise-bar-fill" style={{ width: `${Math.round((expert.open_workload_days / topWorkload) * 100)}%` }} /></div>
                        <small>{expert.open_merge_requests} open · {expert.open_workload_days}d</small>
                      </div>
                    ))}
                  </div>
                );
              })()
            ) : (expertise ? <div className="empty-state compact">No open review workload right now.</div> : null)}
          </div>
        ) : null}
      </section>

      <section className={`panel team-activity-hero team-activity-wide collapsible-panel ${performersExpanded ? "expanded" : "collapsed"}`}>
        <button type="button" className="panel-header collapsible-panel-trigger" onClick={() => setPerformersExpanded((value) => !value)}>
          <div><p className="eyebrow">TEAM / LIVE ACTIVITY</p><h2>Top 5 performers</h2><p className="muted">Ranked by live status and active time today — automatic Online/Offline from the Excel taskpane, Need Help set manually there.</p></div>
          <i className="collapsible-panel-chevron" aria-hidden="true">⌄</i>
        </button>
        {performersExpanded ? (
          <div className="collapsible-panel-body">
            <TeamActivityMatrix activity={topPerformers} onSelect={setSelectedMember} />
            {!activity.length && !loading ? <div className="empty-state compact">No members with access to this repository yet.</div> : null}
            {activity.length > 5 ? <p className="muted team-roster-footnote">Showing top 5 of {activity.length} members. Full roster in the access tree above.</p> : null}
            {selectedMember ? (
              <MemberProfileModal tableId={tableId} member={selectedMember} onError={onError} onClose={() => setSelectedMember(null)} />
            ) : null}
          </div>
        ) : null}
      </section>

      <section className={`panel collapsible-panel ${anomalyExpanded ? "expanded" : "collapsed"}`}>
        <button type="button" className="panel-header collapsible-panel-trigger" onClick={() => setAnomalyExpanded((value) => !value)}>
          <div><p className="eyebrow">TEAM / BEHAVIORAL ANOMALIES</p><h2>Unusual activity</h2><p className="muted">Compares each person against their own trailing baseline only — never against peers — and requires a large, sustained spike before flagging anything.</p></div>
          <i className="collapsible-panel-chevron" aria-hidden="true">⌄</i>
        </button>
        {anomalyExpanded ? (
          <div className="collapsible-panel-body">
            {anomaliesLoading && !anomalies ? <div className="empty-state compact">Scanning for anomalies...</div> : null}
            {anomalies && anomalies.anomalies.length ? (
              <div className="expertise-sheet-card">
                {anomalies.anomalies.map((item, index) => (
                  <div key={index} className="expertise-expert-row">
                    <i className="user-avatar">{(item.display_name || item.email || "?").slice(0, 2).toUpperCase()}</i>
                    <span className="expertise-name">{item.display_name || item.email}</span>
                    <small>{item.message}</small>
                  </div>
                ))}
              </div>
            ) : (anomalies ? <div className="empty-state compact">Nothing unusual — everyone's within their own normal range.</div> : null)}
          </div>
        ) : null}
      </section>

      <section className={`panel collapsible-panel ${discoveryExpanded ? "expanded" : "collapsed"}`}>
        <button type="button" className="panel-header collapsible-panel-trigger" onClick={() => setDiscoveryExpanded((value) => !value)}>
          <div><p className="eyebrow">TEAM / DISCOVERY AGENT</p><h2>Who should I ask?</h2><p className="muted">Weighs real expertise, live presence, and open workload together — not just who's touched a sheet the most.</p></div>
          <i className="collapsible-panel-chevron" aria-hidden="true">⌄</i>
        </button>
        {discoveryExpanded ? (
          <div className="collapsible-panel-body">
            <form className="discovery-ask-form" onSubmit={askDiscovery}>
              <input
                type="text" value={discoveryTopic} onChange={(event) => setDiscoveryTopic(event.target.value)}
                placeholder="e.g. the settlement mapping sheet" disabled={discoveryBusy}
              />
              <button type="submit" disabled={discoveryBusy || !discoveryTopic.trim()}>{discoveryBusy ? "Thinking..." : "Ask"}</button>
            </form>
            {discoveryRunId ? <AgentRunTimeline runId={discoveryRunId} onDone={onDiscoveryRunDone} onError={onError} compact /> : null}
            {discoveryResult ? (
              discoveryResult.candidates && discoveryResult.candidates.length ? (
                <div className="discovery-result">
                  {discoveryResult.rationale ? <ul className="ai-summary-bullets">{summaryToBullets(discoveryResult.rationale).map((sentence, index) => (<li key={index}>{highlightAssessmentText(sentence)}</li>))}</ul> : null}
                  <div className="expertise-sheet-card">
                    {discoveryResult.candidates.map((candidate, index) => (
                      <div key={candidate.user_id} className={`expertise-expert-row ${index === 0 ? "discovery-top-pick" : ""}`}>
                        <span className="expertise-rank">{index + 1}</span>
                        <i className="user-avatar">{(candidate.display_name || candidate.email || "?").slice(0, 2).toUpperCase()}</i>
                        <span className="expertise-name">{candidate.display_name}</span>
                        <small>{candidate.is_active_now ? "Online" : "Offline"} · {candidate.open_merge_requests} open review(s)</small>
                      </div>
                    ))}
                  </div>
                </div>
              ) : <div className="empty-state compact">{discoveryResult.rationale || "Nobody has expertise here yet."}</div>
            ) : null}
          </div>
        ) : null}
      </section>

      <section className={`panel team-activity-wide collapsible-panel ${onboardingExpanded ? "expanded" : "collapsed"}`}>
        <button type="button" className="panel-header collapsible-panel-trigger" onClick={() => setOnboardingExpanded((value) => !value)}>
          <div><p className="eyebrow">TEAM / ONBOARDING</p><h2>Onboarding ramp</h2><p className="muted">Time from joining to first commit — and who's stalled past a week with no first commit yet.</p></div>
          <i className="collapsible-panel-chevron" aria-hidden="true">⌄</i>
        </button>
        {onboardingExpanded ? (
          <div className="collapsible-panel-body">
            {onboardingLoading && !onboarding ? <div className="empty-state compact">Loading onboarding ramp...</div> : null}
            {onboarding ? (
              <>
                <div className="team-roster-kpis">
                  <div className="team-roster-kpi" style={{ "--kpi-color": "#0969da", "--kpi-bg": "rgba(9,105,218,.1)" }}><span>Average ramp</span><strong>{onboarding.average_ramp_days != null ? `${onboarding.average_ramp_days}d` : "—"}</strong></div>
                  <div className="team-roster-kpi" style={{ "--kpi-color": "#cf222e", "--kpi-bg": "rgba(207,34,46,.1)" }}><span>Stalled</span><strong>{onboarding.stalled_count}</strong></div>
                </div>
                <div className="expertise-sheet-card">
                  {onboarding.members.map((member) => (
                    <div key={member.user_id} className="expertise-expert-row">
                      <i className="user-avatar">{(member.display_name || member.email || "?").slice(0, 2).toUpperCase()}</i>
                      <span className="expertise-name">{member.display_name || member.email}</span>
                      {member.stalled ? (
                        <span className="pill device-trust-blocked">Stalled — no first commit</span>
                      ) : member.ramp_days != null ? (
                        <small>Ramped in {member.ramp_days}d</small>
                      ) : (
                        <small className="muted">Still ramping</small>
                      )}
                    </div>
                  ))}
                </div>
              </>
            ) : null}
          </div>
        ) : null}
      </section>

      {isOwner ? (
      <section className={`panel collapsible-panel ${rbacExpanded ? "expanded" : "collapsed"}`}>
        <button type="button" className="panel-header collapsible-panel-trigger" onClick={() => setRbacExpanded((value) => !value)}>
          <div><p className="eyebrow">SECURITY / RBAC ANOMALY AGENT</p><h2>Access anomalies</h2><p className="muted">Cross-references role grants, device trust, and real activity — dormant grants, role/activity mismatches, and unfamiliar devices in active use.</p></div>
          <i className="collapsible-panel-chevron" aria-hidden="true">⌄</i>
        </button>
        {rbacExpanded ? (
          <div className="collapsible-panel-body">
            <button type="button" className="trace-button" disabled={rbacScanning} onClick={runRbacScan}>{rbacScanning ? "Scanning..." : "Scan for anomalies"}</button>
            {rbacRunId ? <AgentRunTimeline runId={rbacRunId} onDone={onRbacRunDone} onError={onError} compact /> : null}
            {rbacFindings && rbacFindings.length ? (
              <div className="expertise-sheet-card">
                {rbacFindings.map((finding) => (
                  <div key={finding.finding_id} className="rbac-finding-row">
                    <span className={`pill device-trust-${finding.severity === "HIGH" || finding.severity === "CRITICAL" ? "blocked" : "unknown"}`}>{finding.severity}</span>
                    <div className="rbac-finding-body">
                      <strong>{finding.anomaly_type.replace(/_/g, " ")}</strong>
                      {finding.explanation ? <ul className="ai-summary-bullets">{summaryToBullets(finding.explanation).map((sentence, index) => (<li key={index}>{highlightAssessmentText(sentence)}</li>))}</ul> : null}
                    </div>
                    <div className="rbac-finding-actions">
                      <button type="button" className="secondary-button" disabled={rbacBusyFindingId === finding.finding_id} onClick={() => decideFinding(finding.finding_id, "ACKNOWLEDGED")}>Acknowledge</button>
                      <button type="button" className="trace-button danger" disabled={rbacBusyFindingId === finding.finding_id} onClick={() => decideFinding(finding.finding_id, "DISMISSED")}>Dismiss</button>
                    </div>
                  </div>
                ))}
              </div>
            ) : (rbacFindings ? <div className="empty-state compact">No open anomalies — run a scan to check for new ones.</div> : null)}
          </div>
        ) : null}
      </section>
      ) : null}

      <section className={`panel collapsible-panel ${expertiseExpanded ? "expanded" : "collapsed"}`}>
        <button type="button" className="panel-header collapsible-panel-trigger" onClick={() => setExpertiseExpanded((value) => !value)}>
          <div><p className="eyebrow">EXPERTISE RADAR</p><h2>Who knows what</h2><p className="muted">Real edit history, per worksheet — so "who do I ask about this sheet" has an actual answer instead of institutional memory.</p></div>
          <i className="collapsible-panel-chevron" aria-hidden="true">⌄</i>
        </button>
        {expertiseExpanded ? (
          <div className="collapsible-panel-body">
            {expertiseLoading && !expertise ? <div className="empty-state compact">Loading expertise radar...</div> : null}
            {expertise && expertise.sheets.length ? (
              <div className="expertise-radar-grid">
                {expertise.sheets.map((sheet) => {
                  const topTouches = sheet.experts[0]?.touches || 1;
                  return (
                    <article key={sheet.sheet_id} className="expertise-sheet-card">
                      <header><strong>{sheet.sheet_name}</strong></header>
                      {sheet.experts.map((expert, index) => (
                        <div key={expert.user_id} className="expertise-expert-row">
                          <span className="expertise-rank">{index + 1}</span>
                          <span className="expertise-name">{expert.display_name}</span>
                          <div className="expertise-bar-track"><div className="expertise-bar-fill" style={{ width: `${Math.round((expert.touches / topTouches) * 100)}%` }} /></div>
                          <small>{expert.touches}</small>
                        </div>
                      ))}
                    </article>
                  );
                })}
              </div>
            ) : (expertise ? <div className="empty-state compact">No commit history yet — expertise builds up as people commit.</div> : null)}
          </div>
        ) : null}
      </section>

      {showDevices ? (
      <section className={`panel team-activity-wide collapsible-panel ${devicesExpanded ? "expanded" : "collapsed"}`}>
        <button type="button" className="panel-header collapsible-panel-trigger" onClick={() => setDevicesExpanded((value) => !value)}>
          <div><p className="eyebrow">DEVICES / DATA PROTECTION</p><h2>Known devices</h2><p className="muted">Block a device to immediately cut off its access to this repository's data — independent of the user's login credentials.</p></div>
          <i className="collapsible-panel-chevron" aria-hidden="true">⌄</i>
        </button>
        {devicesExpanded ? (
          <div className="collapsible-panel-body">
            <div className="device-matrix">
              {devices.map((device) => {
                const trustTone = device.trust_status === "TRUSTED" ? "#3fb950" : device.trust_status === "BLOCKED" ? "#cf222e" : "#d29922";
                return (
                  <article key={device.fingerprint_id} className="device-matrix-card">
                    <div className="device-matrix-person">
                      <i className="user-avatar" style={{ boxShadow: `0 0 0 2px ${trustTone}` }}>
                        {(device.display_name || device.email || "?").slice(0, 2).toUpperCase()}
                      </i>
                      <div>
                        <strong>{device.display_name || device.email}</strong>
                        <small title={device.machine_id}>{device.machine_id}</small>
                      </div>
                      <span className={`pill device-trust-${device.trust_status?.toLowerCase()}`}>{device.trust_status}</span>
                    </div>
                    <div className="team-roster-kpis">
                      <div className="team-roster-kpi" style={{ "--kpi-color": "#0969da", "--kpi-bg": "rgba(9,105,218,.1)" }}>
                        <span>IP address</span>
                        <strong>{device.ip_address || "unknown"}</strong>
                      </div>
                      <div className="team-roster-kpi" style={{ "--kpi-color": trustTone, "--kpi-bg": "rgba(0,0,0,.04)" }}>
                        <span>Last seen on this repository</span>
                        <strong>{device.last_seen_at ? timeAgo(device.last_seen_at) : "Never on this repository"}</strong>
                      </div>
                    </div>
                    {device.trust_status !== "BLOCKED" ? (
                      <button className="trace-button device-matrix-action" disabled={busyFingerprintId === device.fingerprint_id} onClick={() => setDeviceTrust(device.fingerprint_id, "block")}>Block device</button>
                    ) : (
                      <button className="trace-button device-matrix-action" disabled={busyFingerprintId === device.fingerprint_id} onClick={() => setDeviceTrust(device.fingerprint_id, "trust")}>Trust device</button>
                    )}
                  </article>
                );
              })}
            </div>
            {!devices.length && !loading ? <div className="empty-state compact">No devices have connected to this repository yet.</div> : null}
          </div>
        ) : null}
      </section>
      ) : null}
    </div>
  );
}
