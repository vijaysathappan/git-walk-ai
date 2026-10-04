"""Build the bounded Stage 2.4 feature set from Stage 2.1-2.3 evidence."""

from __future__ import annotations

import json

from ....services.semantic_ledger_service import ledger_for_connection


class MigrationFeatureBuilder:
    def __init__(self, conn, asset, inventory_run, dependency_run, intelligence_run):
        self.conn = conn
        self.asset = asset
        self.inventory = inventory_run
        self.dependency = dependency_run
        self.intelligence = intelligence_run

    def build(self) -> dict:
        analysis_id = self.inventory["ANALYSIS_ID"]
        dependency_id = self.dependency["DEPENDENCY_RUN_ID"]
        intelligence_id = self.intelligence["INTELLIGENCE_RUN_ID"]
        ledger = ledger_for_connection(self.conn)
        stage23 = ledger.objects.get(self.conn, self.intelligence["FEATURE_MANIFEST_HASH"])
        return {
            "asset": {key.lower(): self.asset[key] for key in self.asset.keys()},
            "inventory": json.loads(self.inventory["SUMMARY_JSON"] or "{}"),
            "dependency": json.loads(self.dependency["METRICS_JSON"] or "{}"),
            "intelligence": {
                "complexity": self.intelligence["COMPLEXITY_SCORE"],
                "inherent_risk": self.intelligence["INHERENT_RISK_SCORE"],
                "control_strength": self.intelligence["CONTROL_SCORE"],
                "residual_risk": self.intelligence["RESIDUAL_RISK_SCORE"],
                "summary": json.loads(self.intelligence["SUMMARY_JSON"] or "{}"),
            },
            "stage23_features": stage23,
            "sheets": self._rows("SELECT * FROM EUC_SHEET_INVENTORY WHERE ANALYSIS_ID=? ORDER BY SHEET_POSITION", analysis_id),
            "objects": self._json_rows("SELECT * FROM EUC_OBJECT_INVENTORY WHERE ANALYSIS_ID=? ORDER BY OBJECT_TYPE,OBJECT_NAME", analysis_id, "DETAILS_JSON", "details"),
            "patterns": self._pattern_rows(analysis_id, dependency_id),
            "external_links": self._rows("SELECT * FROM EUC_EXTERNAL_LINKS WHERE ANALYSIS_ID=?", analysis_id),
            "connections": self._json_rows("SELECT * FROM EUC_CONNECTIONS WHERE ANALYSIS_ID=?", analysis_id, "DETAILS_JSON", "details"),
            "domains": self._domains(dependency_id),
            "sheet_dependencies": self._rows("SELECT * FROM EUC_SHEET_DEPENDENCIES WHERE DEPENDENCY_RUN_ID=?", dependency_id),
            "controls": self._rows("SELECT * FROM EUC_CONTROL_INVENTORY WHERE INTELLIGENCE_RUN_ID=? ORDER BY CONTROL_CODE", intelligence_id),
            "findings": self._findings(intelligence_id),
            "history": self._history(),
            "macro_governance": self._macro_governance(),
        }

    def _macro_governance(self) -> dict:
        """Whether this repository's VBA is real-migration-blocking or
        already Virtual Run-eligible. Migration blocker/strategy scoring
        used to see any VBA presence as permanently opaque (VERY_HIGH/
        MANUAL_REENGINEERING) even for a workbook whose only macro is a
        runnable, sandboxed per-row calculation with no external effects."""
        rows = self._rows(
            "SELECT STATIC_RISK, COUNT(*) AS N FROM MACRO_DEFINITIONS WHERE REPOSITORY_ID=? GROUP BY STATIC_RISK",
            self.asset["REPOSITORY_ID"],
        )
        by_risk = {row["static_risk"]: row["n"] for row in rows}
        total = sum(by_risk.values())
        return {
            "extracted": total > 0,
            "total_macros": total,
            "runnable": by_risk.get("RUNNABLE", 0),
            "blocked_external": by_risk.get("BLOCKED_EXTERNAL", 0),
            "blocked_unsupported": by_risk.get("BLOCKED_UNSUPPORTED", 0),
            "all_runnable": total > 0 and by_risk.get("RUNNABLE", 0) == total,
        }

    def _pattern_rows(self, analysis_id: str, dependency_id: str) -> list[dict]:
        return self._rows(
            """SELECT P.*,A.AST_DEPTH,A.REFERENCE_COUNT,A.DYNAMIC_REFERENCE_COUNT
               FROM EUC_FORMULA_PATTERNS P LEFT JOIN EUC_FORMULA_ASTS A
               ON A.DEPENDENCY_RUN_ID=? AND A.PATTERN_ID=P.PATTERN_ID
               WHERE P.ANALYSIS_ID=? ORDER BY P.OCCURRENCE_COUNT DESC""",
            dependency_id, analysis_id,
        )

    def _domains(self, dependency_id: str) -> list[dict]:
        return self._rows(
            """SELECT COMPONENT_ID,COUNT(*) AS NODE_COUNT,
                      SUM(CASE WHEN NODE_ROLE='SOURCE' THEN 1 ELSE 0 END) AS SOURCE_COUNT,
                      SUM(CASE WHEN NODE_ROLE='OUTPUT' THEN 1 ELSE 0 END) AS OUTPUT_COUNT,
                      COALESCE(AVG(TECHNICAL_CRITICALITY),0) AS AVG_CRITICALITY,
                      COALESCE(MAX(TECHNICAL_CRITICALITY),0) AS MAX_CRITICALITY,
                      COUNT(DISTINCT SHEET_ID) AS SHEET_COUNT
               FROM EUC_DEPENDENCY_NODES WHERE DEPENDENCY_RUN_ID=? AND COMPONENT_ID IS NOT NULL
               GROUP BY COMPONENT_ID ORDER BY MAX_CRITICALITY DESC,COMPONENT_ID""", dependency_id,
        )

    def _findings(self, intelligence_id: str) -> list[dict]:
        return self._rows(
            """SELECT F.FINDING_ID,F.RULE_ID,F.CATEGORY,F.TITLE,F.STATUS,
                      O.SEVERITY,O.CONFIDENCE,O.NODE_ID,O.SHEET_ID,O.CELL_ADDRESS,
                      O.DEPENDENCY_IMPACT_JSON
               FROM EUC_FINDINGS F JOIN EUC_FINDING_OCCURRENCES O ON O.FINDING_ID=F.FINDING_ID
               WHERE O.INTELLIGENCE_RUN_ID=?""", intelligence_id,
        )

    def _history(self) -> dict:
        repository_id = self.asset["REPOSITORY_ID"]
        row = self.conn.execute(
            """SELECT COUNT(*) AS COMMITS,COUNT(DISTINCT AUTHOR_USER_ID) AS CONTRIBUTORS,
                      COALESCE(SUM(CHANGE_COUNT),0) AS CHANGES,
                      SUM(CASE WHEN REVERTS_COMMIT_ID IS NOT NULL THEN 1 ELSE 0 END) AS REVERTS
               FROM COMMITS WHERE REPOSITORY_ID=?""", (repository_id,),
        ).fetchone()
        commits = self._rows(
            """SELECT COMMIT_ID,CREATED_AT,CHANGE_COUNT FROM COMMITS WHERE REPOSITORY_ID=?
               ORDER BY CREATED_AT DESC LIMIT 200""", repository_id,
        )
        return {**{key.lower(): row[key] or 0 for key in row.keys()}, "recent_commits": commits}

    def _json_rows(self, sql: str, *params_and_names) -> list[dict]:
        *params, json_column, output_name = params_and_names
        rows = self._rows(sql, *params)
        for row in rows:
            row[output_name] = json.loads(row.pop(json_column.lower()) or "{}")
        return rows

    def _rows(self, sql: str, *params) -> list[dict]:
        return [{key.lower(): row[key] for key in row.keys()} for row in self.conn.execute(sql, params)]
