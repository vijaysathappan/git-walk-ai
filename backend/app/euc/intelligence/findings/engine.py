"""Candidate-index rule engine. Rules never scan every cell independently."""

from __future__ import annotations

from .models import FindingCandidate


SEVERITY_ORDER = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def _severity(base: str, criticality: float = 0, downstream: int = 0) -> str:
    level = SEVERITY_ORDER[base]
    if criticality >= 85 or downstream >= 1000:
        level += 1
    if criticality >= 95 or downstream >= 10000:
        level += 1
    return next(name for name, value in SEVERITY_ORDER.items() if value == min(4, level))


class FindingEngine:
    def evaluate(self, features: dict, controls: dict) -> list[FindingCandidate]:
        candidates: list[FindingCandidate] = []
        index = features["candidates"]
        for item in index["broken"]:
            candidates.append(self._node_finding(
                "BROKEN_REFERENCE", "FORMULA_INTEGRITY", "HIGH", .99,
                "Broken formula dependency", "A formula points to a missing or invalid target.",
                "REPAIR_REFERENCE", item, item.get("source_node_id") or item.get("node_id"),
            ))
        for item in index["dynamic"]:
            candidates.append(self._node_finding(
                "DYNAMIC_REFERENCE_HIGH_IMPACT", "FORMULA_INTEGRITY", "MEDIUM", .92,
                "Dynamic dependency requires review", "The dependency target cannot be fully resolved statically.",
                "REPLACE_OR_DOCUMENT_DYNAMIC_REFERENCE", item, item.get("source_node_id") or item.get("node_id"),
            ))
        for item in index["cycles"]:
            candidates.append(FindingCandidate(
                "CIRCULAR_DEPENDENCY", "DEPENDENCY", _severity("HIGH", downstream=item.get("cycle_size", 0)), .99,
                "Circular calculation component", "A strongly connected dependency component prevents acyclic calculation ordering.",
                "BREAK_OR_DOCUMENT_CYCLE", item["cycle_id"], item,
                dependency_impact={"cycle_size": item["cycle_size"], "sheets": item.get("sheets", [])},
            ))
        for item in index["pattern_breaks"]:
            candidates.append(FindingCandidate(
                "FORMULA_PATTERN_BREAK", "FORMULA_INTEGRITY", _severity("MEDIUM", item.get("technical_criticality", 0)), .88,
                "Formula pattern differs from its region", "A formula differs from the consistent formulas immediately surrounding it.",
                "REVIEW_FORMULA_PATTERN", f"{item.get('sheet_id')}:{item.get('cell_address')}", item,
                item.get("node_id"), item.get("sheet_id"), item.get("cell_address"),
                {"downstream": item.get("downstream_count", 0), "sheets": item.get("sheet_spread", 0)},
            ))
        for item in index["formula_overrides"]:
            candidates.append(FindingCandidate(
                "FORMULA_REPLACED_BY_CONSTANT", "FORMULA_INTEGRITY",
                _severity("HIGH", item.get("technical_criticality", 0), item.get("downstream_count", 0)),
                item.get("confidence", .95), "Formula replaced by a constant",
                "Version history shows a formula was replaced by a hardcoded value.",
                "RESTORE_OR_APPROVE_OVERRIDE", f"{item.get('sheet_id')}:{item.get('row_id')}:{item.get('column_id')}", item,
                item.get("node_id"), item.get("sheet_id"), item.get("cell_address"),
                {"downstream": item.get("downstream_count", 0), "sheets": item.get("sheet_spread", 0)},
            ))
        for item in index["unresolved_external"]:
            candidates.append(FindingCandidate(
                "UNRESOLVED_EXTERNAL_WORKBOOK", "EXTERNAL", "HIGH", .98,
                "External workbook is unavailable", "A workbook dependency cannot be resolved to a governed EUC asset.",
                "MAP_EXTERNAL_DEPENDENCY", str(item.get("source_euc_reference") or item.get("target_node_id")), item,
            ))
        for item in index["local_paths"]:
            candidates.append(FindingCandidate(
                "LOCAL_MACHINE_DEPENDENCY", "EXTERNAL", "HIGH", .99,
                "Local-machine dependency reduces portability", "The workbook references a user or local drive path unavailable to other environments.",
                "MOVE_SOURCE_TO_GOVERNED_LOCATION", str(item.get("path")), item,
            ))
        for item in index["change_hotspots"]:
            candidates.append(self._node_finding(
                "HIGH_IMPACT_CHANGE_HOTSPOT", "CHANGE", "HIGH", .96,
                "Frequently changed high-impact logic", "Repeated edits affect a dependency node with a large downstream blast radius.",
                "REQUIRE_REVIEW_FOR_HOTSPOT", item, item.get("node_id"),
            ))
        for item in index["uncontrolled_critical"]:
            candidates.append(self._node_finding(
                "UNPROTECTED_CRITICAL_FORMULA", "CONTROL", "HIGH", .90,
                "Critical formula has weak preventive controls", "A high-criticality formula is neither protected nor validation-controlled.",
                "PROTECT_AND_REVIEW_CRITICAL_FORMULA", item, item.get("node_id"),
            ))
        if features["automation"].get("vba_present"):
            candidates.append(FindingCandidate(
                "VBA_AUTOMATION_REVIEW", "AUTOMATION", "MEDIUM", .99,
                "VBA automation requires governed review", "Static inventory detected VBA. Git Walk did not execute the code.",
                "REVIEW_VBA_CAPABILITIES", "VBA_PROJECT", features["automation"],
            ))
        unique = {}
        for item in candidates:
            key = (item.rule_id, item.identity)
            existing = unique.get(key)
            if not existing or SEVERITY_ORDER[item.severity] > SEVERITY_ORDER[existing.severity]:
                unique[key] = item
        return sorted(unique.values(), key=lambda item: (-SEVERITY_ORDER[item.severity], item.rule_id, item.identity))

    @staticmethod
    def _node_finding(rule: str, category: str, base: str, confidence: float, title: str,
                      description: str, remediation: str, item: dict, node_id: str | None) -> FindingCandidate:
        criticality = float(item.get("technical_criticality", 0) or 0)
        downstream = int(item.get("downstream_count", 0) or 0)
        identity = node_id or str(item.get("edge_id") or item.get("source_name") or item)
        return FindingCandidate(
            rule, category, _severity(base, criticality, downstream), confidence,
            title, description, remediation, identity, item, node_id,
            item.get("sheet_id"), item.get("cell_address"),
            {"downstream": downstream, "sheets": item.get("sheet_spread", 0),
             "technical_criticality": criticality},
        )
