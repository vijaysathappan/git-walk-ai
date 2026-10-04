"""Atomic local-disk implementation using hash-sharded paths."""

import os
import tempfile
from pathlib import Path


class LocalObjectStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)

    def path_for(self, object_hash: str) -> Path:
        if len(object_hash) != 64 or any(char not in "0123456789abcdef" for char in object_hash):
            raise ValueError("Invalid object hash")
        return self.root / "objects" / object_hash[:2] / object_hash[2:4] / object_hash

    def key_for(self, object_hash: str) -> str:
        return str(self.path_for(object_hash))

    def exists(self, object_hash: str) -> bool:
        return self.path_for(object_hash).is_file()

    def put(self, object_hash: str, payload: bytes) -> bool:
        destination = self.path_for(object_hash)
        if destination.exists():
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".object-", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.replace(temporary, destination)
            except FileExistsError:
                return False
            return True
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def get(self, object_hash: str) -> bytes:
        try:
            return self.path_for(object_hash).read_bytes()
        except FileNotFoundError as exc:
            raise KeyError(f"Object {object_hash} does not exist") from exc

    def delete(self, object_hash: str) -> None:
        self.path_for(object_hash).unlink(missing_ok=True)

    def size(self, object_hash: str) -> int:
        return self.path_for(object_hash).stat().st_size
