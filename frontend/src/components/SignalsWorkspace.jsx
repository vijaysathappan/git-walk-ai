import React, { useEffect, useState } from "react";
import {
  createSignalDefinition, deleteSignalDefinition, getSignalDefinitions,
  getSignalRuns, getSignalSources, scanSignals, updateSignalDefinition,
} from "../services/api";

function severityTone(severity) {
  const value = String(severity || "").toUpperCase();
  if (value === "CRITICAL" || value === "HIGH") return "severity-high";
  if (value === "MEDIUM") return "severity-medium";
  return "severity-low";
}

function ConditionSummary({ source, condition }) {
  if (!source || !condition) return null;
  const values = Array.isArray(condition.value) ? condition.value.join(", ") : condition.value;
  return <code className="signal-condition-summary">{source.field.label}: {String(condition.op).replaceAll("_", " ")} {values}</code>;
}

function SignalCard({ signal, sources, busy, onToggle, onDelete, onViewRuns }) {
  const source = sources.find((item) => item.source === signal.source);
  const isBuiltin = signal.origin === "SYSTEM_BUILTIN";
  return (
    <article className={`signal-card ${signal.enabled ? "" : "signal-card-disabled"}`}>
      <header>
        <span className={`signal-severity-badge ${severityTone(signal.severity)}`}>{signal.severity}</span>
        <span className={`signal-origin-badge ${isBuiltin ? "builtin" : "custom"}`}>{isBuiltin ? "Built-in" : "Custom"}</span>
      </header>
      <strong>{signal.name}</strong>
      <p>{signal.description}</p>
      <ConditionSummary source={source} condition={signal.condition} />
      <div className="signal-card-stats">
        <span>{signal.open_insight_count} open</span>
        <span>{signal.last_run_matched ?? "—"} last match</span>
        <span>{signal.last_run_at ? new Date(signal.last_run_at).toLocaleString() : "never run"}</span>
      </div>
      <footer>
        <label className="signal-toggle">
          <input type="checkbox" checked={Boolean(signal.enabled)} onChange={() => onToggle(signal)} disabled={busy} />
          <span>{signal.enabled ? "Enabled" : "Disabled"}</span>
        </label>
        <button type="button" onClick={() => onViewRuns(signal)}>History</button>
        {!isBuiltin ? <button type="button" className="signal-delete" onClick={() => onDelete(signal)} disabled={busy}>Delete</button> : null}
      </footer>
    </article>
  );
}

function AddSignalModal({ sources, busy, onClose, onCreate }) {
  const [sourceKey, setSourceKey] = useState(sources[0]?.source || "");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [severity, setSeverity] = useState(sources[0]?.default_severity || "MEDIUM");
  const [enumValues, setEnumValues] = useState([]);
  const [numberValue, setNumberValue] = useState("");
  const source = sources.find((item) => item.source === sourceKey);
  const isMultiSelect = source?.field.operators.includes("in");

  useEffect(() => {
    setSeverity(source?.default_severity || "MEDIUM");
    setEnumValues([]);
    setNumberValue("");
  }, [sourceKey]);

  const toggleEnumValue = (value) => {
    if (isMultiSelect) {
      setEnumValues((current) => current.includes(value) ? current.filter((item) => item !== value) : [...current, value]);
    } else {
      setEnumValues([value]);
    }
  };

  const canSubmit = name.trim().length > 1 && source && (
    source.field.type === "enum" ? enumValues.length > 0 : numberValue !== ""
  );

  const submit = async (event) => {
    event.preventDefault();
    if (!canSubmit) return;
    const condition = source.field.type === "enum"
      ? { field: source.field.key, op: isMultiSelect ? "in" : "equals", value: isMultiSelect ? enumValues : enumValues[0] }
      : { field: source.field.key, op: "older_than_days", value: Number(numberValue) };
    await onCreate({ name: name.trim(), description: description.trim() || null, source: sourceKey, condition, severity });
  };

  return (
    <div className="checkout-backdrop" onClick={onClose}>
      <form className="checkout-dialog signal-form-dialog" onClick={(event) => event.stopPropagation()} onSubmit={submit}>
        <button type="button" className="checkout-close" onClick={onClose} aria-label="Close">×</button>
        <h2>Add a signal</h2>
        <p className="muted">Pick a whitelisted source and value — no SQL, no raw queries. The signal appears in the catalogue below and as a discoverable tool for your AI agents immediately.</p>
        <label className="signal-form-field">
          <span>Source</span>
          <select value={sourceKey} onChange={(event) => setSourceKey(event.target.value)}>
            {sources.map((item) => <option key={item.source} value={item.source}>{item.label}</option>)}
          </select>
          {source ? <small>{source.description}</small> : null}
        </label>
        {source?.field.type === "enum" ? (
          <fieldset className="signal-enum-fieldset">
            <legend>{source.field.label}</legend>
            {(source.field.enum_values || []).map((value) => (
              <label key={value} className="signal-enum-option">
                <input
                  type={isMultiSelect ? "checkbox" : "radio"}
                  name="signal_enum_value"
                  checked={enumValues.includes(value)}
                  onChange={() => toggleEnumValue(value)}
                />
                <span>{value.replaceAll("_", " ")}</span>
              </label>
            ))}
          </fieldset>
        ) : source ? (
          <label className="signal-form-field">
            <span>{source.field.label}</span>
            <input
              type="number" min={source.field.min_value || 1} max={source.field.max_value || 3650}
              value={numberValue} onChange={(event) => setNumberValue(event.target.value)} required
            />
          </label>
        ) : null}
        <label className="signal-form-field">
          <span>Name</span>
          <input value={name} onChange={(event) => setName(event.target.value)} maxLength={160} required placeholder="e.g. Idle branches past sprint end" />
        </label>
        <label className="signal-form-field">
          <span>Description (optional)</span>
          <textarea value={description} onChange={(event) => setDescription(event.target.value)} maxLength={2000} rows={2} />
        </label>
        <label className="signal-form-field">
          <span>Severity</span>
          <select value={severity} onChange={(event) => setSeverity(event.target.value)}>
            {["LOW", "MEDIUM", "HIGH", "CRITICAL"].map((level) => <option key={level} value={level}>{level}</option>)}
          </select>
        </label>
        <footer className="signal-form-footer">
          <button type="submit" disabled={busy || !canSubmit}>{busy ? "Creating..." : "Create signal"}</button>
        </footer>
      </form>
    </div>
  );
}

function RunHistoryModal({ signal, onClose }) {
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    getSignalRuns(signal.signal_id)
      .then((result) => { if (!cancelled) setRuns(result.runs || []); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [signal.signal_id]);

  return (
    <div className="checkout-backdrop" onClick={onClose}>
      <div className="checkout-dialog signal-history-dialog" onClick={(event) => event.stopPropagation()}>
        <button type="button" className="checkout-close" onClick={onClose} aria-label="Close">×</button>
        <h2>{signal.name}</h2>
        <p className="muted">Run history — every scan this signal has been part of.</p>
        {loading ? <p className="muted">Loading...</p> : runs.length ? (
          <ul className="signal-run-history">
            {runs.map((run) => (
              <li key={run.run_id} className={run.status === "FAILED" ? "signal-run-failed" : ""}>
                <span>{new Date(run.started_at).toLocaleString()}</span>
                <strong>{run.matched_count} matched</strong>
                <span className={`signal-run-status-badge ${run.status.toLowerCase()}`}>{run.status}</span>
                {run.error_message ? <small className="signal-run-error">{run.error_message}</small> : null}
              </li>
            ))}
          </ul>
        ) : <p className="muted">No scans recorded yet for this signal.</p>}
      </div>
    </div>
  );
}

export default function SignalsWorkspace({ repositoryId, onError }) {
  const [sources, setSources] = useState([]);
  const [signals, setSignals] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [showAdd, setShowAdd] = useState(false);
  const [historyTarget, setHistoryTarget] = useState(null);

  const load = async () => {
    setLoading(true);
    try {
      const [sourcesResult, signalsResult] = await Promise.all([getSignalSources(), getSignalDefinitions()]);
      setSources(sourcesResult.sources || []);
      setSignals(signalsResult.signals || []);
    } catch (error) {
      onError?.(error.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const runScan = async () => {
    setBusy(true); setNotice("");
    try {
      const result = await scanSignals(repositoryId || null, null);
      setNotice(`${result.generated} insight(s) generated across ${result.signals_scanned} signal(s).`);
      await load();
    } catch (error) {
      onError?.(error.message);
    } finally {
      setBusy(false);
    }
  };

  const toggleSignal = async (signal) => {
    setBusy(true);
    try {
      await updateSignalDefinition(signal.signal_id, { enabled: !signal.enabled });
      await load();
    } catch (error) {
      onError?.(error.message);
    } finally {
      setBusy(false);
    }
  };

  const removeSignal = async (signal) => {
    if (!window.confirm(`Delete the custom signal "${signal.name}"? This cannot be undone.`)) return;
    setBusy(true);
    try {
      await deleteSignalDefinition(signal.signal_id);
      await load();
    } catch (error) {
      onError?.(error.message);
    } finally {
      setBusy(false);
    }
  };

  const createSignal = async (payload) => {
    setBusy(true);
    try {
      await createSignalDefinition(payload);
      setShowAdd(false);
      setNotice(`Signal "${payload.name}" created and is now watching your organization.`);
      await load();
    } catch (error) {
      onError?.(error.message);
    } finally {
      setBusy(false);
    }
  };

  const builtin = signals.filter((signal) => signal.origin === "SYSTEM_BUILTIN");
  const custom = signals.filter((signal) => signal.origin !== "SYSTEM_BUILTIN");

  return (
    <section className="ai-surface signal-registry">
      <header className="signal-registry-header">
        <div>
          <p className="eyebrow">SIGNAL REGISTRY</p>
          <h3>{signals.length} signal{signals.length === 1 ? "" : "s"} watching your organization</h3>
          <p className="muted">Deterministic detection, built from data — pick a whitelisted source and value, no SQL required. Every signal also registers as a discoverable tool your AI agents can call by name.</p>
        </div>
        <div className="signal-registry-actions">
          <button type="button" onClick={runScan} disabled={busy}>{busy ? "Scanning..." : "Run all signals"}</button>
          <button type="button" className="signal-add-button" onClick={() => setShowAdd(true)}>+ Add signal</button>
        </div>
      </header>
      {notice ? <div className="ai-notice">{notice}</div> : null}
      {loading ? (
        <p className="muted signal-loading">Loading signals...</p>
      ) : (
        <>
          <div className="signal-registry-group">
            <h4>Built-in ({builtin.length})</h4>
            <div className="signal-grid">
              {builtin.map((signal) => (
                <SignalCard
                  key={signal.signal_id} signal={signal} sources={sources} busy={busy}
                  onToggle={toggleSignal} onDelete={removeSignal} onViewRuns={setHistoryTarget}
                />
              ))}
            </div>
          </div>
          <div className="signal-registry-group">
            <h4>Custom ({custom.length})</h4>
            {custom.length ? (
              <div className="signal-grid">
                {custom.map((signal) => (
                  <SignalCard
                    key={signal.signal_id} signal={signal} sources={sources} busy={busy}
                    onToggle={toggleSignal} onDelete={removeSignal} onViewRuns={setHistoryTarget}
                  />
                ))}
              </div>
            ) : (
              <p className="muted">No custom signals yet — add one to monitor something specific to your organization.</p>
            )}
          </div>
        </>
      )}
      {showAdd ? <AddSignalModal sources={sources} busy={busy} onClose={() => setShowAdd(false)} onCreate={createSignal} /> : null}
      {historyTarget ? <RunHistoryModal signal={historyTarget} onClose={() => setHistoryTarget(null)} /> : null}
    </section>
  );
}
