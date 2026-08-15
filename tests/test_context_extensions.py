import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lqe_context import (
    ContextContractError,
    builtin_descriptor_registry,
    canonical_field_ref,
    descriptor_registry,
    extract_segment_context,
    module_review_equivalence_key,
    module_review_equivalence_payload,
    parse_context_columns,
    project_context_for_module,
    project_segment_for_module,
    resolve_context_columns,
    validate_capability_descriptor,
)
from lqe_engine import build_check_scope


def product_descriptor():
    return {
        "schema": "lqe.context-capability-descriptor",
        "version": 1,
        "id": "context.product@1",
        "applies_when": {"content_type": ["interface"]},
        "fields": {
            "build_variant": {
                "type": "string",
                "columns": ["Build Variant"],
                "normalizer": "lowercase",
                "required_when_applicable": True,
                "affects_review_equivalence": True,
            },
            "release_track": {
                "type": "enum",
                "values": ["live", "preview"],
                "columns": ["Release Track"],
                "normalizer": "lowercase",
                "required_when_applicable": False,
                "affects_review_equivalence": True,
            },
            "tracking_token": {
                "type": "string",
                "columns": ["Tracking Token"],
                "normalizer": "trim",
                "required_when_applicable": False,
                "affects_review_equivalence": False,
            },
        },
        "module_views": {
            "accuracy": ["build_variant", "release_track", "tracking_token"],
            "grammar": ["build_variant"],
            "suggestions": ["build_variant", "release_track"],
        },
        "comparison_rules": {"mode": "exact"},
        "window_rules": {"before": 1, "after": 1},
    }


def profile():
    custom = product_descriptor()
    return {
        "capability_descriptors": {custom["id"]: custom},
        "capabilities": {
            "context.core@1": {
                "required": True,
                "config": {
                    "columns": {
                        "content_type": ["Text Class"],
                        "context_note": ["Context Note"],
                    }
                },
            },
            "context.dialogue@1": {
                "required": False,
                "config": {
                    "columns": {
                        "speaker_id": ["Voice"],
                        "addressee_ids": ["Recipients"],
                    },
                    "applies_when": {"content_type": ["interface"]},
                },
            },
            "context.product@1": {"required": False, "config": {}},
        },
        "context_schema": {"required_columns": ["content_type"]},
    }


class DescriptorContractTests(unittest.TestCase):
    def test_builtin_registry_has_versioned_project_neutral_context_descriptors(self):
        registry = builtin_descriptor_registry()
        self.assertEqual(
            set(registry),
            {
                "context.core@1",
                "context.dialogue@1",
                "context.ui@1",
                "context.marketing@1",
            },
        )
        serialized = repr(registry).casefold()
        for forbidden in ("mhg", "korean", "edgar"):
            self.assertNotIn(forbidden, serialized)

    def test_profile_registers_only_declarative_custom_descriptor(self):
        registry = descriptor_registry(profile())
        self.assertIn("context.product@1", registry)
        self.assertEqual(
            registry["context.product@1"]["window_rules"],
            {"before": 1, "after": 1},
        )
        unsafe = product_descriptor()
        unsafe["script"] = "custom.py"
        with self.assertRaisesRegex(ContextContractError, "unknown fields"):
            validate_capability_descriptor(unsafe)
        unsafe = product_descriptor()
        unsafe["comparison_rules"] = {"python": "module:function"}
        with self.assertRaisesRegex(ContextContractError, "forbidden executable"):
            validate_capability_descriptor(unsafe)

    def test_capability_resolution_is_the_explicit_enablement_boundary(self):
        resolution = {
            "enabled": {
                "context.core@1": {},
                "context.product@1": {},
                "source_provenance@1": {},
            }
        }
        registry = descriptor_registry(
            profile(), capability_resolution=resolution
        )
        self.assertEqual(
            set(registry), {"context.core@1", "context.product@1"}
        )
        with self.assertRaisesRegex(ContextContractError, "missing context.core@1"):
            descriptor_registry(profile(), capability_resolution={"enabled": {}})
        with self.assertRaisesRegex(ContextContractError, "no descriptor"):
            descriptor_registry(
                profile(),
                capability_resolution={
                    "enabled": {
                        "context.core@1": {},
                        "context.unknown@1": {},
                    }
                },
            )

    def test_custom_descriptor_cannot_replace_builtin(self):
        custom = product_descriptor()
        custom["id"] = "context.core@1"
        with self.assertRaisesRegex(ContextContractError, "cannot replace built-in"):
            descriptor_registry({"capability_descriptors": [custom]})

    def test_custom_descriptor_cannot_shadow_extension_metadata(self):
        for field_name in (
            "status",
            "capability_id",
            "descriptor_digest",
            "segment_key",
            "provenance",
            "input_status",
        ):
            with self.subTest(field_name=field_name):
                custom = product_descriptor()
                custom["fields"][field_name] = {
                    "type": "string",
                    "columns": [],
                }
                with self.assertRaisesRegex(ContextContractError, "reserved fields"):
                    descriptor_registry({"capability_descriptors": [custom]})

    def test_unqualified_field_must_be_unique(self):
        custom = product_descriptor()
        custom["fields"]["platform"] = {
            "type": "string",
            "columns": [],
            "normalizer": "trim",
            "required_when_applicable": False,
            "affects_review_equivalence": True,
        }
        custom["module_views"]["accuracy"].append("platform")
        registry = descriptor_registry({"capability_descriptors": [custom]})
        with self.assertRaisesRegex(ContextContractError, "ambiguous"):
            canonical_field_ref("platform", registry)

    def test_identity_normalizer_preserves_string_value(self):
        descriptor = {
            "schema": "lqe.context-capability-descriptor",
            "version": 1,
            "id": "context.identity@1",
            "fields": {
                "raw_label": {
                    "type": "string",
                    "columns": ["Label"],
                    "normalizer": "identity",
                }
            },
            "module_views": {"accuracy": ["raw_label"]},
        }
        custom_profile = {
            "capability_descriptors": {descriptor["id"]: descriptor},
            "capabilities": {
                "context.core@1": {"required": True},
                "context.identity@1": {"required": False},
            },
        }
        registry = descriptor_registry(
            custom_profile,
            capability_resolution={
                "enabled": {
                    "context.core@1": {},
                    "context.identity@1": {},
                }
            },
        )
        resolved = resolve_context_columns(["Label"], registry)
        context = extract_segment_context([" padded "], resolved, registry)
        self.assertEqual(
            context["extensions"]["identity"]["raw_label"], " padded "
        )


class ColumnResolutionTests(unittest.TestCase):
    def setUp(self):
        self.profile = profile()
        self.registry = descriptor_registry(self.profile)
        self.headers = [
            "content_type",
            "Text Class",
            "Voice",
            "Voice Override",
            "Recipients",
            "Build Variant",
            "Release Track",
            "Tracking Token",
            "Context Note",
        ]

    def test_cli_overrides_profile_and_profile_overrides_safe_alias(self):
        resolved = resolve_context_columns(
            self.headers,
            self.registry,
            profile=self.profile,
            cli_columns=["speaker_id=Voice Override"],
        )
        self.assertEqual(
            resolved["context.dialogue@1.speaker_id"]["column_index"], 3
        )
        self.assertEqual(
            resolved["context.dialogue@1.speaker_id"]["method"], "cli"
        )
        self.assertEqual(
            resolved["context.core@1.content_type"]["column_index"], 1
        )
        self.assertEqual(
            resolved["context.core@1.content_type"]["method"], "profile"
        )
        self.assertEqual(
            resolved["context.product@1.build_variant"]["method"], "profile"
        )

    def test_safe_alias_requires_one_unique_header(self):
        resolved = resolve_context_columns(
            ["content_type"], builtin_descriptor_registry()
        )
        self.assertEqual(
            resolved["context.core@1.content_type"]["method"], "safe_alias"
        )
        with self.assertRaisesRegex(ContextContractError, "duplicate headers"):
            resolve_context_columns(
                ["speaker_id", "SPEAKER_ID"], builtin_descriptor_registry()
            )

    def test_multiple_profile_matches_and_shared_column_fail_closed(self):
        custom = product_descriptor()
        custom["fields"]["build_variant"]["columns"] = [
            "Build Variant",
            "Build Override",
        ]
        registry = descriptor_registry({"capability_descriptors": [custom]})
        with self.assertRaisesRegex(ContextContractError, "matches multiple columns"):
            resolve_context_columns(
                ["Build Variant", "Build Override"], registry
            )
        with self.assertRaisesRegex(ContextContractError, "multiple context fields"):
            resolve_context_columns(
                ["Shared"],
                builtin_descriptor_registry(),
                cli_columns=["content_type=Shared", "context_note=Shared"],
            )

    def test_explicit_cli_missing_and_duplicate_canonical_mapping_fail(self):
        with self.assertRaisesRegex(ContextContractError, "was not found"):
            resolve_context_columns(
                self.headers,
                self.registry,
                cli_columns=["speaker_id=Missing"],
            )
        with self.assertRaisesRegex(ContextContractError, "duplicate CLI mapping"):
            resolve_context_columns(
                self.headers,
                self.registry,
                cli_columns=[
                    "speaker_id=Voice",
                    "context.extensions.dialogue.speaker_id=Voice Override",
                ],
            )

    def test_context_col_parser_and_headerless_indices(self):
        self.assertEqual(
            parse_context_columns(["content_type=0", "speaker_id=2"]),
            {"content_type": "0", "speaker_id": "2"},
        )
        resolved = resolve_context_columns(
            [None, None, None],
            builtin_descriptor_registry(),
            cli_columns=["content_type=0", "speaker_id=2"],
            no_header=True,
        )
        self.assertEqual(
            resolved["context.core@1.content_type"]["column_index"], 0
        )
        self.assertEqual(
            resolved["context.dialogue@1.speaker_id"]["column_index"], 2
        )
        with self.assertRaisesRegex(ContextContractError, "exactly once"):
            parse_context_columns(["speaker_id"])


class ExtractionProjectionAndEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.profile = profile()
        self.registry = descriptor_registry(self.profile)
        self.headers = [
            "Text Class",
            "Voice",
            "Recipients",
            "Build Variant",
            "Release Track",
            "Tracking Token",
            "Context Note",
        ]
        self.resolved = resolve_context_columns(
            self.headers, self.registry, profile=self.profile
        )

    def context(
        self,
        *,
        speaker="Narrator",
        build="DESKTOP",
        token="audit-1",
        required_fields=None,
    ):
        return extract_segment_context(
            [
                "interface",
                speaker,
                "Reader, Reviewer",
                build,
                "live",
                token,
                "Short copy",
            ],
            self.resolved,
            self.registry,
            profile=self.profile,
            required_fields=required_fields,
            source_provenance={"container": "Primary", "row": 2},
        )

    def test_extraction_preserves_core_extension_status_and_provenance(self):
        context = self.context()
        self.assertEqual(context["status"], "ready")
        self.assertEqual(context["core"]["content_type"], "interface")
        self.assertEqual(context["extensions"]["dialogue"]["status"], "ready")
        self.assertEqual(
            context["extensions"]["dialogue"]["addressee_ids"],
            ["Reader", "Reviewer"],
        )
        self.assertEqual(
            context["extensions"]["product"]["build_variant"], "desktop"
        )
        self.assertEqual(context["extensions"]["ui"]["status"], "not_applicable")
        evidence = context["provenance"]["context.extensions.dialogue.speaker_id"]
        self.assertEqual(evidence["container"], "Primary")
        self.assertEqual(evidence["row"], 2)
        self.assertEqual(evidence["column"], "Voice")
        self.assertEqual(evidence["raw"], "Narrator")

    def test_provenance_raw_value_is_canonical_json(self):
        context = self.context(speaker=float("nan"))
        evidence = context["provenance"]["context.extensions.dialogue.speaker_id"]
        self.assertEqual(evidence["raw"], "nan")
        json.dumps(context, allow_nan=False)

    def test_only_applicable_extension_can_be_incomplete(self):
        context = self.context(build="")
        self.assertEqual(context["status"], "context_incomplete")
        self.assertEqual(context["extensions"]["product"]["status"], "incomplete")
        self.assertEqual(
            context["missing_required"],
            ["context.extensions.product.build_variant"],
        )
        self.assertEqual(context["extensions"]["ui"]["status"], "not_applicable")

    def test_missing_required_speaker_marks_only_dialogue_incomplete(self):
        context = self.context(speaker="")
        self.assertEqual(context["status"], "context_incomplete")
        self.assertEqual(context["extensions"]["dialogue"]["status"], "incomplete")
        self.assertEqual(context["extensions"]["product"]["status"], "ready")
        self.assertEqual(
            context["missing_required"],
            ["context.extensions.dialogue.speaker_id"],
        )

    def test_projection_uses_module_view_and_filters_provenance(self):
        context = self.context()
        accuracy = project_context_for_module(
            context, "accuracy", self.registry, include_provenance=True
        )
        self.assertEqual(
            accuracy["extensions"]["product"],
            {
                "status": "ready",
                "build_variant": "desktop",
                "release_track": "live",
                "tracking_token": "audit-1",
            },
        )
        self.assertIn(
            "context.extensions.product.release_track", accuracy["provenance"]
        )
        naturalness = project_context_for_module(
            context, "naturalness", self.registry
        )
        self.assertNotIn("product", naturalness["extensions"])

    def test_suggestion_segment_projection_receives_declared_context(self):
        segment = {
            "segment_key": "business-1",
            "input_status": "ready",
            "context": self.context(),
        }
        projection = project_segment_for_module(
            segment, "suggestions", self.registry
        )
        self.assertEqual(projection["segment_key"], "business-1")
        self.assertEqual(projection["core"]["content_type"], "interface")
        self.assertEqual(
            projection["extensions"]["dialogue"]["speaker_id"], "Narrator"
        )
        self.assertEqual(
            projection["extensions"]["product"]["build_variant"], "desktop"
        )

    def segment(self, *, key="business-1", speaker="Narrator", token="audit-1"):
        return {
            "id": 1,
            "segment_key": key,
            "source": "Source",
            "target": "Target",
            "input_status": "ready",
            "protected": False,
            "protected_texts": ["{0}"],
            "context": self.context(speaker=speaker, token=token),
            "resolved_constraints": [{"kind": "length", "limit": 20}],
        }

    def test_equivalence_excludes_business_key_and_provenance(self):
        first = self.segment(key="business-1")
        second = self.segment(key="business-2")
        second["context"]["provenance"][
            "context.extensions.dialogue.speaker_id"
        ]["row"] = 999
        self.assertEqual(
            module_review_equivalence_key(first, "accuracy", self.registry),
            module_review_equivalence_key(second, "accuracy", self.registry),
        )
        payload = module_review_equivalence_payload(first, "accuracy", self.registry)
        self.assertNotIn("segment_key", repr(payload))
        self.assertNotIn("provenance", repr(payload))

    def test_different_speaker_or_review_input_cannot_deduplicate(self):
        first = self.segment()
        changed_values = [
            self.segment(speaker="Guide"),
            {**self.segment(), "current_target": "New target"},
            {**self.segment(), "input_status": "blocked"},
            {**self.segment(), "protected": True},
            {
                **self.segment(),
                "resolved_constraints": [{"kind": "length", "limit": 12}],
            },
        ]
        for changed in changed_values:
            with self.subTest(changed=changed):
                self.assertNotEqual(
                    module_review_equivalence_key(first, "accuracy", self.registry),
                    module_review_equivalence_key(changed, "accuracy", self.registry),
                )
        self.assertEqual(
            module_review_equivalence_key(first, "precheck_review", self.registry),
            module_review_equivalence_key(
                self.segment(speaker="Guide"), "precheck_review", self.registry
            ),
        )

    def test_only_actual_equivalence_fields_affect_module_key(self):
        first = self.segment(token="audit-1")
        token_changed = self.segment(token="audit-2")
        self.assertEqual(
            module_review_equivalence_key(first, "accuracy", self.registry),
            module_review_equivalence_key(token_changed, "accuracy", self.registry),
        )
        build_changed = copy.deepcopy(first)
        build_changed["context"]["extensions"]["product"][
            "build_variant"
        ] = "mobile"
        self.assertNotEqual(
            module_review_equivalence_key(first, "accuracy", self.registry),
            module_review_equivalence_key(build_changed, "accuracy", self.registry),
        )
        self.assertEqual(
            module_review_equivalence_key(first, "naturalness", self.registry),
            module_review_equivalence_key(build_changed, "naturalness", self.registry),
        )

    def test_precheck_affects_only_modules_that_receive_it(self):
        segment = self.segment()
        first = [{"category": "Terminology", "problem": "a"}]
        second = [{"category": "Terminology", "problem": "b"}]
        self.assertNotEqual(
            module_review_equivalence_key(
                segment, "terminology", self.registry, precheck=first
            ),
            module_review_equivalence_key(
                segment, "terminology", self.registry, precheck=second
            ),
        )
        self.assertEqual(
            module_review_equivalence_key(
                segment, "accuracy", self.registry, precheck=first
            ),
            module_review_equivalence_key(
                segment, "accuracy", self.registry, precheck=second
            ),
        )


class SplitCompatibilityTests(unittest.TestCase):
    def test_split_never_deduplicates_different_builtin_speakers(self):
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            state = {
                "artifact_contract_version": 1,
                "job_runtime_contract_version": 2,
                "iteration": 0,
                "target_lang": "en",
                "check_scope": build_check_scope(True, "test"),
                "segments": [],
            }
            for segment_id, segment_key, speaker in (
                (0, "key-a", "speaker-a"),
                (1, "key-b", "speaker-b"),
                (2, "key-c", "speaker-a"),
            ):
                state["segments"].append(
                    {
                        "id": segment_id,
                        "segment_key": segment_key,
                        "source": "Same source",
                        "target": "Same target",
                        "context": {
                            "core": {"content_type": "dialogue"},
                            "extensions": {
                                "dialogue": {
                                    "status": "ready",
                                    "speaker_id": speaker,
                                }
                            },
                        },
                    }
                )
            state_path = root / "state.json"
            errors_path = root / "errors.json"
            chunks = root / "chunks"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            errors_path.write_text(
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
                    str(ROOT / "scripts" / "lqe_chunk.py"),
                    "split",
                    "--state",
                    str(state_path),
                    "--errors",
                    str(errors_path),
                    "--outdir",
                    str(chunks),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            dedup = json.loads(
                (chunks / "dedup_map.json").read_text(encoding="utf-8")
            )
            self.assertEqual(dedup, {"0": [0, 2], "1": [1]})


if __name__ == "__main__":
    unittest.main()
