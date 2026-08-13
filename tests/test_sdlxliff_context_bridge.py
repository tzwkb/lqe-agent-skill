import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
IO_SCRIPT = SCRIPTS / "lqe_io.py"
FIXTURE = ROOT / "tests" / "fixtures" / "sdlxliff" / "multi_segment.sdlxliff"


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


def profile(mode: str) -> dict:
    return {
        "profile_contract_version": 2,
        "name": f"sdl-shadow-{mode}",
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
            "context.core@1": {"required": True},
            "source_provenance@1": {"required": True},
            "context.dialogue@1": {
                "required": False,
                "config": {
                    "applies_when": {"content_type": ["dialogue"]}
                },
            },
        },
        "sdlxliff": {
            "tm_protection": "candidate-only",
            "content_type_rules": [
                {
                    "id": "all-dialogue",
                    "glob": "*.sdlxliff",
                    "content_type": "dialogue",
                }
            ],
        },
    }


class SDLXLIFFContextBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.input_path = self.root / "multi_segment.sdlxliff"
        shutil.copy2(FIXTURE, self.input_path)

    def tearDown(self):
        self.tempdir.cleanup()

    def make_project(self, mode: str) -> Path:
        project = self.root / f"project-{mode}"
        project.mkdir()
        (project / "checks.json").write_text("{}", encoding="utf-8")
        (project / "confirmed_rules.md").write_text(
            "# Confirmed rules\n", encoding="utf-8"
        )
        (project / "profile.json").write_text(
            json.dumps(profile(mode), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return project

    def read_mode(self, mode: str) -> tuple[Path, dict, dict]:
        job = self.root / f"job-{mode}"
        result = subprocess.run(
            [
                sys.executable,
                str(IO_SCRIPT),
                "read",
                "--input",
                str(self.input_path),
                "--input-format",
                "sdlxliff",
                "--project",
                str(self.make_project(mode)),
                "--no-terminology",
                "--review-mode",
                "optimized",
                "--out",
                str(job / "state.json"),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((job / "state.json").read_text(encoding="utf-8"))
        candidates = json.loads(
            (job / "tm_candidates.json").read_text(encoding="utf-8")
        )
        return job, state, candidates

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
            (job / "chunks" / "chunk_00.json").read_text(encoding="utf-8")
        )

    def test_off_shadow_enforce_bridge_and_protection_are_independent(self):
        off_job, off, off_candidates = self.read_mode("off")
        shadow_job, shadow, shadow_candidates = self.read_mode("shadow")
        enforce_job, enforce, enforce_candidates = self.read_mode("enforce")

        self.assertEqual(
            set(off["resolved_context_descriptors"]), {"context.core@1"}
        )
        self.assertEqual(
            set(shadow["resolved_context_descriptors"]), {"context.core@1"}
        )
        self.assertEqual(
            set(enforce["resolved_context_descriptors"]),
            {"context.core@1", "context.dialogue@1"},
        )
        self.assertIsNone(off["shadow_context_descriptors"])
        self.assertEqual(
            set(shadow["shadow_context_descriptors"]),
            {"context.core@1", "context.dialogue@1"},
        )
        self.assertIsNone(enforce["shadow_context_descriptors"])

        self.assertNotIn("shadow_context", off["segments"][0])
        self.assertNotIn("shadow_context", enforce["segments"][0])
        self.assertNotIn(
            "dialogue", shadow["segments"][0]["context"]["extensions"]
        )
        self.assertEqual(
            shadow["segments"][0]["shadow_context"]["extensions"][
                "dialogue"
            ]["status"],
            "incomplete",
        )
        self.assertEqual(
            shadow["segments"][0]["shadow_context"]["missing_required"],
            ["context.extensions.dialogue.speaker_id"],
        )
        self.assertEqual(
            enforce["segments"][0]["context"]["extensions"]["dialogue"][
                "status"
            ],
            "incomplete",
        )

        self.assertEqual(
            off["segments"][0]["module_review_equivalence_keys"],
            shadow["segments"][0]["module_review_equivalence_keys"],
        )
        self.assertNotEqual(
            enforce["segments"][0]["module_review_equivalence_keys"]["accuracy"],
            shadow["segments"][0]["module_review_equivalence_keys"]["accuracy"],
        )

        off_chunk = self.split(off_job, off)
        shadow_chunk = self.split(shadow_job, shadow)
        enforce_chunk = self.split(enforce_job, enforce)
        self.assertEqual(
            off_chunk["resolved_context_descriptors"],
            shadow_chunk["resolved_context_descriptors"],
        )
        self.assertEqual(
            set(shadow_chunk["resolved_context_descriptors"]),
            {"context.core@1"},
        )
        self.assertFalse(
            any("shadow_context" in segment for segment in shadow_chunk["segments"])
        )
        self.assertEqual(
            set(enforce_chunk["resolved_context_descriptors"]),
            {"context.core@1", "context.dialogue@1"},
        )
        self.assertEqual(
            enforce_chunk["segments"][0]["context"]["extensions"]["dialogue"][
                "status"
            ],
            "incomplete",
        )

        for state, candidates in (
            (off, off_candidates),
            (shadow, shadow_candidates),
            (enforce, enforce_candidates),
        ):
            self.assertEqual(candidates["candidate_ids"], [0])
            self.assertFalse(state["segments"][0].get("protected", False))
            self.assertTrue(state["segments"][1]["protected"])
            self.assertEqual(
                state["segments"][1]["protected_reason"], "SOURCE_LOCKED"
            )


if __name__ == "__main__":
    unittest.main()
