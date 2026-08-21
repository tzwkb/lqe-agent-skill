from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lqe_corrections import CheckFormatError, build_segment_result
from lqe_chunk import _canonicalize_forced_severities
from lqe_engine import build_review_policy
import lqe_io


def _issue(evidence):
    return {
        "category": "Spelling",
        "severity": "Minor",
        "comment": "The adverb is misspelled.",
        "needs_confirmation": False,
        "edit": {
            "from": "eror",
            "to": "error",
            "evidence": evidence,
        },
    }


class ReviewEditContractTests(unittest.TestCase):
    def setUp(self):
        self.segment = {
            "id": 0,
            "source": "Fix the error.",
            "target": "Fix the eror.",
            "protected_texts": [],
        }

    def test_invalid_rule_shorthand_is_rejected_before_merge(self):
        with self.assertRaisesRegex(CheckFormatError, "evidence has invalid fields"):
            build_segment_result(
                self.segment,
                [_issue({"type": "grammar_rule", "rule": "ko.standard_spelling"})],
                review_policy=build_review_policy("full"),
            )

    def test_forced_severity_is_canonicalized_before_result_contract(self):
        state = {
            "review_policy": build_review_policy("optimized"),
            "scoring_policy": {"scorecard_profile": "legacy"},
        }
        issue = {
            "category": "Terminology",
            "severity": "Minor",
            "comment": "The confirmed term is not used.",
            "needs_confirmation": True,
            "edit": None,
        }

        canonical = _canonicalize_forced_severities(state, [issue])

        self.assertEqual(canonical[0]["severity"], "Major")
        self.assertEqual(issue["severity"], "Minor")

    def test_report_validation_never_mutates_result_severity(self):
        issue = {
            "category": "Terminology",
            "severity": "Minor",
            "comment": "The confirmed term is not used.",
            "needs_confirmation": True,
            "edit": None,
        }
        results = [{"id": 0, "errors": [issue], "corrected": None}]

        messages = lqe_io._validate_errors(results, {0})

        self.assertTrue(any("non-canonical" in message for message in messages))
        self.assertEqual(issue["severity"], "Minor")

    def test_canonical_rule_evidence_is_accepted(self):
        result = build_segment_result(
            self.segment,
            [
                _issue(
                    {
                        "type": "spelling_rule",
                        "source": "en.standard_spelling",
                        "target": "error",
                    }
                )
            ],
            review_policy=build_review_policy("full"),
        )
        self.assertEqual(result["corrected"], "Fix the error.")


if __name__ == "__main__":
    unittest.main()
