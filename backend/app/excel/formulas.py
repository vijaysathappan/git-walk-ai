"""Formula and formatting normalization for deterministic semantic diffs."""

import hashlib
import json
from typing import Any


def normalize_formula(value: Any) -> str | None:
    if value is None:
        return None
    formula = str(value).strip()
    if not formula or not formula.startswith("="):
        return None
    return formula.replace("\r\n", "\n").replace("\r", "\n")


def style_hash(style: Any) -> str | None:
    if style in (None, {}, []):
        return None
    canonical = json.dumps(style, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
