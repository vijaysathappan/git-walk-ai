import unittest

from app.merge_risk import baseline_recommendation, deterministic_risk_level, evaluate_merge_risk


def _request(changes=None, conflicts=None, total=None):
    changes = changes or []
    return {
        "changes": changes,
        "conflicts": conflicts or [],
        "change_summary": {"total": total if total is not None else len(changes)},
    }


class MergeRiskEngineTests(unittest.TestCase):
    def test_trivial_single_value_change_scores_low(self):
        request = _request(changes=[
            {"operation_type": "CELL_VALUE_UPDATE", "sheet_id": "S1", "row_id": "R1", "column_id": "C1",
             "old_value": "KKR", "new_value": "SRH"},
        ])

        result = evaluate_merge_risk(request, field_investigations=[])

        self.assertLess(result.score, 40)
        self.assertEqual("LOW", deterministic_risk_level(result.score))
        self.assertEqual("APPROVE", baseline_recommendation("LOW", open_conflict_count=0))

    def test_open_conflicts_force_at_least_hold_for_review(self):
        request = _request(conflicts=[{"status": "OPEN", "conflict_type": "CELL_VALUE_CONFLICT"}])

        result = evaluate_merge_risk(request, field_investigations=[])
        risk_level = deterministic_risk_level(result.score)

        self.assertEqual("HOLD_FOR_REVIEW", baseline_recommendation(risk_level, open_conflict_count=1))

    def test_destructive_conflicts_score_higher_than_cosmetic_conflicts(self):
        cosmetic = _request(conflicts=[{"status": "OPEN", "conflict_type": "CELL_FORMAT_CONFLICT"}])
        destructive = _request(conflicts=[{"status": "OPEN", "conflict_type": "STRUCTURAL_CONFLICT"}])

        cosmetic_result = evaluate_merge_risk(cosmetic, field_investigations=[])
        destructive_result = evaluate_merge_risk(destructive, field_investigations=[])

        self.assertGreater(destructive_result.score, cosmetic_result.score)

    def test_formula_changes_raise_score_over_plain_value_changes(self):
        value_only = _request(changes=[
            {"operation_type": "CELL_VALUE_UPDATE", "sheet_id": "S1", "row_id": f"R{i}", "column_id": "C1",
             "old_value": "10", "new_value": "20"}
            for i in range(3)
        ])
        with_formulas = _request(changes=[
            {"operation_type": "CELL_FORMULA_UPDATE", "sheet_id": "S1", "row_id": f"R{i}", "column_id": "C1",
             "old_formula": "=A1+1", "new_formula": "=A1+2"}
            for i in range(3)
        ])

        value_result = evaluate_merge_risk(value_only, field_investigations=[])
        formula_result = evaluate_merge_risk(with_formulas, field_investigations=[])

        self.assertGreater(formula_result.score, value_result.score)

    def test_destructive_structural_changes_raise_score_over_inserts(self):
        inserts = _request(changes=[
            {"operation_type": "ROW_INSERT", "sheet_id": "S1", "row_id": f"R{i}"} for i in range(3)
        ])
        deletes = _request(changes=[
            {"operation_type": "ROW_DELETE", "sheet_id": "S1", "row_id": f"R{i}"} for i in range(3)
        ])

        insert_result = evaluate_merge_risk(inserts, field_investigations=[])
        delete_result = evaluate_merge_risk(deletes, field_investigations=[])

        self.assertGreater(delete_result.score, insert_result.score)

    def test_sign_flip_is_detected_as_a_value_anomaly(self):
        no_flip = _request(changes=[
            {"operation_type": "CELL_VALUE_UPDATE", "sheet_id": "S1", "row_id": "R1", "column_id": "C1",
             "old_value": "50", "new_value": "60"},
        ])
        flip = _request(changes=[
            {"operation_type": "CELL_VALUE_UPDATE", "sheet_id": "S1", "row_id": "R1", "column_id": "C1",
             "old_value": "50", "new_value": "-50"},
        ])

        no_flip_result = evaluate_merge_risk(no_flip, field_investigations=[])
        flip_result = evaluate_merge_risk(flip, field_investigations=[])

        flip_component = next(c for c in flip_result.components if c.dimension == "VALUE_ANOMALIES")
        no_flip_component = next(c for c in no_flip_result.components if c.dimension == "VALUE_ANOMALIES")
        self.assertGreater(flip_component.raw_score, no_flip_component.raw_score)
        self.assertEqual(1, flip_component.evidence["sign_flips"])

    def test_heavily_edited_cleared_field_scores_higher_than_freshly_cleared_field(self):
        rarely_edited = [{"column_name": "NOTES", "prior_edit_count": 1}]
        heavily_edited = [{"column_name": "STATUS", "prior_edit_count": 6}]
        request = _request()

        rarely_result = evaluate_merge_risk(request, field_investigations=rarely_edited)
        heavily_result = evaluate_merge_risk(request, field_investigations=heavily_edited)

        self.assertGreater(heavily_result.score, rarely_result.score)

    def test_a_maximally_risky_merge_reaches_high_or_critical(self):
        changes = (
            [{"operation_type": "CELL_FORMULA_UPDATE", "sheet_id": "S1", "row_id": f"R{i}", "column_id": "C1",
              "old_formula": "=A1", "new_formula": "=A2"} for i in range(6)]
            + [{"operation_type": "ROW_DELETE", "sheet_id": "S1", "row_id": f"R{i}"} for i in range(4)]
            + [{"operation_type": "CELL_VALUE_UPDATE", "sheet_id": "S1", "row_id": f"R{i}", "column_id": "C2",
                "old_value": "100", "new_value": "-100"} for i in range(3)]
        )
        conflicts = [
            {"status": "OPEN", "conflict_type": "FORMULA_CONFLICT"},
            {"status": "OPEN", "conflict_type": "STRUCTURAL_CONFLICT"},
        ]
        request = _request(changes=changes, conflicts=conflicts, total=len(changes))
        field_investigations = [{"column_name": "OWNER", "prior_edit_count": 8}]

        result = evaluate_merge_risk(request, field_investigations)
        risk_level = deterministic_risk_level(result.score)

        self.assertIn(risk_level, {"HIGH", "CRITICAL"})
        self.assertEqual("HOLD_FOR_REVIEW", baseline_recommendation(risk_level, open_conflict_count=2))

    def test_evidence_cites_specific_sign_flip_cells(self):
        request = _request(changes=[
            {"operation_type": "CELL_VALUE_UPDATE", "sheet_id": "SHEET_1", "row_id": "ROW_9", "column_id": "COL_X",
             "old_value": "40", "new_value": "-5"},
        ])

        result = evaluate_merge_risk(request, field_investigations=[])

        anomaly = next(c for c in result.components if c.dimension == "VALUE_ANOMALIES")
        self.assertEqual(["SHEET_1:ROW_9:COL_X"], anomaly.evidence["sign_flip_cells"])


if __name__ == "__main__":
    unittest.main()
