"""Server-controlled content hashing."""

import hashlib


def content_hash(payload: bytes, algorithm: str = "sha256") -> str:
    normalized = algorithm.lower()
    if normalized not in hashlib.algorithms_available:
        normalized = "sha256"
    return hashlib.new(normalized, payload).hexdigest()
