"""Structural AIR validation and evidence-derived migration confidence."""

from __future__ import annotations


class AIRValidator:
    def validate(self, air: dict, critical_blockers: int = 0) -> dict:
        errors, warnings = [], []
        component_ids = {
            item["component_id"] for key, values in air.items()
            if isinstance(values, list) for item in values
            if isinstance(item, dict) and item.get("component_id")
        }
        entity_ids = {item["component_id"] for item in air.get("entities", [])}
        role_names = {item["name"] for item in air.get("roles", [])}
        for relation in air.get("relationships", []):
            if relation.get("source_entity_id") not in entity_ids or relation.get("target_entity_id") not in entity_ids:
                errors.append(self._issue("RELATIONSHIP_TARGET_MISSING", relation["component_id"], "Relationship endpoint does not exist."))
        for endpoint in air.get("apis", []):
            if endpoint.get("entity_id") and endpoint["entity_id"] not in entity_ids:
                errors.append(self._issue("API_ENTITY_MISSING", endpoint["component_id"], "API entity does not exist."))
        for workflow in air.get("workflows", []):
            states = set(workflow.get("states", []))
            for transition in workflow.get("transitions", []):
                if transition.get("from") not in states or transition.get("to") not in states:
                    errors.append(self._issue("WORKFLOW_STATE_MISSING", workflow["component_id"], "Workflow transition references an unknown state."))
                if transition.get("actor") not in role_names:
                    errors.append(self._issue("WORKFLOW_ROLE_MISSING", workflow["component_id"], "Workflow transition references an unknown role."))
        for component in [item for values in air.values() if isinstance(values, list) for item in values if isinstance(item, dict)]:
            if component.get("component_id") and not component.get("provenance", {}).get("source_id"):
                warnings.append(self._issue("PROVENANCE_INCOMPLETE", component["component_id"], "Source identity is incomplete."))
        if critical_blockers:
            errors.append(self._issue("CRITICAL_MIGRATION_BLOCKER", None, f"{critical_blockers} critical Stage 2.4 blocker(s) remain."))
        coverage = air.get("coverage", {})
        if coverage.get("criticality_weighted_coverage", 0) < 95:
            warnings.append(self._issue("CRITICAL_COVERAGE_LOW", None, "Criticality-weighted source coverage is below 95%."))
        return {"valid": not errors, "errors": errors, "warnings": warnings,
                "checks": {"component_ids_unique": len(component_ids) == sum(1 for values in air.values() if isinstance(values, list) for item in values if isinstance(item, dict) and item.get("component_id")),
                           "relationships_resolved": not any(item["code"] == "RELATIONSHIP_TARGET_MISSING" for item in errors),
                           "workflows_valid": not any(item["code"].startswith("WORKFLOW_") for item in errors),
                           "critical_blockers_clear": critical_blockers == 0}}

    def confidence(self, air: dict, validation: dict) -> float:
        coverage = air.get("coverage", {})
        raw = float(coverage.get("raw_coverage", 0))
        critical = float(coverage.get("criticality_weighted_coverage", 0))
        components = [item for values in air.values() if isinstance(values, list) for item in values if isinstance(item, dict) and item.get("confidence") is not None]
        inference = 100 * sum(float(item["confidence"]) for item in components) / max(1, len(components))
        validation_score = 100 if validation["valid"] else max(0, 100 - len(validation["errors"]) * 20)
        review_penalty = min(20, coverage.get("manual_review", 0) * 0.35)
        return round(max(0, min(100, raw * 0.25 + critical * 0.35 + inference * 0.2 + validation_score * 0.2 - review_penalty)), 2)

    @staticmethod
    def _issue(code, component_id, message):
        return {"code": code, "component_id": component_id, "message": message}
