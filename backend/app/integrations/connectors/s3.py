"""S3-compatible connector with prefix checkpoints and immutable record envelopes."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from .base import Connector, ConnectorError, ConnectorReadResult, ConnectorRecord
from ..ssrf_guard import SSRFBlockedError, assert_url_is_safe


class S3Connector(Connector):
    connector_type = "S3"
    capabilities = frozenset({"READ", "WRITE", "DISCOVER_SCHEMA", "INCREMENTAL_READ", "BULK_WRITE"})

    def _client(self):
        try:
            import boto3
        except ImportError as exc:
            raise ConnectorError("CONNECTOR_RUNTIME_UNAVAILABLE", "Install the boto3 runtime to use S3") from exc
        bucket = self.configuration.get("bucket")
        if not bucket:
            raise ConnectorError("INVALID_CONFIGURATION", "S3 bucket is required")
        endpoint_url = self.configuration.get("endpoint_url") or None
        if endpoint_url:
            try:
                assert_url_is_safe(endpoint_url)
            except SSRFBlockedError as exc:
                raise ConnectorError("INVALID_CONFIGURATION", str(exc)) from exc
        return boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=self.credentials.get("access_key") or None,
            aws_secret_access_key=self.credentials.get("secret_key") or None,
            aws_session_token=self.credentials.get("session_token") or None,
            region_name=self.configuration.get("region") or None,
        )

    async def test_connection(self) -> dict[str, Any]:
        def run():
            try:
                self._client().head_bucket(Bucket=self.configuration["bucket"])
                return {"status": "SUCCESS", "bucket": self.configuration["bucket"], "capabilities": sorted(self.capabilities)}
            except Exception as exc:
                if isinstance(exc, ConnectorError): raise
                raise ConnectorError("OBJECT_STORAGE_UNREACHABLE", "S3 bucket could not be accessed", transient=True) from exc
        return await asyncio.to_thread(run)

    async def discover(self) -> dict[str, Any]:
        def run():
            client = self._client()
            try:
                response = client.list_objects_v2(Bucket=self.configuration["bucket"], Prefix=self.configuration.get("prefix", ""), MaxKeys=100)
                objects = [{"key": item["Key"], "size": item["Size"], "etag": item.get("ETag", "").strip('"'), "last_modified": item["LastModified"].isoformat()} for item in response.get("Contents", [])]
                return {"bucket": self.configuration["bucket"], "prefix": self.configuration.get("prefix", ""), "objects": objects, "sampled": len(objects)}
            except Exception as exc:
                if isinstance(exc, ConnectorError): raise
                raise ConnectorError("DISCOVERY_FAILED", "S3 object discovery failed", transient=True) from exc
        return await asyncio.to_thread(run)

    async def read(self, request: dict[str, Any]) -> ConnectorReadResult:
        def run():
            client = self._client(); checkpoint = request.get("checkpoint") or {}; kwargs = {
                "Bucket": self.configuration["bucket"], "Prefix": self.configuration.get("prefix", ""),
                "MaxKeys": min(int(request.get("batch_size", 100)), 1000),
            }
            if checkpoint.get("continuation_token"):
                kwargs["ContinuationToken"] = checkpoint["continuation_token"]
            try:
                page = client.list_objects_v2(**kwargs); records = [] ; bytes_processed = 0
                for item in page.get("Contents", []):
                    body = client.get_object(Bucket=self.configuration["bucket"], Key=item["Key"])["Body"].read()
                    bytes_processed += len(body)
                    if len(body) > int(self.configuration.get("max_object_bytes", 25 * 1024 * 1024)):
                        raise ConnectorError("OBJECT_TOO_LARGE", f"S3 object {item['Key']} exceeds the configured limit")
                    content_type = str(item["Key"]).rsplit(".", 1)[-1].lower()
                    if content_type == "json":
                        value = json.loads(body.decode(self.configuration.get("encoding", "utf-8")))
                    else:
                        value = {"key": item["Key"], "size": len(body), "content_sha256": hashlib.sha256(body).hexdigest()}
                    payloads = value if isinstance(value, list) else [value]
                    for index, payload in enumerate(payloads):
                        records.append(ConnectorRecord(
                            self.configuration.get("object_type", "S3Object"),
                            f"{item['Key']}#{index}" if len(payloads) > 1 else item["Key"],
                            item.get("ETag", "").strip('"') or hashlib.sha256(body).hexdigest()[:20],
                            dict(payload) if isinstance(payload, dict) else {"value": payload},
                            source_timestamp=item["LastModified"].isoformat(), metadata={"bucket": self.configuration["bucket"], "key": item["Key"]},
                        ))
                return ConnectorReadResult(records, {"continuation_token": page.get("NextContinuationToken"), "complete": not page.get("IsTruncated", False)}, bytes_processed)
            except ConnectorError:
                raise
            except Exception as exc:
                if isinstance(exc, ConnectorError): raise
                raise ConnectorError("OBJECT_READ_FAILED", "S3 object ingestion failed", transient=True) from exc
        return await asyncio.to_thread(run)

    async def write(self, request: dict[str, Any]) -> dict[str, Any]:
        def run():
            records = request.get("records") or []
            key = request.get("key") or self.configuration.get("write_key")
            if not key:
                raise ConnectorError("INVALID_CONFIGURATION", "S3 write key is required")
            payload = json.dumps(records, default=str, separators=(",", ":")).encode("utf-8")
            try:
                result = self._client().put_object(Bucket=self.configuration["bucket"], Key=key, Body=payload, ContentType="application/json")
                return {"status": "COMPLETED", "records_written": len(records), "bytes_processed": len(payload), "etag": result.get("ETag", "").strip('"')}
            except Exception as exc:
                if isinstance(exc, ConnectorError): raise
                raise ConnectorError("OBJECT_WRITE_FAILED", "S3 object write failed", transient=True) from exc
        return await asyncio.to_thread(run)
