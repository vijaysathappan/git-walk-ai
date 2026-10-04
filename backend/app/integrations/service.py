"""Stage 4 integration runtime spanning connectors, canonical data, and digital thread."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Any

from .. import database
from ..access_control.engine import ResourceContext, authorization_engine
from ..observability import record_audit_event, record_metric
from ..services.semantic_ledger_service import ledger_for_connection
from .canonical import apply_mapping
from .connectors.base import ConnectorError
from .connectors.registry import connector_for
from .search import index_item
from .secrets import secret_provider
from .sync import reconcile_run
from .thread import link_nodes, record_thread_event, upsert_node


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:18].upper()}"


def _row(row) -> dict[str, Any]:
    return {key.lower(): row[key] for key in row.keys()}


def _resource(organization_id: str) -> ResourceContext:
    return ResourceContext("ORGANIZATION", organization_id, organization_id=organization_id)


class IntegrationService:
    def _require(self, actor_id: str, permission: str, organization_id: str, conn=None) -> None:
        authorization_engine.require(actor_id, permission, _resource(organization_id), conn=conn)

    @staticmethod
    def _safe_connection(row) -> dict[str, Any]:
        result = _row(row)
        result["configuration"] = json.loads(result.pop("configuration_json"))
        result["credential_configured"] = bool(result.pop("credential_reference", None))
        return result

    @staticmethod
    def _object_bytes(object_hash: str) -> bytes:
        conn = database._get_connection()
        try:
            value = ledger_for_connection(conn).objects.get_bytes(conn, object_hash)
            conn.commit()
            return value
        finally:
            conn.close()

    def connector_types(self, actor_id: str, organization_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "integration.read", organization_id)
        conn = database._get_connection()
        try:
            return [{**_row(row), "capabilities": json.loads(row["CAPABILITIES_JSON"]),
                     "configuration_schema": json.loads(row["CONFIGURATION_SCHEMA_JSON"]),
                     "credential_schema": json.loads(row["CREDENTIAL_SCHEMA_JSON"])}
                    for row in conn.execute("SELECT * FROM CONNECTOR_TYPES WHERE STATUS='ACTIVE' ORDER BY CATEGORY,CONNECTOR_TYPE")]
        finally:
            conn.close()

    def create_connection(self, organization_id: str, payload: dict[str, Any], actor_id: str) -> dict[str, Any]:
        connector_type = str(payload["connector_type"]).upper(); now = database._utcnow(); connection_id = _id("CON")
        conn = database._get_connection()
        credential_reference = None
        try:
            self._require(actor_id, "integration.manage", organization_id, conn)
            if not conn.execute("SELECT 1 FROM CONNECTOR_TYPES WHERE CONNECTOR_TYPE=? AND STATUS='ACTIVE'", (connector_type,)).fetchone():
                raise ValueError("Connector type is not registered")
            credentials = payload.get("credentials") or {}
            if credentials:
                credential_reference = secret_provider.put(organization_id, credentials, actor_id)
            configuration = payload.get("configuration") or {}
            conn.execute(
                """INSERT INTO INTEGRATION_CONNECTIONS
                   (CONNECTION_ID,ORGANIZATION_ID,CONNECTOR_TYPE,NAME,ENVIRONMENT,CONFIGURATION_JSON,CREDENTIAL_REFERENCE,
                    STATUS,HEALTH_STATUS,CREATED_BY,CREATED_AT,UPDATED_AT)
                   VALUES (?,?,?,?,?,?,?,'ACTIVE','UNKNOWN',?,?,?)""",
                (connection_id, organization_id, connector_type, payload["name"].strip(),
                 str(payload.get("environment", "DEVELOPMENT")).upper(), json.dumps(configuration, sort_keys=True),
                 credential_reference, actor_id, now, now),
            )
            node_id = upsert_node(conn, organization_id, "INTEGRATION_CONNECTION", connection_id, payload["name"].strip(), connection_id,
                                  {"connector_type": connector_type, "environment": payload.get("environment", "DEVELOPMENT")})
            record_thread_event(conn, organization_id, node_id, "CONNECTION_REGISTERED", actor_id=actor_id, source_system="GIT_WALK")
            index_item(conn, organization_id, "CONNECTION", connection_id, payload["name"].strip(),
                       f"{connector_type} integration connection", [connector_type, payload.get("environment")],
                       {"connector_type": connector_type, "environment": payload.get("environment", "DEVELOPMENT"), "health": "UNKNOWN"})
            conn.commit()
        except Exception:
            conn.rollback()
            if credential_reference:
                secret_provider.delete(credential_reference, organization_id)
            raise
        finally:
            conn.close()
        record_audit_event("INTEGRATION_CONNECTION_CREATED", actor_user_id=actor_id,
                           payload={"organization_id": organization_id, "connection_id": connection_id, "connector_type": connector_type})
        return self.get_connection(organization_id, connection_id, actor_id)

    def list_connections(self, organization_id: str, actor_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "integration.read", organization_id)
        conn = database._get_connection()
        try:
            return [self._safe_connection(row) for row in conn.execute(
                "SELECT * FROM INTEGRATION_CONNECTIONS WHERE ORGANIZATION_ID=? AND STATUS<>'DELETED' ORDER BY NAME", (organization_id,)
            )]
        finally:
            conn.close()

    def get_connection(self, organization_id: str, connection_id: str, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "integration.read", organization_id)
        conn = database._get_connection()
        try:
            row = conn.execute("SELECT * FROM INTEGRATION_CONNECTIONS WHERE CONNECTION_ID=? AND ORGANIZATION_ID=?", (connection_id, organization_id)).fetchone()
            if not row: raise KeyError("Integration connection does not exist")
            result = self._safe_connection(row)
            result["source_objects"] = [_row(item) for item in conn.execute("SELECT * FROM INTEGRATION_SOURCE_OBJECTS WHERE CONNECTION_ID=? ORDER BY NAME", (connection_id,))]
            return result
        finally:
            conn.close()

    def _runtime(self, organization_id: str, connection_id: str):
        conn = database._get_connection()
        try:
            row = conn.execute("SELECT * FROM INTEGRATION_CONNECTIONS WHERE CONNECTION_ID=? AND ORGANIZATION_ID=? AND STATUS='ACTIVE'", (connection_id, organization_id)).fetchone()
            if not row: raise KeyError("Active integration connection does not exist")
            configuration = json.loads(row["CONFIGURATION_JSON"])
            credentials = secret_provider.get(row["CREDENTIAL_REFERENCE"], organization_id) if row["CREDENTIAL_REFERENCE"] else {}
            return row["CONNECTOR_TYPE"], row["NAME"], configuration, connector_for(row["CONNECTOR_TYPE"], configuration, credentials, self._object_bytes)
        finally:
            conn.close()

    def upload_source(self, organization_id: str, connection_id: str, filename: str, payload: bytes, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "integration.manage", organization_id)
        conn = database._get_connection(); now = database._utcnow(); source_object_id = _id("SRC")
        try:
            row = conn.execute("SELECT CONFIGURATION_JSON,CONNECTOR_TYPE FROM INTEGRATION_CONNECTIONS WHERE CONNECTION_ID=? AND ORGANIZATION_ID=?", (connection_id, organization_id)).fetchone()
            if not row: raise KeyError("Integration connection does not exist")
            if row["CONNECTOR_TYPE"] != "FILE": raise ValueError("Source upload is available only for FILE connections")
            stored = ledger_for_connection(conn).objects.put_bytes(conn, "INTEGRATION_SOURCE_FILE", payload)
            configuration = json.loads(row["CONFIGURATION_JSON"]); configuration["object_hash"] = stored["object_hash"]
            configuration.setdefault("format", filename.rsplit(".", 1)[-1].upper().replace("XLSX", "EXCEL"))
            conn.execute("UPDATE INTEGRATION_CONNECTIONS SET CONFIGURATION_JSON=?,UPDATED_AT=? WHERE CONNECTION_ID=?", (json.dumps(configuration, sort_keys=True), now, connection_id))
            conn.execute(
                """INSERT INTO INTEGRATION_SOURCE_OBJECTS VALUES (?,?,?,?,?,?,NULL,'ACTIVE',?,?)
                   ON CONFLICT(CONNECTION_ID,NAME) DO UPDATE SET SOURCE_OBJECT_HASH=excluded.SOURCE_OBJECT_HASH,EXTERNAL_PATH=excluded.EXTERNAL_PATH,UPDATED_AT=excluded.UPDATED_AT""",
                (source_object_id, connection_id, filename, "FILE", filename, stored["object_hash"], now, now),
            )
            source = conn.execute("SELECT SOURCE_OBJECT_ID FROM INTEGRATION_SOURCE_OBJECTS WHERE CONNECTION_ID=? AND NAME=?", (connection_id, filename)).fetchone()[0]
            connection_node = upsert_node(conn, organization_id, "INTEGRATION_CONNECTION", connection_id, connection_id, connection_id)
            source_node = upsert_node(conn, organization_id, "SOURCE_OBJECT", source, filename, stored["object_hash"], {"bytes": len(payload)})
            link_nodes(conn, organization_id, connection_node, source_node, "EXPOSES", evidence_type="UPLOAD", evidence_reference=stored["object_hash"])
            record_thread_event(conn, organization_id, source_node, "SOURCE_FILE_UPLOADED", actor_id=actor_id, evidence_reference=stored["object_hash"], payload={"filename": filename, "bytes": len(payload)})
            index_item(conn, organization_id, "SOURCE_OBJECT", source, filename, "Immutable integration source file", [connection_id], {"connector_type": "FILE", "format": configuration["format"]})
            conn.commit()
            return {"source_object_id": source, "filename": filename, "object_hash": stored["object_hash"], "bytes": len(payload), "deduplicated": not stored["created"]}
        finally:
            conn.close()

    async def test_connection(self, organization_id: str, connection_id: str, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "integration.execute", organization_id); _type, _name, _config, runtime = self._runtime(organization_id, connection_id)
        started = asyncio.get_running_loop().time(); now = database._utcnow()
        try:
            result = await runtime.test_connection(); health = "HEALTHY"; success = now
        except ConnectorError as exc:
            result = {"status": "FAILED", "code": exc.code, "message": str(exc), "transient": exc.transient}; health = "UNHEALTHY"; success = None
        latency = (asyncio.get_running_loop().time() - started) * 1000; conn = database._get_connection()
        try:
            conn.execute("UPDATE INTEGRATION_CONNECTIONS SET HEALTH_STATUS=?,LAST_TESTED_AT=?,LAST_SUCCESS_AT=COALESCE(?,LAST_SUCCESS_AT),LAST_FAILURE_AT=CASE WHEN ?='UNHEALTHY' THEN ? ELSE LAST_FAILURE_AT END,UPDATED_AT=? WHERE CONNECTION_ID=?",
                         (health, now, success, health, now, now, connection_id))
            conn.commit()
        finally: conn.close()
        record_metric("integration_connection_test", latency, "ms", status="SUCCESS" if health == "HEALTHY" else "FAILED", user_id=actor_id, tags={"connection_id": connection_id})
        return {**result, "health_status": health, "latency_ms": round(latency, 2)}

    async def discover(self, organization_id: str, connection_id: str, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "integration.execute", organization_id); connector_type, name, configuration, runtime = self._runtime(organization_id, connection_id)
        discovered = await runtime.discover(); canonical = json.dumps(discovered, sort_keys=True, default=str, separators=(",", ":")); schema_hash = hashlib.sha256(canonical.encode()).hexdigest(); now = database._utcnow()
        conn = database._get_connection()
        try:
            previous = conn.execute("SELECT DISCOVERED_SCHEMA_JSON FROM INTEGRATION_SOURCE_OBJECTS WHERE CONNECTION_ID=? ORDER BY UPDATED_AT DESC LIMIT 1", (connection_id,)).fetchone()
            previous_hash = hashlib.sha256(previous[0].encode()).hexdigest() if previous and previous[0] else None
            source_object_id = _id("SRC"); source_name = configuration.get("table") or configuration.get("path") or configuration.get("prefix") or name
            conn.execute("""INSERT INTO INTEGRATION_SOURCE_OBJECTS VALUES (?,?,?,?,?,NULL,?,'ACTIVE',?,?)
                            ON CONFLICT(CONNECTION_ID,NAME) DO UPDATE SET DISCOVERED_SCHEMA_JSON=excluded.DISCOVERED_SCHEMA_JSON,UPDATED_AT=excluded.UPDATED_AT""",
                         (source_object_id, connection_id, source_name, discovered.get("object_type", connector_type), source_name, canonical, now, now))
            source_object_id = conn.execute("SELECT SOURCE_OBJECT_ID FROM INTEGRATION_SOURCE_OBJECTS WHERE CONNECTION_ID=? AND NAME=?", (connection_id, source_name)).fetchone()[0]
            drift_id = None
            if previous_hash and previous_hash != schema_hash:
                drift_id = _id("DRF")
                conn.execute("INSERT INTO SCHEMA_DRIFT_EVENTS VALUES (?,?,?,?,?,?,?,'OPEN',?,NULL)",
                             (drift_id, organization_id, connection_id, discovered.get("object_type", connector_type), previous_hash, schema_hash,
                              json.dumps({"change": "SCHEMA_HASH_CHANGED"}), now))
            node = upsert_node(conn, organization_id, "SOURCE_OBJECT", source_object_id, str(source_name), schema_hash, {"connector_type": connector_type})
            record_thread_event(conn, organization_id, node, "SCHEMA_DISCOVERED", actor_id=actor_id, source_system=name, evidence_reference=schema_hash, payload={"drift_id": drift_id})
            index_item(conn, organization_id, "SOURCE_OBJECT", source_object_id, str(source_name), f"Discovered from {name}", [connector_type, schema_hash], {"connector_type": connector_type, "schema_drift": bool(drift_id)})
            conn.commit()
            return {"connection_id": connection_id, "source_object_id": source_object_id, "schema_hash": schema_hash, "drift_id": drift_id, "schema": discovered}
        finally: conn.close()

    async def execute(self, organization_id: str, connection_id: str, actor_id: str, *, mapping_id: str | None = None,
                      idempotency_key: str | None = None, batch_size: int = 1000, source_object_id: str = "") -> dict[str, Any]:
        self._require(actor_id, "integration.execute", organization_id); run_id = _id("RUN"); now = database._utcnow()
        idempotency_key = idempotency_key or _id("IDEM")
        conn = database._get_connection()
        try:
            existing = conn.execute("SELECT * FROM INTEGRATION_RUNS WHERE CONNECTION_ID=? AND IDEMPOTENCY_KEY=?", (connection_id, idempotency_key)).fetchone()
            if existing: return {**_row(existing), "idempotent_replay": True}
            conn.execute("INSERT INTO INTEGRATION_RUNS (RUN_ID,ORGANIZATION_ID,CONNECTION_ID,OPERATION,STATUS,STARTED_AT,ATTEMPT_COUNT,IDEMPOTENCY_KEY,CREATED_BY) VALUES (?,?,?,'INGEST','RUNNING',?,0,?,?)",
                         (run_id, organization_id, connection_id, now, idempotency_key, actor_id)); conn.commit()
        finally: conn.close()
        connector_type, connection_name, configuration, runtime = self._runtime(organization_id, connection_id)
        checkpoint_conn = database._get_connection()
        try:
            checkpoint_row = checkpoint_conn.execute("SELECT CHECKPOINT_JSON FROM INTEGRATION_CHECKPOINTS WHERE CONNECTION_ID=? AND SOURCE_OBJECT_ID=?", (connection_id, source_object_id)).fetchone()
            checkpoint = json.loads(checkpoint_row[0]) if checkpoint_row else {}
        finally: checkpoint_conn.close()
        max_attempts = max(1, min(int(configuration.get("max_attempts", 3)), 8)); attempt = 0
        try:
            while True:
                attempt += 1
                try:
                    result = await runtime.read({"checkpoint": checkpoint, "batch_size": min(max(batch_size, 1), 10000)})
                    break
                except ConnectorError as exc:
                    if not exc.transient or attempt >= max_attempts: raise
                    await asyncio.sleep(min(0.25 * (2 ** (attempt - 1)), 3))
            conn = database._get_connection(); mapped = quarantined = duplicates = 0; envelope_ids = []
            try:
                run_node = upsert_node(conn, organization_id, "INTEGRATION_RUN", run_id, f"{connection_name} ingestion", run_id, {"status": "RUNNING"})
                connection_node = upsert_node(conn, organization_id, "INTEGRATION_CONNECTION", connection_id, connection_name, connection_id)
                link_nodes(conn, organization_id, connection_node, run_node, "TRIGGERED", evidence_type="RUN", evidence_reference=run_id)
                for record in result.records:
                    stored = ledger_for_connection(conn).objects.put(conn, "RAW_INTEGRATION_PAYLOAD", record.payload)
                    envelope_id = _id("ENV"); ingested_at = database._utcnow()
                    cursor = conn.execute(
                        """INSERT OR IGNORE INTO RAW_INGESTION_ENVELOPES
                           (ENVELOPE_ID,ORGANIZATION_ID,RUN_ID,CONNECTION_ID,SOURCE_SYSTEM,EXTERNAL_TYPE,EXTERNAL_ID,SOURCE_VERSION,
                            SOURCE_TIMESTAMP,INGESTED_AT,PAYLOAD_HASH,METADATA_JSON,STATUS)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'INGESTED')""",
                        (envelope_id, organization_id, run_id, connection_id, connection_name, record.external_type, record.external_id,
                         record.source_version, record.source_timestamp, ingested_at, stored["object_hash"], json.dumps(record.metadata, sort_keys=True, default=str)),
                    )
                    if not cursor.rowcount:
                        duplicates += 1; continue
                    envelope_ids.append(envelope_id)
                    conn.execute("INSERT OR IGNORE INTO INTEGRATION_EVENTS VALUES (?,?,?,?,?,?,?,?,?,'RECEIVED',0)",
                                 (_id("IEV"), organization_id, connection_id, "SOURCE_RECORD_INGESTED", record.external_id, record.source_version,
                                  record.source_timestamp or ingested_at, ingested_at, stored["object_hash"]))
                    raw_node = upsert_node(conn, organization_id, "SOURCE_RECORD", envelope_id, f"{record.external_type} {record.external_id}", stored["object_hash"], {"connection_id": connection_id})
                    link_nodes(conn, organization_id, run_node, raw_node, "PRODUCED", evidence_type="INGESTION", evidence_reference=envelope_id)
                    record_thread_event(conn, organization_id, raw_node, "SOURCE_RECORD_INGESTED", actor_id=actor_id, source_system=connection_name, evidence_reference=envelope_id)
                    if mapping_id:
                        mapped_result = apply_mapping(conn, envelope_id, mapping_id)
                        if mapped_result["status"] == "QUARANTINED": quarantined += 1
                        else: mapped += 1
                finished = database._utcnow()
                conn.execute("""INSERT INTO INTEGRATION_CHECKPOINTS VALUES (?,?,?,?)
                                ON CONFLICT(CONNECTION_ID,SOURCE_OBJECT_ID) DO UPDATE SET CHECKPOINT_JSON=excluded.CHECKPOINT_JSON,UPDATED_AT=excluded.UPDATED_AT""",
                             (connection_id, source_object_id, json.dumps(result.checkpoint, sort_keys=True, default=str), finished))
                reconciliation = reconcile_run(conn, organization_id, run_id) if mapping_id else None
                status = "COMPLETED_WITH_WARNINGS" if quarantined or result.warnings else "COMPLETED"
                conn.execute("""UPDATE INTEGRATION_RUNS SET STATUS=?,COMPLETED_AT=?,RECORDS_READ=?,RECORDS_WRITTEN=?,BYTES_PROCESSED=?,
                                CHECKPOINT_JSON=?,ERROR_COUNT=?,ATTEMPT_COUNT=? WHERE RUN_ID=?""",
                             (status, finished, len(result.records), mapped, result.bytes_processed, json.dumps(result.checkpoint, default=str), quarantined, attempt, run_id))
                conn.execute("UPDATE INTEGRATION_CONNECTIONS SET HEALTH_STATUS='HEALTHY',LAST_SUCCESS_AT=?,UPDATED_AT=? WHERE CONNECTION_ID=?", (finished, finished, connection_id))
                run_node = upsert_node(conn, organization_id, "INTEGRATION_RUN", run_id, f"{connection_name} ingestion", run_id, {"status": status, "records": len(result.records)})
                record_thread_event(conn, organization_id, run_node, "INGESTION_COMPLETED", actor_id=actor_id, source_system=connection_name, payload={"mapped": mapped, "quarantined": quarantined, "duplicates": duplicates})
                index_item(conn, organization_id, "INTEGRATION_RUN", run_id, f"{connection_name} / {run_id}", status, [connection_id, status], {"connector_type": connector_type, "status": status})
                conn.commit()
            finally: conn.close()
            record_metric("integration_records_ingested", len(envelope_ids), "records", status="SUCCESS", user_id=actor_id, tags={"connection_id": connection_id})
            record_audit_event("INTEGRATION_RUN_COMPLETED", actor_user_id=actor_id, payload={"organization_id": organization_id, "connection_id": connection_id, "run_id": run_id, "records": len(envelope_ids), "duplicates": duplicates})
            return {"run_id": run_id, "status": status, "records_read": len(result.records), "records_ingested": len(envelope_ids), "records_mapped": mapped,
                    "quarantined": quarantined, "duplicates": duplicates, "checkpoint": result.checkpoint, "reconciliation": reconciliation, "attempts": attempt}
        except Exception as exc:
            code = exc.code if isinstance(exc, ConnectorError) else "INTEGRATION_EXECUTION_FAILED"; message = str(exc)[:1000]; failed = database._utcnow(); conn = database._get_connection()
            try:
                dead_letter_id = _id("DLQ"); incident_id = _id("INC")
                conn.execute("UPDATE INTEGRATION_RUNS SET STATUS='FAILED',COMPLETED_AT=?,ERROR_COUNT=1,ATTEMPT_COUNT=?,ERROR_CODE=?,ERROR_MESSAGE=? WHERE RUN_ID=?", (failed, attempt, code, message, run_id))
                conn.execute("INSERT INTO INTEGRATION_DEAD_LETTERS VALUES (?,?,?,?,?,?,?,'OPEN',?,NULL,NULL)", (dead_letter_id, organization_id, connection_id, run_id, None, code, message, attempt, failed))
                conn.execute("INSERT INTO INTEGRATION_INCIDENTS VALUES (?,?,?,'HIGH',?,?,'OPEN',?,?,NULL,NULL,NULL,NULL)", (incident_id, organization_id, connection_id, f"Integration run failed: {connection_name}", message, failed, failed))
                conn.execute("UPDATE INTEGRATION_CONNECTIONS SET HEALTH_STATUS='UNHEALTHY',LAST_FAILURE_AT=?,UPDATED_AT=? WHERE CONNECTION_ID=?", (failed, failed, connection_id)); conn.commit()
            finally: conn.close()
            record_audit_event("INTEGRATION_RUN_FAILED", actor_user_id=actor_id, payload={"organization_id": organization_id, "connection_id": connection_id, "run_id": run_id, "error_code": code}, status="FAILED", failure_reason=message)
            raise

    def list_runs(self, organization_id: str, actor_id: str, connection_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        self._require(actor_id, "integration.read", organization_id); conn = database._get_connection()
        try:
            sql = "SELECT * FROM INTEGRATION_RUNS WHERE ORGANIZATION_ID=?"; params: list[Any] = [organization_id]
            if connection_id: sql += " AND CONNECTION_ID=?"; params.append(connection_id)
            sql += " ORDER BY STARTED_AT DESC LIMIT ?"; params.append(min(max(limit, 1), 500))
            return [_row(row) for row in conn.execute(sql, params)]
        finally: conn.close()

    def dead_letters(self, organization_id: str, actor_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "integration.read", organization_id); conn = database._get_connection()
        try: return [_row(row) for row in conn.execute("SELECT * FROM INTEGRATION_DEAD_LETTERS WHERE ORGANIZATION_ID=? ORDER BY CREATED_AT DESC", (organization_id,))]
        finally: conn.close()

    async def replay_dead_letter(self, organization_id: str, dead_letter_id: str, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "integration.replay", organization_id); conn = database._get_connection()
        try:
            row = conn.execute("SELECT * FROM INTEGRATION_DEAD_LETTERS WHERE DEAD_LETTER_ID=? AND ORGANIZATION_ID=? AND STATUS='OPEN'", (dead_letter_id, organization_id)).fetchone()
            if not row: raise KeyError("Open dead letter does not exist")
            connection_id = row["CONNECTION_ID"]
        finally: conn.close()
        result = await self.execute(organization_id, connection_id, actor_id, idempotency_key=f"REPLAY:{dead_letter_id}:{uuid.uuid4().hex}")
        conn = database._get_connection()
        try: conn.execute("UPDATE INTEGRATION_DEAD_LETTERS SET STATUS='REPLAYED',REPLAYED_AT=? WHERE DEAD_LETTER_ID=?", (database._utcnow(), dead_letter_id)); conn.commit()
        finally: conn.close()
        return result


integration_service = IntegrationService()
