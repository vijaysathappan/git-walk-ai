import React, { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  confirmMacroRun,
  deleteMacroRecipe,
  extractRepositoryMacros,
  getMacroExplanation,
  getMacroSource,
  getRepositoryMacros,
  getRepositoryRecipes,
  prepareMacroRun,
  registerMacroSource,
  runMacroRecipe,
  saveMacroRecipe,
  startMacroExplanation,
} from "../services/api";
import AgentRunTimeline from "./AgentRunTimeline";
import AIAnswerView from "./AIAnswerView";

const RISK_META = {
  RUNNABLE: { label: "Runnable", tone: "good" },
  BLOCKED_EXTERNAL: { label: "Blocked", tone: "bad" },
  BLOCKED_UNSUPPORTED: { label: "Not runnable yet", tone: "warn" },
  PENDING: { label: "Analyzing...", tone: "neutral" },
};

const LANE_META = {
  SQL: { label: "SQL lane", title: "Recognized as a per-row calculation — computed in one bulk step, no row-by-row execution." },
  INTERPRETED: { label: "Interpreted", title: "Executed statement by statement against a safe, closed-world copy of this workbook." },
};

function toneClass(tone) {
  return tone === "good" ? "trusted" : tone === "bad" ? "blocked" : "unknown";
}

const OP_ICON = {
  CELL_VALUE_UPDATE: "▣", CELL_FORMULA_UPDATE: "ƒ", ROW_INSERT: "+", ROW_DELETE: "−",
  COLUMN_INSERT: "+", COLUMN_DELETE: "−", SHEET_CREATE: "⌘", SHEET_RENAME: "✎",
};

function RunPreviewModal({ tableId, macro, run, onClose, onConfirmed, onError }) {
  const [commitMessage, setCommitMessage] = useState(run.commit_message || "");
  const [phase, setPhase] = useState("review"); // review | committing | success
  const [progress, setProgress] = useState(0);
  const busy = phase !== "review";

  const confirm = async () => {
    setPhase("committing");
    setProgress(6);
    const tick = setInterval(() => {
      setProgress((prev) => (prev < 90 ? prev + Math.max(1, (90 - prev) / 8) : prev));
    }, 140);
    try {
      const result = await confirmMacroRun(tableId, run.run_id, commitMessage);
      clearInterval(tick);
      setProgress(100);
      setPhase("success");
      setTimeout(() => onConfirmed(result), 700);
    } catch (err) {
      clearInterval(tick);
      setProgress(0);
      setPhase("review");
      onError?.(err.message);
    }
  };

  return createPortal(
    <div className="checkout-backdrop" onClick={busy ? undefined : onClose}>
      <div className="checkout-dialog mutation-ledger-dialog run-preview-dialog" onClick={(event) => event.stopPropagation()}>
        <button type="button" className="checkout-close" onClick={onClose} disabled={busy}>Close</button>
        <div className="panel-header">
          <div>
            <p className="eyebrow">VIRTUAL RUN / PREVIEW</p>
            <h2>{macro.proc_name}</h2>
            <p className="muted">Nothing has been written yet. Review the exact cell changes below, then confirm to commit them to your branch.</p>
          </div>
          <span className="pill">{run.preview_change_count} change{run.preview_change_count === 1 ? "" : "s"}</span>
        </div>
        <label>Commit message<input value={commitMessage} onChange={(event) => setCommitMessage(event.target.value)} maxLength={300} disabled={busy} /></label>
        <div className={`semantic-diff-table run-preview-diff${busy ? " dimmed" : ""}`}>
          {run.preview.map((change, index) => (
            <article key={index} className="run-preview-change">
              <span className="run-preview-op-icon" title={(change.operation_type || "").replaceAll("_", " ")}>{OP_ICON[change.operation_type] || "▣"}</span>
              <div className="run-preview-change-meta">
                <b>{(change.operation_type || "cell value update").replaceAll("_", " ").toLowerCase()}</b>
                <code>row {change.row_id} / col {change.column_id}</code>
              </div>
              <span className="run-preview-values">
                <del>{change.old_value == null || change.old_value === "" ? <i className="diff-empty">empty</i> : String(change.old_value)}</del>
                <i>&rarr;</i>
                <ins>{change.new_value == null || change.new_value === "" ? <i className="diff-empty">empty</i> : String(change.new_value)}</ins>
              </span>
            </article>
          ))}
        </div>
        {phase === "review" ? (
          <div className="conflict-list footer resolution-note" style={{ display: "flex", gap: 8, marginTop: 14 }}>
            <button type="button" className="secondary-button" onClick={onClose}>Cancel</button>
            <button type="button" className="primary-button" onClick={confirm}>Confirm &amp; commit</button>
          </div>
        ) : (
          <div className="run-commit-progress">
            <div className="run-commit-progress-track">
              <div className={`run-commit-progress-fill${phase === "success" ? " done" : ""}`} style={{ width: `${progress}%` }} />
            </div>
            <span className="run-commit-progress-label">
              {phase === "success" ? <><span className="run-commit-success-check">&#10003;</span> Committed</> : `Committing... ${Math.round(progress)}%`}
            </span>
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}

function explanationToAnswerShape(result) {
  const actions = (result.steps || []).map((step) => ({ title: step.title, rationale: step.explanation, action_type: "STEP" }));
  if (result.risk_note) actions.push({ title: "Risk", rationale: result.risk_note, risk_level: "HIGH" });
  return { answer: result.summary, recommended_actions: actions };
}

function MacroCard({ macro, tableId, branchId, canRun, canExplain, onError, onRunPrepared, onRecipeSaved }) {
  const [expanded, setExpanded] = useState(false);
  const [source, setSource] = useState(null);
  const [running, setRunning] = useState(false);
  const [explainRunId, setExplainRunId] = useState(null);
  const [explanation, setExplanation] = useState(null);
  const [explaining, setExplaining] = useState(false);
  const [savingRecipe, setSavingRecipe] = useState(false);
  const [recipeName, setRecipeName] = useState("");
  const meta = RISK_META[macro.static_risk] || RISK_META.PENDING;

  const saveRecipe = async () => {
    if (!recipeName.trim()) return;
    setSavingRecipe(true);
    try {
      await saveMacroRecipe(tableId, macro.macro_id, recipeName.trim());
      setRecipeName("");
      onRecipeSaved?.();
    } catch (err) {
      onError?.(err.message);
    } finally {
      setSavingRecipe(false);
    }
  };

  const toggle = async () => {
    const next = !expanded;
    setExpanded(next);
    if (next && source == null) {
      try {
        const result = await getMacroSource(tableId, macro.macro_id);
        setSource(result.source);
      } catch (err) {
        onError?.(err.message);
      }
    }
  };

  const run = async () => {
    if (!branchId) {
      onError?.("Select a personal branch above before running a macro.");
      return;
    }
    setRunning(true);
    try {
      const prepared = await prepareMacroRun(tableId, macro.macro_id, branchId);
      onRunPrepared(macro, prepared);
    } catch (err) {
      onError?.(err.message);
    } finally {
      setRunning(false);
    }
  };

  const explain = async () => {
    setExplaining(true);
    setExplanation(null);
    try {
      const started = await startMacroExplanation(tableId, macro.macro_id);
      if (started.status === "COMPLETED") {
        setExplanation(started);
        setExplaining(false);
      } else {
        setExplainRunId(started.agent_run_id);
      }
    } catch (err) {
      onError?.(err.message);
      setExplaining(false);
    }
  };

  const onExplainDone = async (progress) => {
    setExplaining(false);
    if (progress.run.status !== "COMPLETED") return;
    try {
      const result = await getMacroExplanation(tableId, macro.macro_id);
      setExplanation(result);
    } catch (err) {
      onError?.(err.message);
    }
  };

  return (
    <article className="macro-card">
      <button type="button" className="macro-card-header" onClick={toggle}>
        <div>
          <strong>{macro.proc_name}</strong>
          <small>{macro.module_name} &middot; {macro.source_line_count} line{macro.source_line_count === 1 ? "" : "s"}</small>
        </div>
        <span className="macro-badges">
          {macro.assurance?.status === "STALE" ? (
            <span className="pill drift-badge drift-risk-changed" title={macro.assurance.reason}>Assumptions stale</span>
          ) : null}
          {macro.drift ? (
            <span className={`pill drift-badge${macro.drift.risk_changed ? " drift-risk-changed" : ""}`} title={macro.drift.risk_changed ? "This macro's risk classification changed since the last upload" : "This macro's logic changed since the last upload"}>
              {macro.drift.risk_changed ? "Risk changed" : "Changed since last upload"}
            </span>
          ) : null}
          {macro.runnable && LANE_META[macro.execution_lane] ? (
            <span className="pill lane-badge" title={LANE_META[macro.execution_lane].title}>{LANE_META[macro.execution_lane].label}</span>
          ) : null}
          <span className={`pill device-trust-${toneClass(meta.tone)}`}>{meta.label}</span>
        </span>
      </button>
      {macro.block_reasons?.length ? (
        <ul className="macro-block-reasons">
          {macro.block_reasons.map((reason, index) => (
            <li key={index}><b>{reason.construct}</b> &mdash; {reason.reason}</li>
          ))}
        </ul>
      ) : null}
      {macro.assurance?.status === "STALE" ? (
        <div className="macro-drift">
          <p className="macro-drift-warning">Assumptions no longer hold: {macro.assurance.reason}</p>
        </div>
      ) : null}
      {macro.drift ? (
        <div className="macro-drift">
          {macro.drift.risk_changed ? (
            <p className="macro-drift-warning">Risk classification changed: was <b>{macro.drift.previous_static_risk}</b>{macro.drift.previous_execution_lane ? ` (${macro.drift.previous_execution_lane})` : ""}, now <b>{macro.static_risk}</b>{macro.execution_lane ? ` (${macro.execution_lane})` : ""}.</p>
          ) : null}
          <pre className="macro-diff">{macro.drift.diff.join("\n")}</pre>
        </div>
      ) : null}
      {expanded ? (
        <>
          <pre className="macro-source">{source == null ? "Loading..." : source}</pre>
          <div className="macro-run-bar">
            {macro.runnable && canRun ? (
              <button type="button" className="primary-button" onClick={run} disabled={running}>{running ? "Computing preview..." : "Run"}</button>
            ) : null}
            {canExplain ? (
              <button type="button" className="secondary-button" onClick={explain} disabled={explaining}>{explaining ? "Explaining..." : "Explain"}</button>
            ) : null}
            {macro.runnable && canRun ? <small className="muted">Computes a preview first &mdash; nothing changes until you confirm it.</small> : null}
          </div>
          {macro.runnable && canRun ? (
            <div className="macro-recipe-save">
              <input
                value={recipeName} onChange={(event) => setRecipeName(event.target.value)}
                placeholder="Name this as a saved recipe..." maxLength={120}
              />
              <button type="button" className="secondary-button" onClick={saveRecipe} disabled={!recipeName.trim() || savingRecipe}>
                {savingRecipe ? "Saving..." : "Save as recipe"}
              </button>
            </div>
          ) : null}
          {explainRunId && explaining ? (
            <div className="macro-explanation">
              <AgentRunTimeline runId={explainRunId} onDone={onExplainDone} onError={onError} compact />
            </div>
          ) : null}
          {explanation ? (
            <div className="macro-explanation">
              <AIAnswerView text={explanationToAnswerShape(explanation)} confidence={explanation.confidence} warnings={explanation.warnings} />
            </div>
          ) : null}
        </>
      ) : null}
    </article>
  );
}

export default function VirtualRunPanel({ tableId, branches, selectedBranchId, canManage, onError }) {
  const [state, setState] = useState(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const fileInputRef = useRef(null);
  const [branchId, setBranchId] = useState(selectedBranchId || "");
  const [activeRun, setActiveRun] = useState(null); // {macro, run}
  const [recipes, setRecipes] = useState([]);
  const [runningRecipeId, setRunningRecipeId] = useState(null);

  const userBranches = (branches || []).filter((branch) => branch.branch_type === "USER");

  useEffect(() => {
    if (selectedBranchId) setBranchId(selectedBranchId);
    else if (!branchId && userBranches.length) setBranchId(userBranches[0].branch_id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedBranchId, userBranches.length]);

  const load = useCallback(() => {
    if (!tableId) return;
    setLoading(true);
    getRepositoryMacros(tableId)
      .then(setState)
      .catch((err) => onError?.(err.message))
      .finally(() => setLoading(false));
  }, [tableId, onError]);

  const loadRecipes = useCallback(() => {
    if (!tableId) return;
    getRepositoryRecipes(tableId).then((result) => setRecipes(result.recipes || [])).catch((err) => onError?.(err.message));
  }, [tableId, onError]);

  useEffect(() => { load(); loadRecipes(); }, [load, loadRecipes]);

  const runRecipe = async (recipe) => {
    if (!branchId) {
      onError?.("Select a personal branch above before running a recipe.");
      return;
    }
    setRunningRecipeId(recipe.recipe_id);
    try {
      const prepared = await runMacroRecipe(tableId, recipe.recipe_id, branchId);
      setActiveRun({ macro: { proc_name: recipe.proc_name, module_name: recipe.module_name }, run: prepared });
    } catch (err) {
      onError?.(err.message);
    } finally {
      setRunningRecipeId(null);
    }
  };

  const removeRecipe = async (recipe) => {
    try {
      await deleteMacroRecipe(tableId, recipe.recipe_id);
      loadRecipes();
    } catch (err) {
      onError?.(err.message);
    }
  };

  const handleFileChosen = async (event) => {
    const picked = event.target.files[0];
    event.target.value = "";
    if (!picked) return;
    setBusy(true);
    try {
      await registerMacroSource(tableId, picked);
      await extractRepositoryMacros(tableId);
      load();
    } catch (err) {
      onError?.(err.message);
    } finally {
      setBusy(false);
    }
  };

  const reextract = async () => {
    setBusy(true);
    try {
      await extractRepositoryMacros(tableId);
      load();
    } catch (err) {
      onError?.(err.message);
    } finally {
      setBusy(false);
    }
  };

  const macros = state?.macros || [];
  const runnableCount = macros.filter((macro) => macro.runnable).length;
  const staleMacros = macros.filter((macro) => macro.assurance?.status === "STALE");

  return (
    <div className="history-stage">
      <section className="panel">
        <div className="panel-header">
          <div>
            <p className="eyebrow">EUC / VIRTUAL RUN</p>
            <h2>Macros in this repository</h2>
            <p className="muted">Every macro is extracted and risk-classified before anything can run. A macro that reaches outside the workbook &mdash; files, the network, other programs &mdash; is never runnable, in any version of this feature.</p>
          </div>
          {state?.has_source ? <span className="pill">{macros.length} macro{macros.length === 1 ? "" : "s"} &middot; {runnableCount} runnable</span> : null}
        </div>

        {staleMacros.length ? (
          <div className="macro-assurance-banner">
            <strong>Continuous Assurance:</strong> {staleMacros.length} macro{staleMacros.length === 1 ? "" : "s"} {staleMacros.length === 1 ? "has" : "have"} stale assumptions since the last merge to main
            ({staleMacros.map((macro) => macro.proc_name).join(", ")}) &mdash; review before running.
          </div>
        ) : null}

        {canManage ? (
          <div className="euc-upload-disclosure">
            <p className="muted macro-source-note">
              {state?.has_source
                ? "A macro source is already registered for this repository (automatically, if it was created from a .xlsm). Macros are stored and analyzed independently of the repository's own version history."
                : "Upload the .xlsm containing this workbook's macros to get started. Macros are stored and analyzed independently of the repository's own version history."}
            </p>
            <input ref={fileInputRef} type="file" accept=".xlsm" style={{ display: "none" }} onChange={handleFileChosen} />
            <div className="macro-source-actions">
              {state?.has_source ? (
                <>
                  <button onClick={reextract} disabled={busy}>{busy ? "Working..." : "Re-analyze current source"}</button>
                  <button type="button" className="secondary-button" onClick={() => fileInputRef.current?.click()} disabled={busy}>Replace with a new file</button>
                </>
              ) : (
                <button onClick={() => fileInputRef.current?.click()} disabled={busy}>{busy ? "Working..." : "Register and analyze"}</button>
              )}
            </div>
          </div>
        ) : null}

        {canManage && runnableCount ? (
          <label className="macro-branch-picker">
            Run against branch
            <select value={branchId} onChange={(event) => setBranchId(event.target.value)}>
              <option value="">Select a personal branch&hellip;</option>
              {userBranches.map((branch) => <option key={branch.branch_id} value={branch.branch_id}>{branch.branch_name}</option>)}
            </select>
            {!userBranches.length ? <small className="muted">Open this repository in Excel once to create a personal branch, then come back here.</small> : null}
          </label>
        ) : null}

        {recipes.length ? (
          <div className="macro-recipe-strip">
            <p className="eyebrow">SAVED RECIPES</p>
            <div className="macro-recipe-list">
              {recipes.map((recipe) => (
                <article key={recipe.recipe_id} className={`macro-recipe-card${recipe.currently_runnable ? "" : " stale"}`}>
                  <div>
                    <strong>{recipe.name}</strong>
                    <small>{recipe.proc_name} &middot; {recipe.module_name} &middot; run {recipe.run_count} time{recipe.run_count === 1 ? "" : "s"}</small>
                    {!recipe.currently_runnable ? <small className="macro-recipe-stale-note">No longer runnable &mdash; the macro source changed since this was saved.</small> : null}
                  </div>
                  <div className="macro-recipe-actions">
                    {canManage && recipe.currently_runnable ? (
                      <button type="button" className="primary-button compact" onClick={() => runRecipe(recipe)} disabled={runningRecipeId === recipe.recipe_id}>
                        {runningRecipeId === recipe.recipe_id ? "Computing..." : "Run"}
                      </button>
                    ) : null}
                    {canManage ? <button type="button" className="text-button" onClick={() => removeRecipe(recipe)}>Remove</button> : null}
                  </div>
                </article>
              ))}
            </div>
          </div>
        ) : null}

        {loading ? <div className="empty-state compact">Loading macros...</div> : null}
        {!loading && state && !state.has_source ? (
          <div className="empty-state compact">No macro source has been registered for this repository yet.</div>
        ) : null}
        {!loading && state?.has_source && state.extraction_status === "RUNNING" ? (
          <div className="empty-state compact">Extracting macros...</div>
        ) : null}
        {!loading && macros.length ? (
          <div className="macro-list">
            {macros.map((macro) => (
              <MacroCard
                key={macro.macro_id} macro={macro} tableId={tableId} branchId={branchId} canRun={canManage} canExplain={canManage}
                onError={onError} onRunPrepared={(macroDef, run) => setActiveRun({ macro: macroDef, run })}
                onRecipeSaved={loadRecipes}
              />
            ))}
          </div>
        ) : null}
        {!loading && state?.has_source && state.extraction_status === "COMPLETED" && !macros.length ? (
          <div className="empty-state compact">No macros were found in the registered workbook.</div>
        ) : null}
      </section>

      {activeRun ? (
        <RunPreviewModal
          tableId={tableId}
          macro={activeRun.macro}
          run={activeRun.run}
          onClose={() => setActiveRun(null)}
          onError={onError}
          onConfirmed={() => { setActiveRun(null); load(); loadRecipes(); }}
        />
      ) : null}
    </div>
  );
}
