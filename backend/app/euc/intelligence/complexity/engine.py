"""Explainable Stage 2.3 complexity dimensions."""

from ..scoring.models import ScoreComponent, ScoreResult
from ..scoring.normalization import blend, log_score, ratio_score, saturation_score


class ComplexityEngine:
    def evaluate(self, features: dict, profile: dict) -> ScoreResult:
        summary = features["inventory"]
        dependency = features["dependency"]
        ast = features["ast"]
        objects = features["objects"]
        history = features["history"]
        weights = profile["complexity"]

        structural = blend(
            (log_score(summary.get("sheet_count", 0), 100), .20),
            (log_score(summary.get("used_cells", 0), 5_000_000), .30),
            (log_score(sum(objects.get(key, 0) for key in ("TABLE", "NAMED_RANGE", "MERGED_RANGE")), 5000), .20),
            (saturation_score(summary.get("hidden_sheets", 0) + summary.get("very_hidden_sheets", 0), 5), .15),
            (log_score(sum(objects.get(key, 0) for key in ("PIVOT_TABLE", "CHART", "VALIDATION")), 1000), .15),
        )
        formula = blend(
            (log_score(summary.get("total_formulas", 0), 1_000_000), .25),
            (log_score(summary.get("unique_patterns", 0), 100_000), .18),
            (saturation_score(ast.get("average_depth", 0), 5), .18),
            (saturation_score(ast.get("function_diversity", 0), 20), .12),
            (ratio_score(dependency.get("dynamic_count", 0), max(1, dependency.get("edge_count", 0))), .14),
            (ratio_score(dependency.get("cross_sheet_edges", 0), max(1, dependency.get("edge_count", 0))), .13),
        )
        dependency_score = blend(
            (saturation_score(dependency.get("maximum_calculation_depth", 0), 8), .20),
            (dependency.get("cross_sheet_coupling", 0), .18),
            (dependency.get("external_coupling", 0), .12),
            (saturation_score(dependency.get("cycle_count", 0), 2), .17),
            (saturation_score(dependency.get("dynamic_count", 0), 20), .13),
            (saturation_score(dependency.get("calculation_components", 0), 20), .10),
            (saturation_score(dependency.get("dependency_density", 0), 8), .10),
        )
        data = blend(
            (log_score(summary.get("used_cells", 0), 5_000_000), .35),
            (log_score(sum(sheet.get("max_column", 0) or 0 for sheet in features["sheets"]), 10_000), .20),
            (log_score(objects.get("TABLE", 0), 500), .20),
            (saturation_score(features["data"].get("mixed_type_columns", 0), 10), .15),
            (saturation_score(features["data"].get("sparse_sheets", 0), 5), .10),
        )
        external = blend(
            (saturation_score(summary.get("external_links", 0), 8), .45),
            (saturation_score(summary.get("connections", 0), 5), .35),
            (saturation_score(summary.get("power_queries", 0), 5), .20),
        )
        automation = blend(
            (100 if summary.get("vba_present") or summary.get("macro_enabled") else 0, .55),
            (saturation_score(summary.get("power_queries", 0), 5), .25),
            (saturation_score(summary.get("connections", 0), 5), .20),
        )
        presentation = blend(
            (log_score(objects.get("CHART", 0), 200), .35),
            (log_score(objects.get("PIVOT_TABLE", 0), 100), .30),
            (log_score(objects.get("MERGED_RANGE", 0), 5000), .20),
            (log_score(objects.get("IMAGE", 0), 200), .15),
        )
        change = blend(
            (saturation_score(history.get("commits", 0), 40), .25),
            (saturation_score(history.get("changes", 0), 1000), .25),
            (saturation_score(history.get("formula_changes", 0), 100), .20),
            (saturation_score(history.get("contributors", 0), 6), .10),
            (saturation_score(history.get("conflicts", 0), 10), .12),
            (saturation_score(history.get("reversions", 0), 8), .08),
        )
        values = {
            "STRUCTURAL": (structural, "Workbook size, hidden structure, and object diversity."),
            "FORMULA": (formula, "Formula volume, AST structure, diversity, and dynamic logic."),
            "DEPENDENCY": (dependency_score, "Calculation depth, coupling, cycles, density, and components."),
            "DATA": (data, "Data volume, width, tables, sparse regions, and mixed types."),
            "EXTERNAL_INTEGRATION": (external, "External links, connections, and query integration."),
            "AUTOMATION": (automation, "Static VBA, Power Query, and refresh automation footprint."),
            "PRESENTATION": (presentation, "Charts, pivots, merged regions, and visual objects."),
            "CHANGE": (change, "Version activity, contributors, conflicts, and reversions."),
        }
        return ScoreResult("COMPLEXITY", tuple(
            ScoreComponent(name, score, weights[name], explanation,
                           self._evidence(name, features))
            for name, (score, explanation) in values.items()
        ))

    @staticmethod
    def _evidence(name: str, features: dict) -> dict:
        mapping = {
            "STRUCTURAL": features["inventory"], "FORMULA": features["ast"],
            "DEPENDENCY": features["dependency"], "DATA": features["data"],
            "EXTERNAL_INTEGRATION": features["external"], "AUTOMATION": features["automation"],
            "PRESENTATION": features["objects"], "CHANGE": features["history"],
        }
        return mapping[name]
