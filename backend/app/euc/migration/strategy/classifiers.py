"""Deterministic multi-level migration-unit classification."""

import hashlib
import json

from ..models import MigrationUnit
from ..rules import DYNAMIC_FUNCTIONS, LOOKUP_FUNCTIONS, SIMPLE_FUNCTIONS, TARGET_RULES


def _id(source_type: str, source_id: str) -> str:
    return f"MUN_{hashlib.sha256(f'{source_type}:{source_id}'.encode()).hexdigest()[:22].upper()}"


class ComponentClassifier:
    def classify(self, features: dict) -> list[MigrationUnit]:
        units = []
        units.extend(self._sheets(features))
        units.extend(self._patterns(features))
        units.extend(self._objects(features))
        units.extend(self._externals(features))
        units.extend(self._connections(features))
        units.extend(self._domains(features))
        units.extend(self._controls(features))
        return sorted(units, key=lambda unit: (unit.source_type, unit.source_name, unit.unit_id))

    def _sheets(self, features):
        result = []
        for sheet in features["sheets"]:
            empty = not (sheet.get("used_cell_count") or 0)
            mode = "RETIRE" if empty else "ASSISTED_MIGRATION" if sheet.get("visibility") == "VERYHIDDEN" else "AUTO_MIGRATABLE"
            difficulty = "LOW" if (sheet.get("formula_cell_count") or 0) < 25 else "MEDIUM" if (sheet.get("formula_cell_count") or 0) < 500 else "HIGH"
            result.append(self._unit("SHEET", sheet["sheet_id"], sheet["sheet_name"], mode, difficulty,
                max(2, (sheet.get("used_cell_count") or 0) ** .35), sheet_id=sheet["sheet_id"], evidence=sheet,
                rationale="Empty sheet can retire." if empty else "Worksheet requires an explicit native interaction or calculation boundary."))
        return result

    def _patterns(self, features):
        result = []
        for item in features["patterns"]:
            functions = set(json.loads(item.get("functions_json") or "[]"))
            if "#REF!" in item.get("normalized_formula", ""):
                mode, difficulty, reason = "UNSUPPORTED", "VERY_HIGH", "Broken references prevent authoritative translation."
            elif functions & DYNAMIC_FUNCTIONS or item.get("dynamic_reference_count"):
                mode, difficulty, reason = "MANUAL_REENGINEERING", "VERY_HIGH", "Dynamic references require explicit target architecture."
            elif item.get("external_reference"):
                mode, difficulty, reason = "ASSISTED_MIGRATION", "HIGH", "External formula dependencies require mapped service contracts."
            elif functions <= SIMPLE_FUNCTIONS:
                mode, difficulty, reason = "AUTO_MIGRATABLE", "LOW", "The formula pattern maps to a deterministic calculation rule."
            elif functions & LOOKUP_FUNCTIONS:
                mode, difficulty, reason = "AUTO_MIGRATABLE", "MEDIUM", "Lookup logic maps to a relational join with parity tests."
            elif (item.get("ast_depth") or 0) >= 8 or len(functions) >= 6:
                mode, difficulty, reason = "ASSISTED_MIGRATION", "HIGH", "Deep or diverse business logic requires engineering review."
            else:
                mode, difficulty, reason = "ASSISTED_MIGRATION", "MEDIUM", "Formula translation is deterministic but needs domain confirmation."
            result.append(self._unit("FORMULA_PATTERN", item["pattern_id"], item["normalized_formula"][:100], mode,
                difficulty, max(1, item.get("occurrence_count") or 1) * (1 + (item.get("ast_depth") or 0) / 5),
                evidence=item, rationale=reason))
        return result

    def _objects(self, features):
        result = []
        for item in features["objects"]:
            source_type = item["object_type"]
            if source_type not in TARGET_RULES:
                continue
            details = item.get("details", {})
            if source_type == "VBA_PROJECT":
                macro_governance = features.get("macro_governance") or {}
                if macro_governance.get("all_runnable"):
                    # Virtual Run's static gate already proved every macro
                    # in this workbook is sandboxed and side-effect-free
                    # (see BlockerEngine's matching downgrade) -- treat it
                    # like any other assisted, well-understood migration
                    # unit rather than an opaque, unbounded-effort one.
                    mode, difficulty, reason = "ASSISTED_MIGRATION", "MEDIUM", "All macros are Virtual Run-eligible (sandboxed, in-workbook only); translate their logic directly."
                else:
                    mode, difficulty, reason = "MANUAL_REENGINEERING", "VERY_HIGH", "VBA capabilities and side effects require service reengineering."
                weight = max(20, details.get("size_bytes", 0) / 5000, details.get("module_count", 0) * 8)
            elif source_type == "POWER_QUERY":
                mode, difficulty, reason, weight = "ASSISTED_MIGRATION", "HIGH", "Query metadata maps to a managed pipeline after source validation.", 15
            elif source_type in {"TABLE", "NAMED_RANGE"}:
                mode, difficulty, reason, weight = "AUTO_MIGRATABLE", "LOW", "Structured workbook object has a direct typed target mapping.", 5
            else:
                mode, difficulty, reason, weight = "ASSISTED_MIGRATION", "MEDIUM", "Presentation or analytical objects require target-view confirmation.", 4
            result.append(self._unit(source_type, item["inventory_id"], item.get("object_name") or source_type,
                mode, difficulty, weight, sheet_id=item.get("sheet_id"), evidence=item, rationale=reason))
        return result

    def _externals(self, features):
        return [self._unit("EXTERNAL_LINK", item["link_id"], item.get("source_euc_reference") or "External workbook",
            "ASSISTED_MIGRATION" if item.get("resolution_status") == "RESOLVED" else "MANUAL_REENGINEERING",
            "MEDIUM" if item.get("resolution_status") == "RESOLVED" else "HIGH", 12, evidence=item,
            rationale="External dependency must become a governed data or service contract.") for item in features["external_links"]]

    def _connections(self, features):
        return [self._unit("CONNECTION", item["connection_id"], item.get("connection_name") or "Data connection",
            "MANUAL_REENGINEERING" if item.get("credential_present") else "ASSISTED_MIGRATION", "HIGH", 15,
            evidence=item, rationale="Connection requires a managed connector, secret, ownership, and integration test.") for item in features["connections"]]

    def _domains(self, features):
        result = []
        for item in features["domains"]:
            criticality = item.get("max_criticality") or 0
            difficulty = "HIGH" if criticality >= 70 or item.get("node_count", 0) >= 100 else "MEDIUM"
            result.append(self._unit("DOMAIN", item["component_id"], f"Calculation domain {item['component_id'].replace('COMPONENT_', '')}",
                "ASSISTED_MIGRATION", difficulty, max(8, item.get("node_count", 0) ** .6), domain_id=item["component_id"],
                evidence=item, rationale="A weakly connected dependency component becomes an independently testable domain boundary."))
        return result

    def _controls(self, features):
        return [self._unit("CONTROL", item["control_code"], item["control_name"],
            "AUTO_MIGRATABLE" if item.get("control_source") == "GITWALK" else "ASSISTED_MIGRATION", "LOW", 4,
            evidence=item, rationale="Source control must be preserved, improved, replaced, or explicitly retired.") for item in features["controls"]]

    @staticmethod
    def _unit(source_type, source_id, name, mode, difficulty, weight, rationale, sheet_id=None,
              domain_id=None, parent_unit_id=None, evidence=None):
        target_type, component, _ = TARGET_RULES[source_type]
        return MigrationUnit(_id(source_type, source_id), source_type, source_id, name, mode, difficulty,
                             round(float(weight), 2), target_type, component, rationale, sheet_id,
                             domain_id, parent_unit_id, None, evidence or {})
