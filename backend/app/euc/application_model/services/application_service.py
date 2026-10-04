"""Stage 2.5 AIR lifecycle, review gate, lineage, and scaffold generation."""

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
from ..builder import AIRBuilder, stable_id
from ..compiler import ScaffoldCompiler
from ..validator import AIRValidator


TARGET_PROFILES = {"WEB_POSTGRES_FASTAPI_REACT", "MODEL_ONLY"}
GENERATION_MODES = {"MODEL_ONLY", "SCAFFOLD"}
REVIEW_DECISIONS = {"CONFIRMED", "REJECTED"}


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20].upper()}"


def _lower(row) -> dict:
    return {key.lower(): row[key] for key in row.keys()}


class ApplicationModelService:
    def build(self, euc_id: str, user_id: str, target_profile: str | None = None) -> dict:
        target_profile = (target_profile or settings.air_default_target_profile).upper()
        if target_profile not in TARGET_PROFILES:
            raise ValueError("Unsupported AIR target profile")
        conn = database._get_connection()
        started = time.perf_counter()
        try:
            asset = self._asset(conn, euc_id, user_id, edit=True)
            migration = self._migration(conn, euc_id)
            inventory = conn.execute("SELECT * FROM EUC_ANALYSIS_RUNS WHERE ANALYSIS_ID=?", (migration["INVENTORY_ANALYSIS_ID"],)).fetchone()
            dependency = conn.execute("SELECT * FROM EUC_DEPENDENCY_RUNS WHERE DEPENDENCY_RUN_ID=?", (migration["DEPENDENCY_RUN_ID"],)).fetchone()
            ledger = ledger_for_connection(conn)
            blueprint = ledger.objects.get(conn, migration["BLUEPRINT_MANIFEST_HASH"])
            air = AIRBuilder(conn, ledger, asset, inventory, dependency, migration, blueprint,
                             settings.air_auto_generate_confidence, settings.air_review_confidence).build(
                                 target_profile, settings.air_engine_version
                             )
            validation = AIRValidator().validate(air, int(migration["CRITICAL_BLOCKER_COUNT"] or 0))
            confidence = AIRValidator().confidence(air, validation)
            air["validation"] = validation
            air["migration_confidence"] = confidence
            stored = ledger.objects.put(conn, "EUC_APPLICATION_MODEL", air)
            model_id = stable_id("APP", euc_id, migration["MIGRATION_RUN_ID"], target_profile, stored["object_hash"])
            existing = conn.execute("SELECT 1 FROM EUC_APPLICATION_MODELS WHERE APPLICATION_MODEL_ID=?", (model_id,)).fetchone()
            if existing:
                conn.rollback()
                return self.overview(euc_id, user_id, model_id)
            review_count = int(air["coverage"]["manual_review"])
            status = "VALIDATION_FAILED" if not validation["valid"] else ("REVIEW_REQUIRED" if review_count else "GENERATED")
            summary = self._summary(air)
            now = database._utcnow()
            conn.execute(
                """INSERT INTO EUC_APPLICATION_MODELS
                   (APPLICATION_MODEL_ID,EUC_ID,SOURCE_COMMIT_ID,MIGRATION_RUN_ID,AIR_VERSION,TARGET_PROFILE,
                    STATUS,RAW_COVERAGE,CRITICALITY_WEIGHTED_COVERAGE,MIGRATION_CONFIDENCE,
                    REVIEW_REQUIRED_COUNT,VALIDATION_ERROR_COUNT,MANIFEST_HASH,SUMMARY_JSON,CREATED_BY,CREATED_AT)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (model_id, euc_id, migration["SOURCE_COMMIT_ID"], migration["MIGRATION_RUN_ID"],
                 settings.air_engine_version, target_profile, status, air["coverage"]["raw_coverage"],
                 air["coverage"]["criticality_weighted_coverage"], confidence, review_count,
                 len(validation["errors"]), stored["object_hash"], json.dumps(summary, sort_keys=True), user_id, now),
            )
            self._persist_components(conn, ledger, model_id, air, stored["object_hash"])
            self._persist_lineage(conn, model_id, air["lineage"])
            self._reference(conn, stored["object_hash"], migration["BLUEPRINT_MANIFEST_HASH"], "MIGRATION_BLUEPRINT", 0)
            conn.commit()
            duration_ms = round((time.perf_counter() - started) * 1000)
            record_metric("air_generation_duration", duration_ms, "ms", user_id=user_id,
                          repository_id=asset["REPOSITORY_ID"], tags={"components": summary["component_total"]})
            record_audit_event("EUC_APPLICATION_MODEL_GENERATED", actor_user_id=user_id,
                               repository_id=asset["REPOSITORY_ID"], payload={"euc_id": euc_id,
                               "application_model_id": model_id, "manifest_hash": stored["object_hash"],
                               "migration_confidence": confidence})
            return self.overview(euc_id, user_id, model_id)
        finally:
            conn.close()

    def overview(self, euc_id: str, user_id: str, model_id: str | None = None) -> dict:
        conn = database._get_connection()
        try:
            self._asset(conn, euc_id, user_id)
            model = self._model(conn, euc_id, model_id)
            if not model:
                return {"euc_id": euc_id, "status": "NOT_BUILT"}
            migration = conn.execute("SELECT SOURCE_COMMIT_ID FROM EUC_MIGRATION_RUNS WHERE MIGRATION_RUN_ID=?", (model["MIGRATION_RUN_ID"],)).fetchone()
            summary = json.loads(model["SUMMARY_JSON"] or "{}")
            decisions = Counter(row[0] for row in conn.execute("SELECT DECISION FROM EUC_APPLICATION_REVIEWS WHERE APPLICATION_MODEL_ID=?", (model["APPLICATION_MODEL_ID"],)))
            return {"application_model_id": model["APPLICATION_MODEL_ID"], "euc_id": euc_id,
                    "status": model["STATUS"], "air_version": model["AIR_VERSION"],
                    "target_profile": model["TARGET_PROFILE"], "source_commit_id": model["SOURCE_COMMIT_ID"],
                    "migration_run_id": model["MIGRATION_RUN_ID"], "manifest_hash": model["MANIFEST_HASH"],
                    "raw_coverage": model["RAW_COVERAGE"],
                    "criticality_weighted_coverage": model["CRITICALITY_WEIGHTED_COVERAGE"],
                    "migration_confidence": model["MIGRATION_CONFIDENCE"],
                    "review_required_count": model["REVIEW_REQUIRED_COUNT"],
                    "validation_error_count": model["VALIDATION_ERROR_COUNT"], "summary": summary,
                    "reviews": dict(decisions), "approved_by": model["APPROVED_BY"],
                    "approved_at": model["APPROVED_AT"], "created_at": model["CREATED_AT"]}
        finally:
            conn.close()

    def manifest(self, euc_id: str, user_id: str) -> dict:
        conn = database._get_connection()
        try:
            self._asset(conn, euc_id, user_id); model = self._required_model(conn, euc_id)
            return ledger_for_connection(conn).objects.get(conn, model["MANIFEST_HASH"])
        finally:
            conn.close()

    def components(self, euc_id: str, user_id: str, component_type: str | None = None) -> dict:
        conn = database._get_connection()
        try:
            self._asset(conn, euc_id, user_id); model = self._required_model(conn, euc_id)
            clauses, params = ["APPLICATION_MODEL_ID=?"], [model["APPLICATION_MODEL_ID"]]
            if component_type:
                clauses.append("COMPONENT_TYPE=?"); params.append(component_type.upper())
            rows = conn.execute(f"SELECT * FROM EUC_APPLICATION_COMPONENTS WHERE {' AND '.join(clauses)} ORDER BY COMPONENT_TYPE,NAME", params).fetchall()
            items = []
            ledger = ledger_for_connection(conn)
            for row in rows:
                item = ledger.objects.get(conn, row["OBJECT_HASH"])
                item["review_state"] = row["REVIEW_STATE"]
                items.append(item)
            return {"application_model_id": model["APPLICATION_MODEL_ID"], "items": items, "count": len(items)}
        finally:
            conn.close()

    def lineage(self, euc_id: str, user_id: str, query: str = "") -> dict:
        conn = database._get_connection()
        try:
            self._asset(conn, euc_id, user_id); model = self._required_model(conn, euc_id)
            params: list[Any] = [model["APPLICATION_MODEL_ID"]]
            where = "APPLICATION_MODEL_ID=?"
            if query:
                where += " AND (SOURCE_ID LIKE ? OR SOURCE_LOCATION LIKE ? OR TARGET_ID LIKE ? OR TARGET_PATH LIKE ?)"
                token = f"%{query}%"; params.extend([token] * 4)
            rows = conn.execute(f"SELECT * FROM EUC_APPLICATION_LINEAGE WHERE {where} ORDER BY SOURCE_TYPE,SOURCE_LOCATION,TARGET_TYPE LIMIT 1000", params).fetchall()
            return {"application_model_id": model["APPLICATION_MODEL_ID"], "items": [_lower(row) for row in rows]}
        finally:
            conn.close()

    def review(self, euc_id: str, component_id: str, user_id: str, decision: str, reason: str) -> dict:
        decision = decision.upper()
        if decision not in REVIEW_DECISIONS:
            raise ValueError("Review decision must be CONFIRMED or REJECTED")
        if len(reason.strip()) < 3:
            raise ValueError("A review reason is required")
        conn = database._get_connection()
        try:
            asset = self._asset(conn, euc_id, user_id, edit=True); model = self._required_model(conn, euc_id)
            component = conn.execute("SELECT * FROM EUC_APPLICATION_COMPONENTS WHERE APPLICATION_MODEL_ID=? AND COMPONENT_ID=?", (model["APPLICATION_MODEL_ID"], component_id)).fetchone()
            if not component:
                raise KeyError("AIR component does not exist")
            now = database._utcnow()
            self._write_review(conn, model["APPLICATION_MODEL_ID"], component_id, decision, reason.strip(), user_id, now)
            self._refresh_model_status(conn, model)
            conn.commit()
            record_audit_event("EUC_APPLICATION_COMPONENT_REVIEWED", actor_user_id=user_id,
                               repository_id=asset["REPOSITORY_ID"], payload={"euc_id": euc_id,
                               "application_model_id": model["APPLICATION_MODEL_ID"], "component_id": component_id,
                               "decision": decision, "reason": reason.strip()})
            return self.overview(euc_id, user_id, model["APPLICATION_MODEL_ID"])
        finally:
            conn.close()

    def bulk_review(
        self, euc_id: str, user_id: str, decision: str, reason: str,
        component_type: str | None = None, min_confidence: float = 0.0,
    ) -> dict:
        """Confirm or reject every still-open (REVIEW_REQUIRED) component
        matching an optional type/confidence filter, in one transaction --
        the practical fix for a real AIR routinely having dozens of
        components needing one-by-one review before approval is even
        possible. Each component still gets its own EUC_APPLICATION_REVIEWS
        row with the same reason and actor, so the audit trail reads
        identically to reviewing them one at a time by hand."""
        decision = decision.upper()
        if decision not in REVIEW_DECISIONS:
            raise ValueError("Review decision must be CONFIRMED or REJECTED")
        if len(reason.strip()) < 3:
            raise ValueError("A review reason is required")
        conn = database._get_connection()
        try:
            asset = self._asset(conn, euc_id, user_id, edit=True); model = self._required_model(conn, euc_id)
            clauses = ["APPLICATION_MODEL_ID=?", "REVIEW_STATE='REVIEW_REQUIRED'", "CONFIDENCE>=?"]
            params: list[Any] = [model["APPLICATION_MODEL_ID"], min_confidence]
            if component_type:
                clauses.append("COMPONENT_TYPE=?")
                params.append(component_type.upper())
            targets = conn.execute(
                f"SELECT COMPONENT_ID FROM EUC_APPLICATION_COMPONENTS WHERE {' AND '.join(clauses)}", params
            ).fetchall()
            now = database._utcnow()
            for row in targets:
                self._write_review(conn, model["APPLICATION_MODEL_ID"], row["COMPONENT_ID"], decision, reason.strip(), user_id, now)
            self._refresh_model_status(conn, model)
            conn.commit()
            record_audit_event("EUC_APPLICATION_COMPONENTS_BULK_REVIEWED", actor_user_id=user_id,
                               repository_id=asset["REPOSITORY_ID"], payload={"euc_id": euc_id,
                               "application_model_id": model["APPLICATION_MODEL_ID"], "decision": decision,
                               "reason": reason.strip(), "component_type": component_type,
                               "min_confidence": min_confidence, "count": len(targets)})
            result = self.overview(euc_id, user_id, model["APPLICATION_MODEL_ID"])
            result["bulk_reviewed_count"] = len(targets)
            return result
        finally:
            conn.close()

    @staticmethod
    def _write_review(conn, application_model_id: str, component_id: str, decision: str, reason: str, user_id: str, now: str) -> None:
        conn.execute(
            """INSERT INTO EUC_APPLICATION_REVIEWS VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(APPLICATION_MODEL_ID,COMPONENT_ID) DO UPDATE SET
               DECISION=excluded.DECISION,REASON=excluded.REASON,ACTOR_USER_ID=excluded.ACTOR_USER_ID,CREATED_AT=excluded.CREATED_AT""",
            (_id("REV"), application_model_id, component_id, decision, reason, user_id, now),
        )
        conn.execute("UPDATE EUC_APPLICATION_COMPONENTS SET REVIEW_STATE=? WHERE APPLICATION_MODEL_ID=? AND COMPONENT_ID=?", (decision, application_model_id, component_id))

    @staticmethod
    def _refresh_model_status(conn, model) -> None:
        pending = conn.execute("SELECT COUNT(*) FROM EUC_APPLICATION_COMPONENTS WHERE APPLICATION_MODEL_ID=? AND REVIEW_STATE='REVIEW_REQUIRED'", (model["APPLICATION_MODEL_ID"],)).fetchone()[0]
        status = "VALIDATION_FAILED" if model["VALIDATION_ERROR_COUNT"] else ("GENERATED" if pending == 0 else "REVIEW_REQUIRED")
        conn.execute("UPDATE EUC_APPLICATION_MODELS SET REVIEW_REQUIRED_COUNT=?,STATUS=? WHERE APPLICATION_MODEL_ID=?", (pending, status, model["APPLICATION_MODEL_ID"]))

    def approve(self, euc_id: str, user_id: str) -> dict:
        conn = database._get_connection()
        try:
            asset = self._asset(conn, euc_id, user_id)
            repository = _repository_access(conn, asset["REPOSITORY_ID"], user_id)
            if repository["ACCESS_ROLE"] != "owner":
                raise PermissionError("Only the repository owner can approve an application model")
            model = self._required_model(conn, euc_id)
            if model["VALIDATION_ERROR_COUNT"]:
                raise ValueError("AIR validation errors must be resolved before approval")
            if model["REVIEW_REQUIRED_COUNT"]:
                raise ValueError("All inferred components requiring review must be confirmed or rejected")
            now = database._utcnow()
            conn.execute("UPDATE EUC_APPLICATION_MODELS SET STATUS='APPROVED',APPROVED_BY=?,APPROVED_AT=? WHERE APPLICATION_MODEL_ID=?", (user_id, now, model["APPLICATION_MODEL_ID"]))
            conn.commit()
            record_audit_event("EUC_APPLICATION_MODEL_APPROVED", actor_user_id=user_id,
                               repository_id=asset["REPOSITORY_ID"], payload={"euc_id": euc_id,
                               "application_model_id": model["APPLICATION_MODEL_ID"], "manifest_hash": model["MANIFEST_HASH"]})
            return self.overview(euc_id, user_id, model["APPLICATION_MODEL_ID"])
        finally:
            conn.close()

    def generate(self, euc_id: str, user_id: str, mode: str, target_profile: str | None = None) -> dict:
        mode = mode.upper(); target_profile = (target_profile or settings.air_default_target_profile).upper()
        if mode not in GENERATION_MODES:
            raise ValueError("Generation mode must be MODEL_ONLY or SCAFFOLD")
        if target_profile not in TARGET_PROFILES:
            raise ValueError("Unsupported generation target profile")
        conn = database._get_connection()
        run_id = _id("GEN")
        try:
            asset = self._asset(conn, euc_id, user_id, edit=True); model = self._required_model(conn, euc_id)
            if mode == "SCAFFOLD" and model["STATUS"] not in {"APPROVED", "CODE_GENERATED"}:
                raise ValueError("Approve the validation-clean AIR before scaffold generation")
            now = database._utcnow()
            conn.execute("""INSERT INTO EUC_APPLICATION_GENERATION_RUNS
                (GENERATION_RUN_ID,APPLICATION_MODEL_ID,GENERATOR_VERSION,TARGET_PROFILE,GENERATION_MODE,
                 SOURCE_MANIFEST_HASH,STATUS,STARTED_BY,STARTED_AT) VALUES (?,?,?,?,?,?,'GENERATING',?,?)""",
                (run_id, model["APPLICATION_MODEL_ID"], settings.air_generator_version, target_profile,
                 mode, model["MANIFEST_HASH"], user_id, now))
            ledger = ledger_for_connection(conn); air = ledger.objects.get(conn, model["MANIFEST_HASH"])
            decisions = {row[0] for row in conn.execute("SELECT COMPONENT_ID FROM EUC_APPLICATION_REVIEWS WHERE APPLICATION_MODEL_ID=? AND DECISION='REJECTED'", (model["APPLICATION_MODEL_ID"],))}
            files = {"air/application-model.json": json.dumps(air, indent=2, sort_keys=True).encode("utf-8")}
            if mode == "SCAFFOLD":
                files.update(ScaffoldCompiler().compile(air, decisions))
            file_entries = []
            for path, payload in sorted(files.items()):
                stored = ledger.objects.put_bytes(conn, "EUC_GENERATED_FILE", payload)
                file_entries.append({"path": path, "object_hash": stored["object_hash"], "size_bytes": len(payload),
                                     "ownership": "GENERATED", "provenance_manifest_hash": model["MANIFEST_HASH"]})
            output = {"schema": "gitwalk.generated-application.v1", "generator_version": settings.air_generator_version,
                      "application_model_id": model["APPLICATION_MODEL_ID"], "source_manifest_hash": model["MANIFEST_HASH"],
                      "target_profile": target_profile, "generation_mode": mode, "files": file_entries,
                      "customization_policy": {"generated_root": "generated/", "custom_root": "custom/", "overwrite_customized": False}}
            output_stored = ledger.objects.put(conn, "EUC_GENERATED_APPLICATION", output)
            bundle = ScaffoldCompiler.bundle(files)
            bundle_stored = ledger.objects.put_bytes(conn, "EUC_GENERATED_BUNDLE", bundle)
            for position, item in enumerate(file_entries):
                self._reference(conn, output_stored["object_hash"], item["object_hash"], "GENERATED_FILE", position)
            self._reference(conn, output_stored["object_hash"], model["MANIFEST_HASH"], "AIR_MODEL", len(file_entries))
            self._reference(conn, output_stored["object_hash"], bundle_stored["object_hash"], "GENERATED_BUNDLE", len(file_entries) + 1)
            version = f"APP_{output_stored['object_hash'][:16].upper()}"
            test_count = sum(path.startswith("tests/") or "/tests/" in path for path in files)
            conn.execute("""UPDATE EUC_APPLICATION_GENERATION_RUNS SET OUTPUT_MANIFEST_HASH=?,BUNDLE_OBJECT_HASH=?,
                APPLICATION_VERSION=?,STATUS='COMPLETED',FILES_GENERATED=?,TESTS_GENERATED=?,COMPLETED_AT=?
                WHERE GENERATION_RUN_ID=?""", (output_stored["object_hash"], bundle_stored["object_hash"], version,
                len(files), test_count, database._utcnow(), run_id))
            if mode == "SCAFFOLD":
                conn.execute("UPDATE EUC_APPLICATION_MODELS SET STATUS='CODE_GENERATED' WHERE APPLICATION_MODEL_ID=?", (model["APPLICATION_MODEL_ID"],))
            conn.commit()
            record_audit_event("EUC_APPLICATION_SCAFFOLD_GENERATED", actor_user_id=user_id,
                               repository_id=asset["REPOSITORY_ID"], payload={"euc_id": euc_id,
                               "generation_run_id": run_id, "mode": mode, "application_version": version,
                               "files_generated": len(files), "bundle_object_hash": bundle_stored["object_hash"]})
            return self.generation(euc_id, user_id, run_id)
        except Exception as exc:
            conn.rollback()
            try:
                conn.execute("UPDATE EUC_APPLICATION_GENERATION_RUNS SET STATUS='FAILED',ERROR_MESSAGE=?,COMPLETED_AT=? WHERE GENERATION_RUN_ID=?", (str(exc), database._utcnow(), run_id)); conn.commit()
            except Exception:
                conn.rollback()
            raise
        finally:
            conn.close()

    def generation(self, euc_id: str, user_id: str, run_id: str | None = None) -> dict:
        conn = database._get_connection()
        try:
            self._asset(conn, euc_id, user_id); model = self._required_model(conn, euc_id)
            if run_id:
                row = conn.execute("SELECT * FROM EUC_APPLICATION_GENERATION_RUNS WHERE APPLICATION_MODEL_ID=? AND GENERATION_RUN_ID=?", (model["APPLICATION_MODEL_ID"], run_id)).fetchone()
            else:
                row = conn.execute("SELECT * FROM EUC_APPLICATION_GENERATION_RUNS WHERE APPLICATION_MODEL_ID=? ORDER BY STARTED_AT DESC,ROWID DESC LIMIT 1", (model["APPLICATION_MODEL_ID"],)).fetchone()
            return {"status": "NOT_GENERATED"} if not row else _lower(row)
        finally:
            conn.close()

    def download(self, euc_id: str, generation_run_id: str, user_id: str) -> tuple[str, bytes]:
        conn = database._get_connection()
        try:
            self._asset(conn, euc_id, user_id); model = self._required_model(conn, euc_id)
            row = conn.execute("SELECT * FROM EUC_APPLICATION_GENERATION_RUNS WHERE APPLICATION_MODEL_ID=? AND GENERATION_RUN_ID=? AND STATUS='COMPLETED'", (model["APPLICATION_MODEL_ID"], generation_run_id)).fetchone()
            if not row:
                raise KeyError("Generated application bundle does not exist")
            payload = ledger_for_connection(conn).objects.get_bytes(conn, row["BUNDLE_OBJECT_HASH"])
            return f"{row['APPLICATION_VERSION'].lower()}.zip", payload
        finally:
            conn.close()

    def _generation_output(self, conn, model, generation_run_id: str):
        row = conn.execute(
            "SELECT * FROM EUC_APPLICATION_GENERATION_RUNS WHERE APPLICATION_MODEL_ID=? AND GENERATION_RUN_ID=? AND STATUS='COMPLETED'",
            (model["APPLICATION_MODEL_ID"], generation_run_id),
        ).fetchone()
        if not row:
            raise KeyError("Generated application bundle does not exist")
        return ledger_for_connection(conn).objects.get(conn, row["OUTPUT_MANIFEST_HASH"])

    def generation_files(self, euc_id: str, user_id: str, generation_run_id: str) -> list[dict]:
        """The generated bundle's file listing without downloading the zip
        -- lets the UI show a real file tree/preview instead of a bundle
        the reviewer can only inspect after downloading and unzipping."""
        conn = database._get_connection()
        try:
            self._asset(conn, euc_id, user_id); model = self._required_model(conn, euc_id)
            output = self._generation_output(conn, model, generation_run_id)
            return [{"path": item["path"], "size_bytes": item["size_bytes"]} for item in output["files"]]
        finally:
            conn.close()

    def generation_file(self, euc_id: str, user_id: str, generation_run_id: str, path: str) -> str:
        conn = database._get_connection()
        try:
            self._asset(conn, euc_id, user_id); model = self._required_model(conn, euc_id)
            ledger = ledger_for_connection(conn)
            output = self._generation_output(conn, model, generation_run_id)
            entry = next((item for item in output["files"] if item["path"] == path), None)
            if not entry:
                raise KeyError("File does not exist in this generated bundle")
            payload = ledger.objects.get_bytes(conn, entry["object_hash"])
            try:
                return payload.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("This file is binary and cannot be previewed as text")
        finally:
            conn.close()

    def _persist_components(self, conn, ledger, model_id, air, manifest_hash):
        for key, values in air.items():
            if not isinstance(values, list):
                continue
            for item in values:
                if not isinstance(item, dict) or not item.get("component_id"):
                    continue
                stored = ledger.objects.put(conn, f"EUC_AIR_{item['component_type']}", item)
                conn.execute("""INSERT INTO EUC_APPLICATION_COMPONENTS VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                             (model_id, item["component_id"], item["component_type"], item["name"],
                              item.get("domain_id"), item["confidence"], item["generation_policy"],
                              item["review_state"], item.get("source_unit_id"),
                              json.dumps(item["provenance"], sort_keys=True), stored["object_hash"]))
                self._reference(conn, manifest_hash, stored["object_hash"], "AIR_COMPONENT", 0)

    @staticmethod
    def _persist_lineage(conn, model_id, lineage):
        for item in lineage:
            conn.execute("INSERT INTO EUC_APPLICATION_LINEAGE VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (model_id, item["lineage_id"], item["source_type"], item["source_id"],
                          item.get("source_location"), item["target_type"], item["target_id"],
                          item.get("target_path"), item["relationship_type"], item["confidence"]))

    @staticmethod
    def _summary(air):
        keys = ["domains", "entities", "relationships", "calculations", "business_rules", "services",
                "apis", "screens", "workflows", "integrations", "controls", "roles", "jobs", "tests"]
        counts = {key: len(air.get(key, [])) for key in keys}
        return {"counts": counts, "component_total": sum(counts.values()), "coverage": air["coverage"],
                "validation": air["validation"], "generation_modes": ["MODEL_ONLY", "SCAFFOLD"]}

    def _asset(self, conn, euc_id, user_id, edit=False):
        asset = conn.execute("SELECT * FROM EUC_ASSETS WHERE EUC_ID=?", (euc_id,)).fetchone()
        if not asset:
            raise KeyError("EUC asset does not exist")
        _repository_access(conn, asset["REPOSITORY_ID"], user_id, edit=edit)
        return asset

    @staticmethod
    def _migration(conn, euc_id):
        row = conn.execute("SELECT * FROM EUC_MIGRATION_RUNS WHERE EUC_ID=? AND STATUS='COMPLETED' ORDER BY STARTED_AT DESC,ROWID DESC LIMIT 1", (euc_id,)).fetchone()
        if not row:
            raise ValueError("A completed Stage 2.4 migration blueprint is required")
        return row

    @staticmethod
    def _model(conn, euc_id, model_id=None):
        if model_id:
            return conn.execute("SELECT * FROM EUC_APPLICATION_MODELS WHERE EUC_ID=? AND APPLICATION_MODEL_ID=?", (euc_id, model_id)).fetchone()
        return conn.execute("SELECT * FROM EUC_APPLICATION_MODELS WHERE EUC_ID=? AND SUPERSEDED_AT IS NULL ORDER BY CREATED_AT DESC,ROWID DESC LIMIT 1", (euc_id,)).fetchone()

    def _required_model(self, conn, euc_id):
        model = self._model(conn, euc_id)
        if not model:
            raise KeyError("Stage 2.5 application model has not been built")
        return model

    @staticmethod
    def _reference(conn, parent, child, kind, position):
        if parent and child:
            conn.execute("INSERT OR IGNORE INTO OBJECT_REFERENCES VALUES (?,?,?,?)", (parent, child, kind, position))
