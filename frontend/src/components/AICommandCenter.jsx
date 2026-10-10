import React, { useEffect, useState } from "react";
import AgentStore from "./AgentStore";
import AIAnswerView from "./AIAnswerView";
import DataInsightsPanel from "./DataInsightsPanel";
import SignalsWorkspace from "./SignalsWorkspace";
import {
  clearAIConfig,
  confirmAIAction,
  explainAIInsight,
  getAgentLedgerBudget,
  getAgentLedgerLeaderboard,
  getAgentLedgerModels,
  getAgentLedgerRunReceipt,
  getAgentLedgerRuns,
  getAgentLedgerTrend,
  getAIAgentRuns,
  getAIConversation,
  getAIConversations,
  getAIEvaluations,
  getAIInsights,
  getAIModels,
  getAIPlatformAdministration,
  getAIPlatformUsage,
  runAIEvaluation,
  saveAIConfig,
  scanAIControls,
  sendAIPlatformMessage,
  updateAIPlatformSettings,
  updateAIModelPolicy,
} from "../services/api";

const prompts = [
  ["Control brief", "Explain the highest-priority control exceptions, their evidence, and the safest next decisions."],
  ["Change impact", "Summarize recent repository changes and identify downstream impact using only cited evidence."],
  ["Operations", "Diagnose integration freshness, reconciliation, conflicts, and dead-letter health. Do not execute actions."],
  ["EUC risk portfolio", "Which of our EUCs carry the most risk right now, and what are their open high-severity findings?"],
];

function safeJson(value, fallback) {
  try { return typeof value === "string" ? JSON.parse(value) : value ?? fallback; } catch { return fallback; }
}

function modelLabel(slug) {
  return String(slug || "")
    .replace(/^nvidia\//, "")
    .replace(/:free$/, "")
    .replaceAll("-", " ");
}

export default function AICommandCenter({ repositoryId, tableId, branchId, onError }) {
  const [view, setView] = useState("copilot");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [failure, setFailure] = useState(null);
  const [models, setModels] = useState({ configured: false, models: [] });
  const [admin, setAdmin] = useState(null);
  const [usage, setUsage] = useState(null);
  const [insights, setInsights] = useState([]);
  const [runs, setRuns] = useState([]);
  const [evaluations, setEvaluations] = useState([]);
  const [conversations, setConversations] = useState([]);
  const [question, setQuestion] = useState(prompts[0][1]);
  const [conversationId, setConversationId] = useState(null);
  const [answer, setAnswer] = useState(null);
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [editingProvider, setEditingProvider] = useState(false);
  const [policyFeature, setPolicyFeature] = useState("ENTERPRISE_COPILOT");
  const [policyModelId, setPolicyModelId] = useState("");
  const [ledgerAgents, setLedgerAgents] = useState([]);
  const [ledgerTrend, setLedgerTrend] = useState([]);
  const [ledgerModels, setLedgerModels] = useState([]);
  const [ledgerRuns, setLedgerRuns] = useState({ items: [], next_cursor: null });
  const [ledgerBudget, setLedgerBudget] = useState(null);
  const [ledgerAgentFilter, setLedgerAgentFilter] = useState("");
  const [ledgerStatusFilter, setLedgerStatusFilter] = useState("");
  const [ledgerReceipt, setLedgerReceipt] = useState(null);
  const [ledgerLoading, setLedgerLoading] = useState(false);
  const [runLedgerOpen, setRunLedgerOpen] = useState(false);
  const [insightExplanations, setInsightExplanations] = useState({});
  const [explainingInsightId, setExplainingInsightId] = useState(null);
  const [historyConversationId, setHistoryConversationId] = useState(null);
  const [historyConversation, setHistoryConversation] = useState(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyFilter, setHistoryFilter] = useState("");
  const [historyExpanded, setHistoryExpanded] = useState(false);
  const [historyExpandedFilter, setHistoryExpandedFilter] = useState("");
  const [historyExpandedDateFrom, setHistoryExpandedDateFrom] = useState("");
  const [historyExpandedDateTo, setHistoryExpandedDateTo] = useState("");
  const [historyExpandedPage, setHistoryExpandedPage] = useState(0);
  const HISTORY_PAGE_SIZE = 24;

  const openHistoryConversation = async (id) => {
    setHistoryConversationId(id);
    setHistoryLoading(true);
    try { setHistoryConversation(await getAIConversation(id)); }
    catch (err) { onError?.(err.message); }
    finally { setHistoryLoading(false); }
  };

  const conversationMatches = (item, query) => {
    if (!query) return true;
    return `${item.title} ${item.resource_type} ${item.resource_id || ""}`.toLowerCase().includes(query);
  };
  const conversationInDateRange = (item, fromDate, toDate) => {
    if (!fromDate && !toDate) return true;
    const updatedAt = new Date(item.updated_at);
    if (fromDate && updatedAt < new Date(`${fromDate}T00:00:00`)) return false;
    if (toDate && updatedAt > new Date(`${toDate}T23:59:59.999`)) return false;
    return true;
  };
  const historyFilteredConversations = conversations.filter((item) => conversationMatches(item, historyFilter.trim().toLowerCase()));
  const historyExpandedMatches = conversations.filter(
    (item) => conversationMatches(item, historyExpandedFilter.trim().toLowerCase())
      && conversationInDateRange(item, historyExpandedDateFrom, historyExpandedDateTo),
  );
  const historyExpandedTotalPages = Math.max(1, Math.ceil(historyExpandedMatches.length / HISTORY_PAGE_SIZE));
  const historyExpandedPageSafe = Math.min(historyExpandedPage, historyExpandedTotalPages - 1);
  const historyExpandedPageItems = historyExpandedMatches.slice(
    historyExpandedPageSafe * HISTORY_PAGE_SIZE, (historyExpandedPageSafe + 1) * HISTORY_PAGE_SIZE,
  );

  const refresh = async () => {
    const requests = await Promise.allSettled([
      getAIModels(), getAIPlatformAdministration(), getAIPlatformUsage(), getAIInsights(),
      getAIAgentRuns(), getAIEvaluations(), getAIConversations(),
    ]);
    if (requests[0].status === "fulfilled") {
      const catalog = requests[0].value.models || [];
      setModels(requests[0].value);
      setModel((current) => catalog.includes(current) ? current : requests[0].value.default_model || catalog[0] || "");
    }
    if (requests[1].status === "fulfilled") setAdmin(requests[1].value);
    if (requests[2].status === "fulfilled") setUsage(requests[2].value);
    if (requests[3].status === "fulfilled") setInsights(requests[3].value);
    if (requests[4].status === "fulfilled") setRuns(requests[4].value);
    if (requests[5].status === "fulfilled") setEvaluations(requests[5].value);
    if (requests[6].status === "fulfilled") setConversations(requests[6].value);
  };

  useEffect(() => { refresh().catch((error) => onError?.(error.message)); }, []);

  const loadLedger = async () => {
    setLedgerLoading(true);
    try {
      const [agents, trend, models, runsResult, budget] = await Promise.all([
        getAgentLedgerLeaderboard(30), getAgentLedgerTrend(30), getAgentLedgerModels(30),
        getAgentLedgerRuns({ agentKey: ledgerAgentFilter, status: ledgerStatusFilter }), getAgentLedgerBudget(),
      ]);
      setLedgerAgents(agents.agents || []); setLedgerTrend(trend.trend || []); setLedgerModels(models.models || []);
      setLedgerRuns(runsResult); setLedgerBudget(budget);
    } catch (error) { onError?.(error.message); }
    finally { setLedgerLoading(false); }
  };

  useEffect(() => { if (view === "ledger") loadLedger(); }, [view, ledgerAgentFilter, ledgerStatusFilter]);

  const openReceipt = async (agentRunId) => {
    setLedgerLoading(true);
    try { setLedgerReceipt(await getAgentLedgerRunReceipt(agentRunId)); }
    catch (error) { onError?.(error.message); }
    finally { setLedgerLoading(false); }
  };

  const perform = async (work, success) => {
    setBusy(true); setNotice(""); setFailure(null);
    try { const result = await work(); if (success) setNotice(success(result)); await refresh(); return result; }
    catch (error) {
      setFailure({ code: error.code || `HTTP_${error.status || "ERROR"}`, message: error.message });
      onError?.(error.message);
      return null;
    }
    finally { setBusy(false); }
  };

  const ask = async () => {
    const result = await perform(() => sendAIPlatformMessage({
      question, conversation_id: conversationId, repository_id: repositoryId || null,
      resource_type: repositoryId ? "REPOSITORY" : "ORGANIZATION", resource_id: repositoryId || null,
      model: model || null,
    }));
    if (result) { setAnswer(result); setConversationId(result.conversation_id); }
  };

  const connect = async (event) => {
    event.preventDefault();
    const result = await perform(
      () => saveAIConfig(apiKey.trim(), model.trim()),
      (value) => value.replaced ? "OpenRouter token replaced and routing updated." : "OpenRouter token encrypted and connected.",
    );
    if (result) {
      setApiKey("");
      setEditingProvider(false);
      setModel(result.default_model);
    }
  };

  const scan = () => perform(() => scanAIControls(repositoryId || null), (result) => `${result.generated} governed control signal(s) refreshed.`);

  const updatePolicy = () => {
    const settings = admin?.settings || {};
    return perform(() => updateAIPlatformSettings({
      ai_enabled: Boolean(settings.ai_enabled), external_ai_enabled: Boolean(settings.external_ai_enabled),
      allowed_classifications: safeJson(settings.allowed_classifications_json, ["PUBLIC", "INTERNAL", "CONFIDENTIAL"]),
      daily_token_quota: Number(settings.daily_token_quota || 1000000),
      user_daily_token_quota: Number(settings.user_daily_token_quota || 150000),
      agent_actions_enabled: Boolean(settings.agent_actions_enabled), retention_days: Number(settings.retention_days || 90),
    }), () => "Organization AI policy saved and audited.");
  };

  const updateRoute = () => perform(() => updateAIModelPolicy(policyFeature, {
    model_role: "REASONING", model_id: policyModelId || null, allow_external: true,
    allowed_classifications: ["PUBLIC", "INTERNAL", "CONFIDENTIAL"],
    max_input_tokens: 24000, max_output_tokens: 3000, temperature: 0.1,
  }), (result) => `${result.feature} routing policy saved.`);

  const explainInsight = async (insightId) => {
    setExplainingInsightId(insightId);
    try {
      setInsightExplanations((current) => ({ ...current, [insightId]: null }));
      const result = await explainAIInsight(insightId);
      setInsightExplanations((current) => ({ ...current, [insightId]: result }));
    } catch (error) {
      onError?.(error.message);
      setInsightExplanations((current) => { const next = { ...current }; delete next[insightId]; return next; });
    } finally {
      setExplainingInsightId(null);
    }
  };

  const tabs = [["copilot", "Copilot"], ["controls", "Controls"], ["signals", "Signals"], ["agents", "Agents"], ["operations", "AI operations"], ["ledger", "Token Ledger"], ["history", "History"]];
  const ledgerMaxDailyTokens = Math.max(1, ...ledgerTrend.map((item) => item.total_tokens || 0));
  const ledgerTotalTokens30d = ledgerAgents.reduce((sum, item) => sum + (item.total_tokens || 0), 0);
  const ledgerTotalRuns30d = ledgerAgents.filter((item) => item.is_agent).reduce((sum, item) => sum + (item.requests || 0), 0);
  const ledgerOverallSuccessRate = (() => {
    const agentRows = ledgerAgents.filter((item) => item.is_agent);
    const succeeded = agentRows.reduce((sum, item) => sum + (item.succeeded || 0), 0);
    const total = agentRows.reduce((sum, item) => sum + (item.requests || 0), 0);
    return total ? succeeded / total : null;
  })();
  const quota = usage?.quota || admin?.settings || {};
  const tokenTotal = usage?.tokens?.total || 0;
  const quotaTotal = quota.daily_token_quota || 1000000;

  return <div className="ai-command-center">
    <section className="ai-command-hero">
      <div>
        <p className="eyebrow">STAGE 5 / GOVERNED DECISION INTELLIGENCE</p>
        <h2>Ask broadly.<br /><em>Act deliberately.</em></h2>
        <p>One policy-aware AI plane across repositories, EUC intelligence, integrations, digital threads, controls, and immutable evidence.</p>
      </div>
      <div className="ai-orbit" aria-label="AI safety architecture">
        <span>CONTEXT</span><span>POLICY</span><strong>AI</strong><span>EVIDENCE</span><span>HUMAN</span><i />
      </div>
    </section>

    <nav className="ai-command-nav">
      {tabs.map(([id, label]) => <button key={id} className={view === id ? "active" : ""} onClick={() => setView(id)}>{label}</button>)}
      <button className="ai-refresh" onClick={() => refresh()} disabled={busy}>Refresh signals</button>
    </nav>
    {notice ? <div className="ai-notice">{notice}</div> : null}
    {failure ? <div className="ai-command-error"><div><strong>{failure.code.replaceAll("_", " ")}</strong><span>{failure.message}</span></div><button onClick={() => setFailure(null)}>Dismiss</button></div> : null}

    <section className="ai-signal-grid">
      <article><span>Grounded requests</span><strong>{usage?.requests || 0}</strong><small>{usage?.average_grounding ? `${Math.round(usage.average_grounding * 100)}% average grounding` : "Evidence required"}</small></article>
      <article><span>Open controls</span><strong>{insights.length}</strong><small>{insights.filter((item) => ["HIGH", "CRITICAL"].includes(item.severity)).length} require priority review</small></article>
      <article><span>Supervised runs</span><strong>{runs.length}</strong><small>{runs.filter((item) => item.status === "WAITING_CONFIRMATION").length} awaiting confirmation</small></article>
      <article><span>Safety baseline</span><strong>{evaluations[0]?.status || "NOT RUN"}</strong><small>{evaluations[0] ? `${evaluations[0].passed_cases}/${evaluations[0].total_cases} checks` : "Run before rollout"}</small></article>
    </section>

    {view === "copilot" ? <div className="ai-copilot-layout">
      <section className="ai-surface ai-conversation">
        <header><div><p className="eyebrow">CONTEXTUAL COPILOT</p><h3>Grounded enterprise reasoning</h3></div><div className="ai-provider-status"><span className={models.configured ? "ai-state connected" : "ai-state"}>{models.configured ? `OpenRouter ${models.masked_key || ""}` : "Provider required"}</span>{models.configured ? <button type="button" onClick={() => setEditingProvider(true)}>Replace token</button> : null}</div></header>
        <div className="ai-prompt-deck">{prompts.map(([label, prompt]) => <button key={label} onClick={() => setQuestion(prompt)}><span>{label}</span><small>{prompt}</small></button>)}</div>
        <textarea value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="Ask about a repository, control, incident, dependency, or decision..." />
        <footer><select aria-label="Free NVIDIA model" value={model} onChange={(event) => setModel(event.target.value)}>{(models.models || []).map((item) => <option key={item} value={item}>NVIDIA {modelLabel(item)} / free</option>)}</select><button onClick={ask} disabled={busy || !models.configured || !model || question.trim().length < 3}>{busy ? "Grounding answer..." : "Ask with evidence"}</button></footer>
        {!models.configured || editingProvider ? <form className="ai-provider-inline" onSubmit={connect}><div><strong>{models.configured ? "Replace OpenRouter token" : "Connect OpenRouter"}</strong><small>The token is encrypted, write-only, and scoped to your account.</small></div><label><span>New API token</span><input type="password" autoComplete="new-password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder="sk-or-v1-..." required /></label><label><span>Free NVIDIA model</span><select value={model} onChange={(event) => setModel(event.target.value)} required>{(models.models || []).map((item) => <option key={item} value={item}>{modelLabel(item)}</option>)}</select></label><div className="ai-provider-actions"><button disabled={busy || !model}>{models.configured ? "Replace securely" : "Connect"}</button>{models.configured ? <button type="button" className="secondary" onClick={() => { setEditingProvider(false); setApiKey(""); }}>Cancel</button> : null}</div><small className="ai-free-notice">Zero-price NVIDIA routes only. Free endpoints can be rate limited and provider-logged; do not submit confidential data unless your OpenRouter policy permits it.</small></form> : null}
      </section>
      <aside className="ai-surface ai-answer">
        <header><p className="eyebrow">ANSWER / PROVENANCE</p>{answer ? <span style={{ "--confidence": `${Math.round(answer.confidence * 100)}%` }}>{Math.round(answer.confidence * 100)}%</span> : null}</header>
        {answer ? <><div className="ai-answer-runtime"><span>TOKENS CONSUMED<strong>{((answer.usage?.input_tokens || 0) + (answer.usage?.output_tokens || 0) + (answer.usage?.reasoning_tokens || 0)).toLocaleString()}</strong></span><span>LATENCY<strong>{answer.latency_ms >= 1000 ? `${(answer.latency_ms / 1000).toFixed(1)} S` : `${Math.round(answer.latency_ms || 0)} MS`}</strong></span><span>DELIVERY<strong>{answer.cache_hit ? "CACHE HIT" : "LIVE RESPONSE"}</strong></span></div><h3>{answer.insufficient_evidence ? "Evidence is incomplete" : "Grounded response"}</h3><p>{answer.answer}</p><div className="ai-evidence-list">{answer.evidence_bundle?.map((item) => <article key={`${item.type}:${item.id}`}><span>{item.type}</span><strong>{item.title}</strong><code>{item.id}</code></article>)}</div>{answer.warnings?.map((warning) => <small className="ai-warning" key={warning}>{warning}</small>)}</> : <div className="ai-empty"><strong>No synthetic certainty</strong><p>Answers appear here only after authorized evidence retrieval and structured-output validation.</p><span>{conversations.length} saved conversation(s)</span></div>}
      </aside>
    </div> : null}

    {view === "controls" ? <section className="ai-surface ai-controls-room">
      <DataInsightsPanel tableId={tableId} branchId={branchId} onError={onError} />
      <header><div><p className="eyebrow">CONTINUOUS CONTROL INTELLIGENCE</p><h3>Deterministic detection, AI explanation</h3></div><button onClick={scan} disabled={busy}>Scan governed signals</button></header>
      <div className="ai-control-list">{insights.map((item) => {
        const explanation = insightExplanations[item.insight_id];
        const isExplaining = explainingInsightId === item.insight_id;
        return (
          <article key={item.insight_id} className={`severity-${item.severity.toLowerCase()}`}>
            <span>{item.severity}</span>
            <div>
              <strong>{item.title}</strong>
              <p>{item.summary}</p>
              <code>{item.resource_type} / {item.resource_id}</code>
              {explanation ? <p className="ai-insight-explanation">{explanation.explanation}</p> : null}
            </div>
            <div className="ai-insight-actions">
              <b>{item.generated_by.replaceAll("_", " ")}</b>
              <button type="button" onClick={() => explainInsight(item.insight_id)} disabled={isExplaining}>
                {isExplaining ? "Explaining..." : explanation ? "Re-explain" : "Explain"}
              </button>
            </div>
          </article>
        );
      })}{!insights.length ? <div className="ai-empty"><strong>No open control signals</strong><p>Run a scan to inspect deterministic Stage 1-4 exceptions.</p></div> : null}</div>
    </section> : null}

    {view === "signals" ? <SignalsWorkspace repositoryId={repositoryId} onError={onError} /> : null}

    {view === "agents" ? <div className="ai-agent-store-layout">
      <AgentStore admin={admin} repositoryId={repositoryId} onError={onError} onRunLaunched={refresh} />
      <section className={`ai-surface ai-run-ledger ${runLedgerOpen ? "" : "collapsed"}`}>
        <button type="button" className="ai-run-ledger-toggle" onClick={() => setRunLedgerOpen((open) => !open)}>
          <p className="eyebrow">AGENT RUN LEDGER</p><span>{runs.length} runs</span><i className={`ai-collapse-caret ${runLedgerOpen ? "open" : ""}`} />
        </button>
        {runLedgerOpen ? <>
          {runs.map((run) => <article key={run.agent_run_id}><div><span>{run.status.replaceAll("_", " ")}</span><strong>{run.name}</strong><code>{run.agent_run_id}</code></div>{run.actions?.map((action) => <button key={action.action_id} onClick={() => perform(() => confirmAIAction(action.action_id), () => "Authorized action executed and audited.")} disabled={busy || action.status !== "PENDING_CONFIRMATION"}>{action.status === "PENDING_CONFIRMATION" ? `Confirm ${action.action_type.replaceAll("_", " ")}` : action.status}</button>)}</article>)}
          {!runs.length ? <div className="ai-empty"><strong>No agent runs</strong><p>Launch one from the store above.</p></div> : null}
        </> : null}
      </section>
    </div> : null}

    {view === "operations" ? <div className="ai-operations-layout">
      <section className="ai-surface ai-usage"><header><div><p className="eyebrow">FINOPS / OBSERVABILITY</p><h3>AI consumption and quality</h3></div><strong>{tokenTotal.toLocaleString()} tokens</strong></header><div className="ai-quota"><i style={{ width: `${Math.min(100, (tokenTotal / quotaTotal) * 100)}%` }} /></div><small>{Math.max(0, quotaTotal - tokenTotal).toLocaleString()} organization tokens remain in today&apos;s configured envelope</small><div className="ai-usage-facts"><span>Cache hits<b>{usage?.cache_hits || 0}</b></span><span>Invalid outputs<b>{usage?.invalid_outputs || 0}</b></span><span>Average latency<b>{Math.round(usage?.average_latency_ms || 0)} ms</b></span><span>Reasoning tokens<b>{usage?.tokens?.reasoning || 0}</b></span></div></section>
      <section className="ai-surface ai-evaluations"><header><div><p className="eyebrow">EVALUATION / RED TEAM</p><h3>Release safety baseline</h3></div><button onClick={() => perform(runAIEvaluation, (result) => `${result.passed_cases}/${result.total_cases} safety checks passed.`)} disabled={busy}>Run baseline</button></header>{evaluations.slice(0, 8).map((evaluation) => <article key={evaluation.evaluation_run_id}><span className={evaluation.status === "PASSED" ? "pass" : "fail"}>{evaluation.status}</span><div><strong>{evaluation.suite_name.replaceAll("_", " ")}</strong><small>{new Date(evaluation.started_at).toLocaleString()}</small></div><b>{evaluation.passed_cases}/{evaluation.total_cases}</b></article>)}</section>
      {admin ? <section className="ai-surface ai-admin-policy"><header><div><p className="eyebrow">ORGANIZATION POLICY</p><h3>AI remains below policy</h3></div><button onClick={updatePolicy} disabled={busy}>Save policy</button></header><div className="ai-policy-grid"><label><span>AI enabled</span><input type="checkbox" checked={Boolean(admin.settings.ai_enabled)} onChange={(event) => setAdmin({ ...admin, settings: { ...admin.settings, ai_enabled: event.target.checked } })} /></label><label><span>External provider</span><input type="checkbox" checked={Boolean(admin.settings.external_ai_enabled)} onChange={(event) => setAdmin({ ...admin, settings: { ...admin.settings, external_ai_enabled: event.target.checked } })} /></label><label><span>Agent actions</span><input type="checkbox" checked={Boolean(admin.settings.agent_actions_enabled)} onChange={(event) => setAdmin({ ...admin, settings: { ...admin.settings, agent_actions_enabled: event.target.checked } })} /></label><label><span>User daily quota</span><input type="number" value={admin.settings.user_daily_token_quota} onChange={(event) => setAdmin({ ...admin, settings: { ...admin.settings, user_daily_token_quota: event.target.value } })} /></label></div><div className="ai-model-route"><label><span>Feature route</span><input value={policyFeature} onChange={(event) => setPolicyFeature(event.target.value.toUpperCase())} /></label><label><span>Enforced model</span><select value={policyModelId} onChange={(event) => setPolicyModelId(event.target.value)}><option value="">Capability-based routing</option>{admin.models.map((item) => <option key={item.model_id} value={item.model_id}>{item.model_slug} / {item.model_role}</option>)}</select></label><button onClick={updateRoute} disabled={busy || !policyFeature.trim()}>Enforce route</button><div>{admin.policies.map((item) => <span key={item.policy_id}>{item.feature} / {item.model_role}</span>)}</div></div><footer><span>{admin.models.length} governed models</span><span>{admin.tools.length} registered tools</span><span>{admin.agents.length} bounded agents</span><button onClick={() => perform(clearAIConfig, () => "Personal provider credential disconnected.")} disabled={!models.configured}>Disconnect provider</button></footer></section> : <section className="ai-surface ai-empty"><strong>Administrative metrics are permission restricted</strong><p>Organization owners and auditors can review quotas, provider policy, and evaluations.</p></section>}
    </div> : null}

    {view === "ledger" ? <div className="ai-ledger-layout">
      <section className="ai-signal-grid ai-ledger-kpis">
        <article><span>Tokens (30d)</span><strong>{ledgerTotalTokens30d.toLocaleString()}</strong><small>Across agents and copilot chat</small></article>
        <article><span>Agent runs (30d)</span><strong>{ledgerTotalRuns30d.toLocaleString()}</strong><small>{ledgerAgents.filter((item) => item.is_agent).length} agent(s) active</small></article>
        <article><span>Agent success rate</span><strong>{ledgerOverallSuccessRate == null ? "-" : `${Math.round(ledgerOverallSuccessRate * 100)}%`}</strong><small>Across every agent run this month</small></article>
        <article><span>Reference savings (30d)</span><strong>${(ledgerBudget?.reference_savings_30d_usd ?? 0).toFixed(2)}</strong><small>vs. reference commercial-tier pricing</small></article>
      </section>

      {ledgerBudget ? <section className="ai-surface ai-ledger-budget">
        <header><div><p className="eyebrow">BUDGET / TODAY</p><h3>Daily token quota burn rate</h3></div><strong>{ledgerBudget.today_usage.toLocaleString()} / {ledgerBudget.daily_token_quota.toLocaleString()}</strong></header>
        <div className="ai-quota"><i style={{ width: `${Math.min(100, ledgerBudget.quota_pct_used * 100)}%` }} /></div>
        <small>Trailing 7-day average: {ledgerBudget.trailing_7day_avg_tokens_per_day.toLocaleString()} tokens/day &middot; projected end-of-day at current pace: {ledgerBudget.projected_end_of_day_usage.toLocaleString()} tokens</small>
        {ledgerBudget.alerts?.map((alert) => <p key={alert.type} className="ai-guardrail-note">{alert.message}</p>)}
        <p className="ai-guardrail-note ai-ledger-caveat">Agent runs are not yet subject to this quota — only direct copilot chat calls are enforced today.</p>
      </section> : null}

      <section className="ai-surface ai-ledger-trend">
        <header><div><p className="eyebrow">TREND / LAST 30 DAYS</p><h3>Daily token burn</h3></div></header>
        <div className="ledger-trend-chart">
          {ledgerTrend.map((day) => <div key={day.day} className="ledger-trend-col" title={`${day.day}: ${day.total_tokens.toLocaleString()} tokens, ${day.failures} failure(s)`}>
            <div className="ledger-trend-bar" style={{ height: `${Math.max(3, (day.total_tokens / ledgerMaxDailyTokens) * 100)}%` }} />
          </div>)}
          {!ledgerTrend.length ? <div className="empty-state compact">No AI activity recorded yet.</div> : null}
        </div>
      </section>

      <div className="ai-ledger-split">
        <section className="ai-surface ai-ledger-agents">
          <header><div><p className="eyebrow">LEADERBOARD</p><h3>Per-agent burn &amp; reliability</h3></div></header>
          <div className="ai-ledger-agent-list">
            {ledgerAgents.map((agent) => <article key={agent.agent_key} className="ai-ledger-agent-row">
              <div><strong>{agent.agent_name}</strong><small>{agent.requests} request(s) &middot; last run {agent.last_run_at ? new Date(agent.last_run_at).toLocaleString() : "never"}</small></div>
              <span className={`pill device-trust-${agent.success_rate >= 0.9 ? "trusted" : agent.success_rate >= 0.5 ? "unknown" : "blocked"}`}>{Math.round(agent.success_rate * 100)}% success</span>
              <b>{agent.total_tokens.toLocaleString()} tok</b>
              <em>${agent.reference_cost_usd.toFixed(3)}</em>
            </article>)}
            {!ledgerAgents.length ? <div className="empty-state compact">No agent activity yet.</div> : null}
          </div>
        </section>

        <section className="ai-surface ai-ledger-models">
          <header><div><p className="eyebrow">BY MODEL</p><h3>Routing mix</h3></div></header>
          <div className="ai-ledger-model-list">
            {ledgerModels.map((item) => <div key={item.model_id || "unknown"} className="ledger-model-row">
              <span>{item.display_name || item.model_slug || "Unknown model"}</span>
              <div className="contribution-track"><i style={{ width: `${Math.min(100, (item.total_tokens / (ledgerModels[0]?.total_tokens || 1)) * 100)}%` }} /></div>
              <b>{item.total_tokens.toLocaleString()}</b>
            </div>)}
            {!ledgerModels.length ? <div className="empty-state compact">No model usage yet.</div> : null}
          </div>
        </section>
      </div>

      <section className="ai-surface ai-ledger-runs">
        <header>
          <div><p className="eyebrow">LEDGER BOOK</p><h3>Every agent run, priced</h3></div>
          <div className="ai-ledger-filters">
            <select value={ledgerAgentFilter} onChange={(event) => setLedgerAgentFilter(event.target.value)}>
              <option value="">All agents</option>
              <option value="MERGE_CONFLICT_AGENT">Merge conflict agent</option>
              <option value="COMMIT_REVIEW_AGENT">Commit review agent</option>
              <option value="EUC_RISK_RADAR_AGENT">EUC risk drift radar</option>
            </select>
            <select value={ledgerStatusFilter} onChange={(event) => setLedgerStatusFilter(event.target.value)}>
              <option value="">Any status</option><option value="COMPLETED">Completed</option><option value="FAILED">Failed</option><option value="RUNNING">Running</option>
            </select>
          </div>
        </header>
        <div className="review-ledger">
          {ledgerRuns.items.map((run) => <article key={run.agent_run_id} onClick={() => openReceipt(run.agent_run_id)} className="ai-ledger-run-row">
            <span className={run.status === "COMPLETED" ? "approved" : run.status === "FAILED" ? "rejected" : ""}>{run.status.slice(0, 1)}</span>
            <div><strong>{run.agent_name}</strong><p>{run.goal}</p></div>
            <b>{run.total_tokens.toLocaleString()} tok &middot; ${run.reference_cost_usd.toFixed(3)}{run.failed_requests ? ` · ${run.failed_requests} failed call(s)` : ""}</b>
          </article>)}
          {!ledgerRuns.items.length ? <div className="empty-state compact">No runs match this filter.</div> : null}
        </div>
      </section>

      {ledgerReceipt ? <section className="ai-surface ai-ledger-receipt">
        <header><div><p className="eyebrow">RECEIPT / {ledgerReceipt.run.agent_run_id}</p><h3>{ledgerReceipt.run.agent_name}</h3></div><button onClick={() => setLedgerReceipt(null)}>Close</button></header>
        <p>{ledgerReceipt.run.goal}</p>
        <div className="merge-timeline">
          {ledgerReceipt.steps.map((step, index) => <article key={step.step_id}>
            <div className="timeline-rail"><span>{index + 1}</span></div>
            <div className="timeline-change"><header><b>{step.step_type}</b><small>{step.status}</small></header><p>{step.description}</p></div>
          </article>)}
        </div>
        <div className="ai-ledger-receipt-requests">
          <p className="ai-field-explainability-title">Priced requests</p>
          {ledgerReceipt.requests.map((request) => <div key={request.ai_request_id} className="ledger-model-row">
            <span>{request.feature} &middot; {request.status}{request.error_code ? ` (${request.error_code})` : ""}</span>
            <b>{((request.input_tokens || 0) + (request.output_tokens || 0)).toLocaleString()} tok &middot; ${request.reference_cost_usd.toFixed(3)}</b>
          </div>)}
        </div>
      </section> : null}
      {ledgerLoading ? <div className="ai-notice">Loading ledger...</div> : null}
    </div> : null}

    {view === "history" ? <div className="ai-history-layout">
      <section className="ai-surface ai-history-list">
        <header>
          <div><p className="eyebrow">CHAT HISTORY</p><h3>{conversations.length} saved conversation{conversations.length === 1 ? "" : "s"}</h3></div>
          {conversations.length > 8 ? (
            <button type="button" className="expand-list-button" onClick={() => { setHistoryExpandedFilter(""); setHistoryExpandedDateFrom(""); setHistoryExpandedDateTo(""); setHistoryExpandedPage(0); setHistoryExpanded(true); }} title="View all conversations">
              <span className="expand-list-icon" aria-hidden="true">&#x2921;</span>View all
            </button>
          ) : null}
        </header>
        {conversations.length > 8 ? (
          <input
            type="search" className="ai-history-filter" value={historyFilter}
            onChange={(event) => setHistoryFilter(event.target.value)}
            placeholder="Filter conversations..."
          />
        ) : null}
        <div className="ai-history-conversations">
          {historyFilteredConversations.map((item) => (
            <button
              key={item.conversation_id}
              className={`ai-history-conversation-row ${historyConversationId === item.conversation_id ? "active" : ""}`}
              onClick={() => openHistoryConversation(item.conversation_id)}
            >
              <strong>{item.title}</strong>
              <small>{item.resource_type}{item.resource_id ? ` / ${item.resource_id}` : ""}</small>
              <time>{new Date(item.updated_at).toLocaleString()}</time>
            </button>
          ))}
          {!conversations.length ? <div className="empty-state compact">No AI conversations yet. Ask something from the Copilot tab.</div> : null}
          {conversations.length && !historyFilteredConversations.length ? <div className="empty-state compact">Nothing matches "{historyFilter}".</div> : null}
        </div>
      </section>

      {historyExpanded ? (
        <div className="checkout-backdrop" role="presentation" onClick={() => setHistoryExpanded(false)}>
          <section className="checkout-dialog working-copies-dialog-full" role="dialog" aria-modal="true" onClick={(event) => event.stopPropagation()}>
            <button type="button" className="checkout-close" onClick={() => setHistoryExpanded(false)}>Close</button>
            <div className="panel-header">
              <div><p className="eyebrow">CHAT HISTORY</p><h2>All saved conversations</h2><p className="muted">Every grounded Copilot exchange, across every repository.</p></div>
              <span className="pill ready">{historyExpandedMatches.length} of {conversations.length} shown</span>
            </div>
            <input
              type="search" className="working-copies-search" value={historyExpandedFilter}
              onChange={(event) => { setHistoryExpandedFilter(event.target.value); setHistoryExpandedPage(0); }}
              placeholder="Filter by title, repository, or resource..."
              autoFocus
            />
            <div className="working-copies-date-filter">
              <label>From<input type="date" value={historyExpandedDateFrom} onChange={(event) => { setHistoryExpandedDateFrom(event.target.value); setHistoryExpandedPage(0); }} /></label>
              <label>To<input type="date" value={historyExpandedDateTo} onChange={(event) => { setHistoryExpandedDateTo(event.target.value); setHistoryExpandedPage(0); }} /></label>
              {historyExpandedDateFrom || historyExpandedDateTo ? (
                <button type="button" onClick={() => { setHistoryExpandedDateFrom(""); setHistoryExpandedDateTo(""); setHistoryExpandedPage(0); }}>Clear dates</button>
              ) : null}
            </div>
            <div className="ai-history-grid">
              {historyExpandedPageItems.map((item) => (
                <article
                  key={item.conversation_id}
                  className={`ai-history-card ${historyConversationId === item.conversation_id ? "active" : ""}`}
                  onClick={() => { openHistoryConversation(item.conversation_id); setHistoryExpanded(false); }}
                >
                  <strong>{item.title}</strong>
                  <small>{item.resource_type}{item.resource_id ? ` / ${item.resource_id}` : ""}</small>
                  <time>{new Date(item.updated_at).toLocaleString()}</time>
                </article>
              ))}
              {!historyExpandedMatches.length ? (
                <div className="empty-state">
                  {historyExpandedFilter.trim() ? `Nothing matches "${historyExpandedFilter}".` : "No AI conversations yet."}
                </div>
              ) : null}
            </div>
            {historyExpandedTotalPages > 1 ? (
              <div className="working-copies-pagination">
                <button type="button" className="secondary-button" disabled={historyExpandedPageSafe === 0} onClick={() => setHistoryExpandedPage((page) => Math.max(0, page - 1))}>&larr; Previous</button>
                <span className="working-copies-pagination-label">
                  Page {historyExpandedPageSafe + 1} of {historyExpandedTotalPages}
                  &nbsp;&middot;&nbsp;showing {historyExpandedPageSafe * HISTORY_PAGE_SIZE + 1}
                  &ndash;{Math.min(historyExpandedMatches.length, (historyExpandedPageSafe + 1) * HISTORY_PAGE_SIZE)} of {historyExpandedMatches.length}
                </span>
                <button type="button" className="secondary-button" disabled={historyExpandedPageSafe >= historyExpandedTotalPages - 1} onClick={() => setHistoryExpandedPage((page) => Math.min(historyExpandedTotalPages - 1, page + 1))}>Next &rarr;</button>
              </div>
            ) : null}
          </section>
        </div>
      ) : null}

      <section className="ai-surface ai-history-reader">
        {historyLoading ? <div className="ai-notice">Loading conversation...</div> : null}
        {!historyLoading && historyConversation ? (
          <article className="ai-history-doc">
            <header>
              <p className="eyebrow">{historyConversation.conversation.resource_type}{historyConversation.conversation.resource_id ? ` / ${historyConversation.conversation.resource_id}` : ""}</p>
              <h2>{historyConversation.conversation.title}</h2>
              <small>Started {new Date(historyConversation.conversation.created_at).toLocaleString()}</small>
            </header>
            <div className="ai-history-messages">
              {historyConversation.messages.map((message) => {
                const isUser = String(message.role).toLowerCase() === "user";
                return (
                <section key={message.message_id} className={`ai-history-message role-${String(message.role).toLowerCase()}`}>
                  <div className="ai-history-message-meta">
                    <b>{isUser ? "You asked" : "Git Walk AI"}</b>
                    <time>{new Date(message.created_at).toLocaleString()}</time>
                    {message.model_id ? <span>{message.model_id}</span> : null}
                  </div>
                  {isUser ? (
                    <p className="ai-history-question">{typeof message.content === "string" ? message.content : message.content?.answer || ""}</p>
                  ) : (
                    <AIAnswerView text={message.content} confidence={message.grounding_confidence} />
                  )}
                  {message.evidence_refs?.length ? (
                    <div className="ai-answer-evidence">
                      {message.evidence_refs.map((ref, index) => <span key={index}>{ref.type}<code>{ref.id}</code></span>)}
                    </div>
                  ) : null}
                </section>
                );
              })}
            </div>
          </article>
        ) : null}
        {!historyLoading && !historyConversation ? (
          <div className="ai-empty"><strong>Pick a conversation</strong><p>Every grounded Copilot exchange is saved here, reader-formatted with its evidence and confidence.</p></div>
        ) : null}
      </section>
    </div> : null}
  </div>;
}
