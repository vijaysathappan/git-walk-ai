"""Stage 2.4 migration-blueprint orchestration and bounded query services."""

from __future__ import annotations

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
from ..planning import MigrationPlanner
from ..readiness import ReadinessEngine
from ..readiness.blockers import BlockerEngine
from ..strategy import ComponentClassifier, StrategyEngine
from ..target import TargetMapper
from ..validation import ValidationPlanner
from .feature_builder import MigrationFeatureBuilder


MODES = {"AUTO_MIGRATABLE", "ASSISTED_MIGRATION", "MANUAL_REENGINEERING",
         "RETAIN_IN_EXCEL", "UNSUPPORTED", "RETIRE"}


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20].upper()}"


def _json(value: str | None, fallback):
    return json.loads(value) if value else fallback


class MigrationService:
    def analyze(self, euc_id: str, user_id: str) -> dict[str, Any]:
        conn = database._get_connection()
        run_id = _id("MIG")
        started = time.perf_counter()
        asset = None
        try:
            asset, inventory, dependency, intelligence = self._required_inputs(conn, euc_id, user_id, edit=True)
            now = database._utcnow()
            conn.execute(
                """INSERT INTO EUC_MIGRATION_RUNS
                   (MIGRATION_RUN_ID,EUC_ID,SOURCE_COMMIT_ID,INVENTORY_ANALYSIS_ID,
                    DEPENDENCY_RUN_ID,INTELLIGENCE_RUN_ID,ENGINE_VERSION,RULESET_VERSION,
                    STATUS,PROGRESS,CURRENT_STEP,STARTED_AT)
                   VALUES (?,?,?,?,?,?,?,?,'BUILDING_FEATURES',5,'MIGRATION_FEATURE_INDEX',?)""",
                (run_id, euc_id, intelligence["SOURCE_COMMIT_ID"], inventory["ANALYSIS_ID"],
                 dependency["DEPENDENCY_RUN_ID"], intelligence["INTELLIGENCE_RUN_ID"],
                 settings.migration_engine_version, settings.migration_ruleset_version, now),
            )
            conn.commit()
            record_audit_event("EUC_MIGRATION_ANALYSIS_STARTED", actor_user_id=user_id,
                               repository_id=asset["REPOSITORY_ID"],
                               payload={"euc_id": euc_id, "migration_run_id": run_id,
                                        "engine_version": settings.migration_engine_version})
            features = MigrationFeatureBuilder(conn, asset, inventory, dependency, intelligence).build()
            self._timeout(started)
            self._progress(conn, run_id, "CLASSIFYING", 20, "COMPONENT_CLASSIFICATION")
            units = ComponentClassifier().classify(features)
            if len(units) > settings.migration_unit_limit:
                raise ValueError("Migration-unit limit exceeded; analyze a decomposed workbook scope")
            blockers = BlockerEngine().evaluate(features, units)
            readiness = ReadinessEngine().evaluate(features, blockers)
            planner = MigrationPlanner()
            coverage = planner.coverage(units)
            strategy = StrategyEngine().evaluate(features, readiness, coverage, blockers)
            self._progress(conn, run_id, "PLANNING", 48, "DEPENDENCY_WAVES")
            units, waves = planner.waves(units, features, blockers)
            mapper = TargetMapper()
            target_mappings = mapper.map_units(units)
            control_mappings = mapper.map_controls(features["controls"])
            architecture = mapper.architecture(units, strategy)
            validation = ValidationPlanner().build(units, features, control_mappings)
            effort = planner.effort(units, blockers, len(validation))
            self._timeout(started)
            self._progress(conn, run_id, "PERSISTING", 82, "BLUEPRINT_OBJECTS")
            ledger = ledger_for_connection(conn)
            feature_object = ledger.objects.put(conn, "EUC_MIGRATION_FEATURES", features)
            evidence_hashes = []
            self._persist_units(conn, ledger, run_id, units, evidence_hashes)
            self._persist_blockers(conn, ledger, run_id, blockers, evidence_hashes)
            self._persist_waves(conn, ledger, run_id, waves, evidence_hashes)
            self._persist_targets(conn, ledger, run_id, target_mappings, evidence_hashes)
            self._persist_controls(conn, ledger, run_id, control_mappings, evidence_hashes)
            self._persist_validation(conn, run_id, validation)
            summary = self._summary(readiness, strategy, coverage, blockers, waves,
                                    control_mappings, validation, effort)
            blueprint = {
                "schema": "gitwalk.migration-blueprint.v1", "engine_version": settings.migration_engine_version,
                "ruleset_version": settings.migration_ruleset_version, "euc_id": euc_id,
                "source_commit_id": intelligence["SOURCE_COMMIT_ID"],
                "inventory_analysis_id": inventory["ANALYSIS_ID"],
                "dependency_run_id": dependency["DEPENDENCY_RUN_ID"],
                "dependency_graph_hash": dependency["GRAPH_MANIFEST_HASH"],
                "intelligence_run_id": intelligence["INTELLIGENCE_RUN_ID"],
                "intelligence_manifest_hash": intelligence["RESULT_MANIFEST_HASH"],
                "readiness": readiness, "strategy": strategy, "coverage": coverage,
                "units": [unit.to_dict() for unit in units],
                "blockers": [item.to_dict() for item in blockers], "waves": waves,
                "target_architecture": architecture, "target_mappings": target_mappings,
                "control_preservation": control_mappings, "validation_plan": validation,
                "effort": effort, "summary": summary,
            }
            blueprint_object = ledger.objects.put(conn, "EUC_MIGRATION_BLUEPRINT", blueprint)
            self._reference(conn, blueprint_object["object_hash"], feature_object["object_hash"], "MIGRATION_FEATURES", 0)
            self._reference(conn, blueprint_object["object_hash"], dependency["GRAPH_MANIFEST_HASH"], "DEPENDENCY_GRAPH", 1)
            self._reference(conn, blueprint_object["object_hash"], intelligence["RESULT_MANIFEST_HASH"], "INTELLIGENCE_RESULT", 2)
            for position, object_hash in enumerate(sorted(set(evidence_hashes)), start=3):
                self._reference(conn, blueprint_object["object_hash"], object_hash, "MIGRATION_EVIDENCE", position)
            duration_ms = round((time.perf_counter() - started) * 1000)
            conn.execute(
                """UPDATE EUC_MIGRATION_RUNS SET STATUS='COMPLETED',PROGRESS=100,CURRENT_STEP='COMPLETED',
                   READINESS_SCORE=?,READINESS_CLASSIFICATION=?,RECOMMENDED_STRATEGY=?,AUTO_PERCENT=?,
                   ASSISTED_PERCENT=?,MANUAL_PERCENT=?,RETAIN_PERCENT=?,UNSUPPORTED_PERCENT=?,RETIRE_PERCENT=?,
                   EFFORT_CLASS=?,UNIT_COUNT=?,BLOCKER_COUNT=?,CRITICAL_BLOCKER_COUNT=?,BLUEPRINT_MANIFEST_HASH=?,
                   FEATURE_MANIFEST_HASH=?,SUMMARY_JSON=?,COMPLETED_AT=?,DURATION_MS=? WHERE MIGRATION_RUN_ID=?""",
                (readiness["score"], readiness["classification"], strategy["recommended"],
                 coverage["AUTO_MIGRATABLE"], coverage["ASSISTED_MIGRATION"],
                 coverage["MANUAL_REENGINEERING"], coverage["RETAIN_IN_EXCEL"],
                 coverage["UNSUPPORTED"], coverage["RETIRE"], effort["overall"], len(units),
                 len(blockers), sum(item.severity == "CRITICAL" for item in blockers),
                 blueprint_object["object_hash"], feature_object["object_hash"],
                 json.dumps(summary, sort_keys=True), database._utcnow(), duration_ms, run_id),
            )
            conn.commit()
            record_metric("migration_analysis_duration", duration_ms, "ms", user_id=user_id,
                          repository_id=asset["REPOSITORY_ID"],
                          tags={"units": len(units), "blockers": len(blockers),
                                "strategy": strategy["recommended"]})
            record_audit_event("EUC_MIGRATION_ANALYSIS_COMPLETED", actor_user_id=user_id,
                               repository_id=asset["REPOSITORY_ID"],
                               payload={"euc_id": euc_id, "migration_run_id": run_id,
                                        "blueprint_manifest_hash": blueprint_object["object_hash"],
                                        "summary": summary})
            return self.overview(euc_id, user_id, run_id)
        except Exception as exc:
            conn.rollback()
            try:
                conn.execute("""UPDATE EUC_MIGRATION_RUNS SET STATUS='FAILED',CURRENT_STEP='FAILED',
                              COMPLETED_AT=?,WARNINGS_JSON=? WHERE MIGRATION_RUN_ID=?""",
                             (database._utcnow(), json.dumps([{"code": "MIGRATION_ANALYSIS_FAILED", "message": str(exc)}]), run_id))
                conn.commit()
            except Exception:
                conn.rollback()
            record_audit_event("EUC_MIGRATION_ANALYSIS_FAILED", actor_user_id=user_id,
                               repository_id=asset["REPOSITORY_ID"] if asset else None,
                               payload={"euc_id": euc_id, "migration_run_id": run_id},
                               status="FAILED", failure_reason=str(exc))
            raise
        finally:
            conn.close()

    def overview(self, euc_id: str, user_id: str, run_id: str | None = None) -> dict:
        conn = database._get_connection()
        try:
            asset = self._asset_access(conn, euc_id, user_id)
            run = self._run(conn, euc_id, run_id)
            if not run:
                return {"euc_id": euc_id, "status": "NOT_BUILT", "freshness": "MISSING"}
            current = conn.execute("""SELECT B.HEAD_COMMIT_ID FROM WORKBOOK_REPOSITORIES R
                LEFT JOIN BRANCHES B ON B.BRANCH_ID=R.DEFAULT_BRANCH_ID WHERE R.REPOSITORY_ID=?""",
                (asset["REPOSITORY_ID"],)).fetchone()
            return {"euc_id": euc_id, "migration_run_id": run["MIGRATION_RUN_ID"],
                    "status": run["STATUS"], "progress": run["PROGRESS"],
                    "current_step": run["CURRENT_STEP"], "source_commit_id": run["SOURCE_COMMIT_ID"],
                    "current_head_commit_id": current[0] if current else None,
                    "freshness": "CURRENT" if run["SOURCE_COMMIT_ID"] == (current[0] if current else None) else "STALE",
                    "engine_version": run["ENGINE_VERSION"], "ruleset_version": run["RULESET_VERSION"],
                    "readiness": {"score": run["READINESS_SCORE"], "classification": run["READINESS_CLASSIFICATION"]},
                    "strategy": run["RECOMMENDED_STRATEGY"],
                    "coverage": {"AUTO_MIGRATABLE": run["AUTO_PERCENT"], "ASSISTED_MIGRATION": run["ASSISTED_PERCENT"],
                                 "MANUAL_REENGINEERING": run["MANUAL_PERCENT"], "RETAIN_IN_EXCEL": run["RETAIN_PERCENT"],
                                 "UNSUPPORTED": run["UNSUPPORTED_PERCENT"], "RETIRE": run["RETIRE_PERCENT"]},
                    "effort_class": run["EFFORT_CLASS"], "unit_count": run["UNIT_COUNT"],
                    "blocker_count": run["BLOCKER_COUNT"], "critical_blocker_count": run["CRITICAL_BLOCKER_COUNT"],
                    "blueprint_manifest_hash": run["BLUEPRINT_MANIFEST_HASH"],
                    "summary": _json(run["SUMMARY_JSON"], {}), "duration_ms": run["DURATION_MS"]}
        finally:
            conn.close()

    def readiness(self, euc_id: str, user_id: str) -> dict:
        overview = self.overview(euc_id, user_id)
        if overview["status"] == "NOT_BUILT": return overview
        return {"migration_run_id": overview["migration_run_id"], **overview["readiness"],
                "dimensions": overview["summary"].get("readiness_dimensions", [])}

    def blockers(self, euc_id: str, user_id: str) -> dict:
        return self._table_result(euc_id, user_id, "EUC_MIGRATION_BLOCKERS", "blockers",
                                  "SEVERITY, BLOCKER_TYPE", json_columns={"dependency_impact_json": "dependency_impact"})

    def components(self, euc_id: str, user_id: str, mode: str | None = None,
                   source_type: str | None = None, wave: int | None = None) -> dict:
        conn = database._get_connection()
        try:
            self._asset_access(conn, euc_id, user_id)
            run = self._required_run(conn, euc_id)
            clauses, params = ["U.MIGRATION_RUN_ID=?"], [run["MIGRATION_RUN_ID"]]
            if mode: clauses.append("U.ENGINE_MODE=?"); params.append(mode.upper())
            if source_type: clauses.append("U.SOURCE_TYPE=?"); params.append(source_type.upper())
            if wave is not None: clauses.append("U.WAVE_NUMBER=?"); params.append(wave)
            rows = conn.execute(f"""SELECT U.*,O.MANUAL_MODE,O.REASON AS OVERRIDE_REASON,
                O.ACTOR_USER_ID AS OVERRIDE_ACTOR,O.CREATED_AT AS OVERRIDE_CREATED_AT
                FROM EUC_MIGRATION_UNITS U LEFT JOIN EUC_MIGRATION_OVERRIDES O ON O.OVERRIDE_ID=(
                    SELECT O2.OVERRIDE_ID FROM EUC_MIGRATION_OVERRIDES O2 WHERE O2.EUC_ID=?
                    AND O2.UNIT_ID=U.UNIT_ID AND O2.REVOKED_AT IS NULL ORDER BY O2.CREATED_AT DESC LIMIT 1)
                WHERE {' AND '.join(clauses)} ORDER BY U.WAVE_NUMBER,U.DIFFICULTY DESC,U.SOURCE_NAME""",
                (euc_id, *params))
            items = []
            for row in rows:
                item = self._lower(row); item["effective_mode"] = item.get("manual_mode") or item["engine_mode"]
                items.append(item)
            return {"migration_run_id": run["MIGRATION_RUN_ID"], "items": items, "count": len(items)}
        finally:
            conn.close()

    def waves(self, euc_id: str, user_id: str) -> dict:
        conn = database._get_connection()
        try:
            self._asset_access(conn, euc_id, user_id); run = self._required_run(conn, euc_id)
            ledger = ledger_for_connection(conn); items = []
            for row in conn.execute("SELECT * FROM EUC_MIGRATION_WAVES WHERE MIGRATION_RUN_ID=? ORDER BY WAVE_NUMBER", (run["MIGRATION_RUN_ID"],)):
                item = self._lower(row); item["depends_on"] = _json(row["DEPENDS_ON_JSON"], [])
                item["unit_ids"] = ledger.objects.get(conn, row["UNIT_IDS_OBJECT_HASH"]); items.append(item)
            return {"migration_run_id": run["MIGRATION_RUN_ID"], "items": items}
        finally: conn.close()

    def target(self, euc_id: str, user_id: str) -> dict:
        conn = database._get_connection()
        try:
            self._asset_access(conn, euc_id, user_id); run = self._required_run(conn, euc_id)
            blueprint = ledger_for_connection(conn).objects.get(conn, run["BLUEPRINT_MANIFEST_HASH"])
            return {"migration_run_id": run["MIGRATION_RUN_ID"],
                    "architecture": blueprint["target_architecture"], "mappings": blueprint["target_mappings"]}
        finally: conn.close()

    def controls(self, euc_id: str, user_id: str) -> dict:
        return self._table_result(euc_id, user_id, "EUC_CONTROL_MAPPINGS", "items",
                                  "PRESERVATION_STATUS,SOURCE_CONTROL_CODE")

    def validation(self, euc_id: str, user_id: str) -> dict:
        return self._table_result(euc_id, user_id, "EUC_VALIDATION_PLANS", "items",
                                  "PRIORITY,VALIDATION_TYPE")

    def override(self, euc_id: str, unit_id: str, user_id: str, manual_mode: str, reason: str) -> dict:
        manual_mode = manual_mode.upper()
        if manual_mode not in MODES: raise ValueError("Unsupported migration mode")
        if len((reason or "").strip()) < 3: raise ValueError("A reason is required for a migration override")
        conn = database._get_connection()
        try:
            asset = self._asset_access(conn, euc_id, user_id, edit=True); run = self._required_run(conn, euc_id)
            unit = conn.execute("SELECT * FROM EUC_MIGRATION_UNITS WHERE MIGRATION_RUN_ID=? AND UNIT_ID=?",
                                (run["MIGRATION_RUN_ID"], unit_id)).fetchone()
            if not unit: raise KeyError("Migration unit does not exist")
            now = database._utcnow()
            conn.execute("UPDATE EUC_MIGRATION_OVERRIDES SET REVOKED_AT=? WHERE EUC_ID=? AND UNIT_ID=? AND REVOKED_AT IS NULL",
                         (now, euc_id, unit_id))
            override_id = _id("MOV")
            conn.execute("""INSERT INTO EUC_MIGRATION_OVERRIDES VALUES (?,?,?,?,?,?,?,?,?,NULL)""",
                         (override_id, euc_id, unit_id, run["MIGRATION_RUN_ID"], unit["ENGINE_MODE"],
                          manual_mode, reason.strip(), user_id, now))
            conn.commit()
            record_audit_event("EUC_MIGRATION_RECOMMENDATION_OVERRIDDEN", actor_user_id=user_id,
                               repository_id=asset["REPOSITORY_ID"],
                               payload={"euc_id": euc_id, "unit_id": unit_id,
                                        "engine_mode": unit["ENGINE_MODE"], "manual_mode": manual_mode,
                                        "reason": reason.strip(), "override_id": override_id})
            return next(item for item in self.components(euc_id, user_id)["items"] if item["unit_id"] == unit_id)
        finally: conn.close()

    @staticmethod
    def _persist_units(conn, ledger, run_id, units, hashes):
        for unit in units:
            evidence = ledger.objects.put(conn, "EUC_MIGRATION_UNIT_EVIDENCE", unit.evidence); hashes.append(evidence["object_hash"])
            conn.execute("""INSERT INTO EUC_MIGRATION_UNITS VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (run_id, unit.unit_id, unit.parent_unit_id, unit.source_type, unit.source_id,
                          unit.source_name, unit.sheet_id, unit.domain_id, unit.mode, unit.difficulty,
                          unit.weight, unit.target_type, unit.target_component, unit.wave,
                          unit.rationale, evidence["object_hash"]))

    @staticmethod
    def _persist_blockers(conn, ledger, run_id, blockers, hashes):
        for item in blockers:
            evidence = ledger.objects.put(conn, "EUC_MIGRATION_BLOCKER_EVIDENCE", item.evidence); hashes.append(evidence["object_hash"])
            conn.execute("""INSERT INTO EUC_MIGRATION_BLOCKERS VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (run_id, item.blocker_id, item.blocker_type, item.category, item.severity,
                          item.affected_unit_id, item.affected_component, item.migration_effect,
                          item.remediation, item.effort_class, json.dumps(item.dependency_impact, sort_keys=True),
                          evidence["object_hash"]))

    @staticmethod
    def _persist_waves(conn, ledger, run_id, waves, hashes):
        for item in waves:
            object_result = ledger.objects.put(conn, "EUC_MIGRATION_WAVE_UNITS", item["unit_ids"]); hashes.append(object_result["object_hash"])
            conn.execute("INSERT INTO EUC_MIGRATION_WAVES VALUES (?,?,?,?,?,?,?,?)",
                         (run_id, item["wave_number"], item["name"], item["objective"], item["unit_count"],
                          item["effort_class"], json.dumps(item["depends_on"]), object_result["object_hash"]))

    @staticmethod
    def _persist_targets(conn, ledger, run_id, mappings, hashes):
        for item in mappings:
            evidence = ledger.objects.put(conn, "EUC_TARGET_MAPPING_EVIDENCE", item["evidence"]); hashes.append(evidence["object_hash"])
            conn.execute("INSERT INTO EUC_TARGET_MAPPINGS VALUES (?,?,?,?,?,?,?,?,?)",
                         (run_id, item["mapping_id"], item["unit_id"], item["source_type"], item["target_type"],
                          item["target_component"], item["mapping_pattern"], json.dumps(item["config"], sort_keys=True),
                          evidence["object_hash"]))

    @staticmethod
    def _persist_controls(conn, ledger, run_id, mappings, hashes):
        for item in mappings:
            evidence = ledger.objects.put(conn, "EUC_CONTROL_MAPPING_EVIDENCE", item["evidence"]); hashes.append(evidence["object_hash"])
            conn.execute("INSERT INTO EUC_CONTROL_MAPPINGS VALUES (?,?,?,?,?,?,?,?,?)",
                         (run_id, item["control_mapping_id"], item["source_control_code"], item["source_control_name"],
                          item["target_control"], item["preservation_status"], item["rationale"],
                          int(item["test_required"]), evidence["object_hash"]))

    @staticmethod
    def _persist_validation(conn, run_id, plans):
        for item in plans:
            conn.execute("INSERT INTO EUC_VALIDATION_PLANS VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (run_id, item["validation_id"], item["unit_id"], item["validation_type"],
                          item["source_output"], item["target_output"], item["comparison_method"],
                          item["absolute_tolerance"], item["relative_tolerance"], item["priority"],
                          item["historical_replay_count"], item["acceptance_criteria"]))

    @staticmethod
    def _summary(readiness, strategy, coverage, blockers, waves, controls, validation, effort):
        blocker_counts = Counter(item.severity for item in blockers)
        control_counts = Counter(item["preservation_status"] for item in controls)
        validation_counts = Counter(item["validation_type"] for item in validation)
        return {"readiness_dimensions": readiness["dimensions"], "strategy": strategy,
                "coverage": coverage, "blockers": {"total": len(blockers), "by_severity": dict(blocker_counts)},
                "waves": len(waves), "control_preservation": dict(control_counts),
                "validation": {"total": len(validation), "by_type": dict(validation_counts),
                               "historical_commits": max((item["historical_replay_count"] for item in validation), default=0)},
                "effort": effort}

    def _required_inputs(self, conn, euc_id, user_id, edit=False):
        asset = self._asset_access(conn, euc_id, user_id, edit)
        inventory = conn.execute("""SELECT * FROM EUC_ANALYSIS_RUNS WHERE EUC_ID=? AND STATUS IN
            ('COMPLETED','COMPLETED_WITH_WARNINGS') ORDER BY STARTED_AT DESC,ROWID DESC LIMIT 1""", (euc_id,)).fetchone()
        if not inventory: raise ValueError("A completed Stage 2.1 inventory is required")
        dependency = conn.execute("""SELECT * FROM EUC_DEPENDENCY_RUNS WHERE EUC_ID=? AND ANALYSIS_ID=?
            AND STATUS IN ('COMPLETED','COMPLETED_WITH_WARNINGS') ORDER BY STARTED_AT DESC,ROWID DESC LIMIT 1""",
            (euc_id, inventory["ANALYSIS_ID"])).fetchone()
        if not dependency: raise ValueError("A completed matching Stage 2.2 dependency graph is required")
        intelligence = conn.execute("""SELECT * FROM EUC_INTELLIGENCE_RUNS WHERE EUC_ID=?
            AND INVENTORY_ANALYSIS_ID=? AND DEPENDENCY_RUN_ID=? AND STATUS='COMPLETED'
            ORDER BY STARTED_AT DESC,ROWID DESC LIMIT 1""",
            (euc_id, inventory["ANALYSIS_ID"], dependency["DEPENDENCY_RUN_ID"])).fetchone()
        if not intelligence: raise ValueError("A completed matching Stage 2.3 intelligence run is required")
        return asset, inventory, dependency, intelligence

    def _asset_access(self, conn, euc_id, user_id, edit=False):
        asset = conn.execute("SELECT * FROM EUC_ASSETS WHERE EUC_ID=?", (euc_id,)).fetchone()
        if not asset: raise KeyError("EUC asset does not exist")
        _repository_access(conn, asset["REPOSITORY_ID"], user_id, edit=edit); return asset

    @staticmethod
    def _run(conn, euc_id, run_id=None):
        if run_id:
            return conn.execute("SELECT * FROM EUC_MIGRATION_RUNS WHERE EUC_ID=? AND MIGRATION_RUN_ID=?", (euc_id, run_id)).fetchone()
        return conn.execute("""SELECT * FROM EUC_MIGRATION_RUNS WHERE EUC_ID=? AND STATUS='COMPLETED'
            ORDER BY STARTED_AT DESC,ROWID DESC LIMIT 1""", (euc_id,)).fetchone()

    def _required_run(self, conn, euc_id):
        run = self._run(conn, euc_id)
        if not run: raise KeyError("Stage 2.4 migration intelligence has not been built")
        return run

    def _table_result(self, euc_id, user_id, table, key, order, json_columns=None):
        conn = database._get_connection()
        try:
            self._asset_access(conn, euc_id, user_id); run = self._required_run(conn, euc_id)
            items = []
            for row in conn.execute(f"SELECT * FROM {table} WHERE MIGRATION_RUN_ID=? ORDER BY {order}", (run["MIGRATION_RUN_ID"],)):
                item = self._lower(row)
                for source, target in (json_columns or {}).items(): item[target] = _json(item.pop(source), {})
                items.append(item)
            return {"migration_run_id": run["MIGRATION_RUN_ID"], key: items, "count": len(items)}
        finally: conn.close()

    @staticmethod
    def _lower(row): return {key.lower(): row[key] for key in row.keys()}

    @staticmethod
    def _progress(conn, run_id, status, progress, step):
        conn.execute("UPDATE EUC_MIGRATION_RUNS SET STATUS=?,PROGRESS=?,CURRENT_STEP=? WHERE MIGRATION_RUN_ID=?",
                     (status, progress, step, run_id)); conn.commit()

    @staticmethod
    def _reference(conn, parent, child, kind, position):
        if child: conn.execute("INSERT OR IGNORE INTO OBJECT_REFERENCES VALUES (?,?,?,?)", (parent, child, kind, position))

    @staticmethod
    def _timeout(started):
        if time.perf_counter() - started > settings.migration_analysis_timeout_seconds:
            raise TimeoutError("Stage 2.4 migration analysis exceeded the configured timeout")
