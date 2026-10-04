"""SQLAlchemy-backed relational connector with schema-only discovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from .base import Connector, ConnectorError, ConnectorReadResult, ConnectorRecord
from ..ssrf_guard import SSRFBlockedError, _is_blocked_address
import socket


class RelationalConnector(Connector):
    connector_type = "RELATIONAL_DB"
    capabilities = frozenset({"READ", "WRITE", "DISCOVER_SCHEMA", "INCREMENTAL_READ", "TRANSACTIONAL_WRITE", "BULK_WRITE"})

    def _engine(self):
        url = self.credentials.get("database_url")
        if not url: raise ConnectorError("INVALID_CONFIGURATION", "database_url credential is required")
        try:
            host = make_url(url).host
        except Exception as exc:
            raise ConnectorError("INVALID_CONFIGURATION", "Database connection configuration is invalid") from exc
        if host:
            try:
                addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
            except socket.gaierror as exc:
                raise ConnectorError("INVALID_CONFIGURATION", f"Could not resolve database host: {host!r}") from exc
            if any(_is_blocked_address(address) for address in addresses):
                raise ConnectorError("INVALID_CONFIGURATION", f"Database host {host!r} resolves to a private/internal address; refusing to connect")
        try: return create_engine(url, pool_pre_ping=True)
        except Exception as exc: raise ConnectorError("INVALID_CONFIGURATION", "Database connection configuration is invalid") from exc

    async def test_connection(self) -> dict[str, Any]:
        def run():
            engine = self._engine()
            try:
                with engine.connect() as conn: conn.execute(text("SELECT 1"))
                return {"status": "SUCCESS", "dialect": engine.dialect.name, "capabilities": sorted(self.capabilities)}
            except DBAPIError as exc: raise ConnectorError("AUTHENTICATION_OR_NETWORK_FAILURE", "Database connection failed", transient=bool(exc.connection_invalidated)) from exc
            finally: engine.dispose()
        return await asyncio.to_thread(run)

    async def discover(self) -> dict[str, Any]:
        def run():
            engine = self._engine(); inspector = inspect(engine)
            try:
                schemas = []
                for schema in inspector.get_schema_names():
                    tables = []
                    for name in inspector.get_table_names(schema=schema):
                        columns = inspector.get_columns(name, schema=schema); pk = set(inspector.get_pk_constraint(name, schema=schema).get("constrained_columns") or [])
                        tables.append({"name": name, "columns": [{"name": item["name"], "type": str(item["type"]), "nullable": item.get("nullable", True), "primary_key": item["name"] in pk} for item in columns], "foreign_keys": inspector.get_foreign_keys(name, schema=schema), "indexes": inspector.get_indexes(name, schema=schema)})
                    schemas.append({"name": schema, "tables": tables, "views": inspector.get_view_names(schema=schema)})
                return {"dialect": engine.dialect.name, "schemas": schemas}
            finally: engine.dispose()
        return await asyncio.to_thread(run)

    async def read(self, request: dict[str, Any]) -> ConnectorReadResult:
        def run():
            engine = self._engine(); checkpoint = request.get("checkpoint") or {}; limit = min(int(request.get("batch_size", 1000)), 10000)
            table_name = self.configuration.get("table"); query = self.configuration.get("query")
            if not table_name and not query: raise ConnectorError("INVALID_CONFIGURATION", "Configure a table or read query")
            parameters: dict[str, Any] = {"limit": limit}; sql = query
            if not sql:
                safe = str(table_name).replace('"', '""'); sql = f'SELECT * FROM "{safe}"'
                checkpoint_column = self.configuration.get("checkpoint_column")
                if checkpoint_column and checkpoint.get("value") is not None:
                    safe_column = str(checkpoint_column).replace('"', '""'); sql += f' WHERE "{safe_column}" > :checkpoint'; parameters["checkpoint"] = checkpoint["value"]
                sql += " LIMIT :limit"
            try:
                with engine.connect() as conn: rows = [dict(row._mapping) for row in conn.execute(text(sql), parameters)]
            except SQLAlchemyError as exc: raise ConnectorError("QUERY_FAILED", "Configured database read failed") from exc
            finally: engine.dispose()
            id_field = self.configuration.get("primary_key", "id"); version_field = self.configuration.get("version_field"); checkpoint_column = self.configuration.get("checkpoint_column")
            records = []
            for index, payload in enumerate(rows):
                canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")); external_id = str(payload.get(id_field, index + 1))
                version = str(payload.get(version_field)) if version_field and payload.get(version_field) is not None else hashlib.sha256(canonical.encode()).hexdigest()[:20]
                records.append(ConnectorRecord(self.configuration.get("object_type", table_name or "QueryRecord"), external_id, version, payload))
            next_checkpoint = {"value": rows[-1].get(checkpoint_column) if rows and checkpoint_column else checkpoint.get("value"), "complete": len(rows) < limit}
            return ConnectorReadResult(records, next_checkpoint, len(json.dumps(rows, default=str)))
        return await asyncio.to_thread(run)

    async def write(self, request: dict[str, Any]) -> dict[str, Any]:
        def run():
            table_name = self.configuration.get("write_table") or self.configuration.get("table"); records = request.get("records") or []
            if not table_name: raise ConnectorError("INVALID_CONFIGURATION", "A write table is required")
            engine = self._engine(); metadata = MetaData()
            try:
                table = Table(table_name, metadata, autoload_with=engine)
                with engine.begin() as conn: conn.execute(table.insert(), records)
                return {"status": "COMPLETED", "records_written": len(records)}
            except SQLAlchemyError as exc: raise ConnectorError("WRITE_FAILED", "Transactional database write failed") from exc
            finally: engine.dispose()
        return await asyncio.to_thread(run)
