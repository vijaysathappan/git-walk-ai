"""Parity, integration, control, and historical replay test planning."""

import hashlib

from ....config import settings


class ValidationPlanner:
    def build(self, units: list, features: dict, controls: list[dict]) -> list[dict]:
        replay_count = min(settings.migration_replay_commit_limit, len(features["history"]["recent_commits"]))
        plans = []
        for unit in units:
            if unit.source_type == "FORMULA_PATTERN":
                plans.append(self._plan(unit, "FORMULA_PARITY", "Excel formula outputs", "Calculation service outputs",
                                        "NUMERIC_TOLERANCE", "HIGH", replay_count,
                                        "All critical outputs match within configured absolute or relative tolerance."))
            elif unit.source_type in {"TABLE", "SHEET", "NAMED_RANGE"}:
                plans.append(self._plan(unit, "DATA_RECONCILIATION", "Source workbook records", "Target persisted records",
                                        "KEYED_EXACT_OR_TYPED_COMPARE", "MEDIUM", replay_count,
                                        "Record counts, keys, nullability, and typed values reconcile."))
            elif unit.source_type in {"CONNECTION", "EXTERNAL_LINK", "POWER_QUERY", "VBA_PROJECT"}:
                plans.append(self._plan(unit, "INTEGRATION_ACCEPTANCE", "Source side effect or integration output",
                                        "Managed integration output", "CONTRACT_AND_SAMPLE_COMPARE", "HIGH", replay_count,
                                        "Contract, error handling, security, and representative outputs are equivalent."))
        for control in controls:
            pseudo = type("ControlUnit", (), {"unit_id": None, "source_name": control["source_control_name"]})()
            plans.append(self._plan(pseudo, "CONTROL_EFFECTIVENESS", control["source_control_name"], control["target_control"],
                                    "CONTROL_EVIDENCE_REVIEW", "HIGH", replay_count,
                                    "The target control operates and produces evidence at least as strong as the source."))
        return plans

    @staticmethod
    def _plan(unit, kind, source, target, method, priority, replay, criteria):
        identity = f"{unit.unit_id}:{kind}:{source}:{target}"
        return {"validation_id": f"VAL_{hashlib.sha256(identity.encode()).hexdigest()[:20].upper()}",
                "unit_id": unit.unit_id, "validation_type": kind, "source_output": source,
                "target_output": target, "comparison_method": method,
                "absolute_tolerance": settings.migration_absolute_tolerance if kind == "FORMULA_PARITY" else None,
                "relative_tolerance": settings.migration_relative_tolerance if kind == "FORMULA_PARITY" else None,
                "priority": priority, "historical_replay_count": replay,
                "acceptance_criteria": criteria}
