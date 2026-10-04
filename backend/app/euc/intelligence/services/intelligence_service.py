"""Stage 2.3 intelligence orchestration and evidence-backed query services."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import Counter
from typing import Any

from .... import database
from ....config import settings
from ....observability import record_audit_event, record_metric
from ....services.semantic_ledger_service import ledger_for_connection
from ...service import _repository_access
from ..complexity import ComplexityEngine
from ..controls import ControlEngine
from ..findings import FindingEngine
from ..risk import RiskEngine
from ..scoring.models import classification
from ..scoring.profiles import get_profile
from .feature_builder import FeatureBuilder


ACTIVE_STATUSES = {"OPEN", "ACKNOWLEDGED", "IN_REVIEW", "ACCEPTED_RISK", "REMEDIATION_PLANNED"}
ALLOWED_STATUSES = ACTIVE_STATUSES | {"RESOLVED", "FALSE_POSITIVE", "SUPPRESSED"}
SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}


def _id(prefix: str, size: int = 20) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:size].upper()}"


def _stable_id(prefix: str, value: str, size: int = 24) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:size].upper()}"


def _json(value: str | None, fallback):
    return json.loads(value) if value else fallback


class IntelligenceService:
    def build(self, euc_id: str, user_id: str, profile_name: str = "DEFAULT") -> dict[str, Any]:
        conn = database._get_connection()
        run_id = _id("INT")
        started = time.perf_counter()
        asset = None
        try:
            asset, inventory_run, dependency_run = self._required_inputs(conn, euc_id, user_id, edit=True)
            profile = get_profile(profile_name or settings.intelligence_default_profile)
            now = database._utcnow()
            self._persist_profile(conn, profile, now)
            conn.execute(
                """INSERT INTO EUC_INTELLIGENCE_RUNS
                   (INTELLIGENCE_RUN_ID,EUC_ID,SOURCE_COMMIT_ID,INVENTORY_ANALYSIS_ID,
                    DEPENDENCY_RUN_ID,ENGINE_VERSION,RULESET_VERSION,SCORING_PROFILE,
                    SCORING_PROFILE_VERSION,STATUS,PROGRESS,CURRENT_STEP,STARTED_AT)
                   VALUES (?,?,?,?,?,?,?,?,?,'BUILDING_FEATURES',5,'FEATURE_INDEX',?)""",
                (run_id, euc_id, dependency_run["SOURCE_COMMIT_ID"], inventory_run["ANALYSIS_ID"],
                 dependency_run["DEPENDENCY_RUN_ID"], settings.intelligence_engine_version,
                 settings.intelligence_ruleset_version, profile["name"], profile["version"], now),
            )
            conn.commit()
            record_audit_event(
                "EUC_INTELLIGENCE_ANALYSIS_STARTED", actor_user_id=user_id,
                repository_id=asset["REPOSITORY_ID"],
                payload={"euc_id": euc_id, "intelligence_run_id": run_id,
                         "profile": profile["name"], "ruleset": settings.intelligence_ruleset_version},
            )
            features = FeatureBuilder(conn, asset, inventory_run, dependency_run).build()
            self._timeout(started)
            self._progress(conn, run_id, "CONTROLS", 30, "CONTROL_EVALUATION")
            controls = ControlEngine().evaluate(features)
            self._progress(conn, run_id, "COMPLEXITY", 45, "COMPLEXITY_SCORING")
            complexity = ComplexityEngine().evaluate(features, profile)
            self._progress(conn, run_id, "RISK", 60, "RISK_SCORING")
            risk = RiskEngine().evaluate(features, controls, profile)
            self._progress(conn, run_id, "FINDINGS", 75, "FINDING_RULES")
            candidates = FindingEngine().evaluate(features, controls)
            self._timeout(started)
            self._progress(conn, run_id, "PERSISTING", 88, "EVIDENCE_PERSISTENCE")
            ledger = ledger_for_connection(conn)
            feature_object = ledger.objects.put(conn, "EUC_INTELLIGENCE_FEATURES", features)
            explanation = {
                "complexity": complexity.to_dict(), "risk": risk["inherent"].to_dict(),
                "controls": controls, "residual_risk": risk["residual_score"],
                "control_reduction": risk["control_reduction"],
            }
            explanation_object = ledger.objects.put(conn, "EUC_INTELLIGENCE_EXPLANATION", explanation)
            self._persist_scores(conn, ledger, run_id, complexity, risk["inherent"])
            self._persist_controls(conn, ledger, run_id, controls)
            finding_rows = self._persist_findings(
                conn, ledger, run_id, euc_id, dependency_run["SOURCE_COMMIT_ID"], candidates, user_id
            )
            summary = self._summary(complexity.score, risk, controls, finding_rows)
            result_manifest = {
                "engine_version": settings.intelligence_engine_version,
                "ruleset_version": settings.intelligence_ruleset_version,
                "profile": {"name": profile["name"], "version": profile["version"]},
                "euc_id": euc_id, "source_commit_id": dependency_run["SOURCE_COMMIT_ID"],
                "inventory_analysis_id": inventory_run["ANALYSIS_ID"],
                "dependency_graph_hash": dependency_run["GRAPH_MANIFEST_HASH"],
                "feature_hash": feature_object["object_hash"],
                "explanation_hash": explanation_object["object_hash"],
                "summary": summary,
                "findings": [{"finding_id": row["finding_id"], "fingerprint": row["fingerprint"],
                              "evidence_hash": row["evidence_hash"]} for row in finding_rows],
            }
            result_object = ledger.objects.put(conn, "EUC_INTELLIGENCE_MANIFEST", result_manifest)
            self._reference(conn, result_object["object_hash"], feature_object["object_hash"], "FEATURES", 0)
            self._reference(conn, result_object["object_hash"], explanation_object["object_hash"], "EXPLANATION", 1)
            self._reference(conn, result_object["object_hash"], dependency_run["GRAPH_MANIFEST_HASH"], "DEPENDENCY_GRAPH", 2)
            evidence_hashes = [row[0] for row in conn.execute(
                """SELECT EVIDENCE_OBJECT_HASH FROM EUC_SCORE_COMPONENTS
                   WHERE INTELLIGENCE_RUN_ID=? UNION SELECT EVIDENCE_OBJECT_HASH
                   FROM EUC_CONTROL_INVENTORY WHERE INTELLIGENCE_RUN_ID=?""", (run_id, run_id),
            ) if row[0]]
            for position, evidence_hash in enumerate(sorted(evidence_hashes), start=3):
                self._reference(conn, result_object["object_hash"], evidence_hash, "SCORING_EVIDENCE", position)
            for position, row in enumerate(finding_rows, start=3 + len(evidence_hashes)):
                self._reference(conn, result_object["object_hash"], row["evidence_hash"], "FINDING_EVIDENCE", position)
            duration_ms = round((time.perf_counter() - started) * 1000)
            conn.execute(
                """UPDATE EUC_INTELLIGENCE_RUNS SET STATUS='COMPLETED',PROGRESS=100,
                   CURRENT_STEP='COMPLETED',COMPLEXITY_SCORE=?,INHERENT_RISK_SCORE=?,
                   CONTROL_SCORE=?,RESIDUAL_RISK_SCORE=?,FINDING_COUNT=?,
                   CRITICAL_FINDING_COUNT=?,RESULT_MANIFEST_HASH=?,FEATURE_MANIFEST_HASH=?,
                   EXPLANATION_MANIFEST_HASH=?,SUMMARY_JSON=?,COMPLETED_AT=?,DURATION_MS=?
                   WHERE INTELLIGENCE_RUN_ID=?""",
                (complexity.score, risk["inherent"].score, controls["effective_control_strength"],
                 risk["residual_score"], len(finding_rows),
                 sum(row["severity"] == "CRITICAL" for row in finding_rows),
                 result_object["object_hash"], feature_object["object_hash"],
                 explanation_object["object_hash"], json.dumps(summary, sort_keys=True),
                 database._utcnow(), duration_ms, run_id),
            )
            conn.commit()
            record_metric("intelligence_analysis_duration", duration_ms, "ms", user_id=user_id,
                          repository_id=asset["REPOSITORY_ID"],
                          tags={"findings": len(finding_rows), "profile": profile["name"]})
            record_audit_event(
                "EUC_INTELLIGENCE_ANALYSIS_COMPLETED", actor_user_id=user_id,
                repository_id=asset["REPOSITORY_ID"],
                payload={"euc_id": euc_id, "intelligence_run_id": run_id,
                         "result_manifest_hash": result_object["object_hash"], "summary": summary},
            )
            return self.overview(euc_id, user_id, run_id)
        except Exception as exc:
            conn.rollback()
            try:
                conn.execute(
                    """UPDATE EUC_INTELLIGENCE_RUNS SET STATUS='FAILED',CURRENT_STEP='FAILED',
                       COMPLETED_AT=?,WARNINGS_JSON=? WHERE INTELLIGENCE_RUN_ID=?""",
                    (database._utcnow(), json.dumps([{"code": "INTELLIGENCE_BUILD_FAILED", "message": str(exc)}]), run_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
            record_audit_event(
                "EUC_INTELLIGENCE_ANALYSIS_FAILED", actor_user_id=user_id,
                repository_id=asset["REPOSITORY_ID"] if asset else None,
                payload={"euc_id": euc_id, "intelligence_run_id": run_id},
                status="FAILED", failure_reason=str(exc),
            )
            raise
        finally:
            conn.close()

    def overview(self, euc_id: str, user_id: str, run_id: str | None = None) -> dict[str, Any]:
        conn = database._get_connection()
        try:
            asset = self._asset_access(conn, euc_id, user_id)
            run = self._run(conn, euc_id, run_id)
            if not run:
                return {"euc_id": euc_id, "status": "NOT_BUILT", "freshness": "MISSING",
                        "available_profiles": sorted(get_profile(name)["name"] for name in
                                                     ("DEFAULT", "FINANCIAL_MODEL", "REGULATORY_REPORTING", "OPERATIONS", "PLANNING", "ANALYTICS"))}
            current = conn.execute(
                """SELECT B.HEAD_COMMIT_ID FROM WORKBOOK_REPOSITORIES R LEFT JOIN BRANCHES B
                   ON B.BRANCH_ID=R.DEFAULT_BRANCH_ID WHERE R.REPOSITORY_ID=?""",
                (asset["REPOSITORY_ID"],),
            ).fetchone()
            counts = Counter(row[0] for row in conn.execute(
                """SELECT O.SEVERITY FROM EUC_FINDING_OCCURRENCES O JOIN EUC_FINDINGS F
                   ON F.FINDING_ID=O.FINDING_ID WHERE O.INTELLIGENCE_RUN_ID=?
                   AND F.STATUS NOT IN ('RESOLVED','FALSE_POSITIVE','SUPPRESSED')""", (run["INTELLIGENCE_RUN_ID"],)
            ))
            top = self._finding_rows(conn, run["INTELLIGENCE_RUN_ID"], limit=8)
            return {
                "euc_id": euc_id, "intelligence_run_id": run["INTELLIGENCE_RUN_ID"],
                "source_commit_id": run["SOURCE_COMMIT_ID"],
                "current_head_commit_id": current[0] if current else None,
                "freshness": "CURRENT" if run["SOURCE_COMMIT_ID"] == (current[0] if current else None) else "STALE",
                "status": run["STATUS"], "progress": run["PROGRESS"], "current_step": run["CURRENT_STEP"],
                "engine_version": run["ENGINE_VERSION"], "ruleset_version": run["RULESET_VERSION"],
                "profile": run["SCORING_PROFILE"], "profile_version": run["SCORING_PROFILE_VERSION"],
                "scores": {"complexity": run["COMPLEXITY_SCORE"],
                           "inherent_risk": run["INHERENT_RISK_SCORE"],
                           "control_strength": run["CONTROL_SCORE"],
                           "residual_risk": run["RESIDUAL_RISK_SCORE"]},
                "classifications": {"complexity": classification(run["COMPLEXITY_SCORE"]),
                                    "inherent_risk": classification(run["INHERENT_RISK_SCORE"]),
                                    "control_strength": classification(run["CONTROL_SCORE"]),
                                    "residual_risk": classification(run["RESIDUAL_RISK_SCORE"])},
                "finding_counts": dict(counts), "top_findings": top,
                "summary": _json(run["SUMMARY_JSON"], {}),
                "result_manifest_hash": run["RESULT_MANIFEST_HASH"], "duration_ms": run["DURATION_MS"],
            }
        finally:
            conn.close()

    def complexity(self, euc_id: str, user_id: str) -> dict[str, Any]:
        return self._score_detail(euc_id, user_id, "COMPLEXITY")

    def explain_risk(self, euc_id: str, user_id: str) -> dict[str, Any]:
        detail = self._score_detail(euc_id, user_id, "INHERENT_RISK")
        overview = self.overview(euc_id, user_id)
        detail.update({"inherent_risk": overview["scores"]["inherent_risk"],
                       "control_strength": overview["scores"]["control_strength"],
                       "residual_risk": overview["scores"]["residual_risk"],
                       "control_reduction": round(overview["scores"]["inherent_risk"] - overview["scores"]["residual_risk"], 2)})
        return detail

    def controls(self, euc_id: str, user_id: str) -> dict[str, Any]:
        conn = database._get_connection()
        try:
            self._asset_access(conn, euc_id, user_id)
            run = self._required_run(conn, euc_id)
            rows = [{key.lower(): row[key] for key in row.keys()} for row in conn.execute(
                "SELECT * FROM EUC_CONTROL_INVENTORY WHERE INTELLIGENCE_RUN_ID=? ORDER BY CONTROL_SOURCE,CONTROL_CODE",
                (run["INTELLIGENCE_RUN_ID"],),
            )]
            summary = _json(run["SUMMARY_JSON"], {}).get("controls", {})
            return {"intelligence_run_id": run["INTELLIGENCE_RUN_ID"], "summary": summary, "controls": rows}
        finally:
            conn.close()

    def findings(self, euc_id: str, user_id: str, **filters) -> dict[str, Any]:
        conn = database._get_connection()
        try:
            self._asset_access(conn, euc_id, user_id)
            run = self._required_run(conn, euc_id)
            rows = self._finding_rows(conn, run["INTELLIGENCE_RUN_ID"], **filters)
            return {"intelligence_run_id": run["INTELLIGENCE_RUN_ID"], "items": rows,
                    "count": len(rows), "next_cursor": None}
        finally:
            conn.close()

    def finding_detail(self, euc_id: str, user_id: str, finding_id: str) -> dict[str, Any]:
        conn = database._get_connection()
        try:
            self._asset_access(conn, euc_id, user_id)
            row = conn.execute(
                """SELECT F.*,O.* FROM EUC_FINDINGS F JOIN EUC_FINDING_OCCURRENCES O
                   ON O.FINDING_ID=F.FINDING_ID WHERE F.EUC_ID=? AND F.FINDING_ID=?
                   ORDER BY O.DETECTED_AT DESC,O.ROWID DESC LIMIT 1""", (euc_id, finding_id),
            ).fetchone()
            if not row:
                raise KeyError("Finding does not exist")
            item = self._finding_dict(conn, row)
            item["actions"] = [{key.lower(): action[key] for key in action.keys()} for action in conn.execute(
                "SELECT * FROM EUC_FINDING_ACTIONS WHERE FINDING_ID=? ORDER BY CREATED_AT DESC", (finding_id,)
            )]
            item["occurrences"] = [{key.lower(): occurrence[key] for key in occurrence.keys()} for occurrence in conn.execute(
                "SELECT * FROM EUC_FINDING_OCCURRENCES WHERE FINDING_ID=? ORDER BY DETECTED_AT DESC", (finding_id,)
            )]
            return item
        finally:
            conn.close()

    def update_finding(self, euc_id: str, user_id: str, finding_id: str, status: str,
                       reason: str, expires_at: str | None = None) -> dict[str, Any]:
        status = (status or "").upper()
        if status not in ALLOWED_STATUSES:
            raise ValueError("Unsupported finding status")
        if not (reason or "").strip():
            raise ValueError("A reason is required for every finding lifecycle action")
        if status == "ACCEPTED_RISK" and not expires_at:
            raise ValueError("Accepted risk requires an expiry date")
        conn = database._get_connection()
        try:
            asset = self._asset_access(conn, euc_id, user_id, edit=True)
            finding = conn.execute("SELECT * FROM EUC_FINDINGS WHERE EUC_ID=? AND FINDING_ID=?", (euc_id, finding_id)).fetchone()
            if not finding:
                raise KeyError("Finding does not exist")
            now = database._utcnow()
            conn.execute(
                """UPDATE EUC_FINDINGS SET STATUS=?,ACCEPTED_UNTIL=?,UPDATED_AT=? WHERE FINDING_ID=?""",
                (status, expires_at if status == "ACCEPTED_RISK" else finding["ACCEPTED_UNTIL"], now, finding_id),
            )
            conn.execute(
                """INSERT INTO EUC_FINDING_ACTIONS
                   (ACTION_ID,FINDING_ID,ACTION_TYPE,PREVIOUS_STATUS,NEW_STATUS,REASON,
                    ACTOR_USER_ID,SOURCE_COMMIT_ID,EXPIRES_AT,CREATED_AT)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (_id("ACT"), finding_id, "STATUS_CHANGE", finding["STATUS"], status,
                 reason.strip(), user_id, finding["LAST_SOURCE_COMMIT_ID"], expires_at, now),
            )
            conn.commit()
            record_audit_event("EUC_FINDING_STATUS_CHANGED", actor_user_id=user_id,
                               repository_id=asset["REPOSITORY_ID"],
                               payload={"euc_id": euc_id, "finding_id": finding_id,
                                        "previous_status": finding["STATUS"], "status": status,
                                        "reason": reason, "expires_at": expires_at})
        finally:
            conn.close()
        return self.finding_detail(euc_id, user_id, finding_id)

    def _score_detail(self, euc_id: str, user_id: str, score_type: str) -> dict[str, Any]:
        conn = database._get_connection()
        try:
            self._asset_access(conn, euc_id, user_id)
            run = self._required_run(conn, euc_id)
            ledger = ledger_for_connection(conn)
            components = []
            for row in conn.execute(
                """SELECT * FROM EUC_SCORE_COMPONENTS WHERE INTELLIGENCE_RUN_ID=? AND SCORE_TYPE=?
                   ORDER BY CONTRIBUTION DESC""", (run["INTELLIGENCE_RUN_ID"], score_type),
            ):
                item = {key.lower(): row[key] for key in row.keys()}
                item["evidence"] = ledger.objects.get(conn, row["EVIDENCE_OBJECT_HASH"]) if row["EVIDENCE_OBJECT_HASH"] else {}
                components.append(item)
            column = "COMPLEXITY_SCORE" if score_type == "COMPLEXITY" else "INHERENT_RISK_SCORE"
            return {"intelligence_run_id": run["INTELLIGENCE_RUN_ID"], "score_type": score_type,
                    "score": run[column], "classification": classification(run[column]), "components": components}
        finally:
            conn.close()

    @staticmethod
    def _persist_profile(conn, profile: dict, now: str) -> None:
        profile_id = _stable_id("PRO", f"{profile['name']}:{profile['version']}")
        conn.execute(
            """INSERT INTO EUC_SCORING_PROFILES
               (PROFILE_ID,PROFILE_NAME,PROFILE_VERSION,DESCRIPTION,WEIGHTS_JSON,
                THRESHOLDS_JSON,STATUS,CREATED_AT,UPDATED_AT)
               VALUES (?,?,?,?,?,?,'ACTIVE',?,?) ON CONFLICT(PROFILE_NAME,PROFILE_VERSION)
               DO UPDATE SET WEIGHTS_JSON=excluded.WEIGHTS_JSON,UPDATED_AT=excluded.UPDATED_AT""",
            (profile_id, profile["name"], profile["version"],
             f"Versioned {profile['name'].lower()} Stage 2.3 scoring profile",
             json.dumps({"complexity": profile["complexity"], "risk": profile["risk"],
                         "residual_control_effect": profile["residual_control_effect"]}, sort_keys=True),
             json.dumps({"very_low": 20, "low": 40, "medium": 60, "high": 80}, sort_keys=True), now, now),
        )

    @staticmethod
    def _persist_scores(conn, ledger, run_id, complexity, inherent) -> None:
        for result in (complexity, inherent):
            for component in result.components:
                evidence = ledger.objects.put(conn, "EUC_SCORE_EVIDENCE", component.evidence)
                conn.execute(
                    """INSERT INTO EUC_SCORE_COMPONENTS VALUES (?,?,?,?,?,?,?,?,?)""",
                    (run_id, result.score_type, component.dimension, component.raw_score,
                     component.weight, component.contribution, classification(component.raw_score),
                     evidence["object_hash"], component.explanation),
                )

    @staticmethod
    def _persist_controls(conn, ledger, run_id: str, controls: dict) -> None:
        for item in controls["inventory"]:
            evidence = ledger.objects.put(conn, "EUC_CONTROL_EVIDENCE", item)
            conn.execute(
                """INSERT INTO EUC_CONTROL_INVENTORY VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (_stable_id("CTL", f"{run_id}:{item['code']}"), run_id, item["code"], item["name"],
                 item["category"], item["source"], item["count"], item["coverage"],
                 item["weighted_coverage"], item["effectiveness"], evidence["object_hash"]),
            )

    def _persist_findings(self, conn, ledger, run_id: str, euc_id: str, source_commit_id: str | None,
                          candidates, user_id: str) -> list[dict]:
        now = database._utcnow()
        detected: set[str] = set()
        rows = []
        for candidate in candidates:
            fingerprint = hashlib.sha256(f"{candidate.rule_id}|{candidate.identity}".encode()).hexdigest()
            finding_id = _stable_id("FND", f"{euc_id}:{fingerprint}")
            detected.add(finding_id)
            existing = conn.execute("SELECT * FROM EUC_FINDINGS WHERE FINDING_ID=?", (finding_id,)).fetchone()
            recurrence = int(existing["RECURRENCE_COUNT"] or 0) if existing else 0
            status = existing["STATUS"] if existing else "OPEN"
            if existing and status == "RESOLVED":
                recurrence += 1
                status = "OPEN"
            if not existing:
                conn.execute(
                    """INSERT INTO EUC_FINDINGS
                       (FINDING_ID,EUC_ID,FINGERPRINT,RULE_ID,CATEGORY,TITLE,STATUS,FIRST_RUN_ID,
                        LAST_RUN_ID,FIRST_SOURCE_COMMIT_ID,LAST_SOURCE_COMMIT_ID,RECURRENCE_COUNT,
                        CREATED_AT,UPDATED_AT) VALUES (?,?,?,?,?,?,'OPEN',?,?,?,?,0,?,?)""",
                    (finding_id, euc_id, fingerprint, candidate.rule_id, candidate.category,
                     candidate.title, run_id, run_id, source_commit_id, source_commit_id, now, now),
                )
            else:
                conn.execute(
                    """UPDATE EUC_FINDINGS SET STATUS=?,LAST_RUN_ID=?,LAST_SOURCE_COMMIT_ID=?,
                       RECURRENCE_COUNT=?,UPDATED_AT=? WHERE FINDING_ID=?""",
                    (status, run_id, source_commit_id, recurrence, now, finding_id),
                )
            evidence_payload = {"rule_id": candidate.rule_id, "identity": candidate.identity,
                                "evidence": candidate.evidence,
                                "dependency_impact": candidate.dependency_impact}
            evidence = ledger.objects.put(conn, "EUC_FINDING_EVIDENCE", evidence_payload)
            conn.execute(
                """INSERT INTO EUC_FINDING_OCCURRENCES
                   (INTELLIGENCE_RUN_ID,FINDING_ID,SEVERITY,CONFIDENCE,NODE_ID,SHEET_ID,
                    CELL_ADDRESS,DESCRIPTION,REMEDIATION_CODE,EVIDENCE_MANIFEST_HASH,
                    DEPENDENCY_IMPACT_JSON,DETECTED_AT) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, finding_id, candidate.severity, candidate.confidence, candidate.node_id,
                 candidate.sheet_id, candidate.cell_address, candidate.description,
                 candidate.remediation_code, evidence["object_hash"],
                 json.dumps(candidate.dependency_impact, sort_keys=True), now),
            )
            if existing and existing["STATUS"] == "RESOLVED":
                conn.execute(
                    """INSERT INTO EUC_FINDING_ACTIONS VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (_id("ACT"), finding_id, "RECURRENCE", "RESOLVED", "OPEN",
                     "The deterministic rule detected the issue in a later source version.", user_id,
                     source_commit_id, None, now),
                )
            rows.append({"finding_id": finding_id, "fingerprint": fingerprint,
                         "evidence_hash": evidence["object_hash"], "severity": candidate.severity})
        previous = conn.execute(
            """SELECT F.* FROM EUC_FINDINGS F WHERE F.EUC_ID=? AND F.STATUS IN
               ('OPEN','ACKNOWLEDGED','IN_REVIEW','REMEDIATION_PLANNED') AND F.FINDING_ID NOT IN
               (SELECT FINDING_ID FROM EUC_FINDING_OCCURRENCES WHERE INTELLIGENCE_RUN_ID=?)""",
            (euc_id, run_id),
        )
        for finding in previous:
            if finding["FINDING_ID"] in detected:
                continue
            conn.execute("UPDATE EUC_FINDINGS SET STATUS='RESOLVED',UPDATED_AT=? WHERE FINDING_ID=?", (now, finding["FINDING_ID"]))
            conn.execute(
                "INSERT INTO EUC_FINDING_ACTIONS VALUES (?,?,?,?,?,?,?,?,?,?)",
                (_id("ACT"), finding["FINDING_ID"], "AUTO_RESOLUTION", finding["STATUS"], "RESOLVED",
                 "The finding is absent from the latest deterministic analysis.", user_id,
                 source_commit_id, None, now),
            )
        return rows

    @staticmethod
    def _summary(complexity: float, risk: dict, controls: dict, findings: list[dict]) -> dict:
        counts = Counter(row["severity"] for row in findings)
        return {"complexity": {"score": complexity, "classification": classification(complexity)},
                "risk": {"inherent": risk["inherent"].score, "residual": risk["residual_score"],
                         "control_reduction": risk["control_reduction"]},
                "controls": {key: value for key, value in controls.items() if key != "inventory"},
                "findings": {"total": len(findings), "by_severity": dict(counts)}}

    def _required_inputs(self, conn, euc_id: str, user_id: str, edit: bool = False):
        asset = conn.execute("SELECT * FROM EUC_ASSETS WHERE EUC_ID=?", (euc_id,)).fetchone()
        if not asset:
            raise KeyError("EUC asset does not exist")
        _repository_access(conn, asset["REPOSITORY_ID"], user_id, edit=edit)
        inventory = conn.execute(
            """SELECT * FROM EUC_ANALYSIS_RUNS WHERE EUC_ID=? AND STATUS IN
               ('COMPLETED','COMPLETED_WITH_WARNINGS') ORDER BY STARTED_AT DESC,ROWID DESC LIMIT 1""", (euc_id,),
        ).fetchone()
        if not inventory:
            raise ValueError("A completed Stage 2.1 inventory is required before intelligence analysis")
        dependency = conn.execute(
            """SELECT * FROM EUC_DEPENDENCY_RUNS WHERE EUC_ID=? AND ANALYSIS_ID=? AND STATUS IN
               ('COMPLETED','COMPLETED_WITH_WARNINGS') ORDER BY STARTED_AT DESC,ROWID DESC LIMIT 1""",
            (euc_id, inventory["ANALYSIS_ID"]),
        ).fetchone()
        if not dependency:
            raise ValueError("A completed Stage 2.2 dependency graph is required before intelligence analysis")
        return asset, inventory, dependency

    def _asset_access(self, conn, euc_id: str, user_id: str, edit: bool = False):
        asset = conn.execute("SELECT * FROM EUC_ASSETS WHERE EUC_ID=?", (euc_id,)).fetchone()
        if not asset:
            raise KeyError("EUC asset does not exist")
        _repository_access(conn, asset["REPOSITORY_ID"], user_id, edit=edit)
        return asset

    @staticmethod
    def _run(conn, euc_id: str, run_id: str | None = None):
        if run_id:
            return conn.execute("SELECT * FROM EUC_INTELLIGENCE_RUNS WHERE EUC_ID=? AND INTELLIGENCE_RUN_ID=?", (euc_id, run_id)).fetchone()
        return conn.execute(
            """SELECT * FROM EUC_INTELLIGENCE_RUNS WHERE EUC_ID=? AND STATUS='COMPLETED'
               ORDER BY STARTED_AT DESC,ROWID DESC LIMIT 1""", (euc_id,),
        ).fetchone()

    def _required_run(self, conn, euc_id: str):
        run = self._run(conn, euc_id)
        if not run:
            raise KeyError("Stage 2.3 intelligence has not been built")
        return run

    def _finding_rows(self, conn, run_id: str, severity: str | None = None,
                      category: str | None = None, sheet_id: str | None = None,
                      status: str | None = None, rule_id: str | None = None,
                      limit: int = 200, **_) -> list[dict]:
        clauses = ["O.INTELLIGENCE_RUN_ID=?"]
        params: list[Any] = [run_id]
        for column, value in (("O.SEVERITY", severity), ("F.CATEGORY", category),
                              ("O.SHEET_ID", sheet_id), ("F.STATUS", status), ("F.RULE_ID", rule_id)):
            if value:
                clauses.append(f"{column}=?")
                params.append(value.upper() if column != "O.SHEET_ID" else value)
        params.append(min(max(int(limit), 1), 500))
        rows = conn.execute(
            f"""SELECT F.*,O.* FROM EUC_FINDINGS F JOIN EUC_FINDING_OCCURRENCES O
                ON O.FINDING_ID=F.FINDING_ID WHERE {' AND '.join(clauses)} LIMIT ?""", params,
        )
        result = [self._finding_dict(conn, row) for row in rows]
        return sorted(result, key=lambda item: (SEVERITY_ORDER.get(item["severity"], 9), item["rule_id"], item["finding_id"]))

    @staticmethod
    def _finding_dict(conn, row) -> dict:
        item = {key.lower(): row[key] for key in row.keys()}
        item["dependency_impact"] = _json(row["DEPENDENCY_IMPACT_JSON"], {})
        item["evidence"] = ledger_for_connection(conn).objects.get(conn, row["EVIDENCE_MANIFEST_HASH"])
        return item

    @staticmethod
    def _progress(conn, run_id: str, status: str, progress: int, step: str) -> None:
        conn.execute("UPDATE EUC_INTELLIGENCE_RUNS SET STATUS=?,PROGRESS=?,CURRENT_STEP=? WHERE INTELLIGENCE_RUN_ID=?",
                     (status, progress, step, run_id))
        conn.commit()

    @staticmethod
    def _reference(conn, parent: str, child: str | None, reference_type: str, position: int) -> None:
        if child:
            conn.execute(
                """INSERT OR IGNORE INTO OBJECT_REFERENCES
                   (PARENT_HASH,CHILD_HASH,REFERENCE_TYPE,POSITION) VALUES (?,?,?,?)""",
                (parent, child, reference_type, position),
            )

    @staticmethod
    def _timeout(started: float) -> None:
        if time.perf_counter() - started > settings.intelligence_analysis_timeout_seconds:
            raise TimeoutError("Stage 2.3 intelligence analysis exceeded the configured timeout")
