import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lqe_capabilities import (
    builtin_descriptor_registry,
    normalize_profile,
    resolve_capabilities,
    validate_capability_resolution,
)
from lqe_context import descriptor_registry
from lqe_language_policies import trusted_provider_registry
from lqe_project_assets import (
    LEGACY_ASSET_FIELDS,
    asset_statuses,
    inspect_project_assets,
    validate_project_asset_snapshot,
)


PROFILE_EXPECTATIONS = {
    "mhg/zh-ko": {
        "wordcount_basis": "source-chars",
        "mode": "enforce",
        "assets": {
            "style_guide",
            "checks",
            "confirmed_rules",
            "entities",
            "context_rules",
            "project_sources",
            "shadow_readme",
            "review_examples",
        },
        "capabilities": {
            "context.core@1",
            "context.dialogue@1",
            "assets.entity_registry@1",
            "language_policy.register@1",
            "source_provenance@1",
        },
    },
    "nrc/zh-en": {
        "wordcount_basis": "target-words",
        "assets": {"style_guide", "terminology", "checks", "confirmed_rules"},
    },
    "nrc/zh-th": {
        "wordcount_basis": "source-chars",
        "assets": {"style_guide", "terminology", "checks", "confirmed_rules"},
    },
    "wwm/zh-en": {
        "wordcount_basis": "target-words",
        "assets": {"style_guide", "terminology", "checks", "confirmed_rules"},
    },
    "xiuxiuyongzhe/zh-en": {
        "wordcount_basis": "target-words",
        "assets": {"style_guide", "terminology", "checks", "confirmed_rules"},
    },
}
FOUNDATIONS = {"context.core@1", "source_provenance@1"}
PUBLIC_CHECKOUT_PRIVATE_ASSETS = {
    "mhg/zh-ko": {
        "entities",
        "context_rules",
        "project_sources",
        "shadow_readme",
        "review_examples",
    }
}


def profile_path(profile_name: str) -> Path:
    return ROOT / "projects" / profile_name / "profile.json"


def load_profile(profile_name: str) -> tuple[Path, dict]:
    path = profile_path(profile_name)
    return path, json.loads(path.read_text(encoding="utf-8"))


def missing_included_assets(profile_name: str, profile: dict) -> set[str]:
    profile_dir = profile_path(profile_name).parent
    return {
        asset_id
        for asset_id, declaration in profile["assets"].items()
        if declaration["availability"] == "included"
        and not (profile_dir / declaration["path"]).is_file()
    }


def runtime(profile_name: str) -> tuple[dict, dict, dict]:
    path, profile = load_profile(profile_name)
    normalized = normalize_profile(profile)
    inspection = inspect_project_assets(
        normalized,
        profile_dir=path.parent,
        strict_required=True,
    )
    resolution = resolve_capabilities(
        normalized,
        asset_statuses=asset_statuses(inspection["snapshot"]),
        provider_registry=trusted_provider_registry(),
    )
    return normalized, inspection, resolution


class LegacyProfilesV2RuntimeTests(unittest.TestCase):
    def test_inventory_contains_all_migrated_public_profiles(self):
        actual = {
            str(path.parent.relative_to(ROOT / "projects"))
            for path in (ROOT / "projects").glob("*/*/profile.json")
        }

        self.assertTrue(set(PROFILE_EXPECTATIONS) <= actual)

    def test_v2_core_is_explicit_and_legacy_top_level_paths_are_preserved(self):
        for profile_name, expected in PROFILE_EXPECTATIONS.items():
            with self.subTest(profile=profile_name):
                _, raw = load_profile(profile_name)
                normalized = normalize_profile(raw)

                self.assertEqual(raw["profile_contract_version"], 2)
                self.assertEqual(normalized["profile_contract_version"], 2)
                self.assertFalse(normalized["legacy_adapter"])
                self.assertEqual(
                    raw["context_pipeline"],
                    {"mode": expected.get("mode", "off")},
                )
                self.assertEqual(raw["wordcount_basis"], expected["wordcount_basis"])
                self.assertEqual(set(raw["assets"]), expected["assets"])
                self.assertEqual(
                    set(raw["capabilities"]),
                    expected.get("capabilities", FOUNDATIONS),
                )
                self.assertEqual(
                    raw["capabilities"]["context.core@1"]["config"]["identity"]["fallback"],
                    "source_coordinate",
                )
                self.assertEqual(
                    set(raw["scoring_policy"]),
                    {
                        "threshold",
                        "scorecard_profile",
                        "severity_scale",
                        "critical_gate",
                        "repeat_dedup",
                    },
                )
                for legacy_field, asset_kind in LEGACY_ASSET_FIELDS.items():
                    if legacy_field not in raw:
                        self.assertNotIn(asset_kind, raw["assets"])
                        continue
                    declaration = raw["assets"][asset_kind]
                    self.assertEqual(declaration["kind"], asset_kind)
                    self.assertEqual(declaration["path"], raw[legacy_field])
                    self.assertEqual(
                        declaration["provenance"],
                        {
                            "kind": "legacy_profile_field",
                            "field": legacy_field,
                        },
                    )

    def test_declared_asset_paths_match_included_and_external_reality(self):
        for profile_name in PROFILE_EXPECTATIONS:
            with self.subTest(profile=profile_name):
                path, raw = load_profile(profile_name)
                missing = missing_included_assets(profile_name, raw)
                allowed_missing = PUBLIC_CHECKOUT_PRIVATE_ASSETS.get(
                    profile_name, set()
                )
                self.assertFalse(missing - allowed_missing)
                normalized = normalize_profile(raw)
                inspection = inspect_project_assets(
                    normalized,
                    profile_dir=path.parent,
                    strict_required=not missing,
                )
                snapshot = validate_project_asset_snapshot(inspection["snapshot"])

                self.assertEqual(set(snapshot["assets"]), set(raw["assets"]))
                self.assertEqual(set(inspection["registry"]), set(raw["assets"]))
                for asset_id, declaration in normalized["assets"].items():
                    entry = snapshot["assets"][asset_id]
                    if declaration["availability"] == "external":
                        self.assertFalse(declaration["required"])
                        self.assertEqual(entry["status"], "external")
                        self.assertIsNone(entry["sha256"])
                        self.assertNotIn(asset_id, inspection["resolved_paths"])
                        continue
                    expected_path = (path.parent / declaration["path"]).resolve()
                    if asset_id in missing:
                        self.assertEqual(declaration["distribution"], "internal_only")
                        self.assertEqual(
                            Path(declaration["path"]).parts[0], "sources"
                        )
                        self.assertEqual(entry["status"], "missing")
                        self.assertIsNone(entry["sha256"])
                        self.assertNotIn(asset_id, inspection["resolved_paths"])
                        continue
                    self.assertTrue(expected_path.is_file())
                    self.assertEqual(entry["status"], "present")
                    self.assertEqual(
                        inspection["resolved_paths"][asset_id].resolve(),
                        expected_path,
                    )
                    self.assertEqual(len(entry["sha256"]), 64)

    def test_release_gate_allows_declared_external_assets(self):
        source = (ROOT / "scripts" / "run_tests.py").read_text(encoding="utf-8")

        self.assertIn('declaration.get("availability") == "external"', source)

    def test_distribution_and_authority_exceptions_are_explicit(self):
        _, mhg = load_profile("mhg/zh-ko")
        _, nrc_th = load_profile("nrc/zh-th")

        self.assertEqual(
            mhg["assets"]["style_guide"]["distribution"], "internal_only"
        )
        self.assertEqual(
            mhg["assets"]["style_guide"]["availability"], "included"
        )
        self.assertEqual(
            nrc_th["assets"]["style_guide"],
            {
                "kind": "style_guide",
                "path": "sources/Style Guide Translation TH_20260430.xlsx",
                "required": False,
                "authority": {
                    "issuer": "external_project_source",
                    "level": "unspecified",
                },
                "provenance": {
                    "kind": "legacy_profile_field",
                    "field": "style_guide",
                },
                "distribution": "internal_only",
                "availability": "external",
            },
        )
        for profile_name in PROFILE_EXPECTATIONS:
            _, raw = load_profile(profile_name)
            for asset_id, declaration in raw["assets"].items():
                self.assertEqual(declaration["distribution"], "internal_only")
                expected_availability = (
                    "external"
                    if (profile_name, asset_id)
                    == ("nrc/zh-th", "style_guide")
                    else "included"
                )
                self.assertEqual(
                    declaration["availability"], expected_availability
                )

    def test_off_mode_resolves_only_foundations_and_is_deterministic(self):
        builtins = set(builtin_descriptor_registry())
        for profile_name, expected in PROFILE_EXPECTATIONS.items():
            if expected.get("mode", "off") != "off":
                continue
            with self.subTest(profile=profile_name):
                normalized, inspection, resolution = runtime(profile_name)
                statuses = asset_statuses(inspection["snapshot"])
                repeated = resolve_capabilities(
                    normalized,
                    asset_statuses=statuses,
                    provider_registry=trusted_provider_registry(),
                )
                descriptors = descriptor_registry(
                    normalized,
                    capability_resolution=resolution,
                )

                validate_capability_resolution(resolution)
                self.assertEqual(resolution, repeated)
                self.assertEqual(resolution["profile_contract_version"], 2)
                self.assertEqual(resolution["context_pipeline_mode"], "off")
                self.assertEqual(set(resolution["enabled"]), FOUNDATIONS)
                self.assertEqual(resolution["warnings"], [])
                for capability_id in FOUNDATIONS:
                    self.assertEqual(
                        resolution["enabled"][capability_id]["effect"], "foundation"
                    )
                self.assertIn(
                    "identity",
                    normalized["capabilities"]["context.core@1"]["config"],
                )
                self.assertNotIn("identity", descriptors["context.core@1"])
                for capability_id in builtins - FOUNDATIONS:
                    self.assertEqual(
                        resolution["disabled"][capability_id]["reason"],
                        "not_declared",
                    )

    def test_mhg_enforce_mode_binds_v2_register_and_context_assets(self):
        _, raw = load_profile("mhg/zh-ko")
        missing = missing_included_assets("mhg/zh-ko", raw)
        self.assertFalse(
            missing - PUBLIC_CHECKOUT_PRIVATE_ASSETS["mhg/zh-ko"]
        )
        if missing:
            self.skipTest(
                "MHG internal-only runtime assets are not distributed in the "
                "public checkout"
            )
        normalized, inspection, resolution = runtime("mhg/zh-ko")
        repeated = resolve_capabilities(
            normalized,
            asset_statuses=asset_statuses(inspection["snapshot"]),
            provider_registry=trusted_provider_registry(),
        )

        validate_capability_resolution(resolution)
        self.assertEqual(resolution, repeated)
        self.assertEqual(resolution["context_pipeline_mode"], "enforce")
        self.assertEqual(
            set(resolution["enabled"]),
            PROFILE_EXPECTATIONS["mhg/zh-ko"]["capabilities"],
        )
        self.assertEqual(
            resolution["enabled"]["language_policy.register@1"]["provider"][
                "api_version"
            ],
            2,
        )
        self.assertEqual(
            resolution["enabled"]["language_policy.register@1"]["asset"],
            "context_rules",
        )
        self.assertEqual(
            resolution["enabled"]["assets.entity_registry@1"]["asset"],
            "entities",
        )
        self.assertEqual(
            resolution["enabled"]["source_provenance@1"]["asset"],
            "project_sources",
        )
        self.assertEqual(resolution["warnings"], [])


if __name__ == "__main__":
    unittest.main()
