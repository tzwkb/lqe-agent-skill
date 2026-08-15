import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lqe_split_contract import state_fingerprint, state_revision_payload
from lqe_profile_ingest import build_project_source_manifest
from lqe_context_bundle import (
    ContextBundleError,
    build_context_bundle_set,
    build_worker_context_manifest,
    load_project_context_assets,
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


def project_profile(mode: str) -> dict:
    return {
        "profile_contract_version": 2,
        "name": "mode-test",
        "language_pair": "zh-ko",
        "source_lang": "zh",
        "target_lang": "ko",
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
            "entities": asset("entity_registry", "entities.json"),
            "examples_primary": asset(
                "review_examples", "examples_primary.json"
            ),
            "examples_secondary": asset(
                "review_examples", "examples_secondary.json"
            ),
            "project_sources": asset(
                "project_source_manifest",
                "provenance/project_sources.json",
            ),
        },
        "context_pipeline": {"mode": mode},
        "module_context_views": {
            "suggestions": {
                "capabilities": [
                    "context.core@1",
                    "context.dialogue@1",
                ],
                "dimensions": ["suggestions"],
                "limits": {"max_runtime_examples": 2},
            }
        },
        "capability_descriptors": {
            "assets.review_examples_extra@1": {
                "schema": "lqe.context-capability-descriptor",
                "version": 1,
                "id": "assets.review_examples_extra@1",
                "fields": {},
                "module_views": {},
            }
        },
        "capabilities": {
            "context.core@1": {
                "required": True,
                "config": {
                    "columns": {"content_type": ["Content Type"]}
                },
            },
            "source_provenance@1": {
                "required": True,
                "asset": "project_sources",
            },
            "context.dialogue@1": {
                "required": False,
                "config": {
                    "columns": {
                        "speaker_id": ["Speaker"],
                        "addressee_ids": ["Addressee"],
                    },
                    "applies_when": {"content_type": ["dialogue"]},
                },
            },
            "assets.entity_registry@1": {
                "required": False,
                "asset": "entities",
            },
            "assets.review_examples@1": {
                "required": False,
                "asset": "examples_primary",
            },
            "assets.review_examples_extra@1": {
                "required": False,
                "asset": "examples_secondary",
            },
            "language_policy.register@1": {
                "required": False,
                "provider": "ko.register@1",
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
                [
                    "Key",
                    "Source",
                    "Target",
                    "Content Type",
                    "Speaker",
                    "Addressee",
                ]
            )
            writer.writerow(
                [
                    "business-a",
                    "Same source",
                    "Same target",
                    "dialogue",
                    "speaker-a",
                    "listener-a",
                ]
            )
            writer.writerow(
                [
                    "business-b",
                    "Same source",
                    "Same target",
                    "dialogue",
                    "speaker-b",
                    "listener-b",
                ]
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
        (project / "entities.json").write_text(
            json.dumps(
                {
                    "schema": "lqe.entities",
                    "version": 1,
                    "entities": [],
                    "relations": [],
                }
            ),
            encoding="utf-8",
        )
        for filename, example_id in (
            ("examples_primary.json", "example.core.primary"),
            ("examples_secondary.json", "example.core.secondary"),
        ):
            (project / filename).write_text(
                json.dumps(
                    {
                        "schema": "lqe.review-examples",
                        "version": 1,
                        "examples": [
                            {
                                "id": example_id,
                                "dimensions": ["suggestions"],
                                "source": "原文示例",
                                "rejected_target": "나쁜 예시",
                                "preferred_target": "좋은 예시",
                                "reason": "Reviewed fixture",
                                "scope": "project",
                                "review_status": "reviewed",
                                "authority": {"issuer": "test"},
                                "uses": ["runtime_reference"],
                                "content_types": ["dialogue"],
                                "capabilities": ["context.core@1"],
                                "provenance": {"source_id": "source.entities"},
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        coverage_details = {"records": []}
        provenance = project / "provenance"
        provenance.mkdir()
        (provenance / "coverage.json").write_text(
            json.dumps(coverage_details),
            encoding="utf-8",
        )
        generated_assets = [
            {
                "asset_id": asset_id,
                "kind": kind,
                "path": filename,
                "sha256": hashlib.sha256(
                    (project / filename).read_bytes()
                ).hexdigest(),
                "derived_from": ["source.entities"],
                "distribution": "internal_only",
                "generator": {"name": "test", "version": 1},
            }
            for asset_id, kind, filename in (
                ("entities", "entity_registry", "entities.json"),
                (
                    "examples_primary",
                    "review_examples",
                    "examples_primary.json",
                ),
                (
                    "examples_secondary",
                    "review_examples",
                    "examples_secondary.json",
                ),
            )
        ]
        manifest = build_project_source_manifest(
            project="mode-test",
            manifest_scope="internal",
            sources=[
                {
                    "id": "source.entities",
                    "kind": "entity_source",
                    "sha256": "a" * 64,
                    "verification_status": "verified",
                    "distribution": "internal_only",
                    "authority": {"issuer": "test"},
                    "availability": "external",
                }
            ],
            generated_assets=generated_assets,
            coverage={
                "total_nonempty": 0,
                "converted": 0,
                "normalized": 0,
                "ignored": 0,
                "unmapped": 0,
                "details_path": "coverage.json",
            },
            coverage_details=coverage_details,
        )
        (provenance / "project_sources.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (project / "profile.json").write_text(
            json.dumps(project_profile(mode), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return project

    def run_read_mode(
        self,
        mode: str,
        *,
        explicit_context: bool = False,
    ) -> tuple[Path, subprocess.CompletedProcess]:
        project = self.make_project(mode)
        job = self.root / f"job-{mode}"
        command = [
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
        ]
        if explicit_context:
            command.extend(
                [
                    "--content-type-col",
                    "Content Type",
                    "--speaker-col",
                    "Speaker",
                    "--addressee-col",
                    "Addressee",
                ]
            )
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        return job, result

    def read_mode(
        self,
        mode: str,
        *,
        explicit_context: bool = False,
    ) -> tuple[Path, dict]:
        job, result = self.run_read_mode(
            mode,
            explicit_context=explicit_context,
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

    def worker_view(self, state: dict) -> tuple[dict, dict]:
        bundle_set = build_context_bundle_set(
            state,
            state["segments"],
            "suggestions",
        )
        manifest = build_worker_context_manifest(
            state,
            "suggestions",
            bundle_set,
            max_worker_bytes=100_000,
        )
        return bundle_set, manifest

    @staticmethod
    def formal_bundles(bundle_set: dict) -> list[dict]:
        excluded = {
            "context_bundle_digest",
            "project_asset_snapshot_digest",
            "capability_resolution_digest",
            "segment_revision_digest",
        }
        return [
            {key: value for key, value in bundle.items() if key not in excluded}
            for bundle in bundle_set["bundles"]
        ]

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
        declared_view = {
            "suggestions": {
                "capabilities": [
                    "context.core@1",
                    "context.dialogue@1",
                ],
                "dimensions": ["suggestions"],
                "limits": {"max_runtime_examples": 2},
            }
        }
        self.assertEqual(off["module_context_views"], {})
        self.assertNotIn("shadow_module_context_views", off)
        self.assertEqual(shadow["module_context_views"], {})
        self.assertEqual(shadow["shadow_module_context_views"], declared_view)
        self.assertEqual(enforce["module_context_views"], declared_view)
        self.assertNotIn("shadow_module_context_views", enforce)
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
        enforce_payload = state_revision_payload(enforce)
        self.assertNotIn("shadow_context", off_payload["segments"][0])
        self.assertNotIn("shadow_context", shadow_payload["segments"][0])
        self.assertEqual(shadow_payload["module_context_views"], {})
        self.assertEqual(
            enforce_payload["module_context_views"], declared_view
        )
        self.assertEqual(
            enforce_payload["profile_digest"], enforce["profile_digest"]
        )
        changed_view_state = json.loads(json.dumps(enforce))
        changed_view_state["module_context_views"]["suggestions"]["limits"][
            "max_runtime_examples"
        ] = 1
        self.assertNotEqual(
            state_fingerprint(enforce), state_fingerprint(changed_view_state)
        )
        self.assertNotEqual(
            enforce["segments"][0]["module_review_equivalence_keys"]["accuracy"],
            enforce["segments"][1]["module_review_equivalence_keys"]["accuracy"],
        )

        off_bundle, off_manifest = self.worker_view(off)
        shadow_bundle, shadow_manifest = self.worker_view(shadow)
        enforce_bundle, enforce_manifest = self.worker_view(enforce)

        self.assertEqual(off_bundle["capabilities"], shadow_bundle["capabilities"])
        self.assertEqual(
            set(shadow_bundle["capabilities"]["enabled"]),
            {"context.core@1", "source_provenance@1"},
        )
        self.assertEqual(shadow_bundle["capabilities"]["disabled"], [])
        self.assertEqual(
            off_bundle["shared_context_assets"],
            shadow_bundle["shared_context_assets"],
        )
        self.assertEqual(
            self.formal_bundles(off_bundle),
            self.formal_bundles(shadow_bundle),
        )
        self.assertNotEqual(
            off_bundle["context_bundle_set_digest"],
            shadow_bundle["context_bundle_set_digest"],
        )

        self.assertEqual(
            off_manifest["project_assets"],
            shadow_manifest["project_assets"],
        )
        self.assertEqual(
            off_manifest["language_providers"],
            shadow_manifest["language_providers"],
        )
        self.assertEqual(shadow_manifest["language_providers"], [])
        self.assertNotIn(
            "entities",
            {item["asset_id"] for item in shadow_manifest["project_assets"]},
        )
        shadow_loaded = load_project_context_assets(shadow)
        self.assertEqual(
            set(shadow_loaded["review_examples"]),
            {"example.core.primary", "example.core.secondary"},
        )
        forced_core_view = {
            "capabilities": ["context.core@1"],
            "dimensions": ["suggestions"],
            "limits": {"max_runtime_examples": 2},
        }
        shadow_forced = build_context_bundle_set(
            shadow,
            shadow["segments"],
            "suggestions",
            loaded_assets=shadow_loaded,
            module_view=forced_core_view,
        )
        self.assertEqual(
            shadow_forced["shared_context_assets"]["review_examples"], {}
        )
        self.assertTrue(
            all(not item["runtime_example_ids"] for item in shadow_forced["bundles"])
        )
        shadow_forced_manifest = build_worker_context_manifest(
            shadow,
            "suggestions",
            shadow_forced,
            max_worker_bytes=100_000,
        )
        self.assertEqual(
            shadow_forced_manifest["shared_context_assets"]["counts"][
                "review_examples"
            ],
            0,
        )
        shadow_with_forced_asset_view = json.loads(json.dumps(shadow))
        shadow_with_forced_asset_view["module_context_views"] = {
            "suggestions": {
                "capabilities": [
                    "context.core@1",
                    "assets.entity_registry@1",
                ]
            }
        }
        with self.assertRaisesRegex(ContextBundleError, "inactive capabilities"):
            self.worker_view(shadow_with_forced_asset_view)

        self.assertIn(
            "assets.entity_registry@1",
            enforce_bundle["capabilities"]["enabled"],
        )
        self.assertEqual(
            set(enforce_bundle["shared_context_assets"]["review_examples"]),
            {"example.core.primary", "example.core.secondary"},
        )
        self.assertTrue(
            all(
                set(item["runtime_example_ids"])
                == {"example.core.primary", "example.core.secondary"}
                for item in enforce_bundle["bundles"]
            )
        )
        self.assertIn(
            "entities",
            {item["asset_id"] for item in enforce_manifest["project_assets"]},
        )
        self.assertTrue(
            {"examples_primary", "examples_secondary"}.issubset(
                {
                    item["asset_id"]
                    for item in enforce_manifest["project_assets"]
                }
            )
        )
        self.assertEqual(
            [item["id"] for item in enforce_manifest["language_providers"]],
            ["ko.register"],
        )

    def test_explicit_shadow_columns_are_shadow_only(self):
        _, shadow = self.read_mode("shadow", explicit_context=True)
        _, enforce = self.read_mode("enforce", explicit_context=True)
        off_job, off_result = self.run_read_mode("off", explicit_context=True)

        shadow_segment = shadow["segments"][0]
        self.assertNotIn("dialogue", shadow_segment["context"]["extensions"])
        self.assertEqual(
            shadow_segment["shadow_context"]["extensions"]["dialogue"],
            {
                "status": "ready",
                "speaker_id": "speaker-a",
                "addressee_ids": ["listener-a"],
            },
        )
        self.assertEqual(
            shadow["context_cols"]["context.core@1.content_type"]["method"],
            "cli",
        )
        self.assertNotIn(
            "context.dialogue@1.speaker_id",
            shadow["context_cols"],
        )
        shadow_source_manifest = json.loads(
            Path(shadow["tabular_source_manifest_path"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            shadow_source_manifest["shadow_context_columns"][
                "context.dialogue@1.speaker_id"
            ]["method"],
            "cli",
        )

        enforce_dialogue = enforce["segments"][0]["context"]["extensions"][
            "dialogue"
        ]
        self.assertEqual(enforce_dialogue["speaker_id"], "speaker-a")
        self.assertEqual(enforce_dialogue["addressee_ids"], ["listener-a"])
        self.assertEqual(
            enforce["context_cols"]["context.dialogue@1.speaker_id"]["method"],
            "cli",
        )

        self.assertNotEqual(off_result.returncode, 0)
        self.assertIn(
            "unknown context field: speaker_id",
            off_result.stdout + off_result.stderr,
        )
        self.assertFalse((off_job / "state.json").exists())

    def test_speaker_without_content_type_triggers_only_the_negotiated_projection(self):
        with self.input_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["Key", "Source", "Target", "Content Type", "Speaker", "Addressee"]
            )
            writer.writerow(
                ["business-a", "台词一", "대사 1", "", "speaker-a", "listener-a"]
            )
            writer.writerow(
                ["business-b", "台词二", "대사 2", "", "", ""]
            )

        _, shadow = self.read_mode("shadow")
        _, enforce = self.read_mode("enforce")

        self.assertNotIn(
            "dialogue", shadow["segments"][0]["context"]["extensions"]
        )
        self.assertEqual(
            shadow["segments"][0]["shadow_context"]["extensions"]["dialogue"][
                "status"
            ],
            "ready",
        )
        self.assertEqual(
            enforce["segments"][0]["context"]["extensions"]["dialogue"][
                "status"
            ],
            "ready",
        )
        self.assertEqual(
            enforce["segments"][1]["context"]["extensions"]["dialogue"],
            {"status": "not_applicable"},
        )


if __name__ == "__main__":
    unittest.main()
