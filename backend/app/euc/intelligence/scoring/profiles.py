"""Versioned, auditable scoring profiles. Weights are data, not engine branches."""

from copy import deepcopy


BASE = {
    "version": "1.0.0",
    "complexity": {
        "STRUCTURAL": .16, "FORMULA": .20, "DEPENDENCY": .20, "DATA": .12,
        "EXTERNAL_INTEGRATION": .10, "AUTOMATION": .08, "PRESENTATION": .05, "CHANGE": .09,
    },
    "risk": {
        "FORMULA_INTEGRITY": .22, "DEPENDENCY": .20, "EXTERNAL": .14, "CHANGE": .16,
        "AUTOMATION": .10, "DATA_INTEGRITY": .10, "AUDITABILITY": .08,
    },
    "residual_control_effect": .65,
}

PROFILES = {
    "DEFAULT": BASE,
    "FINANCIAL_MODEL": {
        **BASE, "version": "1.0.0-financial",
        "risk": {"FORMULA_INTEGRITY": .25, "DEPENDENCY": .20, "EXTERNAL": .15,
                 "CHANGE": .15, "AUTOMATION": .10, "DATA_INTEGRITY": .08, "AUDITABILITY": .07},
    },
    "REGULATORY_REPORTING": {
        **BASE, "version": "1.0.0-regulatory",
        "risk": {"FORMULA_INTEGRITY": .22, "DEPENDENCY": .15, "EXTERNAL": .10,
                 "CHANGE": .15, "AUTOMATION": .08, "DATA_INTEGRITY": .12, "AUDITABILITY": .18},
        "residual_control_effect": .75,
    },
    "OPERATIONS": {**BASE, "version": "1.0.0-operations"},
    "PLANNING": {**BASE, "version": "1.0.0-planning"},
    "ANALYTICS": {**BASE, "version": "1.0.0-analytics"},
}


def get_profile(name: str) -> dict:
    normalized = (name or "DEFAULT").strip().upper()
    if normalized not in PROFILES:
        raise ValueError(f"Unknown scoring profile: {normalized}")
    return {"name": normalized, **deepcopy(PROFILES[normalized])}
