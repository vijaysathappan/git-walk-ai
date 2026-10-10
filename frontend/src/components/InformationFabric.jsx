import React, { useEffect, useState } from "react";
import {
  createFabricConnection, discoverFabricConnection, getFabricCanonicalObjects,
  getFabricConnections, getFabricConnectorTypes, getFabricDeadLetters, getFabricOperations,
  getFabricRuns, getFabricThread, getFabricThreadTimeline, replayFabricDeadLetter,
  runFabricConnection, searchFabric, testFabricConnection, uploadFabricSource,
} from "../services/api";

const DEFAULT_CONFIG = {
  FILE: { format: "CSV", object_type: "Record", id_field: "id" },
  REST_API: { base_url: "https://api.example.com", path: "/records", id_field: "id" },
  RELATIONAL_DB: { table: "records", primary_key: "id" },
  S3: { bucket: "enterprise-landing", prefix: "inbound/" },
  SFTP: { host: "sftp.example.com", port: 22, base_path: "/inbound", host_key_fingerprint: "" },
};

function Metric({ label, value, note, tone = "mint" }) {
  return <article className={`fabric-metric ${tone}`}><span>{label}</span><strong>{value ?? 0}</strong><small>{note}</small></article>;
}

function Status({ value }) {
  const normalized = String(value || "UNKNOWN").toLowerCase();
  return <span className={`fabric-status ${normalized}`}>{String(value || "UNKNOWN").replaceAll("_", " ")}</span>;
}

export default function InformationFabric({ onError }) {
  const [view, setView] = useState("operations");
  const [operations, setOperations] = useState(null);
  const [types, setTypes] = useState([]);
  const [connections, setConnections] = useState([]);
  const [runs, setRuns] = useState([]);
  const [deadLetters, setDeadLetters] = useState([]);
  const [canonical, setCanonical] = useState([]);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResult, setSearchResult] = useState({ items: [], facets: {} });
  const [thread, setThread] = useState(null);
  const [timeline, setTimeline] = useState(null);
  const [sourceFiles, setSourceFiles] = useState({});
  const [form, setForm] = useState({ name: "", connector_type: "FILE", environment: "DEVELOPMENT", configuration: JSON.stringify(DEFAULT_CONFIG.FILE, null, 2), credentials: "{}" });

  const refresh = async () => {
    setBusy("refresh");
    try {
      const [nextOperations, nextTypes, nextConnections, nextRuns, nextDead, nextCanonical] = await Promise.all([
        getFabricOperations(), getFabricConnectorTypes(), getFabricConnections(), getFabricRuns(), getFabricDeadLetters(), getFabricCanonicalObjects(),
      ]);
      setOperations(nextOperations); setTypes(nextTypes); setConnections(nextConnections); setRuns(nextRuns); setDeadLetters(nextDead); setCanonical(nextCanonical);
    } catch (error) { onError(error.message); }
    finally { setBusy(""); }
  };

  useEffect(() => { refresh(); }, []);

  const act = async (key, action, success) => {
    setBusy(key); setNotice("");
    try { const result = await action(); setNotice(success(result)); await refresh(); return result; }
    catch (error) { onError(error.message); }
    finally { setBusy(""); }
  };

  const createConnection = async (event) => {
    event.preventDefault();
    let configuration; let credentials;
    try { configuration = JSON.parse(form.configuration); credentials = JSON.parse(form.credentials); }
    catch { onError("Configuration and credentials must be valid JSON."); return; }
    const result = await act("create", () => createFabricConnection({ ...form, configuration, credentials }), (item) => `${item.name} registered. Credentials were encrypted.`);
    if (result) setForm((current) => ({ ...current, name: "", credentials: "{}" }));
  };

  const connectionAction = (connection, action) => {
    if (action === "upload") {
      const file = sourceFiles[connection.connection_id];
      if (!file) { onError("Choose a source file first."); return; }
      return act(connection.connection_id, () => uploadFabricSource(connection.connection_id, file), (result) => `${result.filename} sealed as ${result.object_hash.slice(0, 12)}.`);
    }
    if (action === "test") return act(connection.connection_id, () => testFabricConnection(connection.connection_id), (result) => `Connection ${result.health_status.toLowerCase()} in ${result.latency_ms} ms.`);
    if (action === "discover") return act(connection.connection_id, () => discoverFabricConnection(connection.connection_id), (result) => `Schema discovered: ${result.schema_hash.slice(0, 12)}${result.drift_id ? " with drift" : ""}.`);
    return act(connection.connection_id, () => runFabricConnection(connection.connection_id, { batch_size: 1000 }), (result) => `${result.records_ingested} record(s) ingested in ${result.run_id}.`);
  };

  const search = async (event) => {
    event.preventDefault(); setBusy("search");
    try { setSearchResult(await searchFabric(searchQuery)); }
    catch (error) { onError(error.message); }
    finally { setBusy(""); }
  };

  const openThread = async (item) => {
    if (!item.node_id) return;
    setBusy("thread"); setView("thread");
    try { const [graph, events] = await Promise.all([getFabricThread(item.node_id, 5), getFabricThreadTimeline(item.node_id)]); setThread(graph); setTimeline(events); }
    catch (error) { onError(error.message); }
    finally { setBusy(""); }
  };

  const nav = [["operations", "Operations"], ["connections", "Connections"], ["runs", "Run ledger"], ["canonical", "Canonical data"], ["thread", "Digital thread"], ["search", "Enterprise search"]];
  return <div className="fabric-stage">
    <section className="fabric-hero">
      <div><p className="eyebrow">STAGE 4 / INFORMATION FABRIC</p><h2>Connect every system.<br /><em>Prove every movement.</em></h2><p>Provider-neutral ingestion, canonical enterprise objects, temporal lineage, reconciliation, and evidence-grade operations in one control plane.</p></div>
      <div className="fabric-orbit" aria-hidden="true"><span>ERP</span><span>CRM</span><strong>GW</strong><span>S3</span><span>API</span><i /></div>
    </section>

    <nav className="fabric-nav">{nav.map(([id, label]) => <button key={id} className={view === id ? "active" : ""} onClick={() => setView(id)}>{label}<small>{id === "connections" ? connections.length : id === "runs" ? runs.length : id === "canonical" ? canonical.length : ""}</small></button>)}<button className="fabric-refresh" onClick={refresh} disabled={busy === "refresh"}>Refresh fabric</button></nav>
    {notice ? <div className="fabric-notice">{notice}</div> : null}

    {view === "operations" ? <>
      <section className="fabric-metrics">
        <Metric label="Connection health" value={`${operations?.connections?.healthy || 0}/${operations?.connections?.total || 0}`} note={`${operations?.connections?.attention || 0} need attention`} />
        <Metric label="Successful runs" value={operations?.runs?.successful} note={`${operations?.runs?.records_read || 0} records observed`} tone="blue" />
        <Metric label="Canonical objects" value={operations?.canonical?.objects} note={`${operations?.canonical?.versions || 0} temporal versions`} tone="violet" />
        <Metric label="Evidence edges" value={operations?.thread?.edges} note={`${operations?.thread?.nodes || 0} thread nodes`} tone="amber" />
      </section>
      <div className="fabric-operations-grid">
        <section className="fabric-panel fabric-flow"><header><div><p className="eyebrow">LIVE TOPOLOGY</p><h3>Data movement pulse</h3></div><Status value={operations?.connections?.attention ? "ATTENTION" : "HEALTHY"} /></header><div className="flow-track"><span>Sources<b>{connections.length}</b></span><i /><span>Raw envelopes<b>{operations?.runs?.records_read || 0}</b></span><i /><span>Canonical<b>{operations?.canonical?.objects || 0}</b></span><i /><span>Consumers<b>{operations?.thread?.edges || 0}</b></span></div></section>
        <section className="fabric-panel fabric-queues"><header><div><p className="eyebrow">EXCEPTION CONTROL</p><h3>Queues requiring action</h3></div></header><div><span>Quarantine<strong>{operations?.queues?.quarantine || 0}</strong></span><span>Dead letters<strong>{operations?.queues?.dead_letters || 0}</strong></span><span>Conflicts<strong>{operations?.queues?.conflicts || 0}</strong></span></div></section>
      </div>
      <section className="fabric-panel"><header><div><p className="eyebrow">RECENT EXECUTIONS</p><h3>Operational ledger</h3></div></header><div className="fabric-table"><header><span>Run</span><span>Connection</span><span>Operation</span><span>Records</span><span>Status</span><span>Started</span></header>{(operations?.latest_runs || []).map((run) => <article key={run.run_id}><code>{run.run_id}</code><code>{run.connection_id}</code><span>{run.operation}</span><strong>{run.records_read}</strong><Status value={run.status} /><time>{new Date(run.started_at).toLocaleString()}</time></article>)}{!operations?.latest_runs?.length ? <p>No integration executions yet.</p> : null}</div></section>
    </> : null}

    {view === "connections" ? <div className="fabric-connections-layout">
      <form className="fabric-panel fabric-composer" onSubmit={createConnection}><p className="eyebrow">REGISTER / CONNECTION</p><h3>New enterprise source</h3><label>Name<input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="Finance ERP production" required /></label><div className="fabric-form-row"><label>Connector<select value={form.connector_type} onChange={(event) => { const type = event.target.value; setForm({ ...form, connector_type: type, configuration: JSON.stringify(DEFAULT_CONFIG[type] || {}, null, 2) }); }}>{types.map((type) => <option key={type.connector_type}>{type.connector_type}</option>)}</select></label><label>Environment<select value={form.environment} onChange={(event) => setForm({ ...form, environment: event.target.value })}><option>DEVELOPMENT</option><option>TEST</option><option>PRODUCTION</option></select></label></div><label>Configuration<textarea value={form.configuration} onChange={(event) => setForm({ ...form, configuration: event.target.value })} spellCheck="false" /></label><label>Credentials <small>encrypted, never returned</small><textarea className="secret-editor" value={form.credentials} onChange={(event) => setForm({ ...form, credentials: event.target.value })} spellCheck="false" /></label><button disabled={busy === "create"}>{busy === "create" ? "Sealing connection..." : "Register connection"}</button></form>
      <section className="fabric-connection-list">{connections.map((connection) => <article key={connection.connection_id} className="fabric-connection-card"><header><span>{connection.connector_type.slice(0, 3)}</span><div><strong>{connection.name}</strong><code>{connection.connection_id}</code></div><Status value={connection.health_status} /></header><dl><div><dt>Environment</dt><dd>{connection.environment}</dd></div><div><dt>Credentials</dt><dd>{connection.credential_configured ? "Vaulted" : "Not required"}</dd></div><div><dt>Last success</dt><dd>{connection.last_success_at ? new Date(connection.last_success_at).toLocaleString() : "Never"}</dd></div></dl>{connection.connector_type === "FILE" ? <label className="fabric-source-picker"><input type="file" onChange={(event) => setSourceFiles({ ...sourceFiles, [connection.connection_id]: event.target.files[0] })} /><span>{sourceFiles[connection.connection_id]?.name || "Choose source file"}</span><button type="button" onClick={() => connectionAction(connection, "upload")} disabled={busy === connection.connection_id}>Seal</button></label> : null}<footer><button onClick={() => connectionAction(connection, "test")} disabled={busy === connection.connection_id}>Test</button><button onClick={() => connectionAction(connection, "discover")} disabled={busy === connection.connection_id}>Discover</button><button className="run" onClick={() => connectionAction(connection, "run")} disabled={busy === connection.connection_id}>Run now</button></footer></article>)}{!connections.length ? <div className="fabric-empty"><strong>No connections registered</strong><p>Start with a file landing zone, API, database, S3 bucket, or SFTP source.</p></div> : null}</section>
    </div> : null}

    {view === "runs" ? <section className="fabric-panel"><header><div><p className="eyebrow">IMMUTABLE EXECUTION EVIDENCE</p><h3>Run ledger</h3></div><span className="fabric-counter">{runs.length} runs</span></header><div className="fabric-table wide"><header><span>Run / idempotency</span><span>Connection</span><span>Read</span><span>Mapped</span><span>Errors</span><span>Status</span></header>{runs.map((run) => <article key={run.run_id}><span><code>{run.run_id}</code><small>{run.idempotency_key}</small></span><code>{run.connection_id}</code><strong>{run.records_read}</strong><strong>{run.records_written}</strong><strong>{run.error_count}</strong><Status value={run.status} /></article>)}</div>{deadLetters.length ? <div className="fabric-dlq"><h4>Dead-letter queue</h4>{deadLetters.map((item) => <article key={item.dead_letter_id}><div><strong>{item.error_code}</strong><p>{item.error_message}</p><code>{item.dead_letter_id}</code></div><button onClick={() => act(item.dead_letter_id, () => replayFabricDeadLetter(item.dead_letter_id), (result) => `Replay ${result.run_id} completed.`)}>Replay safely</button></article>)}</div> : null}</section> : null}

    {view === "canonical" ? <section className="fabric-panel"><header><div><p className="eyebrow">CANONICAL ENTERPRISE MODEL</p><h3>Trusted business objects</h3></div><span className="fabric-counter">{canonical.length} objects</span></header><div className="canonical-grid">{canonical.map((item) => <article key={item.canonical_id}><span>{item.object_type}</span><h4>{item.business_key}</h4><code>{item.canonical_id}</code><footer><small>Version {item.current_version}</small><time>{new Date(item.updated_at).toLocaleString()}</time></footer></article>)}{!canonical.length ? <div className="fabric-empty"><strong>No canonical objects yet</strong><p>Create a schema and mapping through the API, then execute its source connection.</p></div> : null}</div></section> : null}

    {view === "thread" ? <section className="fabric-panel thread-explorer"><header><div><p className="eyebrow">CROSS-SYSTEM PROVENANCE</p><h3>Digital thread explorer</h3></div><span className="fabric-counter">{thread?.nodes?.length || 0} nodes</span></header>{thread ? <div className="thread-layout"><div className="thread-nodes">{thread.nodes.map((node) => <button key={node.node_id} style={{ "--depth": node.depth }} onClick={() => openThread(node)}><span>{node.node_type}</span><strong>{node.display_name}</strong><code>{node.node_id}</code></button>)}</div><div className="thread-timeline"><h4>Evidence timeline</h4>{(timeline?.events || []).map((event) => <article key={event.thread_event_id}><i /><div><strong>{event.event_type.replaceAll("_", " ")}</strong><small>{event.source_system || "Git Walk"} / {new Date(event.event_time).toLocaleString()}</small><code>{event.evidence_reference}</code></div></article>)}</div></div> : <div className="fabric-empty"><strong>Open a thread from enterprise search</strong><p>Search for a source, run, or canonical object and choose “Trace” to traverse its evidence graph.</p><button onClick={() => setView("search")}>Open search</button></div>}</section> : null}

    {view === "search" ? <section className="fabric-panel fabric-search"><header><div><p className="eyebrow">SECURITY-TRIMMED DISCOVERY</p><h3>Search the enterprise thread</h3></div></header><form onSubmit={search}><input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="Search systems, schemas, runs, records, or business keys" /><button disabled={busy === "search"}>Search</button></form><div className="fabric-search-results">{searchResult.items.map((item) => <article key={item.catalogue_id}><span>{item.item_type}</span><div><strong>{item.title}</strong><p>{item.description}</p><code>{item.item_id}</code></div><button disabled={!item.node_id} onClick={() => openThread(item)}>Trace</button></article>)}{!searchResult.items.length ? <div className="fabric-empty"><strong>Search respects enterprise access</strong><p>Results are trimmed using the same organization and repository authorization engine as Git Walk.</p></div> : null}</div></section> : null}
  </div>;
}
