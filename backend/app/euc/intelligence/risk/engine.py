"""Inherent and residual risk calculations; controls remain independently visible."""

from ..scoring.models import ScoreComponent, ScoreResult
from ..scoring.normalization import blend, clamp, ratio_score, saturation_score


class RiskEngine:
    def evaluate(self, features: dict, controls: dict, profile: dict) -> dict:
        dep = features["dependency"]
        candidates = features["candidates"]
        history = features["history"]
        weights = profile["risk"]
        formula = blend(
            (saturation_score(len(candidates["broken"]), 5), .25),
            (saturation_score(len(candidates["pattern_breaks"]), 8), .20),
            (saturation_score(len(candidates["formula_overrides"]), 5), .25),
            (saturation_score(dep.get("dynamic_count", 0), 15), .15),
            (saturation_score(features["formula_errors"], 5), .15),
        )
        dependency = blend(
            (saturation_score(dep.get("cycle_count", 0), 2), .22),
            (saturation_score(dep.get("maximum_calculation_depth", 0), 8), .16),
            (dep.get("cross_sheet_coupling", 0), .15),
            (saturation_score(features["critical_nodes"], 10), .25),
            (saturation_score(dep.get("dynamic_count", 0), 15), .12),
            (saturation_score(dep.get("dependency_density", 0), 8), .10),
        )
        external = blend(
            (saturation_score(len(candidates["unresolved_external"]), 3), .35),
            (saturation_score(len(candidates["local_paths"]), 2), .35),
            (saturation_score(features["inventory"].get("connections", 0), 4), .20),
            (100 if features["external"].get("credentials_present") else 0, .10),
        )
        change = blend(
            (saturation_score(history.get("changes", 0), 500), .30),
            (saturation_score(features["change_hotspot_count"], 8), .25),
            (saturation_score(history.get("conflicts", 0), 8), .15),
            (saturation_score(history.get("contributors", 0), 5), .15),
            (saturation_score(history.get("reversions", 0), 5), .15),
        )
        automation = blend(
            (100 if features["automation"].get("vba_present") else 0, .55),
            (saturation_score(features["automation"].get("power_queries", 0), 4), .20),
            (saturation_score(features["automation"].get("connections", 0), 4), .25),
        )
        data = blend(
            (saturation_score(features["data"].get("mixed_type_columns", 0), 8), .40),
            (saturation_score(features["data"].get("sparse_sheets", 0), 4), .20),
            (ratio_score(features["data"].get("blank_styled_cells", 0), max(1, features["inventory"].get("used_cells", 0))), .15),
            (saturation_score(features["formula_errors"], 5), .25),
        )
        auditability = clamp(100 - controls["governance_score"])
        values = {
            "FORMULA_INTEGRITY": (formula, "Overrides, broken formulas, pattern breaks, and dynamic references."),
            "DEPENDENCY": (dependency, "Cycles, deep chains, coupling, and high-blast-radius nodes."),
            "EXTERNAL": (external, "Unavailable dependencies, portable paths, and connections."),
            "CHANGE": (change, "Change frequency, hotspot concentration, conflicts, and contributors."),
            "AUTOMATION": (automation, "Static automation footprint requiring governed review."),
            "DATA_INTEGRITY": (data, "Structural data-quality and formula-error indicators."),
            "AUDITABILITY": (auditability, "Exposure remaining when governance evidence is absent."),
        }
        inherent = ScoreResult("INHERENT_RISK", tuple(
            ScoreComponent(name, score, weights[name], explanation, self._evidence(name, features, controls))
            for name, (score, explanation) in values.items()
        ))
        control_effect = controls["effective_control_strength"] * profile["residual_control_effect"] / 100
        residual = round(clamp(inherent.score * (1 - control_effect)), 2)
        return {
            "inherent": inherent,
            "residual_score": residual,
            "control_reduction": round(inherent.score - residual, 2),
            "formula": formula, "dependency": dependency, "external": external,
            "change": change, "automation": automation, "data_integrity": data,
        }

    @staticmethod
    def _evidence(name: str, features: dict, controls: dict) -> dict:
        if name == "AUDITABILITY":
            return {"governance_score": controls["governance_score"]}
        return {
            "FORMULA_INTEGRITY": {"formula_errors": features["formula_errors"], **features["candidate_counts"]},
            "DEPENDENCY": features["dependency"], "EXTERNAL": features["external"],
            "CHANGE": features["history"], "AUTOMATION": features["automation"],
            "DATA_INTEGRITY": features["data"],
        }[name]
