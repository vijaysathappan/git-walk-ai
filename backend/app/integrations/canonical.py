"""Canonical enterprise object mapping, validation, identity, and versioning."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .. import database
from ..services.semantic_ledger_service import ledger_for_connection
from .thread import link_nodes, record_thread_event, upsert_node


def _id(prefix: str) -> str: return f"{prefix}_{uuid.uuid4().hex[:18].upper()}"


def create_schema(organization_id: str, name: str, schema: dict[str, Any], business_keys: list[str], actor_id: str) -> dict[str, Any]:
    conn = database._get_connection(); now = database._utcnow()
    try:
        version = conn.execute("SELECT COALESCE(MAX(VERSION),0)+1 FROM CANONICAL_SCHEMAS WHERE ORGANIZATION_ID=? AND NAME=?", (organization_id, name)).fetchone()[0]
        schema_id = _id("CSH")
        conn.execute("INSERT INTO CANONICAL_SCHEMAS VALUES (?,?,?,?,? ,?,'ACTIVE',?,?,?)",
                     (schema_id, organization_id, name.strip(), version, json.dumps(schema, sort_keys=True), json.dumps(business_keys), actor_id, now, now))
        conn.commit(); return {"schema_id": schema_id, "name": name.strip(), "version": version, "schema": schema, "business_keys": business_keys}
    finally: conn.close()


def create_mapping(organization_id: str, connection_id: str, source_type: str, schema_id: str, mapping: dict[str, Any], actor_id: str) -> dict[str, Any]:
    conn = database._get_connection(); now = database._utcnow()
    try:
        schema = conn.execute("SELECT 1 FROM CANONICAL_SCHEMAS WHERE SCHEMA_ID=? AND ORGANIZATION_ID=?", (schema_id, organization_id)).fetchone()
        connection = conn.execute("SELECT 1 FROM INTEGRATION_CONNECTIONS WHERE CONNECTION_ID=? AND ORGANIZATION_ID=?", (connection_id, organization_id)).fetchone()
        if not schema or not connection: raise ValueError("Connection and canonical schema must belong to the organization")
        version = conn.execute("SELECT COALESCE(MAX(VERSION),0)+1 FROM CANONICAL_MAPPINGS WHERE CONNECTION_ID=? AND SOURCE_OBJECT_TYPE=? AND SCHEMA_ID=?", (connection_id, source_type, schema_id)).fetchone()[0]
        mapping_id = _id("MAP")
        conn.execute("INSERT INTO CANONICAL_MAPPINGS VALUES (?,?,?,?,?,?,?,'ACTIVE',?,?,?)",
                     (mapping_id, organization_id, connection_id, source_type, schema_id, version, json.dumps(mapping, sort_keys=True), actor_id, now, now))
        conn.commit(); return {"mapping_id": mapping_id, "version": version, "schema_id": schema_id, "source_object_type": source_type}
    finally: conn.close()


def _source(payload: dict[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, dict): return None
        value = value.get(part)
    return value


def _blank(value: Any) -> bool:
    return value is None or value == ""


def _transform(value: Any, transforms: list[Any], payload: dict[str, Any]) -> Any:
    for transform in transforms:
        name = transform if isinstance(transform, str) else transform.get("op")
        if name == "trim" and isinstance(value, str): value = value.strip()
        elif name == "upper" and isinstance(value, str): value = value.upper()
        elif name == "lower" and isinstance(value, str): value = value.lower()
        elif name == "integer" and not _blank(value): value = int(value)
        elif name == "decimal" and not _blank(value): value = float(value)
        elif name == "string" and value is not None: value = str(value)
        elif name == "coalesce" and _blank(value):
            for path in transform.get("sources", []):
                candidate = _source(payload, path)
                if not _blank(candidate): value = candidate; break
        elif name == "concat": value = str(transform.get("separator", "")).join(str(_source(payload, path) or "") for path in transform.get("sources", []))
        elif name == "date_iso" and value:
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
        elif name not in {"trim", "upper", "lower", "integer", "decimal", "string", "coalesce", "concat", "date_iso"}:
            raise ValueError(f"Unsupported mapping transform {name}")
    return value


def _validate(payload: dict[str, Any], schema: dict[str, Any]) -> list[dict[str, str]]:
    errors = []
    types = {"STRING": str, "INTEGER": int, "NUMBER": (int, float), "BOOLEAN": bool, "OBJECT": dict, "ARRAY": list}
    for name, rule in (schema.get("fields") or {}).items():
        value = payload.get(name)
        if rule.get("required") and _blank(value): errors.append({"field": name, "code": "REQUIRED_FIELD_MISSING"}); continue
        expected = types.get(str(rule.get("type", "")).upper())
        if value is not None and expected and not isinstance(value, expected): errors.append({"field": name, "code": "TYPE_MISMATCH"})
    return errors


def apply_mapping(conn, envelope_id: str, mapping_id: str) -> dict[str, Any]:
    row = conn.execute("""SELECT E.*,M.MAPPING_JSON,M.SCHEMA_ID,S.SCHEMA_JSON,S.BUSINESS_KEY_JSON,S.NAME AS SCHEMA_NAME
                          FROM RAW_INGESTION_ENVELOPES E JOIN CANONICAL_MAPPINGS M ON M.MAPPING_ID=? AND M.ORGANIZATION_ID=E.ORGANIZATION_ID
                          JOIN CANONICAL_SCHEMAS S ON S.SCHEMA_ID=M.SCHEMA_ID WHERE E.ENVELOPE_ID=?""", (mapping_id, envelope_id)).fetchone()
    if not row: raise ValueError("Envelope and mapping are not compatible")
    source = ledger_for_connection(conn).objects.get(conn, row["PAYLOAD_HASH"]); mapping = json.loads(row["MAPPING_JSON"]); schema = json.loads(row["SCHEMA_JSON"])
    canonical = {}
    for target, spec in (mapping.get("fields") or {}).items():
        spec = {"source": spec} if isinstance(spec, str) else spec
        canonical[target] = _transform(_source(source, spec.get("source", target)), spec.get("transforms") or [], source)
    errors = _validate(canonical, schema); now = database._utcnow()
    if errors:
        quarantine_id = _id("QUA")
        conn.execute("INSERT INTO CANONICAL_QUARANTINE VALUES (?,?,?,?,?,?,'OPEN',?,NULL)",
                     (quarantine_id, row["ORGANIZATION_ID"], envelope_id, row["SCHEMA_ID"], "CANONICAL_VALIDATION_FAILED", json.dumps(errors), now))
        conn.execute("UPDATE RAW_INGESTION_ENVELOPES SET STATUS='QUARANTINED' WHERE ENVELOPE_ID=?", (envelope_id,))
        return {"status": "QUARANTINED", "quarantine_id": quarantine_id, "errors": errors}
    business_keys = json.loads(row["BUSINESS_KEY_JSON"]); business_key = "|".join(str(canonical.get(key, "")) for key in business_keys)
    if not business_key.replace("|", ""): raise ValueError("Canonical business key is empty")
    canonical_id = "CAN_" + hashlib.sha256(f"{row['ORGANIZATION_ID']}:{row['SCHEMA_ID']}:{business_key}".encode()).hexdigest()[:20].upper()
    stored = ledger_for_connection(conn).objects.put(conn, "CANONICAL_OBJECT", canonical)
    existing = conn.execute("SELECT CURRENT_VERSION,CURRENT_PAYLOAD_HASH FROM CANONICAL_OBJECTS WHERE CANONICAL_ID=?", (canonical_id,)).fetchone()
    if existing and existing["CURRENT_PAYLOAD_HASH"] == stored["object_hash"]:
        version = existing["CURRENT_VERSION"]; change_type = "UNCHANGED"
    else:
        version = int(existing["CURRENT_VERSION"] if existing else 0) + 1; change_type = "UPDATED" if existing else "CREATED"
        if existing: conn.execute("UPDATE CANONICAL_OBJECT_VERSIONS SET VALID_TO=? WHERE CANONICAL_ID=? AND VERSION=?", (now, canonical_id, existing["CURRENT_VERSION"]))
        conn.execute("""INSERT INTO CANONICAL_OBJECTS VALUES (?,?,?,?,?,?,?,?,?,'ACTIVE',?,?)
                      ON CONFLICT(CANONICAL_ID) DO UPDATE SET CURRENT_VERSION=excluded.CURRENT_VERSION,CURRENT_PAYLOAD_HASH=excluded.CURRENT_PAYLOAD_HASH,UPDATED_AT=excluded.UPDATED_AT""",
                     (canonical_id, row["ORGANIZATION_ID"], row["SCHEMA_ID"], row["SCHEMA_NAME"], business_key, version, stored["object_hash"], now, None, now, now))
        conn.execute("INSERT INTO CANONICAL_OBJECT_VERSIONS VALUES (?,?,?,?,?,NULL,?,?)", (canonical_id, version, stored["object_hash"], envelope_id, now, change_type, now))
    conn.execute("""INSERT INTO CANONICAL_SOURCE_REFERENCES VALUES (?,?,?,?,1.0,'DETERMINISTIC',?,?)
                  ON CONFLICT(CONNECTION_ID,EXTERNAL_TYPE,EXTERNAL_ID) DO UPDATE SET CANONICAL_ID=excluded.CANONICAL_ID,UPDATED_AT=excluded.UPDATED_AT""",
                 (row["CONNECTION_ID"], row["EXTERNAL_TYPE"], row["EXTERNAL_ID"], canonical_id, now, now))
    conn.execute("UPDATE RAW_INGESTION_ENVELOPES SET STATUS='MAPPED' WHERE ENVELOPE_ID=?", (envelope_id,))
    raw_node = upsert_node(conn, row["ORGANIZATION_ID"], "SOURCE_RECORD", envelope_id, f"{row['EXTERNAL_TYPE']} {row['EXTERNAL_ID']}", row["PAYLOAD_HASH"], {"connection_id": row["CONNECTION_ID"]})
    canonical_node = upsert_node(conn, row["ORGANIZATION_ID"], "CANONICAL_OBJECT", canonical_id, f"{row['SCHEMA_NAME']} {business_key}", stored["object_hash"], {"version": version})
    link_nodes(conn, row["ORGANIZATION_ID"], raw_node, canonical_node, "MAPS_TO", 1.0, "MAPPING", mapping_id)
    record_thread_event(conn, row["ORGANIZATION_ID"], canonical_node, f"CANONICAL_{change_type}", source_system=row["SOURCE_SYSTEM"], evidence_reference=envelope_id, payload={"version": version})
    return {"status": change_type, "canonical_id": canonical_id, "version": version, "payload_hash": stored["object_hash"], "node_id": canonical_node}


def canonical_object(organization_id: str, canonical_id: str, version: int | None = None) -> dict[str, Any]:
    conn = database._get_connection()
    try:
        obj = conn.execute("SELECT * FROM CANONICAL_OBJECTS WHERE CANONICAL_ID=? AND ORGANIZATION_ID=?", (canonical_id, organization_id)).fetchone()
        if not obj: raise KeyError("Canonical object does not exist")
        version = version or obj["CURRENT_VERSION"]
        item = conn.execute("SELECT * FROM CANONICAL_OBJECT_VERSIONS WHERE CANONICAL_ID=? AND VERSION=?", (canonical_id, version)).fetchone()
        sources = conn.execute("SELECT * FROM CANONICAL_SOURCE_REFERENCES WHERE CANONICAL_ID=?", (canonical_id,)).fetchall()
        return {"canonical": {key.lower(): obj[key] for key in obj.keys()}, "version": {key.lower(): item[key] for key in item.keys()},
                "payload": ledger_for_connection(conn).objects.get(conn, item["PAYLOAD_HASH"]),
                "sources": [{key.lower(): source[key] for key in source.keys()} for source in sources]}
    finally: conn.close()
