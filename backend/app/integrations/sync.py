"""Synchronization jobs, reconciliation evidence, and conflict operations."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .. import database


def _id(prefix: str) -> str: return f"{prefix}_{uuid.uuid4().hex[:18].upper()}"


def create_sync_job(organization_id: str, payload: dict[str, Any], actor_id: str) -> dict[str, Any]:
    conn = database._get_connection(); now = database._utcnow(); job_id = _id("SYN")
    try:
        source = conn.execute("SELECT 1 FROM INTEGRATION_CONNECTIONS WHERE CONNECTION_ID=? AND ORGANIZATION_ID=?", (payload["source_connection_id"], organization_id)).fetchone()
        if not source: raise ValueError("Source connection does not belong to the organization")
        if payload.get("target_connection_id") and not conn.execute("SELECT 1 FROM INTEGRATION_CONNECTIONS WHERE CONNECTION_ID=? AND ORGANIZATION_ID=?", (payload["target_connection_id"], organization_id)).fetchone(): raise ValueError("Target connection does not belong to the organization")
        conn.execute("INSERT INTO SYNCHRONIZATION_JOBS VALUES (?,?,?,?,?,?,?,?,?,'ACTIVE',?,?,?)",
                     (job_id, organization_id, payload["name"], payload["source_connection_id"], payload.get("target_connection_id"), payload.get("mapping_id"), payload.get("direction", "INBOUND"), payload.get("mode", "INCREMENTAL"), payload.get("deletion_policy", "IGNORE"), payload.get("conflict_strategy", "MANUAL"), actor_id, now, now))
        conn.commit(); return {"sync_job_id": job_id, "status": "ACTIVE", **payload}
    finally: conn.close()


def reconcile_run(conn, organization_id: str, integration_run_id: str, sync_job_id: str | None = None) -> dict[str, Any]:
    now = database._utcnow(); reconciliation_id = _id("REC")
    envelopes = conn.execute("SELECT ENVELOPE_ID,PAYLOAD_HASH,STATUS FROM RAW_INGESTION_ENVELOPES WHERE RUN_ID=? ORDER BY ENVELOPE_ID", (integration_run_id,)).fetchall()
    source_hashes = sorted(row["PAYLOAD_HASH"] for row in envelopes); mapped = [row for row in envelopes if row["STATUS"] == "MAPPED"]
    target_hashes = sorted(row[0] for row in conn.execute("SELECT V.PAYLOAD_HASH FROM CANONICAL_OBJECT_VERSIONS V WHERE V.SOURCE_ENVELOPE_ID IN (SELECT ENVELOPE_ID FROM RAW_INGESTION_ENVELOPES WHERE RUN_ID=?)", (integration_run_id,)))
    source_hash = hashlib.sha256("|".join(source_hashes).encode()).hexdigest(); target_hash = hashlib.sha256("|".join(target_hashes).encode()).hexdigest()
    source_count = len(envelopes); target_count = len(target_hashes); mismatch = source_count - len(mapped); status = "MATCHED" if mismatch == 0 and source_count == target_count else "EXCEPTIONS"
    result = {"source_count": source_count, "target_count": target_count, "matched_count": len(mapped), "mismatch_count": mismatch, "source_hash": source_hash, "target_hash": target_hash}
    conn.execute("INSERT INTO RECONCILIATION_RUNS VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (reconciliation_id, organization_id, sync_job_id, integration_run_id, status, source_count, target_count, len(mapped), mismatch, source_hash, target_hash, json.dumps({"count_equality": True, "payload_hash_evidence": True}), json.dumps(result), now, now))
    return {"reconciliation_id": reconciliation_id, "status": status, **result}


def resolve_conflict(organization_id: str, conflict_id: str, strategy: str, actor_id: str) -> dict[str, Any]:
    allowed = {"SOURCE_WINS", "TARGET_WINS", "LATEST_WINS", "MANUAL"}
    if strategy not in allowed: raise ValueError("Unsupported conflict resolution strategy")
    conn = database._get_connection(); now = database._utcnow()
    try:
        conflict = conn.execute("SELECT * FROM INTEGRATION_CONFLICTS WHERE CONFLICT_ID=? AND ORGANIZATION_ID=? AND STATUS='OPEN'", (conflict_id, organization_id)).fetchone()
        if not conflict: raise KeyError("Open integration conflict does not exist")
        resolved_hash = conflict["SOURCE_HASH"] if strategy == "SOURCE_WINS" else conflict["TARGET_HASH"]
        conn.execute("UPDATE INTEGRATION_CONFLICTS SET RESOLUTION_STRATEGY=?,RESOLVED_HASH=?,STATUS='RESOLVED',RESOLVED_AT=?,RESOLVED_BY=? WHERE CONFLICT_ID=?", (strategy, resolved_hash, now, actor_id, conflict_id)); conn.commit()
        return {"conflict_id": conflict_id, "status": "RESOLVED", "strategy": strategy, "resolved_hash": resolved_hash}
    finally: conn.close()
