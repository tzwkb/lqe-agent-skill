from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from target_languages.ko.register_v2 import (
    API_VERSION,
    KoreanRegisterPolicyError,
    evaluate,
    observe,
    validate_policy,
)


class KoreanRegisterV2ParserTests(unittest.TestCase):
    def test_validates_narration_dimensions_without_changing_register_fields(self):
        result = validate_policy(
            {
                "person": "second",
                "tense": "past",
                "politeness": "formal_polite",
                "ending_families": "hapsyo",
            }
        )

        self.assertEqual(API_VERSION, 2)
        self.assertEqual(result["person"], ["second"])
        self.assertEqual(result["tense"], ["past"])
        self.assertEqual(result["politeness"], ["formal_polite"])
        self.assertEqual(result["ending_families"], ["hapsyo"])
        with self.assertRaisesRegex(KoreanRegisterPolicyError, "person values"):
            validate_policy({"person": ["omniscient"]})
        with self.assertRaisesRegex(KoreanRegisterPolicyError, "tense values"):
            validate_policy({"tense": ["future"]})

    def test_explicit_subject_and_terminal_tense_can_match_or_mismatch(self):
        expected = {
            "person": ["second"],
            "tense": ["past"],
            "politeness": ["formal_polite"],
            "ending_families": ["hapsyo"],
        }

        matching = evaluate(expected, observe("당신은 문을 열었습니다."))
        mismatch = evaluate(expected, observe("나는 문을 엽니다."))

        self.assertEqual(matching["status"], "match")
        self.assertEqual(mismatch["status"], "mismatch")
        self.assertIn("person_mismatch", mismatch["reason_codes"])
        self.assertIn("tense_mismatch", mismatch["reason_codes"])

    def test_omitted_subject_and_object_pronoun_do_not_claim_person(self):
        expected = {"person": ["second"], "tense": ["past"]}

        omitted = evaluate(expected, observe("문을 열었습니다."))
        object_only = evaluate(expected, observe("당신을 봤습니다."))

        for result in (omitted, object_only):
            self.assertEqual(result["status"], "inconclusive")
            self.assertIn("person_not_observed", result["reason_codes"])
            self.assertNotIn("person_mismatch", result["reason_codes"])

    def test_multiple_clauses_and_future_or_modal_forms_abstain(self):
        expected = {"person": ["second"], "tense": ["past"]}

        multiple = evaluate(
            expected,
            observe("당신은 문을 열었습니다. 당신은 안으로 들어왔습니다."),
        )
        future = evaluate(expected, observe("당신은 문을 열겠습니다."))

        self.assertEqual(multiple["status"], "inconclusive")
        self.assertIn("person_not_observed", multiple["reason_codes"])
        self.assertIn("tense_not_observed", multiple["reason_codes"])
        self.assertEqual(future["status"], "inconclusive")
        self.assertIn("tense_not_observed", future["reason_codes"])
        self.assertNotIn("tense_mismatch", future["reason_codes"])

    def test_inherent_ss_stems_are_present_not_past(self):
        absent = observe("나는 문제가 없습니다.")
        existing = observe("나는 여기에 있습니다.")

        self.assertEqual(absent["tense"], "present")
        self.assertEqual(existing["tense"], "present")

    def test_nominal_fragment_ending_in_single_syllable_is_inconclusive(self):
        result = observe("화분 상자")

        self.assertEqual(result["status"], "inconclusive")
        self.assertIn("unrecognized_or_mixed_syntax", result["reason_codes"])


if __name__ == "__main__":
    unittest.main()
