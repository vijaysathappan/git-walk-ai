"""OpenRouter model catalogue rules for Git Walk's no-cost NVIDIA tier."""

from __future__ import annotations

import json
import urllib.request
from typing import Any


NVIDIA_FREE_MODEL_DEFAULTS = (
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "nvidia/nemotron-nano-9b-v2:free",
)

NVIDIA_FREE_STRUCTURED_MODELS = {
    "nvidia/nemotron-3-super-120b-a12b:free",
}


def is_free_nvidia_model_id(model_id: str | None) -> bool:
    normalized = str(model_id or "").strip().lower()
    return normalized.startswith("nvidia/") and normalized.endswith(":free")


def supports_structured_output(model_id: str | None) -> bool:
    return str(model_id or "").strip().lower() in NVIDIA_FREE_STRUCTURED_MODELS


def _zero_price(value: Any) -> bool:
    try:
        return float(value) == 0
    except (TypeError, ValueError):
        return False


def free_nvidia_chat_models(payload: dict[str, Any]) -> list[str]:
    """Return only zero-price NVIDIA models that produce text responses."""
    models: list[str] = []
    for item in payload.get("data", []):
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        pricing = item.get("pricing") or {}
        architecture = item.get("architecture") or {}
        output_modalities = architecture.get("output_modalities") or ["text"]
        if (
            is_free_nvidia_model_id(model_id)
            and _zero_price(pricing.get("prompt"))
            and _zero_price(pricing.get("completion"))
            and "text" in output_modalities
        ):
            models.append(model_id)
    preferred = {slug: index for index, slug in enumerate(NVIDIA_FREE_MODEL_DEFAULTS)}
    return sorted(set(models), key=lambda slug: (preferred.get(slug, 10_000), slug))


def fetch_free_nvidia_models(api_key: str | None = None) -> list[str]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/models?model_authors=nvidia&max_price=0&output_modalities=text",
        headers=headers,
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return free_nvidia_chat_models(payload)
