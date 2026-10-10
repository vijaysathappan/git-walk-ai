import React, { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import AgentRunTimeline from "./AgentRunTimeline";
import { highlightAssessmentText, summaryToBullets } from "../utils/aiText";
import {
  getDatasets, getRepositoryBranches, getMergeRequests, getBranchCommits,
  getEucAssets, getEucFindings,
  startAIAgent,
  startFormulaExplanation, getFormulaExplanation,
  startPortfolioBriefing, getPortfolioBriefing,
  startDiscovery,
  startRbacAnomalyScan, getRbacAnomalies,
  startMergeRequestAIAssessment, getMergeRequestAIAssessment,
  startMergeConflictAIAnalysis, getMergeConflictAISuggestions,
  startCommitAIReview, getCommitAIReview,
  startEucBranchComparison, getEucBranchComparison,
  startFindingRemediation, getFindingRemediations,
} from "../services/api";

/**
 * The full catalogue of every registered agent in the product, presented as
 * a Play-Store-style browsing experience instead of a bare admin list. Each
 * entry carries everything the store needs to render its card AND drive its
 * own launch flow -- `kind` selects which input-gathering + start/result
 * logic in AgentLaunchModal applies, since each agent needs genuinely
 * different context (a repo, a branch, a merge request, a commit, a
 * formula, a topic, or nothing at all).
 */
const AGENT_CATALOG = {
  FORMULA_EXPLAINER_AGENT: {
    kind: "formula", category: "Productivity", icon: "ƒ", gradient: ["#7c3aed", "#c4b5fd"],
    tagline: "Explains exactly what a formula computes, step by step.",
    detail: "Traces a cell's real dependency lineage and edit history, then explains what the formula does in plain language -- grounded in the actual referenced cells, never guessed.",
  },
  DISCOVERY_AGENT: {
    kind: "discovery", category: "Productivity", icon: "◑", gradient: ["#db2777", "#f9a8d4"],
    tagline: "Finds the right person to ask, right now.",
    detail: "Weighs real expertise, live presence, and open review workload together -- not just who's touched a sheet the most -- to recommend who to ask about a topic.",
  },
  PORTFOLIO_BRIEFING_AGENT: {
    kind: "portfolio", category: "Risk & Compliance", icon: "◈", gradient: ["#4338ca", "#a5b4fc"],
    tagline: "Org-wide EUC risk, briefed in plain language.",
    detail: "Synthesizes the entire EUC risk portfolio's deterministic scores and open findings into a manager-readable briefing, with history so you can compare against last time.",
  },
  RBAC_ANOMALY_AGENT: {
    kind: "rbac", category: "Risk & Compliance", icon: "▲", gradient: ["#b91c1c", "#fca5a5"],
    tagline: "Flags access patterns worth a second look.",
    detail: "Cross-references role grants, device trust, and real activity to surface dormant grants, role/activity mismatches, and untrusted devices in active use -- grounded in precedent from patterns you've already reviewed.",
  },
  EUC_RISK_RADAR_AGENT: {
    kind: "radar", category: "Risk & Compliance", icon: "◉", gradient: ["#9f1239", "#fda4af"],
    tagline: "Git blame for spreadsheet risk.",
    detail: "Diffs a branch's EUC risk findings against main and attributes every newly-introduced finding to the exact commit and author that caused it.",
  },
  EUC_REMEDIATION_AGENT: {
    kind: "remediation", category: "Risk & Compliance", icon: "⚑", gradient: ["#c2410c", "#fdba74"],
    tagline: "Drafts the next step for an open finding.",
    detail: "Investigates one EUC finding's full evidence and dependency impact, then drafts a recommended lifecycle status and a written reason for you to review and confirm.",
  },
  MERGE_CONFLICT_AGENT: {
    kind: "merge", category: "Version Control", icon: "⑂", gradient: ["#6d28d9", "#c4b5fd"],
    tagline: "Investigates conflicts and assesses merge risk.",
    detail: "Two capabilities in one agent: proposes confirmation-gated resolutions for open conflicts using resolution precedents, and assesses the overall risk of a merge request before you approve it.",
  },
  COMMIT_REVIEW_AGENT: {
    kind: "commit", category: "Version Control", icon: "✓", gradient: ["#15803d", "#86efac"],
    tagline: "Explains a commit's risk, right after it lands.",
    detail: "Explains a personal-branch commit's deterministic risk score and investigates any fields it cleared -- runs strictly after the commit succeeds, never gates it.",
  },
  INVESTIGATION_AGENT: {
    kind: "generic", category: "Investigation", icon: "◎", gradient: ["#0969da", "#8ec9ff"],
    tagline: "Evidence-backed investigation, no source changes.",
    detail: "Builds an evidence-backed explanation of any question across governed context, citing only authorized, retrieved evidence.",
  },
  INTEGRATION_OPERATIONS_AGENT: {
    kind: "generic", category: "Investigation", icon: "⇄", gradient: ["#0e7490", "#67e8f9"],
    tagline: "Diagnoses integration incidents.",
    detail: "Diagnoses integration freshness, reconciliation, conflicts, and dead-letter health, and can prepare a confirmation-gated recovery action.",
  },
  CONTROL_REMEDIATION_AGENT: {
    kind: "generic", category: "Investigation", icon: "■", gradient: ["#a16207", "#fde047"],
    tagline: "Explains control failures, drafts fixes.",
    detail: "Explains deterministic control failures with cited evidence and drafts a remediation plan for review.",
  },
};

const CATEGORY_ORDER = ["Productivity", "Version Control", "Risk & Compliance", "Investigation"];

function AgentLogo({ meta, size = 44 }) {
  return (
    <span
      className="agent-store-logo"
      style={{ width: size, height: size, fontSize: size * 0.46, background: `linear-gradient(135deg, ${meta.gradient[0]}, ${meta.gradient[1]})` }}
    >
      {meta.icon}
    </span>
  );
}

function useOptions(fetcher, deps, unwrap) {
  const [options, setOptions] = useState([]);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (deps.some((dep) => !dep)) { setOptions([]); return; }
    let cancelled = false;
    setLoading(true);
    fetcher().then((result) => { if (!cancelled) setOptions(unwrap(result) || []); })
      .catch(() => { if (!cancelled) setOptions([]); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return [options, loading];
}

function AgentLaunchModal({ agentKey, dbAgent, defaultTableId, onClose, onError, onRunLaunched }) {
  const meta = AGENT_CATALOG[agentKey];
  const [tableId, setTableId] = useState(defaultTableId || "");
  const [branchId, setBranchId] = useState("");
  const [mergeRequestId, setMergeRequestId] = useState("");
  const [mergeAction, setMergeAction] = useState("assess");
  const [commitId, setCommitId] = useState("");
  const [eucId, setEucId] = useState("");
  const [findingId, setFindingId] = useState("");
  const [topic, setTopic] = useState("");
  const [goal, setGoal] = useState(dbAgent?.default_goal || "Investigate the most important open exception and provide a grounded remediation plan.");
  const [sheetName, setSheetName] = useState("");
  const [cellAddress, setCellAddress] = useState("");
  const [formula, setFormula] = useState("");
  const [busy, setBusy] = useState(false);
  const [runId, setRunId] = useState(null);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  // What the backend actually resolved a blank formula/cell/sheet down to
  // (auto-discovery) or echoed back if the user typed them -- read by
  // onDone for the follow-up GET, since component state could still be
  // blank if the user relied on auto-discovery.
  const resolvedFormulaRef = useRef({ sheetName: "", cellAddress: "", formula: "" });

  const [repos] = useOptions(getDatasets, [true], (r) => r.datasets);
  const needsRepo = ["formula", "discovery", "rbac", "merge", "commit", "radar", "remediation"].includes(meta.kind);
  const [branches] = useOptions(() => getRepositoryBranches(tableId), [tableId, needsRepo], (r) => r.branches);
  const [mergeRequests] = useOptions(() => getMergeRequests(tableId), [tableId, meta.kind === "merge"], (r) => r.merge_requests);
  const [commits] = useOptions(() => getBranchCommits(branchId, 50), [branchId, meta.kind === "commit"], (r) => r.commits);
  const [assets] = useOptions(() => getEucAssets(tableId), [tableId, meta.kind === "remediation"], (r) => r.assets);
  const [findings] = useOptions(() => getEucFindings(eucId, {}), [eucId, meta.kind === "remediation"], (r) => r.items);

  // Auto-pick a sensible default the moment a list loads, so a repository
  // (and sometimes nothing else) is the only choice a user actually has to
  // make -- they can still override any of these before running.
  useEffect(() => { if (branches.length && !branchId && (meta.kind === "formula" || meta.kind === "radar" || meta.kind === "commit")) setBranchId((meta.kind === "radar" ? branches.find((b) => b.branch_type === "USER") : branches[0])?.branch_id || ""); }, [branches, branchId, meta.kind]);
  useEffect(() => { if (mergeRequests.length && !mergeRequestId && meta.kind === "merge") setMergeRequestId(mergeRequests.find((mr) => mr.status !== "MERGED")?.merge_request_id || ""); }, [mergeRequests, mergeRequestId, meta.kind]);
  useEffect(() => { if (commits.length && !commitId && meta.kind === "commit") setCommitId(commits[0].commit_id); }, [commits, commitId, meta.kind]);
  useEffect(() => { if (assets.length && !eucId && meta.kind === "remediation") setEucId(assets[0].euc_id); }, [assets, eucId, meta.kind]);
  useEffect(() => { if (findings.length && !findingId && meta.kind === "remediation") setFindingId(findings[0].finding_id); }, [findings, findingId, meta.kind]);

  const canRun = (() => {
    switch (meta.kind) {
      case "formula": return Boolean(branchId);
      case "discovery": return Boolean(tableId);
      case "rbac": return Boolean(tableId);
      case "merge": return Boolean(mergeRequestId);
      case "commit": return Boolean(commitId);
      case "radar": return Boolean(branchId);
      case "remediation": return Boolean(findingId);
      case "generic": return goal.trim().length > 2;
      case "portfolio": return true;
      default: return false;
    }
  })();

  const run = async () => {
    setBusy(true); setError(""); setResult(null); setRunId(null);
    try {
      let started;
      if (meta.kind === "formula") {
        started = await startFormulaExplanation(tableId, {
          branch_id: branchId, sheet_name: sheetName.trim() || null,
          cell_address: cellAddress.trim() ? cellAddress.trim().toUpperCase() : null,
          formula: formula.trim() || null,
        });
        resolvedFormulaRef.current = { sheetName: started.sheet_name, cellAddress: started.cell_address, formula: started.formula };
        setSheetName(started.sheet_name); setCellAddress(started.cell_address); setFormula(started.formula);
      } else if (meta.kind === "discovery") {
        started = await startDiscovery(tableId, topic.trim());
      } else if (meta.kind === "rbac") {
        started = await startRbacAnomalyScan(tableId);
      } else if (meta.kind === "merge") {
        started = mergeAction === "assess" ? await startMergeRequestAIAssessment(mergeRequestId) : await startMergeConflictAIAnalysis(mergeRequestId);
      } else if (meta.kind === "commit") {
        started = await startCommitAIReview(commitId);
      } else if (meta.kind === "radar") {
        started = await startEucBranchComparison(tableId, branchId);
      } else if (meta.kind === "remediation") {
        started = await startFindingRemediation(eucId, findingId);
      } else if (meta.kind === "generic") {
        started = await startAIAgent({ agent_key: agentKey, goal: goal.trim(), repository_id: tableId || null, resource_type: tableId ? "REPOSITORY" : "ORGANIZATION", resource_id: tableId || null });
      } else if (meta.kind === "portfolio") {
        started = await startPortfolioBriefing();
      }
      if (started.status === "COMPLETED") { setResult(started); setBusy(false); }
      else setRunId(started.agent_run_id);
      onRunLaunched?.();
    } catch (err) {
      setBusy(false);
      setError(err.message);
      onError?.(err.message);
    }
  };

  const onDone = async (progress) => {
    setBusy(false);
    onRunLaunched?.();
    if (progress.run.status === "FAILED") { setError("This run failed — the AI provider may be unavailable right now."); return; }
    try {
      if (meta.kind === "formula") {
        const resolved = resolvedFormulaRef.current;
        setResult(await getFormulaExplanation(tableId, { branch_id: branchId, sheet_name: resolved.sheetName, cell_address: resolved.cellAddress, formula: resolved.formula }));
      }
      else if (meta.kind === "discovery") setResult(progress.result);
      else if (meta.kind === "rbac") setResult({ findings: (await getRbacAnomalies(tableId)).findings });
      else if (meta.kind === "merge" && mergeAction === "assess") setResult((await getMergeRequestAIAssessment(mergeRequestId)).assessment);
      else if (meta.kind === "merge") setResult({ suggestions: (await getMergeConflictAISuggestions(mergeRequestId)).suggestions });
      else if (meta.kind === "commit") setResult((await getCommitAIReview(commitId)).review);
      else if (meta.kind === "radar") setResult((await getEucBranchComparison(tableId, branchId)).comparison);
      else if (meta.kind === "remediation") setResult((await getFindingRemediations(eucId, findingId)).remediations?.[0]);
      else if (meta.kind === "portfolio") setResult(await getPortfolioBriefing());
    } catch (err) { setError(err.message); }
  };

  return createPortal(
    <div className="checkout-backdrop" onClick={onClose}>
      <div className="checkout-dialog agent-store-modal" onClick={(event) => event.stopPropagation()}>
        <button type="button" className="checkout-close" onClick={onClose}>Close</button>
        <div className="agent-store-modal-header">
          <AgentLogo meta={meta} size={52} />
          <div><span className="pill">{meta.category}</span><h3>{dbAgent?.name || agentKey.replaceAll("_", " ")}</h3><p>{meta.detail}</p></div>
        </div>

        {!runId && !result ? (
          <div className="agent-store-inputs">
            {needsRepo ? (
              <label>Repository<select value={tableId} onChange={(event) => { setTableId(event.target.value); setBranchId(""); setMergeRequestId(""); setCommitId(""); setEucId(""); setFindingId(""); }}>
                <option value="">Select a repository...</option>
                {repos.map((repo) => <option key={repo.table_id} value={repo.table_id}>{repo.repository_name}</option>)}
              </select></label>
            ) : null}

            {meta.kind === "formula" || meta.kind === "radar" ? (
              <label>Branch<select value={branchId} onChange={(event) => setBranchId(event.target.value)} disabled={!tableId}>
                <option value="">{tableId ? "Select a branch..." : "Pick a repository first"}</option>
                {branches.filter((b) => meta.kind === "radar" ? b.branch_type === "USER" : true).map((b) => <option key={b.branch_id} value={b.branch_id}>{b.branch_name}</option>)}
              </select></label>
            ) : null}

            {meta.kind === "formula" ? (
              <>
                <p className="muted agent-store-optional-note">Leave the fields below blank and the agent will pick the first real formula it finds on this branch — fill them in only if you want a specific one explained.</p>
                <label>Sheet name (optional)<input value={sheetName} onChange={(event) => setSheetName(event.target.value)} placeholder="e.g. Data — blank picks automatically" disabled={!branchId} /></label>
                <label>Cell address (optional)<input value={cellAddress} onChange={(event) => setCellAddress(event.target.value)} placeholder="e.g. B7 — blank picks automatically" disabled={!branchId} /></label>
                <label>Formula (optional)<textarea value={formula} onChange={(event) => setFormula(event.target.value)} placeholder="=VLOOKUP(A2,Sheet2!A:C,3,FALSE) — blank picks automatically" disabled={!branchId} /></label>
              </>
            ) : null}

            {meta.kind === "discovery" ? (
              <label>What do you need help with? (optional)<input value={topic} onChange={(event) => setTopic(event.target.value)} placeholder="e.g. the settlement mapping sheet — blank ranks overall expertise" disabled={!tableId} /></label>
            ) : null}

            {meta.kind === "merge" ? (
              <>
                <label>Merge request<select value={mergeRequestId} onChange={(event) => setMergeRequestId(event.target.value)} disabled={!tableId}>
                  <option value="">{tableId ? "Select a merge request..." : "Pick a repository first"}</option>
                  {mergeRequests.filter((mr) => mr.status !== "MERGED").map((mr) => <option key={mr.merge_request_id} value={mr.merge_request_id}>{mr.title}</option>)}
                </select></label>
                <div className="agent-store-toggle">
                  <button type="button" className={mergeAction === "assess" ? "active" : ""} onClick={() => setMergeAction("assess")}>Assess risk</button>
                  <button type="button" className={mergeAction === "analyze" ? "active" : ""} onClick={() => setMergeAction("analyze")}>Analyze conflicts</button>
                </div>
              </>
            ) : null}

            {meta.kind === "commit" ? (
              <>
                <label>Branch<select value={branchId} onChange={(event) => { setBranchId(event.target.value); setCommitId(""); }} disabled={!tableId}>
                  <option value="">{tableId ? "Select a branch..." : "Pick a repository first"}</option>
                  {branches.map((b) => <option key={b.branch_id} value={b.branch_id}>{b.branch_name}</option>)}
                </select></label>
                <label>Commit<select value={commitId} onChange={(event) => setCommitId(event.target.value)} disabled={!branchId}>
                  <option value="">{branchId ? "Select a commit..." : "Pick a branch first"}</option>
                  {commits.map((c) => <option key={c.commit_id} value={c.commit_id}>{(c.message || "").slice(0, 60)}</option>)}
                </select></label>
              </>
            ) : null}

            {meta.kind === "remediation" ? (
              <>
                <label>EUC asset<select value={eucId} onChange={(event) => { setEucId(event.target.value); setFindingId(""); }} disabled={!tableId}>
                  <option value="">{tableId ? "Select an asset..." : "Pick a repository first"}</option>
                  {assets.map((a) => <option key={a.euc_id} value={a.euc_id}>{a.original_filename}</option>)}
                </select></label>
                <label>Finding<select value={findingId} onChange={(event) => setFindingId(event.target.value)} disabled={!eucId}>
                  <option value="">{eucId ? "Select a finding..." : "Pick an asset first"}</option>
                  {findings.map((f) => <option key={f.finding_id} value={f.finding_id}>{f.title}</option>)}
                </select></label>
              </>
            ) : null}

            {meta.kind === "generic" ? (
              <>
                <label>Repository (optional)<select value={tableId} onChange={(event) => setTableId(event.target.value)}>
                  <option value="">Organization-wide</option>
                  {repos.map((repo) => <option key={repo.table_id} value={repo.table_id}>{repo.repository_name}</option>)}
                </select></label>
                <label>Goal<textarea value={goal} onChange={(event) => setGoal(event.target.value)} /></label>
              </>
            ) : null}

            {meta.kind === "portfolio" ? <p className="muted">No setup needed — this briefs your whole organization's EUC risk portfolio.</p> : null}

            <button type="button" className="ai-run" onClick={run} disabled={busy || !canRun}>{busy ? "Launching..." : "Run agent"}</button>
            {error ? <p className="ai-warning">{error}</p> : null}
          </div>
        ) : null}

        {runId ? <AgentRunTimeline runId={runId} onDone={onDone} onError={setError} /> : null}
        {error && (runId || result) ? <p className="ai-warning">{error}</p> : null}
        {result ? <AgentResultView kind={meta.kind} result={result} /> : null}
      </div>
    </div>,
    document.body,
  );
}

/** One highlighted, scannable line -- the building block every result view
 * below is made of, instead of a wall of plain prose. Free text gets split
 * into sentences and each sentence gets its key/technical terms (risk
 * levels, recommendations, cell references, function names) visually
 * called out via the shared aiText highlighter, so a reader can scan the
 * bold/marked words first and only read the sentence around them when they
 * need to. */
function Bullets({ text }) {
  const bullets = summaryToBullets(text);
  if (!bullets.length) return null;
  return <ul className="agent-answer-bullets">{bullets.map((sentence, index) => <li key={index}>{highlightAssessmentText(sentence)}</li>)}</ul>;
}

/** A single grounding cross-reference: what the AI's claim is actually
 * backed by, shown as a small citation chip rather than left implicit --
 * the "show your work" counterpart to every bulleted claim above it. */
function ReferenceChip({ label, detail }) {
  return (
    <div className="agent-answer-reference">
      <span className="agent-answer-reference-label">{label}</span>
      {detail ? <span className="agent-answer-reference-detail">{detail}</span> : null}
    </div>
  );
}

/** A connected pipeline diagram for genuinely SEQUENTIAL AI output (this
 * step happens, then that one, then this) -- e.g. how a formula's inputs
 * combine into its result. Reading "step 1 -> step 2 -> step 3" as a
 * top-to-bottom flow of connected boxes is faster to scan than the same
 * content as a wall of numbered prose, and makes the actual DEPENDENCY
 * order of the AI's reasoning visible instead of implicit. Hand-rolled
 * SVG/CSS, matching this codebase's zero-chart-library convention. */
function StepFlow({ steps }) {
  if (!steps?.length) return null;
  return (
    <div className="step-flow">
      {steps.map((step, index) => (
        <div key={index} className="step-flow-item">
          <div className="step-flow-node">
            <span className="step-flow-index">{index + 1}</span>
            <div className="step-flow-content">
              <strong>{highlightAssessmentText(step.title)}</strong>
              <p>{highlightAssessmentText(step.explanation)}</p>
            </div>
          </div>
          {index < steps.length - 1 ? (
            <svg className="step-flow-connector" viewBox="0 0 24 28" aria-hidden="true">
              <line x1="12" y1="0" x2="12" y2="20" stroke="currentColor" strokeWidth="2" />
              <path d="M6 16 L12 24 L18 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          ) : null}
        </div>
      ))}
    </div>
  );
}

/** The collapsible "bubble" every result renders inside: a compact header
 * (badge + headline) that's always visible even when space is tight, and a
 * full bulleted+referenced body that expands on click -- so a busy screen
 * gets one glanceable line, and someone who wants the full detail gets it
 * without leaving the page. */
function AnswerBubble({ badge, badgeTone = "neutral", headline, defaultExpanded = true, children }) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  return (
    <div className={`agent-answer-bubble ${expanded ? "expanded" : "collapsed"}`}>
      <button type="button" className="agent-answer-bubble-header" onClick={() => setExpanded((value) => !value)}>
        {badge ? <span className={`agent-answer-badge tone-${badgeTone}`}>{badge}</span> : null}
        <strong>{headline}</strong>
        <span className="agent-answer-bubble-toggle">{expanded ? "Collapse" : "Expand"}</span>
      </button>
      {expanded ? <div className="agent-answer-bubble-body">{children}</div> : null}
    </div>
  );
}

function riskTone(level) {
  const normalized = String(level || "").toUpperCase();
  if (normalized === "CRITICAL" || normalized === "HIGH" || normalized === "VERY_HIGH") return "high";
  if (normalized === "MEDIUM") return "medium";
  return "low";
}

function AgentResultView({ kind, result }) {
  if (!result) return null;

  if (kind === "formula") {
    const hasSteps = (result.steps || []).length > 0;
    return (
      <div className="agent-store-result">
        {result.cache_hit ? <small className="muted">Served instantly from a prior explanation of this exact formula.</small> : null}
        <AnswerBubble badge="Explanation" badgeTone="neutral" headline={result.summary || `What this formula does, in ${result.steps?.length || 0} step(s)`}>
          {hasSteps ? <StepFlow steps={result.steps} /> : <p className="muted">No step-by-step breakdown was returned for this formula.</p>}
          {(result.referenced_cells || []).length ? (
            <div className="agent-answer-references">
              <h5>References — cells this formula actually depends on</h5>
              {result.referenced_cells.map((cell) => (
                <ReferenceChip
                  key={`${cell.sheet_id}:${cell.row_id}:${cell.column_id}`}
                  label={cell.cell_address}
                  detail={`current value ${JSON.stringify(cell.current_value)}${cell.changed_by ? ` · last changed by ${cell.changed_by}` : " · no edit history on record"}`}
                />
              ))}
            </div>
          ) : null}
        </AnswerBubble>
        {result.risk_note ? (
          <AnswerBubble badge="Worth knowing" badgeTone="medium" headline="A gotcha worth reading" defaultExpanded>
            <Bullets text={result.risk_note} />
          </AnswerBubble>
        ) : null}
      </div>
    );
  }

  if (kind === "discovery") {
    const candidates = result.candidates || [];
    return (
      <div className="agent-store-result">
        <AnswerBubble badge="Recommendation" badgeTone="neutral" headline={candidates[0] ? `Ask ${candidates[0].display_name}` : "No recommendation yet"}>
          <Bullets text={result.rationale} />
          {candidates.length ? (
            <div className="agent-answer-references">
              <h5>Ranked candidates</h5>
              {candidates.map((candidate, index) => (
                <ReferenceChip
                  key={candidate.user_id}
                  label={`${index + 1}. ${candidate.display_name}`}
                  detail={`${candidate.is_active_now ? "Online now" : "Offline"} · ${candidate.open_merge_requests} open review(s) · relevance ${candidate.relevant_score}`}
                />
              ))}
            </div>
          ) : <p className="muted">Nobody has recorded expertise here yet.</p>}
        </AnswerBubble>
      </div>
    );
  }

  if (kind === "rbac") {
    const findings = result.findings || [];
    return (
      <div className="agent-store-result">
        {findings.length ? findings.map((finding) => (
          <AnswerBubble
            key={finding.finding_id} badge={finding.severity} badgeTone={riskTone(finding.severity)}
            headline={finding.anomaly_type?.replaceAll("_", " ")} defaultExpanded={findings.length === 1}
          >
            <Bullets text={finding.explanation} />
          </AnswerBubble>
        )) : <p className="muted">No anomalies found this scan.</p>}
      </div>
    );
  }

  if (kind === "merge" && result.suggestions) {
    const suggestions = result.suggestions;
    return (
      <div className="agent-store-result">
        {suggestions.length ? suggestions.map((suggestion) => (
          <AnswerBubble
            key={suggestion.suggestion_id} badge={suggestion.risk_level} badgeTone={riskTone(suggestion.risk_level)}
            headline={suggestion.resolution_type?.replaceAll("_", " ")} defaultExpanded={suggestions.length === 1}
          >
            <Bullets text={suggestion.rationale} />
            {(suggestion.precedents || []).length ? (
              <div className="agent-answer-references">
                <h5>Grounded in {suggestion.precedents.length} resolution precedent(s)</h5>
                {suggestion.precedents.map((docId) => <ReferenceChip key={docId} label={docId} />)}
              </div>
            ) : null}
          </AnswerBubble>
        )) : <p className="muted">No open conflicts to resolve.</p>}
      </div>
    );
  }

  if (kind === "merge" || kind === "commit") {
    const badge = (result.recommendation || result.risk_level || "").replaceAll("_", " ");
    return (
      <div className="agent-store-result">
        <AnswerBubble badge={result.risk_level} badgeTone={riskTone(result.risk_level)} headline={`${badge} · ${Math.round((result.confidence || 0) * 100)}% confidence`}>
          <Bullets text={result.summary} />
          {(result.risk_breakdown || []).length ? (
            <div className="agent-answer-references">
              <h5>Risk breakdown</h5>
              {result.risk_breakdown.map((component) => (
                <ReferenceChip key={component.dimension} label={component.dimension?.replaceAll("_", " ")} detail={`${component.score}/100 (weight ${component.weight}) — ${component.explanation}`} />
              ))}
            </div>
          ) : null}
          {(result.field_investigations || []).length ? (
            <div className="agent-answer-references">
              <h5>Cleared field(s) investigated</h5>
              {result.field_investigations.map((field, index) => (
                <ReferenceChip key={index} label={field.column_name} detail={field.explanation || `previously ${JSON.stringify(field.previous_value)}`} />
              ))}
            </div>
          ) : null}
        </AnswerBubble>
      </div>
    );
  }

  if (kind === "radar") {
    const delta = result.risk_score_delta;
    return (
      <div className="agent-store-result">
        <AnswerBubble
          badge={delta > 0 ? "Risk increased" : delta < 0 ? "Risk decreased" : "No change"}
          badgeTone={delta > 0 ? "high" : "low"}
          headline={`Risk score delta vs main: ${delta > 0 ? "+" : ""}${delta}`}
        >
          <Bullets text={result.summary} />
          {(result.findings_introduced || []).length ? (
            <div className="agent-answer-references">
              <h5>Newly introduced finding(s)</h5>
              {result.findings_introduced.map((finding) => {
                const attribution = (result.attribution || []).find((item) => item.finding_id === finding.finding_id);
                return (
                  <ReferenceChip
                    key={finding.finding_id} label={finding.title || finding.rule_id}
                    detail={attribution ? `introduced by ${attribution.author_email} in commit ${attribution.commit_id?.slice(0, 12)}` : "could not be attributed to a single commit"}
                  />
                );
              })}
            </div>
          ) : null}
        </AnswerBubble>
      </div>
    );
  }

  if (kind === "remediation") {
    return (
      <div className="agent-store-result">
        <AnswerBubble badge={result.risk_level} badgeTone={riskTone(result.risk_level)} headline={`${result.recommended_status?.replaceAll("_", " ")} · ${Math.round((result.confidence || 0) * 100)}% confidence`}>
          <Bullets text={result.reason} />
        </AnswerBubble>
      </div>
    );
  }

  if (kind === "portfolio") {
    return (
      <div className="agent-store-result">
        <AnswerBubble badge="Briefing" badgeTone="neutral" headline="Portfolio risk briefing">
          <Bullets text={result.narrative} />
          {(result.observations || []).length ? (
            <ul className="agent-answer-bullets">{result.observations.map((observation, index) => <li key={index}>{highlightAssessmentText(observation)}</li>)}</ul>
          ) : null}
        </AnswerBubble>
      </div>
    );
  }

  return (
    <div className="agent-store-result">
      <AnswerBubble badge="Answer" badgeTone="neutral" headline="Grounded response">
        <Bullets text={result.answer} />
        {(result.evidence || []).length ? (
          <div className="agent-answer-references">
            <h5>Cited evidence</h5>
            {result.evidence.map((item) => <ReferenceChip key={`${item.type}:${item.id}`} label={item.type} detail={item.id} />)}
          </div>
        ) : null}
      </AnswerBubble>
    </div>
  );
}

/**
 * Play-Store-style catalogue of every registered agent in the product.
 * Clicking a card opens AgentLaunchModal, which gathers exactly the context
 * that ONE agent needs (a repo, a branch, a merge request, a formula, a
 * topic, or nothing) and drives it through its own start_xxx endpoint with
 * the same live-progress timeline every agent in this product now shares.
 */
export default function AgentStore({ admin, repositoryId, onError, onRunLaunched }) {
  const [openAgentKey, setOpenAgentKey] = useState(null);
  const dbAgents = admin?.agents || [];
  const byKey = Object.fromEntries(dbAgents.map((agent) => [agent.agent_key, agent]));
  const available = Object.keys(AGENT_CATALOG).filter((key) => byKey[key]);

  const grouped = CATEGORY_ORDER.map((category) => ({
    category,
    agents: available.filter((key) => AGENT_CATALOG[key].category === category),
  })).filter((group) => group.agents.length);

  return (
    <section className="ai-surface agent-store">
      <header className="agent-store-header">
        <div><p className="eyebrow">AGENT STORE</p><h3>{available.length} agents, ready to run</h3><p className="muted">Every AI capability in Git Walk, in one place — pick one, give it what it needs, and watch it work.</p></div>
      </header>
      {grouped.map((group) => (
        <div key={group.category} className="agent-store-category">
          <h4>{group.category}</h4>
          <div className="agent-store-grid">
            {group.agents.map((key) => {
              const meta = AGENT_CATALOG[key];
              const dbAgent = byKey[key];
              return (
                <button key={key} type="button" className="agent-store-card" onClick={() => setOpenAgentKey(key)}>
                  <AgentLogo meta={meta} />
                  <strong>{dbAgent.name}</strong>
                  <small>{meta.tagline}</small>
                  <span className="agent-store-level">{dbAgent.ai_level || "A2"}</span>
                </button>
              );
            })}
          </div>
        </div>
      ))}
      {openAgentKey ? (
        <AgentLaunchModal
          agentKey={openAgentKey} dbAgent={byKey[openAgentKey]} defaultTableId={repositoryId}
          onClose={() => setOpenAgentKey(null)} onError={onError} onRunLaunched={onRunLaunched}
        />
      ) : null}
    </section>
  );
}
