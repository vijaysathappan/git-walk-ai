"""Bounded deterministic normalizers; score growth is intentionally non-linear."""

import math


def clamp(value: float, minimum: float = 0, maximum: float = 100) -> float:
    return min(maximum, max(minimum, float(value)))


def log_score(value: float, reference_max: float) -> float:
    if value <= 0 or reference_max <= 0:
        return 0.0
    return round(clamp(math.log1p(value) / math.log1p(reference_max) * 100), 2)


def ratio_score(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return round(clamp(numerator / denominator * 100), 2)


def saturation_score(value: float, midpoint: float) -> float:
    if value <= 0:
        return 0.0
    return round(clamp(100 * value / (value + max(1.0, midpoint))), 2)


def blend(*weighted_values: tuple[float, float]) -> float:
    total = sum(weight for _, weight in weighted_values) or 1
    return round(clamp(sum(value * weight for value, weight in weighted_values) / total), 2)
