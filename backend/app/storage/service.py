"""Verified immutable object reads/writes and metadata registration."""

from pathlib import Path
from typing import Any

from ..config import settings
from .compression import compress, decompress
from .hashing import content_hash
from .local_store import LocalObjectStore
from .object_store import ObjectStore
from .serializer import canonical_deserialize, canonical_serialize


class ObjectCorruptionError(RuntimeError):
    pass


class ObjectService:
    def __init__(self, store: ObjectStore, hash_algorithm: str = "sha256", compression: str = "zlib"):
        self.store = store
        self.hash_algorithm = hash_algorithm
        self.compression = compression

    @classmethod
    def local(cls, db_path: Path | str) -> "ObjectService":
        if settings.object_store_provider in {"s3", "minio"}:
            from .s3_store import S3ObjectStore

            store = S3ObjectStore(
                settings.object_store_bucket, settings.object_store_endpoint,
                settings.object_store_access_key, settings.object_store_secret_key,
            )
        else:
            configured = Path(settings.object_store_path).expanduser() if settings.object_store_path else Path(db_path).parent / "object_store"
            store = LocalObjectStore(configured)
        return cls(store, settings.object_hash_algorithm, settings.object_compression)

    def put(self, conn, object_type: str, value: Any) -> dict[str, Any]:
        raw = canonical_serialize(value)
        object_hash = content_hash(raw, self.hash_algorithm)
        compressed, compression = compress(raw, self.compression)
        created = self.store.put(object_hash, compressed)
        storage_key = self.store.key_for(object_hash)
        conn.execute(
            """
            INSERT INTO STORAGE_OBJECTS
                (OBJECT_HASH,OBJECT_TYPE,STORAGE_KEY,RAW_SIZE,COMPRESSED_SIZE,
                 COMPRESSION,ENCODING,STATUS,VERIFICATION_STATUS,CREATED_AT,LAST_VERIFIED_AT)
            VALUES (?,?,?,?,?,?,?,'AVAILABLE','VALID',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            ON CONFLICT(OBJECT_HASH) DO UPDATE SET LAST_ACCESSED_AT=CURRENT_TIMESTAMP
            """,
            (object_hash, object_type, storage_key, len(raw), len(compressed), compression, "canonical-json-v1"),
        )
        return {
            "object_hash": object_hash, "created": created, "raw_size": len(raw),
            "compressed_size": len(compressed), "object_type": object_type,
        }

    def put_bytes(self, conn, object_type: str, payload: bytes) -> dict[str, Any]:
        """Store opaque source artifacts without JSON or database BLOB encoding."""
        object_hash = content_hash(payload, self.hash_algorithm)
        compressed, compression = compress(payload, self.compression)
        created = self.store.put(object_hash, compressed)
        storage_key = self.store.key_for(object_hash)
        conn.execute(
            """
            INSERT INTO STORAGE_OBJECTS
                (OBJECT_HASH,OBJECT_TYPE,STORAGE_KEY,RAW_SIZE,COMPRESSED_SIZE,
                 COMPRESSION,ENCODING,STATUS,VERIFICATION_STATUS,CREATED_AT,LAST_VERIFIED_AT)
            VALUES (?,?,?,?,?,?,?,'AVAILABLE','VALID',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            ON CONFLICT(OBJECT_HASH) DO UPDATE SET LAST_ACCESSED_AT=CURRENT_TIMESTAMP
            """,
            (object_hash, object_type, storage_key, len(payload), len(compressed), compression, "binary-v1"),
        )
        return {
            "object_hash": object_hash, "created": created, "raw_size": len(payload),
            "compressed_size": len(compressed), "object_type": object_type,
        }

    def get_bytes(self, conn, object_hash: str) -> bytes:
        row = conn.execute(
            "SELECT COMPRESSION,ENCODING FROM STORAGE_OBJECTS WHERE OBJECT_HASH=? AND STATUS='AVAILABLE'",
            (object_hash,),
        ).fetchone()
        if not row:
            raise KeyError(f"Object {object_hash} is not registered")
        if row[1] != "binary-v1":
            raise TypeError(f"Object {object_hash} is not an opaque binary object")
        raw = decompress(self.store.get(object_hash), row[0])
        if content_hash(raw, self.hash_algorithm) != object_hash:
            conn.execute(
                "UPDATE STORAGE_OBJECTS SET STATUS='CORRUPT',VERIFICATION_STATUS='CORRUPT',LAST_VERIFIED_AT=CURRENT_TIMESTAMP WHERE OBJECT_HASH=?",
                (object_hash,),
            )
            raise ObjectCorruptionError(f"Object {object_hash} failed hash verification")
        conn.execute(
            "UPDATE STORAGE_OBJECTS SET LAST_ACCESSED_AT=CURRENT_TIMESTAMP,LAST_VERIFIED_AT=CURRENT_TIMESTAMP WHERE OBJECT_HASH=?",
            (object_hash,),
        )
        return raw

    def get(self, conn, object_hash: str) -> Any:
        row = conn.execute(
            "SELECT COMPRESSION FROM STORAGE_OBJECTS WHERE OBJECT_HASH=? AND STATUS='AVAILABLE'",
            (object_hash,),
        ).fetchone()
        if not row:
            raise KeyError(f"Object {object_hash} is not registered")
        raw = decompress(self.store.get(object_hash), row[0])
        actual = content_hash(raw, self.hash_algorithm)
        if actual != object_hash:
            conn.execute(
                "UPDATE STORAGE_OBJECTS SET STATUS='CORRUPT',VERIFICATION_STATUS='CORRUPT',LAST_VERIFIED_AT=CURRENT_TIMESTAMP WHERE OBJECT_HASH=?",
                (object_hash,),
            )
            raise ObjectCorruptionError(f"Object {object_hash} failed hash verification")
        conn.execute(
            "UPDATE STORAGE_OBJECTS SET LAST_ACCESSED_AT=CURRENT_TIMESTAMP,LAST_VERIFIED_AT=CURRENT_TIMESTAMP WHERE OBJECT_HASH=?",
            (object_hash,),
        )
        return canonical_deserialize(raw)
