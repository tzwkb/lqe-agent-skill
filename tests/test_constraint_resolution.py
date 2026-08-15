from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lqe_constraints import (
    ConstraintContractError,
    resolve_constraints,
    validate_context_rules,
)


def rule(
    rule_id,
    expected,
    *,
    when=None,
    issuer="client",
    priority=100,
    status="confirmed",
    valid_from=None,
    valid_until=None,
):
    return {
        "id": rule_id,
        "capability": "context.ui.length",
        "provider": {"id": "core.ui", "api_version": 1},
        "rule_status": status,
        "priority": priority,
        "authority": {"issuer": issuer},
        "valid_from": valid_from,
        "valid_until": valid_until,
        "when": when or {},
        "expect": expected,
        "provenance": {"source_id": "fixture"},
    }


def policy(*rules):
    return {
        "schema": "lqe.context-rules",
        "version": 1,
        "authority_rank": ["client", "language_lead", "pm"],
        "rules": list(rules),
    }


def resolve(value, context):
    return resolve_constraints(
        value,
        context,
        capability="context.ui.length",
        provider={"id": "core.ui", "api_version": 1},
        as_of=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )


class ConstraintResolutionTests(unittest.TestCase):
    def test_only_confirmed_and_current_rules_are_candidates(self):
        value = policy(
            rule("draft", {"max": 1}, status="draft"),
            rule("expired", {"max": 2}, valid_until="2026-08-12"),
            rule(
                "current",
                {"max": 3},
                valid_from="2026-08-13",
                valid_until="2026-08-14",
            ),
        )

        result = resolve(value, {})

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["rule_ids"], ["current"])
        self.assertEqual(result["expected"], {"max": 3})
        self.assertEqual(result["ignored"]["not_confirmed"], ["draft"])
        self.assertEqual(result["ignored"]["outside_validity"], ["expired"])

    def test_specificity_precedes_authority_and_priority(self):
        value = policy(
            rule(
                "broad-client",
                {"max": 10},
                when={"content_type": ["ui_button", "ui_label"]},
                issuer="client",
                priority=999,
            ),
            rule(
                "specific-pm",
                {"max": 8},
                when={"content_type": ["ui_button"], "platform": ["mobile"]},
                issuer="pm",
                priority=1,
            ),
        )

        result = resolve(value, {"content_type": "ui_button", "platform": "mobile"})

        self.assertEqual(result["rule_ids"], ["specific-pm"])
        self.assertEqual(result["expected"], {"max": 8})

    def test_narrower_alternative_list_is_more_specific(self):
        value = policy(
            rule(
                "two-types",
                {"max": 12},
                when={"content_type": ["ui_button", "ui_label"]},
            ),
            rule(
                "one-type",
                {"max": 9},
                when={"content_type": ["ui_button"]},
            ),
        )

        result = resolve(value, {"content_type": "ui_button"})

        self.assertEqual(result["rule_ids"], ["one-type"])

    def test_authority_precedes_priority_at_equal_specificity(self):
        value = policy(
            rule("client", {"max": 8}, issuer="client", priority=1),
            rule("pm", {"max": 20}, issuer="pm", priority=999),
        )

        result = resolve(value, {})

        self.assertEqual(result["rule_ids"], ["client"])

    def test_priority_breaks_equal_authority_tie(self):
        value = policy(
            rule("low", {"max": 8}, priority=10),
            rule("high", {"max": 12}, priority=20),
        )

        result = resolve(value, {})

        self.assertEqual(result["rule_ids"], ["high"])

    def test_equal_rank_conflict_abstains(self):
        result = resolve(
            policy(rule("a", {"max": 8}), rule("b", {"max": 12})),
            {},
        )

        self.assertEqual(result["status"], "conflict")
        self.assertIsNone(result["expected"])
        self.assertEqual(
            result["reason_codes"], ["equal_rank_conflicting_expectations"]
        )

    def test_equal_rank_identical_expectation_can_coexist(self):
        result = resolve(
            policy(rule("a", {"max": 8}), rule("b", {"max": 8})),
            {},
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["rule_ids"], ["a", "b"])

    def test_missing_nested_context_abstains_and_unique_leaf_can_match(self):
        value = policy(
            rule(
                "mobile",
                {"max": 8},
                when={"platform": ["mobile"]},
            )
        )
        missing = resolve(value, {"context": {"core": {"content_type": "ui"}}})
        nested = resolve(
            value,
            {"context": {"extensions": {"ui": {"platform": "mobile"}}}},
        )

        self.assertEqual(missing["status"], "insufficient_context")
        self.assertEqual(missing["reason_codes"], ["insufficient_context"])
        self.assertEqual(nested["status"], "resolved")

    def test_contract_rejects_missing_language_target_and_reversed_validity(self):
        language_rule = rule("language", {"level": "plain"})
        language_rule["capability"] = "language.register"
        with self.assertRaisesRegex(ConstraintContractError, "target_lang"):
            validate_context_rules(policy(language_rule))

        reversed_rule = rule(
            "reversed",
            {"max": 8},
            valid_from="2026-08-14",
            valid_until="2026-08-13",
        )
        with self.assertRaisesRegex(ConstraintContractError, "later"):
            validate_context_rules(policy(reversed_rule))

    def test_contract_rejects_duplicate_ids(self):
        with self.assertRaisesRegex(ConstraintContractError, "duplicate rule id"):
            validate_context_rules(policy(rule("same", {"max": 1}), rule("same", {"max": 1})))


if __name__ == "__main__":
    unittest.main()
