import unittest

from app.ai.response_parsing import extract_recommendation_and_summary

_ALLOWED = {"APPROVE", "HOLD_FOR_REVIEW"}


class ResponseParsingTests(unittest.TestCase):
    def test_normal_structured_result_uses_the_overall_actions_rationale(self):
        result = {
            "answer": "See recommended_actions.",
            "recommended_actions": [
                {"title": "Author summary", "rationale": "Looks safe to merge.", "action_type": "APPROVE", "risk_level": "LOW"},
            ],
        }
        overall, summary = extract_recommendation_and_summary(result, _ALLOWED)
        self.assertEqual(summary, "Looks safe to merge.")
        self.assertEqual(overall["action_type"], "APPROVE")

    def test_fallback_raw_json_answer_recovers_the_nested_rationale_instead_of_showing_raw_json(self):
        # This is the gateway's own safe_fallback() shape: recommended_actions
        # is empty and the model's whole (schema-shaped but strictly-invalid)
        # response landed in `answer` as a raw string.
        result = {
            "answer": (
                '{ "answer": "The risk-scoring engine found no issues.", '
                '"recommended_actions": [{"title": "", "rationale": '
                '"All risk components show zero or negligible impact.", '
                '"action_type": "APPROVE", "risk_level": "LOW"}], '
                '"confidence": 0.96, "warnings": [] }'
            ),
            "recommended_actions": [],
            "insufficient_evidence": True,
            "warnings": ["The free model returned an unstructured answer."],
        }
        overall, summary = extract_recommendation_and_summary(result, _ALLOWED)
        self.assertEqual(summary, "All risk components show zero or negligible impact.")
        self.assertNotIn("{", summary, "a user must never see a raw JSON blob as the summary")
        self.assertEqual(overall["action_type"], "APPROVE")

    def test_fallback_plain_prose_answer_is_returned_as_is(self):
        result = {"answer": "This change looks fine to merge.", "recommended_actions": []}
        overall, summary = extract_recommendation_and_summary(result, _ALLOWED)
        self.assertEqual(summary, "This change looks fine to merge.")
        self.assertIsNone(overall)

    def test_severely_malformed_answer_never_crashes_and_never_shows_full_raw_json(self):
        # Nothing usably quoted for either field — the regex recovery can't
        # find a real "answer"/"rationale" value, so this correctly falls
        # all the way back to the raw text rather than crashing.
        result = {"answer": '{ this is not json at all [[[', "recommended_actions": []}
        overall, summary = extract_recommendation_and_summary(result, _ALLOWED)
        self.assertEqual(summary, '{ this is not json at all [[[')
        self.assertIsNone(overall)

    def test_truncated_json_missing_closing_brace_still_recovers_via_regex(self):
        # The exact real-world failure mode reported: a response that is a
        # nearly-complete JSON object, with a genuinely well-formed quoted
        # "rationale" field inside it, but the overall text is truncated or
        # has a stray duplicated brace so json.loads() rejects it outright.
        result = {
            "answer": (
                '{ { "answer": "The baseline risk assessment is low because there are no merge conflicts.", '
                '"evidence": [ {"type": "risk_component", "id": "CONFLICTS"} ], "confidence": 0.96, '
                '"insufficient_evidence": false, "recommended_actions": [ { "title": "Approve merge request", '
                '"rationale": "All risk dimensions except change volume show zero impact. The change volume '
                'contributed a medium score (56.9/100) but with a low weight (0.06).", "action_type": "APPROVE"'
            ),
            "recommended_actions": [],
        }
        overall, summary = extract_recommendation_and_summary(result, _ALLOWED)
        self.assertNotIn("{", summary, "a genuinely truncated JSON response must still never be shown raw")
        self.assertIn("All risk dimensions except change volume show zero impact.", summary)

    def test_missing_answer_and_actions_returns_empty_summary_without_raising(self):
        overall, summary = extract_recommendation_and_summary({}, _ALLOWED)
        self.assertEqual(summary, "")
        self.assertIsNone(overall)


if __name__ == "__main__":
    unittest.main()
