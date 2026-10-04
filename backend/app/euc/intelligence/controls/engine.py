"""Control inventory and dependency-weighted coverage."""

from ..scoring.normalization import blend, ratio_score


class ControlEngine:
    def evaluate(self, features: dict) -> dict:
        source = features["controls"]
        formula_nodes = max(1, source["formula_nodes"])
        critical_nodes = max(1, source["critical_nodes"])
        formula_protection = ratio_score(source["protected_formula_nodes"], formula_nodes)
        input_validation = ratio_score(source["validated_critical_nodes"], critical_nodes)
        critical_coverage = ratio_score(source["controlled_critical_nodes"], critical_nodes)
        weighted_coverage = ratio_score(source["controlled_criticality"], source["total_criticality"])
        native_score = blend(
            (formula_protection, .30), (input_validation, .25),
            (critical_coverage, .25), (weighted_coverage, .20),
        )
        governance_signals = source["governance"]
        governance_score = ratio_score(sum(bool(value) for value in governance_signals.values()), len(governance_signals))
        effective = blend((native_score, .55), (governance_score, .45))
        inventory = [
            self._control("DATA_VALIDATION", "Input validation", "PREVENTIVE", "WORKBOOK",
                          source["validations"], input_validation),
            self._control("SHEET_PROTECTION", "Protected formula regions", "PREVENTIVE", "WORKBOOK",
                          source["protected_sheets"], formula_protection),
            self._control("CRITICAL_NODE_COVERAGE", "Critical dependency coverage", "PREVENTIVE", "COMBINED",
                          source["controlled_critical_nodes"], critical_coverage, weighted_coverage),
            self._control("AUDIT_LEDGER", "Immutable audit trail", "GOVERNANCE", "GITWALK",
                          int(governance_signals["audit_ledger"]), 100 if governance_signals["audit_ledger"] else 0),
            self._control("VERSION_HISTORY", "Version history and attribution", "CORRECTIVE", "GITWALK",
                          int(governance_signals["version_history"]), 100 if governance_signals["version_history"] else 0),
            self._control("REVIEW_WORKFLOW", "Owner-approved merge review", "GOVERNANCE", "GITWALK",
                          int(governance_signals["review_workflow"]), 100 if governance_signals["review_workflow"] else 0),
            self._control("BRANCH_PROTECTION", "Protected main branch", "PREVENTIVE", "GITWALK",
                          int(governance_signals["branch_protection"]), 100 if governance_signals["branch_protection"] else 0),
            self._control("ROLLBACK", "Commit revert and branch recovery", "CORRECTIVE", "GITWALK",
                          int(governance_signals["rollback"]), 100 if governance_signals["rollback"] else 0),
        ]
        return {
            "native_control_strength": native_score,
            "governance_score": governance_score,
            "effective_control_strength": effective,
            "formula_protection": formula_protection,
            "input_validation": input_validation,
            "critical_node_coverage": critical_coverage,
            "dependency_weighted_coverage": weighted_coverage,
            "review_coverage": 100 if governance_signals["review_workflow"] else 0,
            "audit_coverage": 100 if governance_signals["audit_ledger"] else 0,
            "inventory": inventory,
        }

    @staticmethod
    def _control(code: str, name: str, category: str, source: str, count: int,
                 coverage: float, weighted: float | None = None) -> dict:
        score = weighted if weighted is not None else coverage
        effectiveness = "STRONG" if score >= 75 else "MODERATE" if score >= 40 else "WEAK"
        return {"code": code, "name": name, "category": category, "source": source,
                "count": count, "coverage": coverage,
                "weighted_coverage": weighted if weighted is not None else coverage,
                "effectiveness": effectiveness}
