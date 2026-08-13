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

from lqe_split_contract import state_revision_payload


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


def project_profile(mode: str) -> dict:
    return {
        "profile_contract_version": 2,
        "name": f"mode-test-{mode}",
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
        },
        "context_pipeline": {"mode": mode},
        "capabilities": {
            "context.core@1": {
                "required": True,
                "config": {
                    "columns": {"content_type": ["Content Type"]}
                },
            },
            "source_provenance@1": {"required": True},
            "context.dialogue@1": {
                "required": False,
                "config": {
                    "columns": {"speaker_id": ["Speaker"]},
                    "applies_when": {"content_type": ["dialogue"]},
                },
            },
        },
    }


class ContextPipelineModeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.input_path = self.root / "input.csv"
        with self.input_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["Key", "Source", "Target", "Content Type", "Speaker"]
            )
            writer.writerow(
                ["business-a", "Same source", "Same target", "dialogue", "speaker-a"]
            )
            writer.writerow(
                ["business-b", "Same source", "Same target", "dialogue", "speaker-b"]
            )

    def tearDown(self):
        self.tempdir.cleanup()

    def make_project(self, mode: str) -> Path:
        project = self.root / f"profile-{mode}"
        project.mkdir()
        (project / "checks.json").write_text("{}", encoding="utf-8")
        (project / "confirmed_rules.md").write_text(
            "# Confirmed rules\n", encoding="utf-8"
        )
        (project / "profile.json").write_text(
            json.dumps(project_profile(mode), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return project

    def read_mode(self, mode: str) -> tuple[Path, dict]:
        project = self.make_project(mode)
        job = self.root / f"job-{mode}"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "lqe_io.py"),
                "read",
                "--input",
                str(self.input_path),
                "--project",
                str(project),
                "--source-col",
                "Source",
                "--target-col",
                "Target",
                "--key-col",
                "Key",
                "--no-terminology",
                "--out",
                str(job / "state.json"),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return job, json.loads((job / "state.json").read_text(encoding="utf-8"))

    def split(self, job: Path, state: dict) -> dict:
        errors = job / "errors_precheck.json"
        errors.write_text(
            json.dumps(
                [
                    {"id": segment["id"], "issues": []}
                    for segment in state["segments"]
                ]
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "lqe_chunk.py"),
                "split",
                "--state",
                str(job / "state.json"),
                "--errors",
                str(errors),
                "--outdir",
                str(job / "chunks"),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(
            (job / "chunks" / "dedup_map.json").read_text(encoding="utf-8")
        )

    def test_off_shadow_and_enforce_formal_truth(self):
        jobs_and_states = {
            mode: self.read_mode(mode) for mode in ("off", "shadow", "enforce")
        }
        off_job, off = jobs_and_states["off"]
        shadow_job, shadow = jobs_and_states["shadow"]
        enforce_job, enforce = jobs_and_states["enforce"]

        self.assertEqual(
            off["capability_resolution"]["disabled"]["context.dialogue@1"][
                "reason"
            ],
            "pipeline_off",
        )
        self.assertEqual(
            shadow["capability_resolution"]["enabled"]["context.dialogue@1"][
                "effect"
            ],
            "shadow",
        )
        self.assertEqual(
            enforce["capability_resolution"]["enabled"]["context.dialogue@1"][
                "effect"
            ],
            "enforce",
        )

        self.assertEqual(set(off["resolved_context_descriptors"]), {"context.core@1"})
        self.assertEqual(
            set(shadow["resolved_context_descriptors"]), {"context.core@1"}
        )
        self.assertEqual(
            set(enforce["resolved_context_descriptors"]),
            {"context.core@1", "context.dialogue@1"},
        )
        self.assertEqual(
            set(shadow["shadow_context_descriptors"]),
            {"context.core@1", "context.dialogue@1"},
        )
        self.assertNotIn("shadow_context", off["segments"][0])
        self.assertNotIn("dialogue", shadow["segments"][0]["context"]["extensions"])
        self.assertEqual(
            shadow["segments"][0]["shadow_context"]["extensions"]["dialogue"][
                "speaker_id"
            ],
            "speaker-a",
        )
        shadow_artifact = json.loads(
            Path(shadow["shadow_context_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(shadow_artifact["schema"], "lqe.shadow-context")
        self.assertEqual(
            shadow_artifact["artifact_digest"],
            shadow["shadow_context_digest"],
        )
        self.assertEqual(
            shadow_artifact["segments"][0]["shadow_context"]["extensions"][
                "dialogue"
            ]["speaker_id"],
            "speaker-a",
        )
        self.assertFalse((off_job / "shadow_context" / "context.json").exists())
        self.assertFalse(
            (enforce_job / "shadow_context" / "context.json").exists()
        )
        self.assertEqual(
            enforce["segments"][0]["context"]["extensions"]["dialogue"][
                "speaker_id"
            ],
            "speaker-a",
        )
        self.assertEqual(
            len(
                {
                    off["capability_resolution_digest"],
                    shadow["capability_resolution_digest"],
                    enforce["capability_resolution_digest"],
                }
            ),
            3,
        )

        self.assertEqual(self.split(off_job, off), {"0": [0, 1]})
        self.assertEqual(self.split(shadow_job, shadow), {"0": [0, 1]})
        self.assertEqual(self.split(enforce_job, enforce), {"0": [0], "1": [1]})
        shadow_chunk = json.loads(
            (shadow_job / "chunks" / "chunk_00.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("shadow_context", shadow_chunk["segments"][0])
        self.assertEqual(
            set(shadow_chunk["resolved_context_descriptors"]),
            {"context.core@1"},
        )
        self.assertEqual(
            off["segments"][0]["module_review_equivalence_keys"]["accuracy"],
            shadow["segments"][0]["module_review_equivalence_keys"]["accuracy"],
        )
        off_payload = state_revision_payload(off)
        shadow_payload = state_revision_payload(shadow)
        self.assertNotIn("shadow_context", off_payload["segments"][0])
        self.assertNotIn("shadow_context", shadow_payload["segments"][0])
        self.assertNotEqual(
            enforce["segments"][0]["module_review_equivalence_keys"]["accuracy"],
            enforce["segments"][1]["module_review_equivalence_keys"]["accuracy"],
        )


if __name__ == "__main__":
    unittest.main()
