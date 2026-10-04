"""Stage 4 secret-provider abstraction with encrypted local implementation."""

from __future__ import annotations

import hashlib
import json
import uuid
from abc import ABC, abstractmethod
from typing import Any

from .. import database
from ..secret_store import decrypt_secret, encrypt_secret


class SecretProvider(ABC):
    @abstractmethod
    def put(self, organization_id: str, values: dict[str, Any], actor_id: str) -> str: ...

    @abstractmethod
    def get(self, secret_id: str, organization_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def delete(self, secret_id: str, organization_id: str) -> None: ...


class LocalEncryptedSecretProvider(SecretProvider):
    provider_name = "LOCAL_ENCRYPTED"

    def put(self, organization_id: str, values: dict[str, Any], actor_id: str) -> str:
        raw = json.dumps(values, sort_keys=True, separators=(",", ":")); secret_id = f"ISEC_{uuid.uuid4().hex[:18].upper()}"; now = database._utcnow()
        conn = database._get_connection()
        try:
            conn.execute("INSERT INTO INTEGRATION_SECRETS VALUES (?,?,?,?,?,?,?,?)",
                         (secret_id, organization_id, self.provider_name, encrypt_secret(raw), hashlib.sha256(raw.encode()).hexdigest(), actor_id, now, now))
            conn.commit(); return secret_id
        finally: conn.close()

    def get(self, secret_id: str, organization_id: str) -> dict[str, Any]:
        conn = database._get_connection()
        try: row = conn.execute("SELECT ENCRYPTED_VALUE FROM INTEGRATION_SECRETS WHERE SECRET_ID=? AND ORGANIZATION_ID=?", (secret_id, organization_id)).fetchone()
        finally: conn.close()
        if not row: raise KeyError("Credential reference does not exist")
        return json.loads(decrypt_secret(row[0]))

    def delete(self, secret_id: str, organization_id: str) -> None:
        conn = database._get_connection()
        try: conn.execute("DELETE FROM INTEGRATION_SECRETS WHERE SECRET_ID=? AND ORGANIZATION_ID=?", (secret_id, organization_id)); conn.commit()
        finally: conn.close()


secret_provider = LocalEncryptedSecretProvider()
