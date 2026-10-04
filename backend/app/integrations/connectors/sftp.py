"""SFTP connector with mandatory host-key verification."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
from pathlib import PurePosixPath
from typing import Any

from .base import Connector, ConnectorError, ConnectorReadResult, ConnectorRecord


class SftpConnector(Connector):
    connector_type = "SFTP"
    capabilities = frozenset({"READ", "WRITE", "DISCOVER_SCHEMA", "INCREMENTAL_READ"})

    def _session(self):
        try:
            import paramiko
        except ImportError as exc:
            raise ConnectorError("CONNECTOR_RUNTIME_UNAVAILABLE", "Install the paramiko runtime to use SFTP") from exc
        host = self.configuration.get("host"); expected = str(self.configuration.get("host_key_fingerprint", "")).replace(":", "").lower()
        if not host or not expected:
            raise ConnectorError("INVALID_CONFIGURATION", "SFTP host and host_key_fingerprint are required")
        transport = paramiko.Transport((host, int(self.configuration.get("port", 22))))
        try:
            private_key = self.credentials.get("private_key")
            pkey = paramiko.RSAKey.from_private_key(io.StringIO(private_key)) if private_key else None
            transport.connect(username=self.credentials.get("username"), password=self.credentials.get("password"), pkey=pkey)
            actual = hashlib.sha256(transport.get_remote_server_key().asbytes()).hexdigest().lower()
            if actual != expected:
                transport.close()
                raise ConnectorError("HOST_KEY_MISMATCH", "SFTP host key does not match the trusted fingerprint")
            return transport, paramiko.SFTPClient.from_transport(transport)
        except ConnectorError:
            raise
        except Exception as exc:
            transport.close()
            raise ConnectorError("SFTP_CONNECTION_FAILED", "SFTP authentication or network connection failed", transient=True) from exc

    async def test_connection(self) -> dict[str, Any]:
        def run():
            transport, client = self._session()
            try:
                client.listdir(self.configuration.get("base_path", "."))
                return {"status": "SUCCESS", "host": self.configuration["host"], "capabilities": sorted(self.capabilities)}
            finally: client.close(); transport.close()
        return await asyncio.to_thread(run)

    async def discover(self) -> dict[str, Any]:
        def run():
            transport, client = self._session(); base = self.configuration.get("base_path", ".")
            try:
                entries = [{"name": item.filename, "size": item.st_size, "modified": item.st_mtime, "path": str(PurePosixPath(base) / item.filename)} for item in client.listdir_attr(base) if not str(item.filename).startswith(".")]
                return {"path": base, "objects": entries[:500], "sampled": min(len(entries), 500)}
            finally: client.close(); transport.close()
        return await asyncio.to_thread(run)

    async def read(self, request: dict[str, Any]) -> ConnectorReadResult:
        def run():
            transport, client = self._session(); base = self.configuration.get("base_path", "."); checkpoint = request.get("checkpoint") or {}; last_modified = int(checkpoint.get("last_modified", 0)); records = []; highest = last_modified; size = 0
            try:
                entries = sorted(client.listdir_attr(base), key=lambda item: (item.st_mtime, item.filename))
                for item in entries:
                    if item.st_mtime <= last_modified or len(records) >= min(int(request.get("batch_size", 100)), 1000): continue
                    path = str(PurePosixPath(base) / item.filename)
                    with client.open(path, "rb") as handle: payload = handle.read(int(self.configuration.get("max_object_bytes", 25 * 1024 * 1024)) + 1)
                    if len(payload) > int(self.configuration.get("max_object_bytes", 25 * 1024 * 1024)): raise ConnectorError("OBJECT_TOO_LARGE", f"SFTP object {path} exceeds the configured limit")
                    size += len(payload); highest = max(highest, item.st_mtime)
                    value = json.loads(payload.decode(self.configuration.get("encoding", "utf-8"))) if item.filename.lower().endswith(".json") else {"path": path, "size": len(payload), "content_base64": base64.b64encode(payload).decode("ascii")}
                    records.append(ConnectorRecord(self.configuration.get("object_type", "SftpObject"), path, f"{item.st_mtime}:{item.st_size}", dict(value) if isinstance(value, dict) else {"value": value}, metadata={"path": path}))
                return ConnectorReadResult(records, {"last_modified": highest, "complete": len(records) < int(request.get("batch_size", 100))}, size)
            finally: client.close(); transport.close()
        return await asyncio.to_thread(run)

    async def write(self, request: dict[str, Any]) -> dict[str, Any]:
        def run():
            path = request.get("path") or self.configuration.get("write_path")
            if not path: raise ConnectorError("INVALID_CONFIGURATION", "SFTP write path is required")
            payload = json.dumps(request.get("records") or [], default=str).encode("utf-8"); transport, client = self._session()
            try:
                with client.open(path, "wb") as handle: handle.write(payload)
                return {"status": "COMPLETED", "records_written": len(request.get("records") or []), "bytes_processed": len(payload)}
            finally: client.close(); transport.close()
        return await asyncio.to_thread(run)
