from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lqe_context import builtin_descriptor_registry, extract_segment_context
from lqe_context_overrides import (
    ContextOverrideError,
    apply_job_context_overrides,
    build_context_gap_report,
    build_context_override_scaffold,
    segment_set_digest,
)
from lqe_profile_ingest import canonical_digest
from lqe_split_contract import state_fingerprint
import lqe_io


def source_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def runtime_registry() -> dict:
    registry = builtin_descriptor_registry()
    registry = {
        key: value
        for key, value in registry.items()
        if key in {"context.core@1", "context.dialogue@1"}
    }
    registry["context.dialogue@1"] = deepcopy(registry["context.dialogue@1"])
    registry["context.dialogue@1"]["applies_when"] = {
        "content_type": ["dialogue"]
    }
    return registry


def blank_segment(key: str = "line-1", source: str = "马上检查设备！") -> dict:
    registry = runtime_registry()
    context = extract_segment_context([], {}, registry)
    return {
        "id": 0,
        "segment_key": key,
        "key_origin": "input",
        "source": source,
        "source_digest": source_digest(source),
        "target": "지금 기기를 확인해요.",
        "context": context,
        "context_status": context["status"],
        "context_missing_required": context["missing_required"],
    }


def verified_sidecar(segments: list[dict]) -> dict:
    segment = segments[0]
    return {
        "schema": "lqe.job-context-overrides",
        "version": 1,
        "segment_set_digest": segment_set_digest(segments),
        "authority_source": {
            "source_id": "pm.context-confirmation.20260814",
            "kind": "human",
            "issuer": "localization_pm",
        },
        "entries": [
            {
                "segment_key": segment["segment_key"],
                "source_digest": segment["source_digest"],
                "verification_status": "verified",
                "context_patch": {
                    "context.core@1.content_type": "dialogue",
                    "context.dialogue@1.speaker_id": "entity.test.commander",
                },
                "provenance": {"record_id": "ctx-line-1"},
            }
        ],
    }


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


class ContextOverrideRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_verified_override_updates_formal_context_and_fingerprint(self):
        segments = [blank_segment()]
        sidecar = verified_sidecar(segments)
        original = deepcopy(segments)
        result = apply_job_context_overrides(
            sidecar,
            segments=segments,
            registry=runtime_registry(),
            capability_resolution_digest="a" * 64,
            source_file_sha256="b" * 64,
        )
        self.assertEqual(segments, original)
        updated = result["segments"][0]
        self.assertEqual(updated["context"]["core"]["content_type"], "dialogue")
        self.assertEqual(
            updated["context"]["extensions"]["dialogue"]["speaker_id"],
            "entity.test.commander",
        )
        self.assertEqual(updated["context_status"], "ready")
        evidence = updated["context"]["provenance"][
            "context.extensions.dialogue.speaker_id"
        ]
        self.assertEqual(evidence["method"], "human_sidecar")
        self.assertEqual(evidence["status"], "verified")
        self.assertEqual(result["audit"]["patched_fields"], 2)
        self.assertRegex(result["audit"]["fingerprint"], r"^[0-9a-f]{64}$")

        speaker_only = verified_sidecar(segments)
        speaker_only["entries"][0]["context_patch"].pop(
            "context.core@1.content_type"
        )
        activated = apply_job_context_overrides(
            speaker_only,
            segments=segments,
            registry=runtime_registry(),
            capability_resolution_digest="a" * 64,
        )
        self.assertEqual(activated["segments"][0]["context_status"], "ready")

    def test_drift_shadow_bypass_missing_and_conflicts_fail_closed(self):
        segments = [blank_segment()]
        stale = verified_sidecar(segments)
        stale["entries"][0]["source_digest"] = "0" * 64
        with self.assertRaisesRegex(ContextOverrideError, "source_digest is stale"):
            apply_job_context_overrides(
                stale,
                segments=segments,
                registry=runtime_registry(),
                capability_resolution_digest="a" * 64,
            )

        duplicate = verified_sidecar(segments)
        duplicate["entries"].append(deepcopy(duplicate["entries"][0]))
        with self.assertRaisesRegex(ContextOverrideError, "duplicate.*segment_key"):
            apply_job_context_overrides(
                duplicate,
                segments=segments,
                registry=runtime_registry(),
                capability_resolution_digest="a" * 64,
            )

        unverified = verified_sidecar(segments)
        unverified["entries"][0]["verification_status"] = "pending"
        with self.assertRaisesRegex(ContextOverrideError, "not verified"):
            apply_job_context_overrides(
                unverified,
                segments=segments,
                registry=runtime_registry(),
                capability_resolution_digest="a" * 64,
            )

        shadow_bypass = verified_sidecar(segments)
        with self.assertRaisesRegex(ContextOverrideError, "unknown context field"):
            apply_job_context_overrides(
                shadow_bypass,
                segments=segments,
                registry={"context.core@1": runtime_registry()["context.core@1"]},
                capability_resolution_digest="a" * 64,
            )

        missing = verified_sidecar(segments)
        missing["entries"][0]["context_patch"].pop(
            "context.dialogue@1.speaker_id"
        )
        with self.assertRaisesRegex(ContextOverrideError, "required fields missing"):
            apply_job_context_overrides(
                missing,
                segments=segments,
                registry=runtime_registry(),
                capability_resolution_digest="a" * 64,
            )

        conflict_segments = [blank_segment()]
        conflict_segments[0]["context"]["core"]["content_type"] = "dialogue"
        conflict = verified_sidecar(conflict_segments)
        conflict["segment_set_digest"] = segment_set_digest(conflict_segments)
        with self.assertRaisesRegex(ContextOverrideError, "existing field"):
            apply_job_context_overrides(
                conflict,
                segments=conflict_segments,
                registry=runtime_registry(),
                capability_resolution_digest="a" * 64,
            )

        status_conflict = [blank_segment()]
        status_conflict[0]["context"]["extensions"]["dialogue"][
            "status"
        ] = "conflict"
        sidecar = verified_sidecar(status_conflict)
        with self.assertRaisesRegex(ContextOverrideError, "existing status 'conflict'"):
            apply_job_context_overrides(
                sidecar,
                segments=status_conflict,
                registry=runtime_registry(),
                capability_resolution_digest="a" * 64,
            )

        alias_segments = [blank_segment()]
        alias_segments[0]["context"]["core"]["content_type"] = "dialogue"
        alias_segments[0]["context"]["extensions"]["dialogue"].update(
            {"status": "ready", "speaker_id": "Alex"}
        )
        resolved = verified_sidecar(alias_segments)
        resolved["segment_set_digest"] = segment_set_digest(alias_segments)
        resolved["entries"][0]["context_patch"] = {
            "context.dialogue@1.speaker_id": "entity.alex.one"
        }
        resolved["entries"][0]["expected_context"] = {
            "context.dialogue@1.speaker_id": "Alex"
        }
        applied = apply_job_context_overrides(
            resolved,
            segments=alias_segments,
            registry=runtime_registry(),
            capability_resolution_digest="a" * 64,
        )
        self.assertEqual(
            applied["segments"][0]["context"]["extensions"]["dialogue"][
                "speaker_id"
            ],
            "entity.alex.one",
        )
        self.assertEqual(applied["audit"]["replaced_fields"], 1)
        stale_expected = deepcopy(resolved)
        stale_expected["entries"][0]["expected_context"][
            "context.dialogue@1.speaker_id"
        ] = "Different"
        with self.assertRaisesRegex(ContextOverrideError, "expected value is stale"):
            apply_job_context_overrides(
                stale_expected,
                segments=alias_segments,
                registry=runtime_registry(),
                capability_resolution_digest="a" * 64,
            )

    def test_gap_report_distinguishes_alias_states(self):
        entities = {
            "schema": "lqe.entities",
            "version": 1,
            "entities": [
                {
                    "id": "entity.alex.one",
                    "entity_type": "character",
                    "names": {"source": ["Alex"], "target": []},
                    "tags": [],
                    "facts": [],
                },
                {
                    "id": "entity.alex.two",
                    "entity_type": "character",
                    "names": {"source": ["Alex"], "target": []},
                    "tags": [],
                    "facts": [],
                },
            ],
            "relations": [],
        }
        entity_path = self.root / "entities.json"
        entity_path.write_text(json.dumps(entities), encoding="utf-8")
        ambiguous = blank_segment("ambiguous")
        ambiguous["context"]["core"]["content_type"] = "dialogue"
        ambiguous["context"]["extensions"]["dialogue"].update(
            {"status": "ready", "speaker_id": "Alex"}
        )
        unresolved = blank_segment("unresolved")
        unresolved["context"]["core"]["content_type"] = "dialogue"
        unresolved["context"]["extensions"]["dialogue"].update(
            {"status": "ready", "speaker_id": "Nobody"}
        )
        absent = blank_segment("absent")
        not_applicable = blank_segment("not-applicable")
        not_applicable["context"]["core"]["content_type"] = "ui"
        state = {
            "job_runtime_contract_version": 2,
            "resolved_context_descriptors": runtime_registry(),
            "shadow_context_descriptors": None,
            "project_asset_snapshot": {
                "assets": {
                    "entities": {
                        "kind": "entity_registry",
                        "status": "present",
                        "sha256": hashlib.sha256(entity_path.read_bytes()).hexdigest(),
                    }
                }
            },
            "project_asset_paths": {"entities": str(entity_path)},
            "segments": [ambiguous, unresolved, absent, not_applicable],
        }
        report = build_context_gap_report(state)
        by_key = {
            row["segment_key"]: {
                field["field_ref"]: field for field in row["fields"]
            }
            for row in report["segments"]
        }
        self.assertEqual(
            by_key["ambiguous"]["context.dialogue@1.speaker_id"]["status"],
            "ambiguous_alias",
        )
        self.assertEqual(
            by_key["unresolved"]["context.dialogue@1.speaker_id"]["status"],
            "unresolved_alias",
        )
        self.assertEqual(
            by_key["absent"]["context.core@1.content_type"]["status"],
            "not_provided",
        )
        self.assertGreater(report["summary"]["not_applicable"], 0)
        scaffold = build_context_override_scaffold(
            state, source_id="pm.context", issuer="pm"
        )
        ambiguous_entry = next(
            entry
            for entry in scaffold["entries"]
            if entry["segment_key"] == "ambiguous"
        )
        self.assertEqual(
            ambiguous_entry["expected_context"][
                "context.dialogue@1.speaker_id"
            ],
            "Alex",
        )

    def test_scaffold_never_invents_unknown_context_and_rejects_old_job(self):
        segment = blank_segment()
        segment["shadow_context"] = segment["context"]
        formal_registry = {"context.core@1": runtime_registry()["context.core@1"]}
        segment["context"] = extract_segment_context([], {}, formal_registry)
        state = {
            "job_runtime_contract_version": 2,
            "resolved_context_descriptors": formal_registry,
            "shadow_context_descriptors": runtime_registry(),
            "segments": [segment],
        }
        scaffold = build_context_override_scaffold(state)
        self.assertEqual(scaffold["entries"][0]["verification_status"], "pending")
        self.assertTrue(
            all(
                value is None
                for value in scaffold["entries"][0]["context_patch"].values()
            )
        )
        self.assertEqual(
            set(scaffold["entries"][0]["context_patch"]),
            {
                "context.core@1.content_type",
                "context.dialogue@1.speaker_id",
                "context.dialogue@1.addressee_ids",
                "context.dialogue@1.scene_id",
                "context.dialogue@1.relationship_stage",
                "context.dialogue@1.scene_tone",
            },
        )
        with self.assertRaisesRegex(ContextOverrideError, "job_runtime_contract_version 2"):
            build_context_gap_report({**state, "job_runtime_contract_version": 1})

    def _write_profile(self) -> Path:
        project = self.root / "profile"
        project.mkdir()
        (project / "checks.json").write_text("{}", encoding="utf-8")
        (project / "confirmed_rules.md").write_text("# Rules\n", encoding="utf-8")
        profile = {
            "profile_contract_version": 2,
            "name": "override-test/zh-ko",
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
            },
            "context_pipeline": {"mode": "enforce"},
            "module_context_views": {
                "accuracy": {
                    "capabilities": ["context.core@1", "context.dialogue@1"],
                    "dimensions": ["accuracy"],
                },
                "naturalness": {
                    "capabilities": ["context.core@1", "context.dialogue@1"],
                    "dimensions": ["naturalness"],
                },
                "suggestions": {
                    "capabilities": ["context.core@1", "context.dialogue@1"],
                    "dimensions": ["suggestions"],
                },
            },
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
        }
        (project / "profile.json").write_text(
            json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return project

    def test_read_flag_publishes_bound_sidecar_manifest_and_is_atomic(self):
        project = self._write_profile()
        input_path = self.root / "input.csv"
        with input_path.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(
                [
                    ["Key", "Source", "Target"],
                    ["line-1", "马上检查设备！", "지금 기기를 확인해요."],
                ]
            )
        segments = [blank_segment()]
        sidecar = verified_sidecar(segments)
        sidecar_path = self.root / "context.json"
        sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
        original_input = input_path.read_bytes()
        original_sidecar = sidecar_path.read_bytes()
        job = self.root / "job"
        command = [
            sys.executable,
            str(SCRIPTS / "lqe_io.py"),
            "read",
            "--input",
            str(input_path),
            "--project",
            str(project),
            "--source-col",
            "Source",
            "--target-col",
            "Target",
            "--key-col",
            "Key",
            "--context-overrides",
            str(sidecar_path),
            "--no-terminology",
            "--out",
            str(job / "state.json"),
        ]
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads((job / "state.json").read_text(encoding="utf-8"))
        manifest = json.loads(
            (job / "tabular_source_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            state["segments"][0]["context"]["extensions"]["dialogue"]["speaker_id"],
            "entity.test.commander",
        )
        self.assertEqual(
            state["context_overrides_digest"],
            canonical_digest(sidecar),
        )
        self.assertEqual(
            manifest["context_overrides"]["fingerprint"],
            state["context_overrides_fingerprint"],
        )
        self.assertTrue((job / "context_overrides.json").is_file())
        self.assertTrue((job / "context_gap_report.json").is_file())
        fingerprint = state_fingerprint(state)
        (job / "context_overrides.json").write_text("{}", encoding="utf-8")
        self.assertNotEqual(state_fingerprint(state), fingerprint)
        self.assertEqual(input_path.read_bytes(), original_input)
        self.assertEqual(sidecar_path.read_bytes(), original_sidecar)

        failed_job = self.root / "failed-job"
        invalid = deepcopy(sidecar)
        invalid["entries"][0]["context_patch"].pop(
            "context.dialogue@1.speaker_id"
        )
        sidecar_path.write_text(json.dumps(invalid), encoding="utf-8")
        failed_command = list(command)
        failed_command[-1] = str(failed_job / "state.json")
        failed = subprocess.run(
            failed_command, cwd=ROOT, text=True, capture_output=True
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("required fields missing", failed.stdout + failed.stderr)
        self.assertFalse((failed_job / "state.json").exists())
        self.assertFalse((failed_job / "scope.json").exists())
        if failed_job.exists():
            self.assertEqual(list(failed_job.iterdir()), [])

    def test_verified_job_override_is_applied_before_language_rules(self):
        project = self._write_profile()
        profile_path = project / "profile.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        context_rules = {
            "schema": "lqe.context-rules",
            "version": 1,
            "authority_rank": ["test"],
            "rules": [
                {
                    "id": "rule.test.hostile_dialogue_plain",
                    "capability": "language.register",
                    "provider": {"id": "ko.register", "api_version": 1},
                    "target_lang": "ko",
                    "rule_status": "confirmed",
                    "priority": 100,
                    "authority": {"issuer": "test"},
                    "valid_from": None,
                    "valid_until": None,
                    "when": {
                        "content_type": ["dialogue"],
                        "scene_tone": ["hostile"],
                    },
                    "expect": {
                        "politeness": ["plain"],
                        "ending_families": ["hae", "haera"],
                        "forbidden_families": ["haeyo", "hapsyo"],
                    },
                    "provenance": {"source_id": "pm.context"},
                }
            ],
        }
        (project / "context_rules.json").write_text(
            json.dumps(context_rules, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        profile["assets"]["context_rules"] = {
            **asset("context_rules", "context_rules.json"),
            "media_type": "application/json",
            "content_schema": "lqe.context-rules",
        }
        profile["capabilities"]["language_policy.register@1"] = {
            "required": True,
            "provider": "ko.register@1",
            "asset": "context_rules",
        }
        for module in ("naturalness", "suggestions"):
            profile["module_context_views"][module]["capabilities"].append(
                "language_policy.register@1"
            )
            profile["module_context_views"][module]["constraint_kinds"] = [
                "language.register"
            ]
        profile_path.write_text(
            json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        input_path = self.root / "policy-input.csv"
        with input_path.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(
                [
                    ["Key", "Source", "Target"],
                    ["line-1", "马上离开！", "지금 떠나 주세요!"],
                ]
            )
        segments = [blank_segment(source="马上离开！")]
        sidecar = verified_sidecar(segments)
        sidecar["entries"][0]["context_patch"][
            "context.dialogue@1.scene_tone"
        ] = "hostile"
        sidecar_path = self.root / "policy-context.json"
        sidecar_path.write_text(
            json.dumps(sidecar, ensure_ascii=False), encoding="utf-8"
        )
        job = self.root / "policy-job"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "lqe_io.py"),
                "read",
                "--input",
                str(input_path),
                "--project",
                str(project),
                "--source-col",
                "Source",
                "--target-col",
                "Target",
                "--key-col",
                "Key",
                "--context-overrides",
                str(sidecar_path),
                "--no-terminology",
                "--out",
                str(job / "state.json"),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads((job / "state.json").read_text(encoding="utf-8"))
        constraint = state["segments"][0]["resolved_constraints"][0]
        self.assertEqual(constraint["status"], "resolved")
        self.assertEqual(
            constraint["rule_ids"], ["rule.test.hostile_dialogue_plain"]
        )
        self.assertEqual(
            constraint["runtime_evaluation"]["status"], "mismatch"
        )

    def test_project_then_job_override_then_language_rule_phase_order(self):
        segment = blank_segment(source="马上离开！")
        registry = runtime_registry()
        common = {
            "context_pipeline": {"mode": "enforce"},
            "target_lang": "ko",
            "created_at": "2026-08-14T00:00:00Z",
            "capability_resolution_digest": "a" * 64,
            "capability_resolution": {
                "enabled": {
                    "source_provenance@1": {"effect": "foundation"},
                    "context.dialogue@1": {
                        "effect": "enforce",
                        "asset": "project_overrides",
                    },
                    "language_policy.register@1": {
                        "effect": "enforce",
                        "asset": "rules",
                        "provider": {"id": "ko.register", "api_version": 1},
                    },
                }
            },
            "project_asset_snapshot": {
                "assets": {
                    "source_manifest": {
                        "kind": "project_source_manifest",
                        "status": "present",
                    },
                    "project_overrides": {
                        "kind": "segment_context_overrides",
                        "status": "present",
                    },
                    "rules": {"kind": "context_rules", "status": "present"},
                }
            },
            "project_asset_paths": {
                "source_manifest": str(self.root / "project_sources.json"),
                "project_overrides": str(self.root / "project_overrides.json"),
                "rules": str(self.root / "context_rules.json"),
            },
        }
        project_document = {"project": "override"}
        rules_document = {"rules": "register"}

        def apply_project_override(document, *, segments, **_kwargs):
            self.assertIs(document, project_document)
            updated = deepcopy(segments)
            updated[0]["context"]["core"]["content_type"] = "dialogue"
            dialogue = updated[0]["context"]["extensions"]["dialogue"]
            dialogue.update(
                {
                    "status": "ready",
                    "speaker_id": "entity.test.commander",
                    "scene_tone": "neutral",
                }
            )
            return updated

        evaluated_contexts = []

        def evaluate_rule(_rules, context, _target, **_kwargs):
            evaluated_contexts.append(deepcopy(context))
            return {
                "status": "match",
                "reason_codes": [],
                "observation": {},
                "evaluation": {},
                "evaluation_digest": "b" * 64,
                "constraint": {
                    "status": "resolved",
                    "rule_ids": ["rule.test.phase_order"],
                },
            }

        def read_asset(_path, *, label):
            if label == "segment_context_overrides":
                return project_document
            if label == "context_rules":
                return rules_document
            raise AssertionError(label)

        with (
            patch.object(
                lqe_io,
                "load_project_source_manifest",
                return_value={
                    "sources": [{"id": "source.context"}],
                    "manifest_digest": "c" * 64,
                },
            ),
            patch.object(lqe_io, "_read_json_asset", side_effect=read_asset),
            patch.object(
                lqe_io,
                "apply_segment_context_overrides",
                side_effect=apply_project_override,
            ),
            patch.object(
                lqe_io,
                "validate_context_rules",
                return_value=rules_document,
            ),
            patch.object(
                lqe_io,
                "evaluate_language_policy",
                side_effect=evaluate_rule,
            ),
        ):
            segments = lqe_io._apply_runtime_project_context(
                [segment],
                common,
                {"profile": True},
                registry,
                phase="project_overrides",
            )
            self.assertEqual(
                segments[0]["context"]["extensions"]["dialogue"]["scene_tone"],
                "neutral",
            )

            job_sidecar = {
                "schema": "lqe.job-context-overrides",
                "version": 1,
                "segment_set_digest": segment_set_digest(segments),
                "authority_source": {
                    "source_id": "pm.context-confirmation.20260814",
                    "kind": "human",
                    "issuer": "localization_pm",
                },
                "entries": [
                    {
                        "segment_key": segments[0]["segment_key"],
                        "source_digest": segments[0]["source_digest"],
                        "verification_status": "verified",
                        "context_patch": {
                            "context.dialogue@1.scene_tone": "hostile"
                        },
                        "expected_context": {
                            "context.dialogue@1.scene_tone": "neutral"
                        },
                        "provenance": {"record_id": "job-override"},
                    }
                ],
            }
            segments = apply_job_context_overrides(
                job_sidecar,
                segments=segments,
                registry=registry,
                capability_resolution_digest="a" * 64,
            )["segments"]
            segments = lqe_io._apply_runtime_project_context(
                segments,
                common,
                {"profile": True},
                registry,
                phase="rules",
            )

        self.assertEqual(
            evaluated_contexts[0]["extensions"]["dialogue"]["scene_tone"],
            "hostile",
        )
        self.assertEqual(
            segments[0]["resolved_constraints"][0]["rule_ids"],
            ["rule.test.phase_order"],
        )

    def test_sdlxliff_read_accepts_verified_foundation_override(self):
        fixture = ROOT / "tests/fixtures/sdlxliff/multi_segment.sdlxliff"
        baseline_job = self.root / "sdl-baseline"
        base_command = [
            sys.executable,
            str(SCRIPTS / "lqe_io.py"),
            "read",
            "--input",
            str(fixture),
            "--input-format",
            "sdlxliff",
            "--source-lang",
            "zh",
            "--target-lang",
            "en",
            "--no-terminology",
        ]
        baseline = subprocess.run(
            [*base_command, "--out", str(baseline_job / "state.json")],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)
        baseline_state = json.loads(
            (baseline_job / "state.json").read_text(encoding="utf-8")
        )
        scaffold = build_context_override_scaffold(
            baseline_state,
            source_id="pm.context-confirmation.sdl",
            issuer="localization_pm",
        )
        scaffold["entries"] = [scaffold["entries"][0]]
        scaffold["entries"][0]["verification_status"] = "verified"
        scaffold["entries"][0]["context_patch"] = {
            "context.core@1.content_type": "dialogue"
        }
        scaffold["entries"][0]["provenance"]["record_id"] = "ctx-sdl-1"
        sidecar = self.root / "sdl-context.json"
        sidecar.write_text(json.dumps(scaffold), encoding="utf-8")
        job = self.root / "sdl-job"
        result = subprocess.run(
            [
                *base_command,
                "--context-overrides",
                str(sidecar),
                "--out",
                str(job / "state.json"),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads((job / "state.json").read_text(encoding="utf-8"))
        manifest = json.loads(
            (job / "source_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            state["segments"][0]["context"]["core"]["content_type"],
            "dialogue",
        )
        self.assertEqual(
            manifest["context_overrides"]["fingerprint"],
            state["context_overrides_fingerprint"],
        )


if __name__ == "__main__":
    unittest.main()
