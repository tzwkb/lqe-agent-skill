from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lqe_profile_ingest import (
    ProfileIngestError,
    SCHEMA_FILES,
    build_project_source_manifest,
    canonical_digest,
    load_project_source_manifest,
    validate_canonical_asset,
    validate_context_rules,
    validate_entity_registry,
    validate_project_source_manifest,
    validate_review_examples,
)


def source_record() -> dict:
    return {
        "id": "source.guide.v1",
        "kind": "style_guide_source",
        "filename": "guide.xlsx",
        "sha256": "1" * 64,
        "verification_status": "source_backed",
        "distribution": "internal_only",
        "authority": {"issuer": "client"},
        "availability": "included",
        "containers": [
            {"kind": "worksheet", "name": "Guide", "used_range": "A1:B2"}
        ],
    }


def generated_record() -> dict:
    return {
        "asset_id": "style",
        "kind": "style_guide",
        "path": "sg.md",
        "sha256": "2" * 64,
        "derived_from": ["source.guide.v1"],
        "distribution": "internal_only",
        "generator": {"name": "lqe_profile_ingest", "version": 1},
    }


def coverage() -> dict:
    return {
        "total_nonempty": 3,
        "converted": 1,
        "normalized": 1,
        "ignored": 1,
        "unmapped": 0,
        "details_path": "project_source_coverage.json",
    }


def coverage_details() -> dict:
    return {
        "records": [
            {"coordinate": "Guide!A1", "status": "converted"},
            {"coordinate": "Guide!A2", "status": "normalized"},
            {
                "coordinate": "Guide!B2",
                "status": "ignored",
                "reason": "decorative heading",
            },
        ]
    }


class CanonicalProjectAssetTests(unittest.TestCase):
    def test_all_bundled_context_schemas_are_valid_json(self):
        self.assertEqual(
            set(SCHEMA_FILES),
            {
                "lqe.entities",
                "lqe.review-examples",
                "lqe.context-rules",
                "lqe.segment-context-overrides",
                "lqe.project-source-manifest",
            },
        )
        for path in SCHEMA_FILES.values():
            self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)

    def test_entity_registry_is_generic_and_references_declared_entities(self):
        registry = {
            "schema": "lqe.entities",
            "version": 1,
            "entities": [
                {
                    "id": "entity.product.widget",
                    "entity_type": "product",
                    "names": {"source": ["Widget"], "target": []},
                    "tags": ["catalog"],
                    "facts": [
                        {
                            "id": "fact.widget.color",
                            "text": "Available in blue",
                            "verification_status": "source_backed",
                            "authority": {"issuer": "client"},
                            "provenance": {"source_id": "source.catalog.v1"},
                        }
                    ],
                }
            ],
            "relations": [],
        }
        self.assertEqual(
            validate_entity_registry(registry, source_ids=["source.catalog.v1"]),
            registry,
        )
        invalid = deepcopy(registry)
        invalid["relations"] = [
            {
                "id": "relation.widget.missing",
                "from": "entity.product.widget",
                "to": "entity.product.unknown",
                "relation_type": "compatible_with",
                "verification_status": "verified",
                "authority": {"issuer": "pm"},
                "provenance": {"source_id": "source.catalog.v1"},
            }
        ]
        with self.assertRaisesRegex(ProfileIngestError, "unknown entity"):
            validate_entity_registry(invalid)

    def test_review_examples_keep_regression_and_runtime_sets_separate(self):
        example = {
            "schema": "lqe.review-examples",
            "version": 1,
            "examples": [
                {
                    "id": "review.1",
                    "segment_key": "line-1",
                    "dimensions": ["accuracy"],
                    "source": "Open",
                    "rejected_target": "Shut",
                    "preferred_target": "Open",
                    "reason": "Opposite meaning",
                    "scope": "segment",
                    "review_status": "reviewed",
                    "authority": {"issuer": "translator"},
                    "uses": ["regression_only"],
                }
            ],
        }
        self.assertEqual(validate_review_examples(example), example)
        mixed = deepcopy(example)
        mixed["examples"][0]["uses"].append("runtime_reference")
        with self.assertRaisesRegex(ProfileIngestError, "cannot mix"):
            validate_review_examples(mixed)

    def test_context_rules_are_provider_declared_and_language_bound(self):
        rules = {
            "schema": "lqe.context-rules",
            "version": 1,
            "authority_rank": ["client", "pm"],
            "rules": [
                {
                    "id": "rule.language.1",
                    "capability": "language.register",
                    "provider": {"id": "ja.register", "api_version": 1},
                    "target_lang": "ja",
                    "rule_status": "confirmed",
                    "priority": 10,
                    "authority": {"issuer": "client"},
                    "valid_from": None,
                    "valid_until": None,
                    "when": {"content_type": ["dialogue"]},
                    "expect": {"provider_field": ["provider_value"]},
                    "provenance": {"source_id": "source.rules.v1"},
                }
            ],
        }
        self.assertEqual(validate_context_rules(rules, target_lang="ja"), rules)
        with self.assertRaisesRegex(ProfileIngestError, "does not match profile"):
            validate_context_rules(rules, target_lang="en")

    def test_manifest_binds_coverage_details_and_all_generated_sources(self):
        manifest = build_project_source_manifest(
            project="sample/en-ja",
            manifest_scope="internal",
            sources=[source_record()],
            generated_assets=[generated_record()],
            coverage=coverage(),
            coverage_details=coverage_details(),
        )
        self.assertEqual(
            manifest["coverage"]["details_sha256"],
            canonical_digest(coverage_details()),
        )
        self.assertEqual(validate_project_source_manifest(manifest), manifest)

        tampered = deepcopy(manifest)
        tampered["coverage"]["converted"] = 2
        with self.assertRaisesRegex(ProfileIngestError, "digest mismatch"):
            validate_project_source_manifest(tampered)

        unknown = deepcopy(generated_record())
        unknown["derived_from"] = ["source.unknown"]
        with self.assertRaisesRegex(ProfileIngestError, "undeclared sources"):
            build_project_source_manifest(
                project="sample/en-ja",
                manifest_scope="internal",
                sources=[source_record()],
                generated_assets=[unknown],
                coverage=coverage(),
                coverage_details=coverage_details(),
            )

    def test_manifest_publication_rejects_coverage_gaps_and_ignored_without_reason(self):
        gap = coverage()
        gap["unmapped"] = 1
        gap["total_nonempty"] = 4
        details = coverage_details()
        details["records"].append({"coordinate": "Guide!B3", "status": "unmapped"})
        with self.assertRaisesRegex(ProfileIngestError, "unmapped"):
            build_project_source_manifest(
                project="sample/en-ja",
                manifest_scope="internal",
                sources=[source_record()],
                generated_assets=[generated_record()],
                coverage=gap,
                coverage_details=details,
            )

    def test_runtime_manifest_binds_coverage_file_and_rejects_drift(self):
        details = coverage_details()
        manifest = build_project_source_manifest(
            project="sample/en-ja",
            manifest_scope="internal",
            sources=[source_record()],
            generated_assets=[generated_record()],
            coverage=coverage(),
            coverage_details=details,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "project_sources.json"
            details_path = root / manifest["coverage"]["details_path"]
            details_path.write_text(
                json.dumps(details, ensure_ascii=False),
                encoding="utf-8",
            )
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
            )

            self.assertEqual(load_project_source_manifest(manifest_path), manifest)

            drifted = deepcopy(details)
            drifted["records"][0]["coordinate"] = "Guide!A9"
            details_path.write_text(
                json.dumps(drifted, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ProfileIngestError, "details digest mismatch"):
                load_project_source_manifest(manifest_path)

            details_path.unlink()
            with self.assertRaisesRegex(ProfileIngestError, "cannot inspect"):
                load_project_source_manifest(manifest_path)

    def test_runtime_manifest_rejects_summary_drift_and_unsafe_details_path(self):
        details = coverage_details()
        manifest = build_project_source_manifest(
            project="sample/en-ja",
            manifest_scope="internal",
            sources=[source_record()],
            generated_assets=[generated_record()],
            coverage=coverage(),
            coverage_details=details,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "project_sources.json"
            details_path = root / "project_source_coverage.json"
            details_path.write_text(json.dumps(details), encoding="utf-8")

            mismatched = deepcopy(manifest)
            mismatched["coverage"]["converted"] = 0
            mismatched["coverage"]["normalized"] = 2
            mismatched["manifest_digest"] = canonical_digest({
                key: value
                for key, value in mismatched.items()
                if key != "manifest_digest"
            })
            manifest_path.write_text(json.dumps(mismatched), encoding="utf-8")
            with self.assertRaisesRegex(
                ProfileIngestError, "details do not match manifest summary"
            ):
                load_project_source_manifest(manifest_path)

            escaped = deepcopy(manifest)
            escaped["coverage"]["details_path"] = "../coverage.json"
            escaped["manifest_digest"] = canonical_digest({
                key: value
                for key, value in escaped.items()
                if key != "manifest_digest"
            })
            manifest_path.write_text(json.dumps(escaped), encoding="utf-8")
            with self.assertRaisesRegex(ProfileIngestError, "must not escape"):
                load_project_source_manifest(manifest_path)

    def test_manifest_requires_coverage_digest_and_portable_generated_paths(self):
        manifest = build_project_source_manifest(
            project="sample/en-ja",
            manifest_scope="internal",
            sources=[source_record()],
            generated_assets=[generated_record()],
            coverage=coverage(),
            coverage_details=coverage_details(),
        )
        missing_digest = deepcopy(manifest)
        missing_digest["coverage"].pop("details_sha256")
        missing_digest["manifest_digest"] = canonical_digest({
            key: value
            for key, value in missing_digest.items()
            if key != "manifest_digest"
        })
        with self.assertRaisesRegex(ProfileIngestError, "details_sha256"):
            validate_project_source_manifest(missing_digest)

        escaped_asset = deepcopy(generated_record())
        escaped_asset["path"] = "../sg.md"
        with self.assertRaisesRegex(ProfileIngestError, "must not escape"):
            build_project_source_manifest(
                project="sample/en-ja",
                manifest_scope="internal",
                sources=[source_record()],
                generated_assets=[escaped_asset],
                coverage=coverage(),
                coverage_details=coverage_details(),
            )

        bad_details = coverage_details()
        bad_details["records"][2].pop("reason")
        with self.assertRaisesRegex(ProfileIngestError, "lacks reason"):
            build_project_source_manifest(
                project="sample/en-ja",
                manifest_scope="internal",
                sources=[source_record()],
                generated_assets=[generated_record()],
                coverage=coverage(),
                coverage_details=bad_details,
            )

    def test_validate_cli_reports_schema_and_digest(self):
        registry = {
            "schema": "lqe.entities",
            "version": 1,
            "entities": [],
            "relations": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "entities.json"
            path.write_text(json.dumps(registry), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "lqe_profile_ingest.py"),
                    "validate",
                    str(path),
                    "--schema",
                    "lqe.entities",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["digest"], canonical_digest(registry))

    def test_unknown_fields_and_versions_fail_closed(self):
        registry = {
            "schema": "lqe.entities",
            "version": 1,
            "entities": [],
            "relations": [],
            "plot_only": True,
        }
        with self.assertRaisesRegex(ProfileIngestError, "unknown property"):
            validate_canonical_asset(registry)
        registry.pop("plot_only")
        registry["version"] = 2
        with self.assertRaisesRegex(ProfileIngestError, "unsupported"):
            validate_canonical_asset(registry)


if __name__ == "__main__":
    unittest.main()
