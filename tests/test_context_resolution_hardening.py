from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lqe_context import (
    descriptor_registry,
    extract_segment_context,
    resolve_context_columns,
)
from lqe_capabilities import normalize_profile
from lqe_context_bundle import (
    ContextBundleError,
    build_context_bundle,
    build_context_bundle_set,
    build_worker_context_manifest,
)
from tests.test_context_bundle_contract import ContextBundleFixture


class SpeakerApplicabilityTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "capabilities": {
                "context.core@1": {"required": True, "config": {}},
                "context.dialogue@1": {
                    "required": False,
                    "config": {
                        "columns": {"speaker_id": ["Speaker"]},
                        "applies_when": {"content_type": ["dialogue"]},
                        "applies_if_present": ["scene_id"],
                    },
                },
            }
        }
        self.registry = descriptor_registry(
            self.profile,
            capability_resolution={
                "enabled": {
                    "context.core@1": {},
                    "context.dialogue@1": {},
                }
            },
        )
        self.columns = resolve_context_columns(
            ["Speaker"], self.registry, profile=self.profile
        )

    def test_nonempty_speaker_is_dialogue_evidence_without_content_type(self):
        self.assertEqual(
            self.registry["context.dialogue@1"]["applies_if_present"],
            ["speaker_id", "scene_id"],
        )
        context = extract_segment_context(
            ["埃德加"], self.columns, self.registry, profile=self.profile
        )

        self.assertEqual(context["extensions"]["dialogue"]["status"], "ready")
        self.assertEqual(
            context["extensions"]["dialogue"]["speaker_id"], "埃德加"
        )
        self.assertIsNone(context["core"]["content_type"])

    def test_missing_speaker_does_not_infer_dialogue_from_text(self):
        context = extract_segment_context(
            [""], self.columns, self.registry, profile=self.profile
        )

        self.assertEqual(
            context["extensions"]["dialogue"], {"status": "not_applicable"}
        )


class EntityResolutionTests(ContextBundleFixture):
    def entity_view(self) -> dict:
        view = deepcopy(self.view)
        view["capabilities"].append("assets.entity_registry@1")
        return view

    def test_source_and_target_aliases_resolve_to_unique_canonical_ids(self):
        segment = deepcopy(self.current)
        dialogue = segment["context"]["extensions"]["dialogue"]
        dialogue["speaker_id"] = "操作员"
        dialogue["addressee_ids"] = ["관리자"]

        bundle = build_context_bundle(
            self.state,
            segment,
            "accuracy",
            module_view=self.entity_view(),
        )
        resolved = bundle["segment"]["context"]["extensions"]["dialogue"]

        self.assertEqual(resolved["speaker_id"], "entity.operator")
        self.assertEqual(resolved["addressee_ids"], ["entity.supervisor"])
        self.assertEqual(
            resolved["entity_resolution"]["speaker"]["status"],
            "unique_alias",
        )
        self.assertEqual(
            resolved["entity_resolution"]["addressees"][0]["status"],
            "unique_alias",
        )
        self.assertEqual(
            bundle["entity_fact_ids"],
            ["fact.operator.verified", "fact.supervisor.source"],
        )
        self.assertEqual(
            bundle["relation_ids"],
            ["relation.operator.supervisor.verified"],
        )
        self.assertNotIn("fact.operator.candidate", bundle["entity_fact_ids"])

    def test_alias_normalization_uses_nfkc_trim_and_casefold(self):
        document = json.loads(self.files["entities"].read_text(encoding="utf-8"))
        document["entities"][0]["names"]["target"].append("Operator")
        self.files["entities"].write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
        self._rebind_asset("entities")
        segment = deepcopy(self.current)
        segment["context"]["extensions"]["dialogue"]["speaker_id"] = (
            "  ＯＰＥＲＡＴＯＲ  "
        )
        segment["context"]["extensions"]["dialogue"]["addressee_ids"] = []

        bundle = build_context_bundle(
            self.state,
            segment,
            "accuracy",
            module_view=self.entity_view(),
        )
        resolution = bundle["segment"]["context"]["extensions"]["dialogue"]

        self.assertEqual(resolution["speaker_id"], "entity.operator")
        self.assertEqual(
            resolution["entity_resolution"]["speaker"]["normalized_alias"],
            "operator",
        )

    def test_ambiguous_alias_is_explicit_conflict_and_selects_no_entity_fact(self):
        document = json.loads(self.files["entities"].read_text(encoding="utf-8"))
        document["entities"][0]["names"]["source"].append("共同名")
        document["entities"][1]["names"]["source"].append("共同名")
        self.files["entities"].write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
        self._rebind_asset("entities")
        segment = deepcopy(self.current)
        dialogue = segment["context"]["extensions"]["dialogue"]
        dialogue["speaker_id"] = "共同名"
        dialogue["addressee_ids"] = []

        bundle = build_context_bundle(
            self.state,
            segment,
            "accuracy",
            module_view=self.entity_view(),
        )
        resolution = bundle["segment"]["context"]["extensions"]["dialogue"]

        self.assertEqual(bundle["context_status"], "conflict")
        self.assertEqual(resolution["status"], "conflict")
        self.assertEqual(
            resolution["entity_resolution"]["speaker"]["candidate_ids"],
            ["entity.operator", "entity.supervisor"],
        )
        self.assertEqual(
            resolution["entity_resolution"]["speaker"]["resolution_status"],
            "ambiguous",
        )
        self.assertEqual(
            resolution["entity_resolution"]["speaker"]["candidates"],
            ["entity.operator", "entity.supervisor"],
        )
        self.assertEqual(bundle["entity_fact_ids"], [])

    def test_unknown_alias_is_auditable_incomplete_and_not_silently_dropped(self):
        segment = deepcopy(self.current)
        dialogue = segment["context"]["extensions"]["dialogue"]
        dialogue["speaker_id"] = "未知角色"
        dialogue["addressee_ids"] = []

        bundle = build_context_bundle(
            self.state,
            segment,
            "accuracy",
            module_view=self.entity_view(),
        )
        resolution = bundle["segment"]["context"]["extensions"]["dialogue"]

        self.assertEqual(bundle["context_status"], "incomplete")
        self.assertEqual(resolution["speaker_id"], "未知角色")
        self.assertEqual(
            resolution["entity_resolution"]["speaker"]["status"], "unknown"
        )
        self.assertEqual(
            resolution["entity_resolution"]["speaker"]["raw_label"],
            "未知角色",
        )
        self.assertEqual(
            resolution["entity_resolution"]["speaker"]["reason"],
            "no_canonical_id_or_unique_name_alias",
        )
        self.assertEqual(bundle["entity_fact_ids"], [])

    def test_exact_canonical_id_wins_before_alias_lookup(self):
        bundle = build_context_bundle(
            self.state,
            self.current,
            "accuracy",
            module_view=self.entity_view(),
        )
        resolution = bundle["segment"]["context"]["extensions"]["dialogue"]

        self.assertEqual(
            resolution["entity_resolution"]["speaker"]["status"],
            "canonical_id",
        )


class NeighborBoundaryTests(ContextBundleFixture):
    @staticmethod
    def set_scene(segment: dict, value: str) -> None:
        segment["context"]["extensions"].setdefault(
            "dialogue", {"status": "ready"}
        )["scene_id"] = value

    @staticmethod
    def set_group(segment: dict, value: str) -> None:
        segment["context"]["core"]["group_id"] = value

    def test_scene_boundary_excludes_adjacent_segments_from_other_scenes(self):
        state = deepcopy(self.state)
        self.set_scene(state["segments"][2], "scene-current")
        self.set_scene(state["segments"][0], "scene-other")
        self.set_scene(state["segments"][4], "scene-current")

        bundle = build_context_bundle(
            state,
            state["segments"][2],
            "accuracy",
            module_view=self.view,
        )

        self.assertEqual([item["id"] for item in bundle["neighbors"]], [4])
        self.assertEqual(
            bundle["neighbor_selection"]["boundary"],
            {"scene_id": "scene-current"},
        )
        self.assertEqual(
            bundle["neighbor_selection"]["boundary_status"], "bounded"
        )

    def test_group_boundary_is_enforced_independently(self):
        state = deepcopy(self.state)
        self.set_group(state["segments"][2], "group-current")
        self.set_group(state["segments"][0], "group-current")
        self.set_group(state["segments"][4], "group-other")

        bundle = build_context_bundle(
            state,
            state["segments"][2],
            "accuracy",
            module_view=self.view,
        )

        self.assertEqual([item["id"] for item in bundle["neighbors"]], [0])
        self.assertEqual(
            bundle["neighbor_selection"]["boundary"],
            {"group_id": "group-current"},
        )

    def test_missing_boundary_fallback_is_explicitly_audited(self):
        bundle = build_context_bundle(
            self.state,
            self.current,
            "accuracy",
            module_view=self.view,
        )

        self.assertEqual(
            bundle["neighbor_selection"]["boundary_status"],
            "fallback_no_boundary",
        )
        self.assertEqual([item["id"] for item in bundle["neighbors"]], [0, 4])

    def test_strict_mode_refuses_cross_scene_fallback_without_boundary(self):
        view = deepcopy(self.view)
        view["neighbors"]["boundary_mode"] = "strict"

        bundle = build_context_bundle(
            self.state,
            self.current,
            "accuracy",
            module_view=view,
        )

        self.assertEqual(bundle["neighbors"], [])
        self.assertEqual(bundle["context_status"], "incomplete")
        self.assertEqual(
            bundle["neighbor_selection"]["boundary_status"],
            "strict_missing_boundary",
        )

    def test_profile_can_declare_strict_neighbor_boundary_mode(self):
        profile = self._profile()
        profile["module_context_views"] = {
            "accuracy": {
                "capabilities": ["context.core@1", "context.dialogue@1"],
                "neighbors": {"before": 1, "after": 1, "boundary_mode": "strict"},
            }
        }

        normalized = normalize_profile(profile)

        self.assertEqual(
            normalized["module_context_views"]["accuracy"]["neighbors"][
                "boundary_mode"
            ],
            "strict",
        )


class ProjectionUnionTests(ContextBundleFixture):
    def union_view(self) -> dict:
        return {
            "capabilities": ["context.core@1", "context.dialogue@1"],
            "dimensions": ["grammar"],
            "include_constraints": False,
            "neighbors": {"before": 0, "after": 0},
            "limits": {
                "max_facts_per_entity": 0,
                "max_relations": 0,
                "max_runtime_examples": 0,
            },
        }

    def test_projection_modules_union_descriptor_fields_and_bind_digest(self):
        narrow = build_context_bundle(
            self.state,
            self.current,
            "grammar",
            module_view=self.union_view(),
        )
        union = build_context_bundle(
            self.state,
            self.current,
            "grammar",
            module_view=self.union_view(),
            projection_modules=["grammar", "accuracy", "grammar"],
        )

        self.assertNotIn("extensions", narrow["segment"]["context"])
        self.assertEqual(union["projection_modules"], ["accuracy", "grammar"])
        self.assertEqual(
            union["segment"]["context"]["extensions"]["dialogue"][
                "speaker_id"
            ],
            "entity.operator",
        )
        self.assertNotEqual(
            narrow["context_bundle_digest"], union["context_bundle_digest"]
        )

    def test_bundle_set_and_worker_rebuild_preserve_projection_union(self):
        bundle_set = build_context_bundle_set(
            self.state,
            [self.current],
            "grammar",
            module_view=self.union_view(),
            projection_modules=["accuracy", "grammar"],
        )
        manifest = build_worker_context_manifest(
            self.state,
            "grammar",
            bundle_set,
            max_worker_bytes=1_000_000,
            common_instructions_path=self.files["common"],
            module_instructions_path=self.files["module"],
        )

        self.assertEqual(
            bundle_set["projection_modules"], ["accuracy", "grammar"]
        )
        self.assertEqual(manifest["module"], "grammar")

    def test_projection_modules_must_include_bundle_module(self):
        with self.assertRaisesRegex(ContextBundleError, "must include"):
            build_context_bundle(
                self.state,
                self.current,
                "grammar",
                module_view=self.union_view(),
                projection_modules=["accuracy"],
            )


class RuntimeExampleRankingTests(ContextBundleFixture):
    def append_example(
        self,
        example_id: str,
        source: str,
        *,
        scope: str = "project",
        segment_key: str | None = None,
        include_content_types: bool = True,
    ) -> None:
        document = json.loads(self.files["examples"].read_text(encoding="utf-8"))
        item = {
            "id": example_id,
            "dimensions": ["accuracy"],
            "source": source,
            "rejected_target": "Rejected",
            "preferred_target": "Preferred",
            "reason": "Ranking fixture",
            "scope": scope,
            "review_status": "reviewed",
            "authority": {"issuer": "reviewer"},
            "uses": ["runtime_reference"],
            "capabilities": ["context.dialogue@1"],
            "provenance": {"source_id": "fixture-source"},
        }
        if segment_key is not None:
            item["segment_key"] = segment_key
        if include_content_types:
            item["content_types"] = ["dialogue"]
        document["examples"].append(item)
        self.files["examples"].write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
        self._rebind_asset("examples")

    def one_example_view(self) -> dict:
        view = deepcopy(self.view)
        view["limits"]["max_runtime_examples"] = 1
        return view

    def test_broader_examples_rank_by_lexical_overlap_before_id(self):
        self.append_example(
            "example.zz.lexical",
            self.current["source"],
        )

        bundle = build_context_bundle(
            self.state,
            self.current,
            "accuracy",
            module_view=self.one_example_view(),
        )

        self.assertEqual(bundle["runtime_example_ids"], ["example.zz.lexical"])
        evidence = bundle["runtime_example_selection"][0]
        self.assertEqual(evidence["selection_reason"], "lexical_overlap")
        self.assertEqual(evidence["lexical_overlap"]["score_ppm"], 1_000_000)

    def test_exact_segment_key_precedes_broader_lexical_match(self):
        self.append_example(
            "example.zz.lexical",
            self.current["source"],
        )
        self.append_example(
            "example.segment.current",
            "Unrelated source text",
            scope="segment",
            segment_key="current",
            include_content_types=False,
        )

        bundle = build_context_bundle(
            self.state,
            self.current,
            "accuracy",
            module_view=self.one_example_view(),
        )

        self.assertEqual(
            bundle["runtime_example_ids"], ["example.segment.current"]
        )
        self.assertEqual(
            bundle["runtime_example_selection"][0]["selection_reason"],
            "exact_segment_key",
        )

    def test_exact_segment_key_is_reachable_without_content_type(self):
        self.append_example(
            "example.segment.current",
            "Unrelated source text",
            scope="segment",
            segment_key="current",
            include_content_types=False,
        )
        state = deepcopy(self.state)
        state["segments"][2]["context"]["core"]["content_type"] = None

        bundle = build_context_bundle(
            state,
            state["segments"][2],
            "accuracy",
            module_view=self.one_example_view(),
        )

        self.assertEqual(
            bundle["runtime_example_ids"], ["example.segment.current"]
        )
        evidence = bundle["runtime_example_selection"][0]
        self.assertEqual(evidence["selection_reason"], "exact_segment_key")
        self.assertIsNone(evidence["content_type"])

    def test_zero_overlap_uses_exact_content_type_fallback(self):
        bundle = build_context_bundle(
            self.state,
            self.current,
            "accuracy",
            module_view=self.one_example_view(),
        )

        self.assertEqual(bundle["runtime_example_ids"], ["example.match"])
        self.assertEqual(
            bundle["runtime_example_selection"][0]["selection_reason"],
            "content_type_fallback",
        )


if __name__ == "__main__":
    unittest.main()
