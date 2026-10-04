"""REST connector with bounded pagination, stable errors, and timeout handling."""

from __future__ import annotations

import asyncio
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .base import Connector, ConnectorError, ConnectorReadResult, ConnectorRecord
from ..ssrf_guard import SSRFBlockedError, assert_url_is_safe


class RestConnector(Connector):
    connector_type = "REST_API"
    capabilities = frozenset({"READ", "WRITE", "DISCOVER_SCHEMA", "INCREMENTAL_READ"})

    def _headers(self) -> dict[str, str]:
        headers = {str(key): str(value) for key, value in (self.configuration.get("headers") or {}).items()}
        if self.credentials.get("token"): headers["Authorization"] = f"Bearer {self.credentials['token']}"
        if self.credentials.get("api_key"): headers[self.configuration.get("api_key_header", "X-API-Key")] = self.credentials["api_key"]
        return headers

    async def _request(self, url: str, method: str = "GET", payload: bytes | None = None) -> tuple[Any, dict[str, str], int]:
        try:
            assert_url_is_safe(url)
        except SSRFBlockedError as exc:
            raise ConnectorError("INVALID_CONFIGURATION", str(exc)) from exc

        def send():
            request = urllib.request.Request(url, data=payload, method=method, headers=self._headers())
            try:
                with urllib.request.urlopen(request, timeout=float(self.configuration.get("timeout_seconds", 20))) as response:
                    raw = response.read(int(self.configuration.get("max_response_bytes", 10 * 1024 * 1024)) + 1)
                    if len(raw) > int(self.configuration.get("max_response_bytes", 10 * 1024 * 1024)):
                        raise ConnectorError("RESPONSE_TOO_LARGE", "REST response exceeded the configured limit")
                    return json.loads(raw or b"null"), dict(response.headers), len(raw)
            except urllib.error.HTTPError as exc:
                code = "AUTHENTICATION_FAILED" if exc.code in {401, 403} else "REMOTE_SERVER_ERROR" if exc.code >= 500 else "INVALID_REQUEST"
                raise ConnectorError(code, f"Remote API returned HTTP {exc.code}", transient=exc.code >= 500) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                raise ConnectorError("NETWORK_UNREACHABLE", "Remote API could not be reached", transient=True) from exc
        return await asyncio.to_thread(send)

    def _url(self, checkpoint: dict[str, Any] | None = None) -> str:
        base = str(self.configuration.get("base_url", "")).rstrip("/")
        if not base.startswith(("http://", "https://")): raise ConnectorError("INVALID_CONFIGURATION", "REST base_url must use HTTP or HTTPS")
        url = base + "/" + str(self.configuration.get("path", "")).lstrip("/")
        params = dict(self.configuration.get("query") or {})
        if checkpoint and checkpoint.get("cursor"): params[self.configuration.get("cursor_parameter", "cursor")] = checkpoint["cursor"]
        return url + (("?" + urllib.parse.urlencode(params)) if params else "")

    async def test_connection(self) -> dict[str, Any]:
        value, _headers, size = await self._request(self._url())
        return {"status": "SUCCESS", "bytes": size, "response_type": type(value).__name__, "capabilities": sorted(self.capabilities)}

    async def discover(self) -> dict[str, Any]:
        openapi_url = self.configuration.get("openapi_url")
        if openapi_url:
            document, _, _ = await self._request(openapi_url)
            return {"type": "OPENAPI", "title": document.get("info", {}).get("title"), "version": document.get("info", {}).get("version"),
                    "endpoints": [{"path": path, "methods": sorted(method.upper() for method in operations)} for path, operations in document.get("paths", {}).items()]}
        value, _, _ = await self._request(self._url())
        sample = value[0] if isinstance(value, list) and value else value
        return {"type": "INFERRED_JSON", "fields": [{"name": key, "type": type(field).__name__.upper()} for key, field in (sample or {}).items()] if isinstance(sample, dict) else []}

    async def read(self, request: dict[str, Any]) -> ConnectorReadResult:
        checkpoint = request.get("checkpoint") or {}; value, headers, size = await self._request(self._url(checkpoint))
        records_path = self.configuration.get("records_path")
        records = value.get(records_path, []) if records_path and isinstance(value, dict) else value if isinstance(value, list) else [value]
        id_field = self.configuration.get("id_field", "id"); version_field = self.configuration.get("version_field")
        output = []
        for index, payload in enumerate(records):
            canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")); external_id = str(payload.get(id_field, index + 1))
            version = str(payload.get(version_field)) if version_field and payload.get(version_field) is not None else hashlib.sha256(canonical.encode()).hexdigest()[:20]
            output.append(ConnectorRecord(self.configuration.get("object_type", "Record"), external_id, version, dict(payload), metadata={"http_etag": headers.get("ETag")}))
        cursor = value.get(self.configuration.get("next_cursor_field", "next_cursor")) if isinstance(value, dict) else None
        return ConnectorReadResult(output, {"cursor": cursor, "complete": not bool(cursor)}, size)

    async def write(self, request: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(request.get("records") or [], default=str).encode(); value, _, size = await self._request(self._url(), self.configuration.get("write_method", "POST"), payload)
        return {"status": "COMPLETED", "records_written": len(request.get("records") or []), "response": value, "bytes_processed": size}
