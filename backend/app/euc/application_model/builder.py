"""Deterministic transformation from migration evidence to canonical AIR."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any


def stable_id(prefix: str, *parts: Any) -> str:
    source = "|".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha256(source.encode()).hexdigest()[:16].upper()}"


def slug(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", str(value or "").strip()).strip("_").lower()
    return text or "unnamed"


def _json(value: str | None, fallback):
    return json.loads(value) if value else fallback


def _generation_policy(confidence: float, auto_threshold: float, review_threshold: float) -> str:
    if confidence >= auto_threshold:
        return "AUTO_GENERATE"
    if confidence >= review_threshold:
        return "GENERATE_AFTER_REVIEW"
    return "MODEL_ONLY"


def _field_type(header: str, values: list[Any]) -> str:
    name = slug(header)
    non_null = [value for value in values if value not in (None, "")]
    if name.endswith("_id") or name == "id" or "identifier" in name:
        return "IDENTIFIER"
    if "percent" in name or "rate" in name or name.endswith("_pct"):
        return "PERCENTAGE"
    if any(token in name for token in ("amount", "price", "cost", "revenue", "margin")):
        return "MONEY"
    if any(token in name for token in ("date", "day")):
        return "DATE"
    if any(token in name for token in ("time", "timestamp", "created_at", "updated_at")):
        return "DATETIME"
    if non_null and all(isinstance(value, bool) for value in non_null):
        return "BOOLEAN"
    if non_null and all(isinstance(value, int) and not isinstance(value, bool) for value in non_null):
        return "INTEGER"
    if non_null and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in non_null):
        return "DECIMAL"
    return "STRING"


def _entity_classification(sheet_name: str, formula_count: int) -> str:
    name = slug(sheet_name)
    if any(token in name for token in ("config", "setting", "assumption", "parameter")):
        return "CONFIGURATION"
    if any(token in name for token in ("lookup", "reference", "rate", "code")):
        return "REFERENCE_DATA"
    if any(token in name for token in ("report", "dashboard", "summary", "output")):
        return "REPORTING"
    if any(token in name for token in ("order", "sale", "transaction", "invoice", "entry")):
        return "TRANSACTION"
    if formula_count > 0:
        return "CALCULATION_OUTPUT"
    return "MASTER_DATA"


def _screen_type(classification: str, sheet: dict) -> str:
    if sheet.get("chart_count", 0) or classification == "REPORTING":
        return "DASHBOARD"
    if classification == "CONFIGURATION":
        return "SETTINGS"
    if sheet.get("table_count", 0) or sheet.get("max_row", 0) > 15:
        return "DATA_GRID"
    if sheet.get("formula_cell_count", 0) > sheet.get("constant_cell_count", 0):
        return "DETAIL_VIEW"
    return "FORM"


class AIRBuilder:
    """Build stack-independent AIR from persisted Stage 2.1-2.4 evidence."""

    def __init__(self, conn, ledger, asset, inventory, dependency, migration, blueprint,
                 auto_threshold: float, review_threshold: float):
        self.conn = conn
        self.ledger = ledger
        self.asset = asset
        self.inventory = inventory
        self.dependency = dependency
        self.migration = migration
        self.blueprint = blueprint
        self.auto_threshold = auto_threshold
        self.review_threshold = review_threshold
        self.analysis_id = inventory["ANALYSIS_ID"]
        self.dependency_id = dependency["DEPENDENCY_RUN_ID"]

    def build(self, target_profile: str, air_version: str) -> dict[str, Any]:
        sheets = self._rows(
            "SELECT * FROM EUC_SHEET_INVENTORY WHERE ANALYSIS_ID=? ORDER BY SHEET_POSITION",
            self.analysis_id,
        )
        objects = self._object_rows()
        domains = self._domains(sheets)
        entities = self._entities(sheets, objects)
        relationships = self._relationships(entities)
        calculations, rules = self._calculations()
        parameters = self._parameters(objects)
        services = self._services(domains, entities, calculations)
        apis = self._apis(entities, services)
        screens = self._screens(sheets, entities, objects)
        workflows = self._workflows(sheets, objects)
        integrations = self._integrations()
        controls = self._controls()
        roles = self._roles()
        jobs = self._jobs(integrations, calculations)
        tests = self._tests()
        components = [
            *domains, *entities, *relationships, *calculations, *rules, *parameters,
            *services, *apis, *screens, *workflows, *integrations, *controls, *roles, *jobs, *tests,
        ]
        coverage = self._coverage(components)
        lineage = self._lineage(components)
        return {
            "schema": "gitwalk.air.v1",
            "air_version": air_version,
            "target_profile": target_profile,
            "application": {
                "name": re.sub(r"\.(xlsx|xlsm|xlsb|csv)$", "", self.asset["ORIGINAL_FILENAME"], flags=re.I),
                "source_euc": self.asset["EUC_ID"],
                "source_commit": self.migration["SOURCE_COMMIT_ID"],
                "repository_id": self.asset["REPOSITORY_ID"],
                "migration_run_id": self.migration["MIGRATION_RUN_ID"],
                "migration_blueprint_hash": self.migration["BLUEPRINT_MANIFEST_HASH"],
            },
            "domains": domains,
            "entities": entities,
            "relationships": relationships,
            "calculations": calculations,
            "business_rules": rules,
            "configuration_parameters": parameters,
            "services": services,
            "apis": apis,
            "screens": screens,
            "workflows": workflows,
            "integrations": integrations,
            "controls": controls,
            "roles": roles,
            "jobs": jobs,
            "tests": tests,
            "coverage": coverage,
            "lineage": lineage,
        }

    def _base(self, component_type: str, name: str, confidence: float, provenance: dict,
              domain_id: str | None = None, source_unit_id: str | None = None) -> dict:
        component_id = stable_id(component_type[:4], self.asset["EUC_ID"], component_type,
                                 provenance.get("source_id"), name)
        review_state = "REVIEW_REQUIRED" if confidence < self.auto_threshold or component_type in {
            "RELATIONSHIP", "ROLE", "WORKFLOW"
        } else "AUTO_CONFIRMED"
        return {
            "component_id": component_id,
            "component_type": component_type,
            "name": name,
            "domain_id": domain_id,
            "confidence": round(confidence, 4),
            "generation_policy": _generation_policy(
                confidence, self.auto_threshold, self.review_threshold
            ),
            "review_state": review_state,
            "source_unit_id": source_unit_id,
            "provenance": {
                "source_commit": self.migration["SOURCE_COMMIT_ID"],
                **provenance,
            },
        }

    def _domains(self, sheets: list[dict]) -> list[dict]:
        names = sorted({unit.get("domain_id") for unit in self.blueprint.get("units", []) if unit.get("domain_id")})
        if not names:
            names = [slug(sheet["sheet_name"]) for sheet in sheets]
        result = []
        for name in names:
            item = self._base("DOMAIN", str(name).replace("_", " ").title(), 0.9,
                              {"source_type": "MIGRATION_DOMAIN", "source_id": name})
            item["domain_key"] = slug(name)
            result.append(item)
        return result

    def _entities(self, sheets: list[dict], objects: list[dict]) -> list[dict]:
        table_by_sheet = {item["sheet_id"]: item for item in objects if item["object_type"] == "TABLE"}
        result = []
        for sheet in sheets:
            if sheet["max_column"] < 1 or sheet["max_row"] < 1:
                continue
            table = table_by_sheet.get(sheet["sheet_id"])
            details = table.get("details", {}) if table else {}
            headers = [str(value).strip() for value in details.get("columns", []) if str(value).strip()]
            if not headers:
                headers = self._repository_headers(sheet["sheet_id"], sheet["max_column"])
            if not headers:
                headers = [f"Column {index}" for index in range(1, min(sheet["max_column"], 50) + 1)]
            fields = []
            seen = set()
            for position, header in enumerate(headers):
                semantic = slug(header)
                if semantic in seen:
                    semantic = f"{semantic}_{position + 1}"
                seen.add(semantic)
                values = self._column_samples(sheet["sheet_id"], header)
                non_null = [value for value in values if value not in (None, "")]
                unique = bool(non_null) and len({str(value) for value in non_null}) == len(non_null)
                pk = unique and (position == 0 or semantic == "id" or semantic.endswith("_id"))
                fields.append({
                    "field_id": stable_id("FLD", sheet["sheet_id"], semantic),
                    "name": semantic,
                    "semantic_name": str(header),
                    "type": _field_type(header, values),
                    "nullable": len(non_null) < len(values) if values else True,
                    "unique": unique,
                    "primary_key_candidate": pk,
                    "confirmed_primary_key": pk and bool(table),
                    "foreign_key_candidate": semantic.endswith("_id") and not pk,
                    "source_column_position": position + 1,
                })
            classification = _entity_classification(sheet["sheet_name"], sheet["formula_cell_count"])
            confidence = 0.97 if table else (0.84 if len(headers) >= 2 and sheet["max_row"] >= 2 else 0.58)
            item = self._base("ENTITY", table["object_name"] if table else sheet["sheet_name"], confidence,
                              {"source_type": "SHEET", "source_id": sheet["sheet_id"],
                               "sheet": sheet["sheet_name"], "source_range": table.get("cell_or_range") if table else sheet["used_range"]})
            item.update({"entity_key": slug(item["name"]), "classification": classification,
                         "persistent": classification not in {"REPORTING", "CALCULATION_OUTPUT", "TEMPORARY"},
                         "fields": fields})
            result.append(item)
        return result

    def _relationships(self, entities: list[dict]) -> list[dict]:
        key_fields: dict[str, list[tuple[dict, dict]]] = {}
        for entity in entities:
            for field in entity["fields"]:
                if field["primary_key_candidate"]:
                    key_fields.setdefault(field["name"], []).append((entity, field))
        result = []
        for entity in entities:
            for field in entity["fields"]:
                targets = key_fields.get(field["name"], [])
                for target, target_field in targets:
                    if target["component_id"] == entity["component_id"]:
                        continue
                    item = self._base("RELATIONSHIP", f"{entity['name']} to {target['name']}", 0.82,
                                      {"source_type": "FIELD_MATCH", "source_id": field["field_id"],
                                       "sheet": entity["provenance"].get("sheet")})
                    item.update({"relationship_type": "MANY_TO_ONE", "status": "INFERRED",
                                 "source_entity_id": entity["component_id"], "source_field_id": field["field_id"],
                                 "target_entity_id": target["component_id"], "target_field_id": target_field["field_id"]})
                    result.append(item)
        return result

    def _calculations(self) -> tuple[list[dict], list[dict]]:
        calculations, rules = [], []
        rows = self.conn.execute(
            """SELECT P.*,A.AST_OBJECT_HASH FROM EUC_FORMULA_PATTERNS P
               LEFT JOIN EUC_FORMULA_ASTS A ON A.DEPENDENCY_RUN_ID=? AND A.PATTERN_ID=P.PATTERN_ID
               WHERE P.ANALYSIS_ID=? ORDER BY P.NORMALIZED_HASH""",
            (self.dependency_id, self.analysis_id),
        ).fetchall()
        for row in rows:
            functions = [str(value).upper() for value in _json(row["FUNCTIONS_JSON"], [])]
            if row["EXTERNAL_REFERENCE"]:
                category, confidence = "SERVICE_TRANSLATION", 0.55
            elif any(name in {"INDIRECT", "OFFSET"} for name in functions):
                category, confidence = "MANUAL_REVIEW", 0.48
            elif any(name in {"VLOOKUP", "HLOOKUP", "XLOOKUP", "INDEX", "MATCH"} for name in functions):
                category, confidence = "QUERY_TRANSLATION", 0.82
            elif any(name in {"SUMIF", "SUMIFS", "COUNTIF", "COUNTIFS", "AVERAGEIF"} for name in functions):
                category, confidence = "QUERY_TRANSLATION", 0.88
            elif "IF" in functions:
                category, confidence = "SEMANTIC_TRANSLATION", 0.9
            else:
                category, confidence = "DIRECT_TRANSLATION", 0.96
            occurrences = self._rows(
                "SELECT SHEET_ID,CELL_ADDRESS,RAW_FORMULA FROM EUC_FORMULA_OCCURRENCES WHERE ANALYSIS_ID=? AND PATTERN_ID=? ORDER BY SHEET_ID,CELL_ADDRESS LIMIT 25",
                self.analysis_id, row["PATTERN_ID"],
            )
            ast = self.ledger.objects.get(self.conn, row["AST_OBJECT_HASH"]) if row["AST_OBJECT_HASH"] else None
            provenance = {"source_type": "FORMULA_PATTERN", "source_id": row["PATTERN_ID"],
                          "formula_pattern": row["PATTERN_ID"], "source_cells": [f"{item['sheet_id']}!{item['cell_address']}" for item in occurrences]}
            item = self._base("CALCULATION", f"Calculation {row['PATTERN_ID'][-8:]}", confidence, provenance)
            item.update({"translation_category": category, "normalized_formula": row["NORMALIZED_FORMULA"],
                         "expression_ast": ast, "functions": functions,
                         "occurrence_count": row["OCCURRENCE_COUNT"],
                         "unsupported": category == "MANUAL_REVIEW"})
            calculations.append(item)
            if "IF" in functions:
                rule = self._base("BUSINESS_RULE", f"Rule {row['PATTERN_ID'][-8:]}", 0.86, provenance)
                rule.update({"condition_expression": ast, "action": "Evaluate conditional outcome",
                             "calculation_id": item["component_id"]})
                rules.append(rule)
        return calculations, rules

    def _parameters(self, objects: list[dict]) -> list[dict]:
        result = []
        for source in objects:
            if source["object_type"] != "NAMED_RANGE":
                continue
            item = self._base("CONFIGURATION", source["object_name"] or "Named parameter", 0.87,
                              {"source_type": "NAMED_RANGE", "source_id": source["inventory_id"],
                               "source_range": source["cell_or_range"]})
            item.update({"parameter_key": slug(item["name"]), "type": "STRING", "scope": "APPLICATION",
                         "versioned": True})
            result.append(item)
        return result

    def _services(self, domains, entities, calculations):
        result = []
        for domain in domains:
            domain_key = domain["domain_key"]
            operations = [f"manage_{entity['entity_key']}" for entity in entities if domain_key in slug(entity["name"])]
            if calculations:
                operations.append(f"calculate_{domain_key}")
            item = self._base("SERVICE", f"{domain['name']} Service", 0.88,
                              {"source_type": "DOMAIN", "source_id": domain["component_id"]}, domain["component_id"])
            item["operations"] = sorted(set(operations or [f"execute_{domain_key}"]))
            result.append(item)
        return result

    def _apis(self, entities, services):
        result = []
        for entity in entities:
            if not entity["persistent"] or entity["confidence"] < self.review_threshold:
                continue
            for method, suffix, operation in (("GET", "", "list"), ("GET", "/{id}", "read"),
                                               ("POST", "", "create"), ("PUT", "/{id}", "update")):
                path = f"/{entity['entity_key'].replace('_', '-')}s{suffix}"
                item = self._base("API", f"{method} {path}", 0.9,
                                  {"source_type": "ENTITY", "source_id": entity["component_id"],
                                   "sheet": entity["provenance"].get("sheet")})
                item.update({"method": method, "path": path, "operation": operation,
                             "entity_id": entity["component_id"], "permissions": ["READ" if method == "GET" else "WRITE"],
                             "audit_required": method != "GET", "idempotent": method in {"GET", "PUT"}})
                result.append(item)
        return result

    def _screens(self, sheets, entities, objects):
        entities_by_sheet = {item["provenance"].get("source_id"): item for item in entities}
        result = []
        for sheet in sheets:
            entity = entities_by_sheet.get(sheet["sheet_id"])
            classification = entity["classification"] if entity else "UNKNOWN"
            component_type = _screen_type(classification, sheet)
            confidence = 0.9 if component_type in {"DATA_GRID", "DASHBOARD", "SETTINGS"} else 0.78
            item = self._base("SCREEN", sheet["sheet_name"], confidence,
                              {"source_type": "SHEET", "source_id": sheet["sheet_id"], "sheet": sheet["sheet_name"]})
            item.update({"screen_type": component_type,
                         "interaction_mode": "SPREADSHEET_GRID" if component_type == "DATA_GRID" else component_type,
                         "entity_id": entity["component_id"] if entity else None,
                         "components": ["SEARCH", "AUDIT_HISTORY"] + (["CHART", "KPI", "FILTER"] if component_type == "DASHBOARD" else [])})
            result.append(item)
        return result

    def _workflows(self, sheets, objects):
        signals = [sheet for sheet in sheets if any(token in slug(sheet["sheet_name"]) for token in ("approval", "review", "submit"))]
        vba = [item for item in objects if item["object_type"] == "VBA_PROJECT"]
        if not signals and not vba:
            return []
        source = signals[0] if signals else {"sheet_id": None, "sheet_name": "Workbook automation"}
        item = self._base("WORKFLOW", f"{source['sheet_name']} Workflow", 0.76,
                          {"source_type": "WORKFLOW_SIGNAL", "source_id": source.get("sheet_id") or vba[0]["inventory_id"],
                           "sheet": source.get("sheet_name")})
        item.update({"states": ["DRAFT", "SUBMITTED", "UNDER_REVIEW", "APPROVED", "REJECTED"],
                     "transitions": [{"from": "DRAFT", "to": "SUBMITTED", "actor": "EDITOR"},
                                     {"from": "SUBMITTED", "to": "UNDER_REVIEW", "actor": "REVIEWER"},
                                     {"from": "UNDER_REVIEW", "to": "APPROVED", "actor": "APPROVER"},
                                     {"from": "UNDER_REVIEW", "to": "REJECTED", "actor": "APPROVER"}],
                     "audit_events": ["SUBMITTED", "APPROVED", "REJECTED"]})
        return [item]

    def _integrations(self):
        result = []
        rows = self._rows("SELECT * FROM EUC_CONNECTIONS WHERE ANALYSIS_ID=? ORDER BY CONNECTION_ID", self.analysis_id)
        links = self._rows("SELECT * FROM EUC_EXTERNAL_LINKS WHERE ANALYSIS_ID=? ORDER BY LINK_ID", self.analysis_id)
        for source in [*rows, *links]:
            source_id = source.get("connection_id") or source.get("link_id")
            name = source.get("connection_name") or source.get("source_euc_reference") or "External source"
            item = self._base("INTEGRATION", name, 0.72,
                              {"source_type": "CONNECTION", "source_id": source_id})
            item.update({"integration_type": "DATABASE" if source.get("database_name") else "FILE",
                         "mode": "READ", "required_secret_names": [f"{slug(name).upper()}_CONNECTION"],
                         "production_connection_enabled": False})
            result.append(item)
        return result

    def _controls(self):
        result = []
        for source in self.blueprint.get("control_preservation", []):
            confidence = 0.95 if source.get("preservation_status") in {"PRESERVED", "ENHANCED"} else 0.75
            item = self._base("CONTROL", source.get("target_control", "Application control"), confidence,
                              {"source_type": "CONTROL_MAPPING", "source_id": source.get("control_mapping_id") or source.get("source_control_code")})
            item.update({"source_control": source.get("source_control_name"),
                         "preservation_status": source.get("preservation_status"),
                         "test_required": bool(source.get("test_required", True)), "audit_required": True})
            result.append(item)
        return result

    def _roles(self):
        result = []
        permissions = {
            "ADMIN": ["READ", "WRITE", "APPROVE", "CONFIGURE"],
            "EDITOR": ["READ", "WRITE", "SUBMIT"],
            "REVIEWER": ["READ", "REVIEW"],
            "APPROVER": ["READ", "APPROVE"],
            "VIEWER": ["READ"],
        }
        for name, values in permissions.items():
            item = self._base("ROLE", name, 0.85, {"source_type": "CONTROL_PROFILE", "source_id": name})
            item["permissions"] = values
            result.append(item)
        return result

    def _jobs(self, integrations, calculations):
        if not integrations or not calculations:
            return []
        item = self._base("JOB", "Refresh and recalculate", 0.72,
                          {"source_type": "INTEGRATION_PIPELINE", "source_id": integrations[0]["component_id"]})
        item.update({"schedule": None, "steps": ["FETCH_SOURCE", "RUN_CALCULATIONS", "PERSIST_VERSION"],
                     "enabled": False})
        return [item]

    def _tests(self):
        result = []
        rows = self._rows("SELECT * FROM EUC_VALIDATION_PLANS WHERE MIGRATION_RUN_ID=? ORDER BY VALIDATION_ID",
                          self.migration["MIGRATION_RUN_ID"])
        for source in rows:
            item = self._base("TEST", source["validation_type"].replace("_", " ").title(), 0.96,
                              {"source_type": "VALIDATION_PLAN", "source_id": source["validation_id"]},
                              source_unit_id=source.get("unit_id"))
            item.update({"test_type": source["validation_type"], "comparison_method": source["comparison_method"],
                         "acceptance_criteria": source["acceptance_criteria"],
                         "historical_replay_count": source["historical_replay_count"]})
            result.append(item)
        return result

    def _coverage(self, components):
        units = self.blueprint.get("units", [])
        unsupported = {unit.get("unit_id") for unit in units if unit.get("mode") == "UNSUPPORTED"}
        mapped_ids = {component.get("source_unit_id") for component in components if component.get("source_unit_id")}
        mapped_count = sum(1 for unit in units if unit.get("unit_id") not in unsupported or unit.get("unit_id") in mapped_ids)
        raw = 100.0 if not units else 100 * mapped_count / len(units)
        total_weight = sum(float(unit.get("weight", 1)) for unit in units) or 1
        mapped_weight = sum(float(unit.get("weight", 1)) for unit in units if unit.get("unit_id") not in unsupported or unit.get("unit_id") in mapped_ids)
        critical = 100 * mapped_weight / total_weight
        review = sum(component["review_state"] == "REVIEW_REQUIRED" for component in components)
        unsupported_components = sum(component.get("unsupported", False) for component in components)
        return {"source_components": len(units), "mapped": mapped_count,
                "manual_review": review, "unsupported": unsupported_components,
                "raw_coverage": round(raw, 2), "criticality_weighted_coverage": round(critical, 2)}

    def _lineage(self, components):
        result = []
        for component in components:
            source = component["provenance"]
            source_id = source.get("source_id")
            if not source_id:
                continue
            result.append({"lineage_id": stable_id("LIN", source_id, component["component_id"]),
                           "source_type": source.get("source_type", "UNKNOWN"), "source_id": source_id,
                           "source_location": source.get("sheet") or source.get("source_range"),
                           "target_type": component["component_type"], "target_id": component["component_id"],
                           "target_path": component.get("path") or component.get("entity_key"),
                           "relationship_type": "GENERATED_FROM", "confidence": component["confidence"]})
        return result

    def _object_rows(self):
        result = self._rows("SELECT * FROM EUC_OBJECT_INVENTORY WHERE ANALYSIS_ID=? ORDER BY OBJECT_TYPE,OBJECT_NAME", self.analysis_id)
        for item in result:
            item["details"] = _json(item.pop("details_json"), {})
        return result

    def _repository_headers(self, sheet_id: str, limit: int) -> list[str]:
        row = self.conn.execute(
            """SELECT B.BRANCH_ID FROM WORKBOOK_REPOSITORIES R JOIN BRANCHES B ON B.BRANCH_ID=R.DEFAULT_BRANCH_ID
               WHERE R.REPOSITORY_ID=?""", (self.asset["REPOSITORY_ID"],)
        ).fetchone()
        if not row:
            return []
        return [item[0] for item in self.conn.execute(
            "SELECT COLUMN_NAME FROM SHEET_COLUMNS WHERE BRANCH_ID=? AND SHEET_ID=? ORDER BY COLUMN_POSITION LIMIT ?",
            (row[0], sheet_id, min(limit, 200)),
        ).fetchall()]

    def _column_samples(self, sheet_id: str, header: str) -> list[Any]:
        # Stage 1 keeps canonical branch state behind repository-specific tables.
        # Field naming and workbook metadata remain deterministic without coupling AIR
        # inference to those physical storage tables.
        return []

    def _rows(self, sql: str, *params) -> list[dict]:
        return [{key.lower(): row[key] for key in row.keys()} for row in self.conn.execute(sql, params)]
