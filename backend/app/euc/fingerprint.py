"""Content and normalized package structure fingerprints."""

import hashlib
import io
import json
import zipfile


def content_fingerprint(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def structure_fingerprint(payload: bytes, file_type: str) -> str:
    if file_type == "csv":
        first_line = payload.decode("utf-8-sig").splitlines()[0] if payload else ""
        structure = {"type": "csv", "columns": first_line}
    else:
        with zipfile.ZipFile(io.BytesIO(payload)) as package:
            structure = [
                (item.filename, item.file_size)
                for item in package.infolist()
                if item.filename.startswith("xl/")
                and not item.filename.startswith("xl/media/")
                and item.filename not in {"xl/calcChain.xml"}
            ]
    canonical = json.dumps(structure, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()
