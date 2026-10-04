"""Authoritative semantic-object ledger and legacy migration boundary."""

import json
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import settings
from ..manifests import ManifestEngine
from ..storage import ObjectService


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class SemanticLedgerService:
    def __init__(self, db_path: Path | str):
        self.objects = ObjectService.local(db_path)
        self.manifests = ManifestEngine(self.objects)

    def persist_commit(self, conn, commit_id: str, repository_id: str, branch_id: str, state: dict[str, Any]) -> dict[str, Any]:
        result = self.manifests.build(conn, state)
        now = _utcnow()
        conn.execute(
            """
            INSERT INTO COMMIT_MANIFESTS
                (COMMIT_ID,REPOSITORY_ID,BRANCH_ID,ROOT_MANIFEST_HASH,CREATED_AT)
            VALUES (?,?,?,?,?)
            ON CONFLICT(COMMIT_ID) DO UPDATE SET ROOT_MANIFEST_HASH=excluded.ROOT_MANIFEST_HASH
            """,
            (commit_id, repository_id, branch_id, result["root_manifest_hash"], now),
        )
        conn.execute(
            """
            UPDATE COMMITS SET ROOT_MANIFEST_HASH=?,OBJECTS_CREATED=?,OBJECTS_REUSED=?,
                PHYSICAL_BYTES_ADDED=?,LOGICAL_BYTES_CHANGED=? WHERE COMMIT_ID=?
            """,
            (
                result["root_manifest_hash"], result["objects_created"], result["objects_reused"],
                result["new_physical_bytes"], result["logical_changed_bytes"], commit_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO OUTBOX_EVENTS
                (EVENT_ID,EVENT_TYPE,AGGREGATE_ID,PAYLOAD_JSON,STATUS,CREATED_AT)
            VALUES (?, 'COMMIT_CREATED', ?, ?, 'PENDING', ?)
            """,
            (
                f"EVT_{uuid.uuid4().hex[:16].upper()}", commit_id,
                json.dumps({"commit_id": commit_id, "repository_id": repository_id,
                            "branch_id": branch_id, **result}, separators=(",", ":")), now,
            ),
        )
        return result

    def reconstruct(self, conn, commit_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT ROOT_MANIFEST_HASH FROM COMMIT_MANIFESTS WHERE COMMIT_ID=?", (commit_id,)
        ).fetchone()
        if not row:
            return None
        state = self.manifests.hydrate(conn, row[0])
        state["head_commit_id"] = commit_id
        return state

    def migrate_branch(self, conn, branch: Any, state: dict[str, Any]) -> dict[str, Any]:
        commit_id = branch["HEAD_COMMIT_ID"]
        existing = conn.execute(
            "SELECT ROOT_MANIFEST_HASH FROM COMMIT_MANIFESTS WHERE COMMIT_ID=?", (commit_id,)
        ).fetchone()
        if existing:
            return {"root_manifest_hash": existing[0], "objects_created": 0,
                    "objects_reused": 0, "new_physical_bytes": 0, "logical_changed_bytes": 0}
        result = self.persist_commit(
            conn, commit_id, branch["REPOSITORY_ID"], branch["BRANCH_ID"], state,
        )
        conn.execute(
            "UPDATE WORKBOOK_REPOSITORIES SET STORAGE_ENGINE=?,UPDATED_AT=? WHERE REPOSITORY_ID=?",
            ("semantic_object_v1", _utcnow(), branch["REPOSITORY_ID"]),
        )
        return result

    def storage_metrics(self, conn, repository_id: str) -> dict[str, Any]:
        roots = conn.execute(
            "SELECT DISTINCT ROOT_MANIFEST_HASH FROM COMMIT_MANIFESTS WHERE REPOSITORY_ID=?",
            (repository_id,),
        ).fetchall()
        euc_roots = conn.execute(
            """SELECT A.ORIGINAL_OBJECT_HASH,X.RESULT_MANIFEST_HASH,D.GRAPH_MANIFEST_HASH,
                      I.RESULT_MANIFEST_HASH,M.BLUEPRINT_MANIFEST_HASH,AM.MANIFEST_HASH,
                      G.OUTPUT_MANIFEST_HASH,G.BUNDLE_OBJECT_HASH
               FROM EUC_ASSETS A LEFT JOIN EUC_ANALYSIS_RUNS X ON X.ANALYSIS_ID=A.LATEST_ANALYSIS_ID
               LEFT JOIN EUC_DEPENDENCY_RUNS D ON D.DEPENDENCY_RUN_ID=(
                   SELECT D2.DEPENDENCY_RUN_ID FROM EUC_DEPENDENCY_RUNS D2
                   WHERE D2.EUC_ID=A.EUC_ID AND D2.STATUS IN ('COMPLETED','COMPLETED_WITH_WARNINGS')
                   ORDER BY D2.STARTED_AT DESC LIMIT 1
               )
               LEFT JOIN EUC_INTELLIGENCE_RUNS I ON I.INTELLIGENCE_RUN_ID=(
                   SELECT I2.INTELLIGENCE_RUN_ID FROM EUC_INTELLIGENCE_RUNS I2
                   WHERE I2.EUC_ID=A.EUC_ID AND I2.STATUS='COMPLETED'
                   ORDER BY I2.STARTED_AT DESC,I2.ROWID DESC LIMIT 1
               )
               LEFT JOIN EUC_MIGRATION_RUNS M ON M.MIGRATION_RUN_ID=(
                   SELECT M2.MIGRATION_RUN_ID FROM EUC_MIGRATION_RUNS M2
                   WHERE M2.EUC_ID=A.EUC_ID AND M2.STATUS='COMPLETED'
                   ORDER BY M2.STARTED_AT DESC,M2.ROWID DESC LIMIT 1
               )
               LEFT JOIN EUC_APPLICATION_MODELS AM ON AM.APPLICATION_MODEL_ID=(
                   SELECT AM2.APPLICATION_MODEL_ID FROM EUC_APPLICATION_MODELS AM2
                   WHERE AM2.EUC_ID=A.EUC_ID AND AM2.SUPERSEDED_AT IS NULL
                   ORDER BY AM2.CREATED_AT DESC,AM2.ROWID DESC LIMIT 1
               )
               LEFT JOIN EUC_APPLICATION_GENERATION_RUNS G ON G.GENERATION_RUN_ID=(
                   SELECT G2.GENERATION_RUN_ID FROM EUC_APPLICATION_GENERATION_RUNS G2
                   WHERE G2.APPLICATION_MODEL_ID=AM.APPLICATION_MODEL_ID AND G2.STATUS='COMPLETED'
                   ORDER BY G2.STARTED_AT DESC,G2.ROWID DESC LIMIT 1
               )
               WHERE A.REPOSITORY_ID=?""",
            (repository_id,),
        ).fetchall()
        all_roots = [row[0] for row in roots]
        all_roots.extend(value for row in euc_roots for value in row if value)
        reachable = self.reachable_hashes(conn, all_roots)
        if reachable:
            placeholders = ",".join("?" for _ in reachable)
            row = conn.execute(
                f"SELECT COUNT(*),COALESCE(SUM(RAW_SIZE),0),COALESCE(SUM(COMPRESSED_SIZE),0) FROM STORAGE_OBJECTS WHERE OBJECT_HASH IN ({placeholders})",
                tuple(reachable),
            ).fetchone()
        else:
            row = (0, 0, 0)
        logical = conn.execute(
            "SELECT COALESCE(SUM(LOGICAL_BYTES_CHANGED),0) FROM COMMITS WHERE REPOSITORY_ID=?",
            (repository_id,),
        ).fetchone()[0]
        references = conn.execute(
            "SELECT COUNT(*) FROM OBJECT_REFERENCES WHERE PARENT_HASH IN (SELECT OBJECT_HASH FROM STORAGE_OBJECTS)",
        ).fetchone()[0]
        physical = int(row[2] or 0)
        raw = int(row[1] or 0)
        return {
            "storage_engine": "semantic_object_v1", "unique_objects": int(row[0]),
            "object_references": int(references), "logical_bytes": int(logical or raw),
            "raw_object_bytes": raw, "physical_bytes": physical,
            "compression_saved_bytes": max(0, raw - physical),
            "deduplication_saved_bytes": max(0, int(logical or raw) - raw),
            "compression_ratio": round(raw / physical, 2) if physical else 1.0,
            "root_manifests": len(roots), "euc_assets": len(euc_roots),
        }

    def reachable_hashes(self, conn, roots: list[str]) -> set[str]:
        reachable = set(filter(None, roots))
        queue = list(reachable)
        while queue:
            parent = queue.pop()
            for row in conn.execute(
                "SELECT CHILD_HASH FROM OBJECT_REFERENCES WHERE PARENT_HASH=?", (parent,)
            ).fetchall():
                if row[0] not in reachable:
                    reachable.add(row[0]); queue.append(row[0])
        return reachable

    def collect_garbage(self, conn, dry_run: bool = True) -> dict[str, Any]:
        if conn.execute("SELECT 1 FROM LEGAL_HOLDS WHERE STATUS='ACTIVE' LIMIT 1").fetchone():
            raise PermissionError("Garbage collection is blocked by an active legal hold")
        run_id = f"GC_{uuid.uuid4().hex[:16].upper()}"
        now = _utcnow()
        conn.execute("INSERT INTO STORAGE_GC_RUNS (GC_RUN_ID,STARTED_AT,STATUS) VALUES (?,?,'RUNNING')", (run_id, now))
        roots = [row[0] for row in conn.execute("SELECT ROOT_MANIFEST_HASH FROM COMMIT_MANIFESTS").fetchall()]
        roots.extend(row[0] for row in conn.execute("SELECT ORIGINAL_OBJECT_HASH FROM EUC_ASSETS").fetchall())
        roots.extend(row[0] for row in conn.execute(
            "SELECT RESULT_MANIFEST_HASH FROM EUC_ANALYSIS_RUNS WHERE RESULT_MANIFEST_HASH IS NOT NULL"
        ).fetchall())
        roots.extend(row[0] for row in conn.execute(
            "SELECT GRAPH_MANIFEST_HASH FROM EUC_DEPENDENCY_RUNS WHERE GRAPH_MANIFEST_HASH IS NOT NULL"
        ).fetchall())
        roots.extend(row[0] for row in conn.execute(
            "SELECT RESULT_MANIFEST_HASH FROM EUC_INTELLIGENCE_RUNS WHERE RESULT_MANIFEST_HASH IS NOT NULL"
        ).fetchall())
        roots.extend(row[0] for row in conn.execute(
            "SELECT BLUEPRINT_MANIFEST_HASH FROM EUC_MIGRATION_RUNS WHERE BLUEPRINT_MANIFEST_HASH IS NOT NULL"
        ).fetchall())
        roots.extend(row[0] for row in conn.execute(
            "SELECT MANIFEST_HASH FROM EUC_APPLICATION_MODELS WHERE MANIFEST_HASH IS NOT NULL"
        ).fetchall())
        roots.extend(value for row in conn.execute(
            "SELECT OUTPUT_MANIFEST_HASH,BUNDLE_OBJECT_HASH FROM EUC_APPLICATION_GENERATION_RUNS WHERE STATUS='COMPLETED'"
        ).fetchall() for value in row if value)
        reachable = self.reachable_hashes(conn, roots)
        candidates = conn.execute(
            "SELECT OBJECT_HASH,COMPRESSED_SIZE FROM STORAGE_OBJECTS WHERE STATUS='AVAILABLE'"
        ).fetchall()
        garbage = [row for row in candidates if row[0] not in reachable]
        reclaimed = sum(int(row[1]) for row in garbage)
        if not dry_run:
            for row in garbage:
                self.objects.store.delete(row[0])
                conn.execute("DELETE FROM STORAGE_OBJECTS WHERE OBJECT_HASH=?", (row[0],))
                conn.execute("DELETE FROM OBJECT_REFERENCES WHERE PARENT_HASH=? OR CHILD_HASH=?", (row[0], row[0]))
        conn.execute(
            "UPDATE STORAGE_GC_RUNS SET COMPLETED_AT=?,STATUS=?,OBJECTS_SCANNED=?,OBJECTS_DELETED=?,BYTES_RECLAIMED=?,DETAILS_JSON=? WHERE GC_RUN_ID=?",
            (_utcnow(), "DRY_RUN" if dry_run else "COMPLETED", len(candidates), len(garbage) if not dry_run else 0,
             reclaimed if not dry_run else 0, json.dumps({"candidates": len(garbage), "candidate_bytes": reclaimed}), run_id),
        )
        return {"gc_run_id": run_id, "dry_run": dry_run, "objects_scanned": len(candidates),
                "objects_candidates": len(garbage), "candidate_bytes": reclaimed}

    def begin_idempotent(self, conn, key: str, actor_id: str, operation: str, request: Any) -> dict[str, Any] | None:
        request_hash = hashlib.sha256(
            json.dumps(request, sort_keys=True, default=str, separators=(",", ":")).encode()
        ).hexdigest()
        existing = conn.execute(
            "SELECT REQUEST_HASH,RESPONSE_JSON,STATUS,EXPIRES_AT FROM IDEMPOTENCY_KEYS WHERE IDEMPOTENCY_KEY=? AND ACTOR_ID=? AND OPERATION=?",
            (key, actor_id, operation),
        ).fetchone()
        now = datetime.now(timezone.utc)
        if existing:
            if existing[0] != request_hash:
                raise ValueError("An Idempotency-Key cannot be reused with a different request")
            if existing[2] == "COMPLETED" and existing[1]:
                return json.loads(existing[1])
            expires_at = existing[3]
            is_expired = expires_at and datetime.fromisoformat(expires_at) <= now
            if existing[2] == "PROCESSING" and is_expired:
                # The request that owned this key crashed/never finished
                # before its TTL — treat it as abandoned instead of
                # permanently blocking every future retry with this key.
                conn.execute(
                    "DELETE FROM IDEMPOTENCY_KEYS WHERE IDEMPOTENCY_KEY=? AND ACTOR_ID=? AND OPERATION=?",
                    (key, actor_id, operation),
                )
                conn.commit()
            else:
                raise RuntimeError("An identical request is already being processed")
        conn.execute(
            "INSERT INTO IDEMPOTENCY_KEYS VALUES (?,?,?,?,NULL,'PROCESSING',?,?)",
            (key, actor_id, operation, request_hash, now.isoformat(),
             (now + timedelta(hours=settings.idempotency_ttl_hours)).isoformat()),
        )
        conn.commit()
        return None

    def finish_idempotent(self, conn, key: str, actor_id: str, operation: str, response: Any) -> None:
        conn.execute(
            "UPDATE IDEMPOTENCY_KEYS SET RESPONSE_JSON=?,STATUS='COMPLETED' WHERE IDEMPOTENCY_KEY=? AND ACTOR_ID=? AND OPERATION=?",
            (json.dumps(response, default=str, separators=(",", ":")), key, actor_id, operation),
        )
        conn.commit()

    def abandon_idempotent(self, conn, key: str, actor_id: str, operation: str) -> None:
        conn.execute(
            "DELETE FROM IDEMPOTENCY_KEYS WHERE IDEMPOTENCY_KEY=? AND ACTOR_ID=? AND OPERATION=? AND STATUS='PROCESSING'",
            (key, actor_id, operation),
        )
        conn.commit()


def ledger_for(db_path: Path | str) -> SemanticLedgerService:
    return SemanticLedgerService(db_path)


def ledger_for_connection(conn) -> SemanticLedgerService:
    database_path = conn.execute("PRAGMA database_list").fetchone()[2]
    return SemanticLedgerService(database_path)
