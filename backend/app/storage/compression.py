"""Object compression with an always-available local fallback."""

import zlib


def compress(payload: bytes, algorithm: str = "zlib") -> tuple[bytes, str]:
    if algorithm in {"none", "identity"}:
        return payload, "none"
    if algorithm == "zstd":
        try:
            import zstandard  # type: ignore

            return zstandard.ZstdCompressor(level=6).compress(payload), "zstd"
        except ImportError:
            pass
    return zlib.compress(payload, level=6), "zlib"


def decompress(payload: bytes, algorithm: str) -> bytes:
    if algorithm == "none":
        return payload
    if algorithm == "zstd":
        try:
            import zstandard  # type: ignore

            return zstandard.ZstdDecompressor().decompress(payload)
        except ImportError as exc:
            raise RuntimeError("zstandard is required to read this object") from exc
    return zlib.decompress(payload)
