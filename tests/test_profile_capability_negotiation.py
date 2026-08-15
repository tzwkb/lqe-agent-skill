from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lqe_capabilities import (
    CapabilityNegotiationError,
    ProfileContractError,
    normalize_profile,
    resolve_capabilities,
    validate_capability_descriptor,
    validate_capability_resolution,
)


def asset(kind: str, path: str, *, required: bool = True) -> dict:
    return {
        "kind": kind,
        "path": path,
        "required": required,
        "authority": {"issuer": "test", "level": "authoritative"},
        "provenance": {"kind": "test_fixture"},
        "distribution": "internal_only",
        "availability": "included",
    }


def v2_profile(mode: str = "enforce") -> dict:
    return {
        "profile_contract_version": 2,
        "name": "test/zh-en",
        "language_pair": "zh-en",
        "source_lang": "zh",
        "target_lang": "en",
        "wordcount_basis": "source-chars",
        "scoring_policy": {
            "threshold": 98,
            "scorecard_profile": "legacy",
            "severity_scale": "lisa",
            "critical_gate": False,
            "repeat_dedup": True,
        },
        "assets": {
            "checks": asset("checks", "checks.json"),
            "rules": asset("confirmed_rules", "confirmed_rules.md"),
        },
        "context_pipeline": {"mode": mode},
        "capabilities": {
            "context.core@1": {"required": True},
            "source_provenance@1": {"required": True},
        },
    }


def custom_descriptor(capability_id: str = "custom.release_context@1") -> dict:
    return {
        "schema": "lqe.context-capability-descriptor",
        "version": 1,
        "id": capability_id,
        "fields": {
            "platform": {
                "type": "string",
                "columns": ["platform"],
                "normalizer": "trim",
                "affects_review_equivalence": True,
            }
        },
        "module_views": {
            "naturalness": ["platform"],
            "suggestions": ["platform"],
        },
    }


class ProfileCapabilityNegotiationTests(unittest.TestCase):
    def test_legacy_profile_normalizes_to_off_with_only_foundations(self):
        profile = {
            "name": "legacy/zh-en",
            "language_pair": "zh-en",
            "source_lang": "zh",
            "target_lang": "en",
            "style_guide": "sg.md",
            "terminology": "terms.json",
        }

        normalized = normalize_profile(profile)
        resolution = resolve_capabilities(normalized)

        self.assertEqual(normalized["profile_contract_version"], 1)
        self.assertTrue(normalized["legacy_adapter"])
        self.assertEqual(normalized["context_pipeline"], {"mode": "off"})
        self.assertEqual(normalized["module_context_views"], {})
        self.assertEqual(set(normalized["assets"]), {"style_guide", "terminology"})
        self.assertEqual(
            set(resolution["enabled"]),
            {"context.core@1", "source_provenance@1"},
        )
        self.assertEqual(
            resolution["disabled"]["context.dialogue@1"]["reason"],
            "not_declared",
        )
        validate_capability_resolution(resolution)

        historical_normalized = deepcopy(normalized)
        del historical_normalized["module_context_views"]
        self.assertEqual(
            resolve_capabilities(historical_normalized)["context_pipeline_mode"],
            "off",
        )

    def test_v2_requires_foundations_and_core_asset_kinds(self):
        profile = v2_profile()
        del profile["capabilities"]["context.core@1"]
        with self.assertRaisesRegex(ProfileContractError, "context.core@1"):
            normalize_profile(profile)

        profile = v2_profile()
        del profile["assets"]["checks"]
        with self.assertRaisesRegex(ProfileContractError, "checks"):
            normalize_profile(profile)

    def test_unknown_required_fails_and_unknown_optional_is_disabled(self):
        required = v2_profile()
        required["capabilities"]["custom.unknown@1"] = {"required": True}
        with self.assertRaisesRegex(
            CapabilityNegotiationError, "unsupported_capability"
        ):
            resolve_capabilities(required)

        optional = v2_profile()
        optional["capabilities"]["custom.unknown@1"] = {"required": False}
        resolution = resolve_capabilities(optional)
        self.assertEqual(
            resolution["disabled"]["custom.unknown@1"]["reason"],
            "unsupported_capability",
        )

    def test_pipeline_modes_are_bound_into_resolution(self):
        off = v2_profile("off")
        off["capabilities"]["context.ui@1"] = {"required": False}
        shadow = deepcopy(off)
        shadow["context_pipeline"]["mode"] = "shadow"
        enforce = deepcopy(off)
        enforce["context_pipeline"]["mode"] = "enforce"

        off_result = resolve_capabilities(off)
        shadow_result = resolve_capabilities(shadow)
        enforce_result = resolve_capabilities(enforce)

        self.assertEqual(
            off_result["disabled"]["context.ui@1"]["reason"], "pipeline_off"
        )
        self.assertEqual(shadow_result["enabled"]["context.ui@1"]["effect"], "shadow")
        self.assertEqual(enforce_result["enabled"]["context.ui@1"]["effect"], "enforce")
        self.assertEqual(len({off_result["digest"], shadow_result["digest"], enforce_result["digest"]}), 3)

    def test_module_context_views_are_normalized_and_profile_bound(self):
        profile = v2_profile()
        profile["capabilities"]["context.dialogue@1"] = {"required": False}
        profile["module_context_views"] = {
            "suggestions": {
                "capabilities": ["context.dialogue@1", "context.core@1"],
                "dimensions": ["naturalness", "accuracy"],
                "constraint_kinds": [],
                "neighbors": {"after": 1, "before": 2},
                "limits": {"max_runtime_examples": 3},
            }
        }

        normalized = normalize_profile(profile)

        self.assertEqual(
            normalized["module_context_views"],
            {
                "suggestions": {
                    "capabilities": ["context.core@1", "context.dialogue@1"],
                    "dimensions": ["accuracy", "naturalness"],
                    "constraint_kinds": [],
                    "neighbors": {"before": 2, "after": 1},
                    "limits": {"max_runtime_examples": 3},
                }
            },
        )
        changed = deepcopy(profile)
        changed["module_context_views"]["suggestions"]["limits"][
            "max_runtime_examples"
        ] = 4
        self.assertNotEqual(
            normalized["source_profile_digest"],
            normalize_profile(changed)["source_profile_digest"],
        )

        invalid = deepcopy(profile)
        invalid["module_context_views"]["suggestions"]["capabilities"] = [
            "context.core@1",
            "context.ui@1",
        ]
        with self.assertRaisesRegex(ProfileContractError, "undeclared capabilities"):
            normalize_profile(invalid)

        invalid = deepcopy(profile)
        invalid["module_context_views"]["suggestions"]["limits"][
            "max_runtime_examples"
        ] = -1
        with self.assertRaisesRegex(ProfileContractError, "non-negative integer"):
            normalize_profile(invalid)

    def test_language_policy_view_requires_explicit_constraint_kinds(self):
        profile = v2_profile()
        profile["capabilities"]["language_policy.register@1"] = {
            "required": False,
            "provider": "ko.register@1",
        }
        profile["module_context_views"] = {
            "naturalness": {
                "capabilities": [
                    "context.core@1",
                    "language_policy.register@1",
                ],
                "dimensions": ["naturalness"],
            }
        }
        with self.assertRaisesRegex(
            ProfileContractError,
            "does not declare constraint_kinds",
        ):
            normalize_profile(profile)

        declared = deepcopy(profile)
        declared["module_context_views"]["naturalness"][
            "constraint_kinds"
        ] = ["language.register"]
        normalized = normalize_profile(declared)
        self.assertEqual(
            normalized["module_context_views"]["naturalness"][
                "constraint_kinds"
            ],
            ["language.register"],
        )

        audit_only = deepcopy(profile)
        audit_only["module_context_views"]["naturalness"][
            "include_constraints"
        ] = False
        normalized = normalize_profile(audit_only)
        self.assertFalse(
            normalized["module_context_views"]["naturalness"][
                "include_constraints"
            ]
        )

    def test_asset_backed_capability_uses_only_explicit_snapshot_status(self):
        profile = v2_profile()
        profile["assets"]["entities"] = asset(
            "entity_registry", "entities.json", required=False
        )
        profile["capabilities"]["assets.entity_registry@1"] = {
            "required": False,
            "asset": "entities",
        }

        missing = resolve_capabilities(
            profile,
            asset_statuses={"entities": {"status": "missing"}},
        )
        self.assertEqual(
            missing["disabled"]["assets.entity_registry@1"]["reason"],
            "asset_missing",
        )

        present = resolve_capabilities(
            profile,
            asset_statuses={
                "entities": {"status": "present", "sha256": "a" * 64}
            },
        )
        self.assertEqual(
            present["enabled"]["assets.entity_registry@1"]["asset_digest"],
            "a" * 64,
        )

    def test_custom_declarative_descriptor_is_supported_without_code_hook(self):
        profile = v2_profile()
        descriptor = custom_descriptor()
        profile["capability_descriptors"] = {descriptor["id"]: descriptor}
        profile["capabilities"][descriptor["id"]] = {
            "required": True,
            "config": {"columns": {"platform": ["Platform", "平台"]}},
        }

        resolution = resolve_capabilities(profile)
        self.assertIn(descriptor["id"], resolution["enabled"])

        unsafe = custom_descriptor("custom.unsafe@1")
        unsafe["python_path"] = "/tmp/plugin.py"
        with self.assertRaisesRegex(ProfileContractError, "unknown fields"):
            validate_capability_descriptor(unsafe, custom=True)

        unsafe = custom_descriptor("custom.unsafe@1")
        unsafe["comparison_rules"] = {"exec": "do_something"}
        with self.assertRaisesRegex(ProfileContractError, "forbidden executable"):
            validate_capability_descriptor(unsafe, custom=True)

    def test_language_provider_is_target_checked(self):
        profile = v2_profile()
        profile["capabilities"]["language_policy.register@1"] = {
            "required": False,
            "provider": "ko.register@1",
        }
        providers = {
            "ko.register@1": {
                "id": "ko.register",
                "api_version": 1,
                "target_lang": "ko",
            }
        }

        resolution = resolve_capabilities(profile, provider_registry=providers)
        self.assertEqual(
            resolution["disabled"]["language_policy.register@1"]["reason"],
            "provider_target_mismatch",
        )

        required = deepcopy(profile)
        required["capabilities"]["language_policy.register@1"]["required"] = True
        with self.assertRaisesRegex(
            CapabilityNegotiationError, "provider_target_mismatch"
        ):
            resolve_capabilities(required, provider_registry=providers)

    def test_canonical_digest_is_order_independent_and_tampering_is_rejected(self):
        first = v2_profile()
        second = json.loads(json.dumps(first, sort_keys=True))
        first_result = resolve_capabilities(first)
        second_result = resolve_capabilities(second)
        self.assertEqual(first_result["digest"], second_result["digest"])

        tampered = deepcopy(first_result)
        tampered["context_pipeline_mode"] = "shadow"
        with self.assertRaisesRegex(
            CapabilityNegotiationError, "digest mismatch"
        ):
            validate_capability_resolution(tampered)


if __name__ == "__main__":
    unittest.main()
