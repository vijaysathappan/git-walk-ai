"""Relative effort estimation without false person-day precision."""


def effort_class(points: float) -> str:
    if points < 15: return "XS"
    if points < 35: return "S"
    if points < 75: return "M"
    if points < 150: return "L"
    if points < 300: return "XL"
    return "XXL"


def estimate(units: list, blockers: list, validation_count: int) -> dict:
    mode_factor = {"AUTO_MIGRATABLE": .35, "ASSISTED_MIGRATION": .8,
                   "MANUAL_REENGINEERING": 1.6, "RETAIN_IN_EXCEL": .2,
                   "UNSUPPORTED": 2.0, "RETIRE": .1}
    areas = {"DATA": 0.0, "FORMULA": 0.0, "AUTOMATION": 0.0,
             "INTEGRATION": 0.0, "CONTROLS": 0.0, "TESTING": validation_count * .15}
    for unit in units:
        area = "FORMULA" if unit.source_type in {"FORMULA_PATTERN", "DOMAIN"} else \
               "AUTOMATION" if unit.source_type in {"VBA_PROJECT", "POWER_QUERY"} else \
               "INTEGRATION" if unit.source_type in {"CONNECTION", "EXTERNAL_LINK"} else \
               "CONTROLS" if unit.source_type == "CONTROL" else "DATA"
        areas[area] += unit.weight * mode_factor[unit.mode]
    blocker_factor = sum({"CRITICAL": 18, "HIGH": 9, "MEDIUM": 4, "LOW": 1}.get(item.severity, 0) for item in blockers)
    areas["INTEGRATION"] += blocker_factor * .4
    areas["TESTING"] += blocker_factor * .35
    return {"areas": {key.lower(): {"points": round(value, 1), "class": effort_class(value)} for key, value in areas.items()},
            "points": round(sum(areas.values()), 1), "overall": effort_class(sum(areas.values()))}
