"""Rule-based workbook migration strategy recommendation."""


class StrategyEngine:
    def evaluate(self, features: dict, readiness: dict, coverage: dict, blockers: list) -> dict:
        inventory = features["inventory"]
        dependency = features["dependency"]
        complexity = features["intelligence"]["complexity"]
        has_automation = bool(inventory.get("vba_present") or inventory.get("power_queries") or inventory.get("connections"))
        critical = sum(item.severity == "CRITICAL" for item in blockers)
        domains = dependency.get("calculation_components", 0)
        if not inventory.get("used_cells"):
            strategy, reason = "RETIRE", "The workbook has no material active data or calculation surface."
        elif critical and readiness["score"] < 35:
            strategy, reason = "RETAIN_IN_EXCEL", "Critical blockers must be remediated before an executable migration can begin."
        elif complexity >= 75 and domains >= 4:
            strategy, reason = "DECOMPOSE_AND_MIGRATE", "High complexity and multiple dependency domains should become bounded services, not one monolith."
        elif has_automation or coverage.get("MANUAL_REENGINEERING", 0) >= 10:
            strategy, reason = "HYBRID_MODERNIZATION", "Structured data and formulas can move first while automation is reengineered behind an Excel-compatible interaction layer."
        elif coverage.get("AUTO_MIGRATABLE", 0) >= 75 and readiness["score"] >= 70:
            strategy, reason = "NATIVE_REBUILD", "Most weighted units have direct native mappings and the evidence chain is migration-ready."
        else:
            strategy, reason = "LIFT_AND_GOVERN", "Retain the Excel experience while moving state, versioning, controls, and integrations into Git Walk."
        return {"recommended": strategy, "reason": reason,
                "decomposition_required": strategy == "DECOMPOSE_AND_MIGRATE",
                "authoritative_basis": "DETERMINISTIC_RULESET"}
