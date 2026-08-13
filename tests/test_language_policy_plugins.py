from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lqe_language_policies import (
    ProviderNotFoundError,
    ProviderTargetMismatchError,
    evaluate_resolved_constraint,
    evaluate_language_policy,
    load_provider,
    observe,
    provider_for,
    resolve_language_policy,
    trusted_provider_registry,
    validate_policy,
)


PROVIDER = {"id": "ko.register", "api_version": 1}


def context_rules(expect=None):
    return {
        "schema": "lqe.context-rules",
        "version": 1,
        "authority_rank": ["client", "language_lead", "pm"],
        "rules": [
            {
                "id": "register.operator.supervisor",
                "capability": "language.register",
                "provider": PROVIDER,
                "target_lang": "ko",
                "rule_status": "confirmed",
                "priority": 100,
                "authority": {"issuer": "client"},
                "valid_from": None,
                "valid_until": None,
                "when": {
                    "speaker_id": ["operator"],
                    "addressee_ids": ["supervisor"],
                },
                "expect": expect
                or {
                    "politeness": ["plain"],
                    "ending_families": ["hae", "haera"],
                    "forbidden_families": ["haeyo", "hapsyo"],
                },
                "provenance": {"source_id": "confirmed-client-note"},
            }
        ],
    }


CONTEXT = {"speaker_id": "operator", "addressee_ids": ["supervisor"]}


class LanguagePolicyPluginTests(unittest.TestCase):
    def test_registry_is_fixed_and_target_filtered_without_module_paths(self):
        registry = trusted_provider_registry()

        self.assertEqual(set(registry), {"ko.register@1"})
        self.assertNotIn("module_path", registry["ko.register@1"])
        self.assertEqual(trusted_provider_registry(target_lang="en"), {})
        self.assertEqual(trusted_provider_registry(target_lang="th"), {})
        self.assertIsNone(provider_for("en", "language.register"))
        self.assertIsNone(provider_for("th", "language.register"))

    def test_untrusted_path_and_unknown_provider_are_rejected(self):
        with self.assertRaises(ProviderNotFoundError):
            load_provider("/tmp/register.py@1", "ko")
        with self.assertRaises(ProviderNotFoundError):
            load_provider("custom.register@1", "ko")
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            load_provider(
                {"id": "ko.register", "api_version": 1, "path": "/tmp/x.py"},
                "ko",
            )

    def test_provider_is_bound_to_korean(self):
        with self.assertRaisesRegex(ProviderTargetMismatchError, "not en"):
            load_provider(PROVIDER, "en")
        with self.assertRaisesRegex(ProviderTargetMismatchError, "not th"):
            load_provider(PROVIDER, "th")

    def test_provider_exposes_fixed_interface(self):
        module = load_provider(PROVIDER, "ko")
        for name in ("validate_policy", "resolve", "observe", "evaluate"):
            self.assertTrue(callable(getattr(module, name)))

    def test_dispatcher_validates_provider_policy(self):
        normalized = validate_policy(
            PROVIDER,
            "ko",
            {"politeness": "plain", "ending_families": "hae"},
        )

        self.assertEqual(normalized["politeness"], ["plain"])
        self.assertEqual(normalized["ending_families"], ["hae"])

        with self.assertRaisesRegex(ValueError, "unknown fields"):
            resolve_language_policy(
                context_rules({"unsupported": ["value"]}),
                CONTEXT,
                provider=PROVIDER,
                target_lang="ko",
                as_of="2026-08-13",
            )

    def test_integrated_resolution_uses_generic_rule_precedence(self):
        result = resolve_language_policy(
            context_rules(),
            CONTEXT,
            provider=PROVIDER,
            target_lang="ko",
            as_of="2026-08-13T00:00:00+08:00",
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["kind"], "language.register")
        self.assertEqual(result["rule_ids"], ["register.operator.supervisor"])
        self.assertEqual(result["expected"]["politeness"], ["plain"])
        self.assertEqual(len(result["resolution_digest"]), 64)

    def test_integrated_evaluation_matches_plain_and_rejects_polite(self):
        matching = evaluate_language_policy(
            context_rules(),
            CONTEXT,
            "지금 확인해!",
            provider=PROVIDER,
            target_lang="ko",
            as_of="2026-08-13",
        )
        mismatch = evaluate_language_policy(
            context_rules(),
            CONTEXT,
            "돌아가요!",
            provider=PROVIDER,
            target_lang="ko",
            as_of="2026-08-13",
        )

        self.assertEqual(matching["status"], "match")
        self.assertEqual(mismatch["status"], "mismatch")
        self.assertIn(
            "forbidden_ending_family", mismatch["evaluation"]["reason_codes"]
        )

    def test_observe_dispatcher_does_not_claim_a_result_for_ambiguous_text(self):
        result = observe(PROVIDER, "ko", "그는 말했다. ‘확인해!’라고.")

        self.assertEqual(result["status"], "inconclusive")

    def test_bound_constraint_is_re_evaluated_for_each_candidate(self):
        constraint = resolve_language_policy(
            context_rules(),
            CONTEXT,
            provider=PROVIDER,
            target_lang="ko",
            as_of="2026-08-13",
        )

        self.assertEqual(
            evaluate_resolved_constraint(
                constraint, "지금 확인해!"
            )["status"],
            "match",
        )
        self.assertEqual(
            evaluate_resolved_constraint(constraint, "돌아가요!")["status"],
            "mismatch",
        )
        self.assertEqual(
            evaluate_resolved_constraint(
                constraint, "그는 말했다. ‘확인해!’라고."
            )["status"],
            "inconclusive",
        )

    def test_bound_constraint_rejects_digest_and_provider_drift(self):
        constraint = resolve_language_policy(
            context_rules(),
            CONTEXT,
            provider=PROVIDER,
            target_lang="ko",
            as_of="2026-08-13",
        )
        changed_expected = dict(constraint)
        changed_expected["expected"] = {"politeness": ["polite"]}
        with self.assertRaisesRegex(ValueError, "digest is invalid"):
            evaluate_resolved_constraint(changed_expected, "돌아가요!")

        changed_provider = dict(constraint)
        changed_provider["provider"] = dict(constraint["provider"])
        changed_provider["provider"]["module_sha256"] = "0" * 64
        digest_basis = dict(changed_provider)
        digest_basis.pop("resolution_digest")
        from lqe_constraints import canonical_digest
        changed_provider["resolution_digest"] = canonical_digest(digest_basis)
        with self.assertRaisesRegex(ValueError, "stale or untrusted"):
            evaluate_resolved_constraint(changed_provider, "돌아가요!")


if __name__ == "__main__":
    unittest.main()
