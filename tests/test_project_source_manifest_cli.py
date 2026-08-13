import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lqe_profile_ingest import (
    build_project_source_manifest,
    load_project_source_manifest,
)


def asset(kind: str, path: str) -> dict:
    return {
        "kind": kind,
        "path": path,
        "required": True,
        "authority": {"issuer": "test", "level": "authoritative"},
        "provenance": {"kind": "test_fixture"},
        "distribution": "internal_only",
        "availability": "included",
    }


class ProjectSourceManifestCLIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.profile_dir = self.root / "profile"
        self.manifest_dir = self.profile_dir / "provenance"
        self.details_relative = Path("coverage/v1/details.json")
        self.details_path = self.manifest_dir / self.details_relative
        self.manifest_path = self.manifest_dir / "project_sources.json"
        self.input_path = self.root / "input.csv"
        self.job_dir = self.root / "job"
        self.state_path = self.job_dir / "state.json"

        self.manifest_dir.mkdir(parents=True)
        self.details_path.parent.mkdir(parents=True)
        with self.input_path.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(
                [
                    ["Key", "Source", "Target"],
                    ["segment-1", "Open", "Open"],
                ]
            )
        (self.profile_dir / "checks.json").write_text("{}", encoding="utf-8")
        (self.profile_dir / "confirmed_rules.md").write_text(
            "# Confirmed rules\n", encoding="utf-8"
        )

        self.coverage_details = {"records": []}
        self.manifest = build_project_source_manifest(
            project="cli-test/zh-en",
            manifest_scope="internal",
            sources=[],
            generated_assets=[],
            coverage={
                "total_nonempty": 0,
                "converted": 0,
                "normalized": 0,
                "ignored": 0,
                "unmapped": 0,
                "details_path": self.details_relative.as_posix(),
            },
            coverage_details=self.coverage_details,
        )
        self.details_path.write_text(
            json.dumps(self.coverage_details, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.manifest_path.write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        profile = {
            "profile_contract_version": 2,
            "name": "cli-test/zh-en",
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
            "checks": "checks.json",
            "confirmed_rules": "confirmed_rules.md",
            "assets": {
                "checks": asset("checks", "checks.json"),
                "rules": asset("confirmed_rules", "confirmed_rules.md"),
                "source_manifest": asset(
                    "project_source_manifest",
                    "provenance/project_sources.json",
                ),
            },
            "context_pipeline": {"mode": "enforce"},
            "capabilities": {
                "context.core@1": {"required": True},
                "source_provenance@1": {
                    "required": True,
                    "asset": "source_manifest",
                },
            },
        }
        (self.profile_dir / "profile.json").write_text(
            json.dumps(profile, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def run_read(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "lqe_io.py"),
                "read",
                "--input",
                str(self.input_path),
                "--project",
                str(self.profile_dir),
                "--source-col",
                "Source",
                "--target-col",
                "Target",
                "--key-col",
                "Key",
                "--no-terminology",
                "--out",
                str(self.state_path),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_read_failed_without_publication(
        self, result: subprocess.CompletedProcess, message: str
    ) -> None:
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(message, result.stdout + result.stderr)
        self.assertFalse(self.state_path.exists())
        self.assertFalse((self.job_dir / "scope.json").exists())
        self.assertFalse((self.job_dir / "project_assets").exists())
        if self.job_dir.exists():
            self.assertEqual(list(self.job_dir.iterdir()), [])

    def test_read_publishes_loadable_manifest_with_nested_coverage_sidecar(self):
        result = self.run_read()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        published_manifest = Path(state["project_source_manifest_path"])
        self.assertEqual(
            published_manifest,
            self.job_dir
            / "project_assets"
            / "source_manifest"
            / "project_sources.json",
        )
        self.assertEqual(
            state["project_asset_paths"]["source_manifest"],
            str(published_manifest),
        )
        self.assertNotEqual(published_manifest.resolve(), self.manifest_path.resolve())
        self.assertEqual(load_project_source_manifest(published_manifest), self.manifest)
        published_details = published_manifest.parent / self.details_relative
        self.assertEqual(
            json.loads(published_details.read_text(encoding="utf-8")),
            self.coverage_details,
        )
        self.assertEqual(
            state["project_source_manifest_digest"],
            self.manifest["manifest_digest"],
        )

    def test_read_rejects_missing_sidecar_without_publishing_job(self):
        self.details_path.unlink()

        result = self.run_read()

        self.assert_read_failed_without_publication(
            result, "project source coverage details"
        )

    def test_read_rejects_sidecar_drift_without_publishing_job(self):
        self.details_path.write_text(
            json.dumps({"records": [], "drift": True}),
            encoding="utf-8",
        )

        result = self.run_read()

        self.assert_read_failed_without_publication(
            result, "coverage details digest mismatch"
        )


if __name__ == "__main__":
    unittest.main()
