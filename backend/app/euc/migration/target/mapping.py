"""Deterministic source-to-target technology and control mappings."""

import hashlib

from ..rules import TARGET_RULES


class TargetMapper:
    def map_units(self, units: list) -> list[dict]:
        result = []
        for unit in units:
            target_type, component, pattern = TARGET_RULES[unit.source_type]
            mapping_id = self._id("MAP", unit.unit_id)
            result.append({"mapping_id": mapping_id, "unit_id": unit.unit_id,
                           "source_type": unit.source_type, "target_type": target_type,
                           "target_component": component, "mapping_pattern": pattern,
                           "config": {"migration_mode": unit.mode, "difficulty": unit.difficulty,
                                      "requires_human_approval": unit.mode != "AUTO_MIGRATABLE"},
                           "evidence": unit.evidence})
        return result

    def map_controls(self, controls: list[dict]) -> list[dict]:
        targets = {
            "DATA_VALIDATION": ("API validation plus UI constraints", "IMPROVED"),
            "SHEET_PROTECTION": ("RBAC and field-level write policy", "REPLACED"),
            "CRITICAL_NODE_COVERAGE": ("Policy-enforced critical-rule review", "IMPROVED"),
            "AUDIT_LEDGER": ("Immutable semantic audit ledger", "PRESERVED"),
            "VERSION_HISTORY": ("Content-addressed version history", "PRESERVED"),
            "REVIEW_WORKFLOW": ("Owner-approved merge workflow", "PRESERVED"),
            "BRANCH_PROTECTION": ("Protected main branch policy", "PRESERVED"),
            "ROLLBACK": ("Immutable inverse commit", "PRESERVED"),
        }
        result = []
        for control in controls:
            target, status = targets.get(control["control_code"], ("Target control design required", "MISSING"))
            result.append({"control_mapping_id": self._id("CMP", control["control_code"]),
                           "source_control_code": control["control_code"],
                           "source_control_name": control["control_name"], "target_control": target,
                           "preservation_status": status,
                           "rationale": "The target must provide equivalent or stronger enforceable evidence.",
                           "test_required": True, "evidence": control})
        return result

    def architecture(self, units: list, strategy: dict) -> dict:
        components = {}
        edges = set()
        for unit in units:
            components.setdefault(unit.target_component, {"id": self._slug(unit.target_component),
                                                           "name": unit.target_component,
                                                           "unit_count": 0, "target_types": set()})
            components[unit.target_component]["unit_count"] += 1
            components[unit.target_component]["target_types"].add(unit.target_type)
        names = set(components)
        if "Experience Layer" in names:
            for target in names & {"Application Platform", "Data Platform", "Reporting Platform"}:
                edges.add(("Experience Layer", target))
        if "Application Platform" in names and "Data Platform" in names:
            edges.add(("Application Platform", "Data Platform"))
        if "Integration Platform" in names and "Data Platform" in names:
            edges.add(("Integration Platform", "Data Platform"))
        if "Reporting Platform" in names and "Data Platform" in names:
            edges.add(("Reporting Platform", "Data Platform"))
        if "Control Plane" in names:
            for target in names - {"Control Plane"}:
                edges.add(("Control Plane", target))
        return {"strategy": strategy["recommended"],
                "components": [{**item, "target_types": sorted(item["target_types"])} for item in components.values()],
                "edges": [{"source": self._slug(source), "target": self._slug(target),
                           "relationship": "GOVERNS_OR_DEPENDS_ON"} for source, target in sorted(edges)]}

    @staticmethod
    def _id(prefix, value):
        return f"{prefix}_{hashlib.sha256(value.encode()).hexdigest()[:20].upper()}"

    @staticmethod
    def _slug(value):
        return value.upper().replace(" ", "_")
