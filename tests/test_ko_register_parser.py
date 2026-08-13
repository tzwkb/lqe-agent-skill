from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from target_languages.ko.register import (
    KoreanRegisterPolicyError,
    evaluate,
    observe,
    resolve,
    validate_policy,
)


class KoreanRegisterParserTests(unittest.TestCase):
    def test_observes_required_ending_families(self):
        cases = {
            "지금 확인해!": ("hae", "plain"),
            "돌아가요!": ("haeyo", "polite"),
            "따라오십시오.": ("hapsyo", "formal_polite"),
            "지금 갑니다.": ("hapsyo", "formal_polite"),
            "당장 돌아가라!": ("haera", "plain"),
        }

        for target, expected in cases.items():
            with self.subTest(target=target):
                result = observe(target)
                self.assertEqual(result["status"], "observed")
                self.assertEqual((result["family"], result["politeness"]), expected)
                self.assertEqual(result["confidence"], "high")

    def test_single_outer_quote_is_safe_but_nested_or_embedded_quote_abstains(self):
        outer = observe("“지금 확인해!”")
        nested = observe("“그가 ‘확인해’라고 말했다.”")
        embedded = observe("그가 ‘확인해’라고 말했다.")

        self.assertEqual(outer["status"], "observed")
        self.assertEqual(outer["family"], "hae")
        self.assertEqual(nested["status"], "inconclusive")
        self.assertIn("nested_or_multiple_quotes", nested["reason_codes"])
        self.assertEqual(embedded["status"], "inconclusive")
        self.assertIn("embedded_quote", embedded["reason_codes"])

    def test_mixed_endings_and_markup_abstain(self):
        mixed = observe("확인해. 이제 돌아가요.")
        tagged = observe("<color=red>확인해!</color>")
        variable = observe("{player}, 확인해!")
        stage_direction = observe("[웃음] 확인해!")

        self.assertEqual(mixed["status"], "inconclusive")
        self.assertIn("mixed_ending_families", mixed["reason_codes"])
        self.assertEqual(tagged["status"], "inconclusive")
        self.assertEqual(variable["status"], "inconclusive")
        self.assertEqual(stage_direction["status"], "inconclusive")
        self.assertIn("markup_interference", tagged["reason_codes"])

    def test_unrecognized_nominal_text_does_not_get_a_fake_register(self):
        result = observe("필요")

        self.assertEqual(result["status"], "inconclusive")
        self.assertIn("unrecognized_or_mixed_syntax", result["reason_codes"])

    def test_validate_policy_normalizes_and_rejects_contradictions(self):
        result = validate_policy(
            {
                "politeness": "plain",
                "ending_families": ["hae", "haera"],
                "forbidden_families": ["haeyo", "hapsyo"],
            }
        )

        self.assertEqual(result["politeness"], ["plain"])
        with self.assertRaisesRegex(KoreanRegisterPolicyError, "overlap"):
            validate_policy(
                {"ending_families": ["hae"], "forbidden_families": ["hae"]}
            )
        with self.assertRaisesRegex(KoreanRegisterPolicyError, "conflict"):
            validate_policy(
                {"politeness": ["plain"], "ending_families": ["haeyo"]}
            )

    def test_evaluate_reports_match_mismatch_or_inconclusive_without_edit(self):
        expected = {
            "politeness": ["plain"],
            "ending_families": ["hae", "haera"],
            "forbidden_families": ["haeyo", "hapsyo"],
        }
        match = evaluate(expected, observe("지금 확인해!"))
        mismatch = evaluate(expected, observe("돌아가요!"))
        inconclusive = evaluate(expected, observe("필요"))

        self.assertEqual(match["status"], "match")
        self.assertEqual(mismatch["status"], "mismatch")
        self.assertEqual(inconclusive["status"], "inconclusive")
        self.assertNotIn("edit", match)
        self.assertNotIn("edit", mismatch)

    def test_provider_resolve_only_combines_equal_selected_expectations(self):
        expected = {"politeness": ["plain"]}
        same = resolve({}, [{"expect": expected}, {"expect": expected}])
        conflict = resolve(
            {},
            [
                {"expect": {"politeness": ["plain"]}},
                {"expect": {"politeness": ["polite"]}},
            ],
        )

        self.assertEqual(same["status"], "resolved")
        self.assertEqual(conflict["status"], "conflict")


if __name__ == "__main__":
    unittest.main()
