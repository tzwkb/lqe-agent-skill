import csv
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lqe_context import descriptor_registry, module_review_equivalence_key
from lqe_capabilities import canonical_digest
from lqe_engine import build_check_scope
from tests.runtime_helpers import bind_empty_runtime_context


def character_descriptor() -> dict:
    return {
        "schema": "lqe.context-capability-descriptor",
        "version": 1,
        "id": "context.character@1",
        "fields": {
            "persona_id": {
                "type": "string",
                "columns": ["Persona"],
                "normalizer": "trim",
                "affects_review_equivalence": True,
            }
        },
        "module_views": {
            "accuracy": ["persona_id"],
            "naturalness": ["persona_id"],
            "suggestions": ["persona_id"],
        },
    }


def resolved_registry() -> dict:
    custom = character_descriptor()
    profile = {
        "capability_descriptors": {custom["id"]: custom},
        "capabilities": {
            "context.core@1": {"required": True},
            "context.dialogue@1": {"required": False},
            "context.character@1": {"required": False},
        },
    }
    resolution = {
        "enabled": {
            "context.core@1": {},
            "context.dialogue@1": {},
            "context.character@1": {},
        }
    }
    return descriptor_registry(profile, capability_resolution=resolution)


def segment(
    segment_id: int,
    business_key: str,
    speaker_id: str,
    persona_id: str,
) -> dict:
    return {
        "id": segment_id,
        "segment_key": business_key,
        "source": "Same source",
        "target": "Same target",
        "input_status": "ready",
        "context": {
            "context_contract_version": 1,
            "status": "ready",
            "core": {"content_type": "dialogue"},
            "extensions": {
                "dialogue": {
                    "status": "ready",
                    "speaker_id": speaker_id,
                },
                "character": {
                    "status": "ready",
                    "persona_id": persona_id,
                },
            },
            "provenance": {},
            "missing_required": [],
        },
        "resolved_constraints": [],
    }


class ContextAwareDedupTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.job = Path(self.tempdir.name) / "job"
        self.job.mkdir()
        self.registry = resolved_registry()
        self.segments = [
            segment(0, "business-a", "speaker-a", "persona-a"),
            segment(1, "business-b", "speaker-b", "persona-a"),
            segment(2, "business-c", "speaker-a", "persona-a"),
            segment(3, "business-d", "speaker-a", "persona-b"),
        ]
        self.state = {
            "artifact_contract_version": 1,
            "job_runtime_contract_version": 2,
            "iteration": 0,
            "target_lang": "en",
            "check_scope": build_check_scope(True, "test"),
            "capability_resolution": {
                "enabled": {
                    capability_id: {
                        "effect": (
                            "foundation"
                            if capability_id == "context.core@1"
                            else "enforce"
                        ),
                        "descriptor_digest": canonical_digest(descriptor),
                    }
                    for capability_id, descriptor in self.registry.items()
                }
            },
            "resolved_context_descriptors": self.registry,
            "segments": deepcopy(self.segments),
        }
        bind_empty_runtime_context(self.state)
        self.state["capability_resolution"]["enabled"].update(
            {
                capability_id: {
                    "required": False,
                    "effect": "enforce",
                    "descriptor_digest": canonical_digest(descriptor),
                    "reason": "enabled",
                }
                for capability_id, descriptor in self.registry.items()
                if capability_id != "context.core@1"
            }
        )
        self.state["capability_resolution"]["digest"] = canonical_digest(
            {
                key: value
                for key, value in self.state["capability_resolution"].items()
                if key != "digest"
            }
        )
        self.state["capability_resolution_digest"] = self.state[
            "capability_resolution"
        ]["digest"]
        self.state["resolved_context_descriptors"] = self.registry
        for segment_value, source_segment in zip(
            self.state["segments"], self.segments
        ):
            segment_value["context"] = deepcopy(source_segment["context"])
            segment_value["segment_revision_digest"] = canonical_digest(
                {
                    "segment_key": segment_value["segment_key"],
                    "source": segment_value["source"],
                    "target": segment_value["target"],
                    "protected": False,
                    "context": segment_value["context"],
                }
            )
        self.write_json(self.job / "state.json", self.state)
        self.write_json(
            self.job / "errors_precheck.json",
            [{"id": item["id"], "issues": []} for item in self.segments],
        )

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def write_json(path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def run_script(self, script: str, *arguments: object) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), *map(str, arguments)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def split(self) -> dict:
        result = self.run_script(
            "lqe_chunk.py",
            "split",
            "--state",
            self.job / "state.json",
            "--errors",
            self.job / "errors_precheck.json",
            "--outdir",
            self.job / "chunks",
            "--size",
            100,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(
            (self.job / "chunks" / "dedup_map.json").read_text(encoding="utf-8")
        )

    def test_speaker_and_custom_projection_split_dedup_but_business_key_does_not(self):
        self.assertEqual(
            module_review_equivalence_key(
                self.segments[0], "accuracy", self.registry
            ),
            module_review_equivalence_key(
                self.segments[2], "accuracy", self.registry
            ),
        )
        self.assertNotEqual(
            module_review_equivalence_key(
                self.segments[0], "accuracy", self.registry
            ),
            module_review_equivalence_key(
                self.segments[1], "accuracy", self.registry
            ),
        )
        self.assertNotEqual(
            module_review_equivalence_key(
                self.segments[0], "accuracy", self.registry
            ),
            module_review_equivalence_key(
                self.segments[3], "accuracy", self.registry
            ),
        )
        self.assertEqual(
            self.split(),
            {"0": [0, 2], "1": [1], "3": [3]},
        )

    def test_custom_registry_survives_split_and_review_projection(self):
        self.split()
        chunk = json.loads(
            (self.job / "chunks" / "chunk_00.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            chunk["resolved_context_descriptors"]["context.character@1"],
            self.registry["context.character@1"],
        )

        result = self.run_script("lqe_review.py", "prepare", "--job", self.job)
        self.assertEqual(result.returncode, 0, result.stderr)
        packet = json.loads(
            (
                self.job
                / "review_packets"
                / "accuracy"
                / "chunk_00.json"
            ).read_text(encoding="utf-8")
        )
        by_id = {item["id"]: item for item in packet["segments"]}
        self.assertEqual(set(by_id), {0, 1, 3})
        self.assertEqual(
            by_id[0]["context_projection"]["extensions"]["character"],
            {"status": "ready", "persona_id": "persona-a"},
        )
        self.assertEqual(
            by_id[1]["context_projection"]["extensions"]["dialogue"][
                "speaker_id"
            ],
            "speaker-b",
        )

    def test_split_rejects_tampered_resolved_descriptor(self):
        tampered = deepcopy(self.state)
        tampered["resolved_context_descriptors"]["context.character@1"][
            "module_views"
        ]["accuracy"] = []
        self.write_json(self.job / "state.json", tampered)

        result = self.run_script(
            "lqe_chunk.py",
            "split",
            "--state",
            self.job / "state.json",
            "--errors",
            self.job / "errors_precheck.json",
            "--outdir",
            self.job / "tampered-chunks",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "resolved context descriptor digest mismatch",
            result.stdout + result.stderr,
        )


class CustomRegistryReadIntegrationTests(unittest.TestCase):
    def test_custom_descriptor_runs_from_profile_through_review(self):
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            project = root / "project"
            project.mkdir()
            (project / "checks.json").write_text("{}", encoding="utf-8")
            (project / "confirmed_rules.md").write_text(
                "# Confirmed rules\n", encoding="utf-8"
            )
            custom = character_descriptor()
            asset = lambda kind, path: {
                "kind": kind,
                "path": path,
                "required": True,
                "authority": {"issuer": "test", "level": "authoritative"},
                "provenance": {"kind": "test_fixture"},
                "distribution": "internal_only",
                "availability": "included",
            }
            profile = {
                "profile_contract_version": 2,
                "name": "custom-context-test",
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
                "context_pipeline": {"mode": "enforce"},
                "capability_descriptors": {custom["id"]: custom},
                "capabilities": {
                    "context.core@1": {"required": True},
                    "source_provenance@1": {"required": True},
                    "context.character@1": {"required": False},
                },
            }
            (project / "profile.json").write_text(
                json.dumps(profile, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            source = root / "input.csv"
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["Key", "Source", "Target", "Persona"])
                writer.writerow(["business-a", "Same source", "Same target", "a"])
                writer.writerow(["business-b", "Same source", "Same target", "b"])
                writer.writerow(["business-c", "Same source", "Same target", "a"])
            job = root / "job"
            read_result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "lqe_io.py"),
                    "read",
                    "--input",
                    str(source),
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
            self.assertEqual(read_result.returncode, 0, read_result.stderr)
            state = json.loads((job / "state.json").read_text(encoding="utf-8"))
            self.assertIn(
                "context.character@1", state["resolved_context_descriptors"]
            )
            self.assertEqual(
                state["segments"][0]["context"]["extensions"]["character"],
                {"status": "ready", "persona_id": "a"},
            )

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
            split_result = subprocess.run(
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
            self.assertEqual(split_result.returncode, 0, split_result.stderr)
            dedup = json.loads(
                (job / "chunks" / "dedup_map.json").read_text(encoding="utf-8")
            )
            self.assertEqual(dedup, {"0": [0, 2], "1": [1]})

            review_result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "lqe_review.py"),
                    "prepare",
                    "--job",
                    str(job),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            self.assertEqual(review_result.returncode, 0, review_result.stderr)
            packet = json.loads(
                (
                    job
                    / "review_packets"
                    / "accuracy"
                    / "chunk_00.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                packet["segments"][0]["context_projection"]["extensions"][
                    "character"
                ]["persona_id"],
                "a",
            )


if __name__ == "__main__":
    unittest.main()
