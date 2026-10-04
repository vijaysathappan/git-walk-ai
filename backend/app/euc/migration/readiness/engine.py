"""Explainable migration readiness; deliberately independent from complexity inversion."""

from ...intelligence.scoring.normalization import blend, clamp, ratio_score, saturation_score
from ..rules import READINESS_WEIGHTS


class ReadinessEngine:
    def evaluate(self, features: dict, blockers: list) -> dict:
        inventory = features["inventory"]
        dependency = features["dependency"]
        stage23 = features["stage23_features"]
        intelligence = features["intelligence"]
        sheets = features["sheets"]
        visible_structure = ratio_score(
            sum(item.get("visibility") == "VISIBLE" for item in sheets), max(1, len(sheets))
        )
        structural = blend(
            (visible_structure, .25),
            (ratio_score(sum((item.get("table_count") or 0) > 0 for item in sheets), max(1, len(sheets))), .25),
            (clamp(100 - saturation_score(inventory.get("blank_styled_cells", 0), 1000)), .20),
            (clamp(100 - saturation_score(inventory.get("very_hidden_sheets", 0), 3)), .30),
        )
        formula = blend(
            (dependency.get("dependency_coverage", 100), .45),
            (clamp(100 - saturation_score(dependency.get("broken_count", 0), 3)), .25),
            (clamp(100 - saturation_score(dependency.get("dynamic_count", 0), 15)), .20),
            (clamp(100 - saturation_score(stage23.get("formula_errors", 0), 5)), .10),
        )
        dependency_complete = blend(
            (dependency.get("dependency_coverage", 100), .55),
            (clamp(100 - saturation_score(dependency.get("unresolved_count", 0), 8)), .30),
            (clamp(100 - saturation_score(dependency.get("cycle_count", 0), 2)), .15),
        )
        external = stage23.get("external", {})
        external_resolution = blend(
            (clamp(100 - saturation_score(external.get("unresolved", 0), 3)), .55),
            (clamp(100 - saturation_score(external.get("local_paths", 0), 2)), .30),
            (0 if external.get("credentials_present") else 100, .15),
        )
        automation = stage23.get("automation", {})
        automation_migratability = blend(
            (35 if automation.get("vba_present") else 100, .55),
            (clamp(100 - saturation_score(automation.get("power_queries", 0), 5) * .55), .25),
            (clamp(100 - saturation_score(automation.get("connections", 0), 5) * .45), .20),
        )
        controls = intelligence.get("control_strength", 0)
        data = stage23.get("data", {})
        data_clarity = blend(
            (clamp(100 - saturation_score(data.get("mixed_type_columns", 0), 8)), .45),
            (clamp(100 - saturation_score(data.get("sparse_sheets", 0), 4)), .25),
            (ratio_score(inventory.get("constant_cells", 0) + inventory.get("total_formulas", 0), max(1, inventory.get("used_cells", 0))), .30),
        )
        history = features["history"]
        stability = blend(
            (clamp(100 - saturation_score(history.get("reverts", 0), 5)), .40),
            (clamp(100 - saturation_score(stage23.get("history", {}).get("conflicts", 0), 8)), .35),
            (70 if history.get("commits", 0) else 45, .25),
        )
        severity_penalty = sum({"CRITICAL": 25, "HIGH": 12, "MEDIUM": 5, "LOW": 2}.get(item.severity, 0) for item in blockers)
        blocker_adjustment = clamp(100 - severity_penalty)
        dimensions = {
            "STRUCTURAL_CLARITY": structural,
            "FORMULA_RESOLVABILITY": formula,
            "DEPENDENCY_COMPLETENESS": dependency_complete,
            "EXTERNAL_RESOLUTION": external_resolution,
            "AUTOMATION_MIGRATABILITY": automation_migratability,
            "CONTROL_COMPLETENESS": controls,
            "DATA_MODEL_CLARITY": data_clarity,
            "CHANGE_STABILITY": stability,
            "BLOCKER_ADJUSTMENT": blocker_adjustment,
        }
        score = round(sum(dimensions[key] * READINESS_WEIGHTS[key] for key in dimensions), 2)
        return {
            "score": score, "classification": self.classification(score),
            "dimensions": [{"dimension": key, "score": round(value, 2),
                            "weight": READINESS_WEIGHTS[key],
                            "contribution": round(value * READINESS_WEIGHTS[key], 2),
                            "explanation": self._explanation(key)} for key, value in dimensions.items()],
        }

    @staticmethod
    def classification(score: float) -> str:
        if score >= 80: return "READY"
        if score >= 60: return "CONDITIONALLY_READY"
        if score >= 40: return "MAJOR_REMEDIATION_REQUIRED"
        return "NOT_READY"

    @staticmethod
    def _explanation(name: str) -> str:
        return {
            "STRUCTURAL_CLARITY": "Visible, bounded, tabular workbook structure.",
            "FORMULA_RESOLVABILITY": "Formula references that can be translated without guessing.",
            "DEPENDENCY_COMPLETENESS": "Complete acyclic calculation and integration paths.",
            "EXTERNAL_RESOLUTION": "External sources are portable, mapped, and credential-safe.",
            "AUTOMATION_MIGRATABILITY": "Automation can be expressed as governed services or pipelines.",
            "CONTROL_COMPLETENESS": "Current preventive and governance controls have evidence.",
            "DATA_MODEL_CLARITY": "Data regions are typed, dense, and relationally understandable.",
            "CHANGE_STABILITY": "History shows stable behavior with limited conflict and reversal.",
            "BLOCKER_ADJUSTMENT": "Critical blockers reduce executable migration readiness.",
        }[name]
