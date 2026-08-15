from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lqe_project_assets import (
    ProjectAssetError,
    build_project_asset_snapshot,
    copy_project_assets,
    inspect_project_assets,
    normalize_asset_registry,
    validate_project_asset_snapshot,
)
from lqe_profile_ingest import (
    build_project_source_manifest,
    load_project_source_manifest,
)


def asset(
    kind: str,
    path: str,
    *,
    required: bool = True,
    availability: str = "included",
) -> dict:
    return {
        "kind": kind,
        "path": path,
        "required": required,
        "authority": {"issuer": "test", "level": "authoritative"},
        "provenance": {"kind": "test_fixture"},
        "distribution": "internal_only",
        "availability": availability,
    }


def profile(assets: dict) -> dict:
    return {
        "profile_contract_version": 2,
        "language_pair": "zh-en",
        "source_lang": "zh",
        "target_lang": "en",
        "assets": assets,
    }


class ProjectAssetSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_snapshot_contains_only_declared_assets(self):
        (self.root / "sg.md").write_text("Style", encoding="utf-8")
        (self.root / "entities.json").write_text("{}", encoding="utf-8")
        configured = profile({"style": asset("style_guide", "sg.md")})

        snapshot = build_project_asset_snapshot(configured, profile_dir=self.root)

        self.assertEqual(set(snapshot["assets"]), {"style"})
        self.assertEqual(snapshot["assets"]["style"]["status"], "present")
        self.assertNotIn("entities", snapshot["assets"])
        validate_project_asset_snapshot(snapshot)

    def test_content_or_metadata_change_changes_snapshot_digest(self):
        guide = self.root / "sg.md"
        guide.write_text("First", encoding="utf-8")
        configured = profile({"style": asset("style_guide", "sg.md")})
        first = build_project_asset_snapshot(configured, profile_dir=self.root)

        guide.write_text("Second", encoding="utf-8")
        second = build_project_asset_snapshot(configured, profile_dir=self.root)
        self.assertNotEqual(first["digest"], second["digest"])

        changed_metadata = deepcopy(configured)
        changed_metadata["assets"]["style"]["authority"]["issuer"] = "client"
        third = build_project_asset_snapshot(
            changed_metadata, profile_dir=self.root
        )
        self.assertNotEqual(second["digest"], third["digest"])

    def test_required_missing_fails_and_optional_missing_is_recorded(self):
        required = profile({"style": asset("style_guide", "missing.md")})
        with self.assertRaisesRegex(ProjectAssetError, "required.*missing"):
            build_project_asset_snapshot(required, profile_dir=self.root)

        optional = profile(
            {"style": asset("style_guide", "missing.md", required=False)}
        )
        snapshot = build_project_asset_snapshot(optional, profile_dir=self.root)
        self.assertEqual(snapshot["assets"]["style"]["status"], "missing")
        self.assertIsNone(snapshot["assets"]["style"]["sha256"])

    def test_external_optional_asset_is_not_opened(self):
        configured = profile(
            {
                "private": asset(
                    "entity_registry",
                    "not-present.json",
                    required=False,
                    availability="external",
                )
            }
        )
        snapshot = build_project_asset_snapshot(configured, profile_dir=self.root)
        self.assertEqual(snapshot["assets"]["private"]["status"], "external")

    def test_v2_path_traversal_absolute_path_and_symlink_are_rejected(self):
        traversal = profile({"style": asset("style_guide", "../sg.md")})
        with self.assertRaisesRegex(ProjectAssetError, "parent traversal"):
            normalize_asset_registry(traversal, legacy=False)

        outside = self.root.parent / "outside-style.md"
        outside.write_text("Outside", encoding="utf-8")
        absolute = profile({"style": asset("style_guide", str(outside))})
        with self.assertRaisesRegex(ProjectAssetError, "escapes profile"):
            build_project_asset_snapshot(absolute, profile_dir=self.root)

        real = self.root / "real.md"
        real.write_text("Real", encoding="utf-8")
        link = self.root / "link.md"
        link.symlink_to(real)
        linked = profile({"style": asset("style_guide", "link.md")})
        with self.assertRaisesRegex(ProjectAssetError, "symbolic link"):
            build_project_asset_snapshot(linked, profile_dir=self.root)

    def test_copy_helper_writes_only_to_caller_staging_directory(self):
        guide = self.root / "sg.md"
        guide.write_text("Style", encoding="utf-8")
        inspection = inspect_project_assets(
            profile({"style": asset("style_guide", "sg.md")}),
            profile_dir=self.root,
        )
        destination = self.root / "staging" / "project_assets"

        copied = copy_project_assets(inspection, destination)

        self.assertEqual(copied["style"].read_text(encoding="utf-8"), "Style")
        self.assertEqual(copied["style"], destination / "style" / "sg.md")

    def test_copy_helper_stages_project_source_coverage_sidecar(self):
        details = {"records": []}
        manifest = build_project_source_manifest(
            project="sample/zh-en",
            manifest_scope="internal",
            sources=[],
            generated_assets=[],
            coverage={
                "total_nonempty": 0,
                "converted": 0,
                "normalized": 0,
                "ignored": 0,
                "unmapped": 0,
                "details_path": "coverage/details.json",
            },
            coverage_details=details,
        )
        source_dir = self.root / "profile"
        (source_dir / "coverage").mkdir(parents=True)
        manifest_path = source_dir / "project_sources.json"
        details_path = source_dir / "coverage" / "details.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        details_path.write_text(json.dumps(details), encoding="utf-8")
        inspection = inspect_project_assets(
            profile(
                {
                    "sources": asset(
                        "project_source_manifest", "project_sources.json"
                    )
                }
            ),
            profile_dir=source_dir,
        )
        destination = self.root / "staging" / "project_assets"

        copied = copy_project_assets(inspection, destination)

        copied_manifest = copied["sources"]
        self.assertEqual(load_project_source_manifest(copied_manifest), manifest)
        self.assertEqual(
            json.loads(
                (destination / "sources" / "coverage" / "details.json").read_text(
                    encoding="utf-8"
                )
            ),
            details,
        )

    def test_copy_helper_rejects_source_manifest_sidecar_drift_atomically(self):
        details = {"records": []}
        manifest = build_project_source_manifest(
            project="sample/zh-en",
            manifest_scope="internal",
            sources=[],
            generated_assets=[],
            coverage={
                "total_nonempty": 0,
                "converted": 0,
                "normalized": 0,
                "ignored": 0,
                "unmapped": 0,
                "details_path": "details.json",
            },
            coverage_details=details,
        )
        source_dir = self.root / "profile"
        source_dir.mkdir()
        manifest_path = source_dir / "project_sources.json"
        details_path = source_dir / "details.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        details_path.write_text(json.dumps(details), encoding="utf-8")
        inspection = inspect_project_assets(
            profile(
                {
                    "sources": asset(
                        "project_source_manifest", "project_sources.json"
                    )
                }
            ),
            profile_dir=source_dir,
        )
        details_path.write_text(
            json.dumps({"records": [{"coordinate": "A1", "status": "converted"}]}),
            encoding="utf-8",
        )
        destination = self.root / "staging" / "project_assets"

        with self.assertRaisesRegex(ProjectAssetError, "details"):
            copy_project_assets(inspection, destination)

        self.assertFalse((destination / "sources").exists())

    def test_snapshot_tampering_is_rejected(self):
        guide = self.root / "sg.md"
        guide.write_text("Style", encoding="utf-8")
        snapshot = build_project_asset_snapshot(
            profile({"style": asset("style_guide", "sg.md")}),
            profile_dir=self.root,
        )
        snapshot["assets"]["style"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ProjectAssetError, "digest mismatch"):
            validate_project_asset_snapshot(snapshot)


if __name__ == "__main__":
    unittest.main()
