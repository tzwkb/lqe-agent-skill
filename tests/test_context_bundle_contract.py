from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lqe_capabilities import normalize_profile, resolve_capabilities
from lqe_context import descriptor_registry
from lqe_context_bundle import (
    ContextBundleError,
    WorkerContextBudgetError,
    build_context_bundle,
    build_context_bundle_set,
    build_worker_context_manifest,
    calculate_worker_input_bytes,
    canonical_digest,
    enforce_worker_byte_budget,
    load_project_context_assets,
    validate_context_bundle,
    validate_loaded_project_context_assets,
    validate_shared_context_assets,
    validate_worker_context_manifest,
)
from lqe_language_policies import trusted_provider_registry
from lqe_profile_ingest import build_project_source_manifest, source_digest
from lqe_project_assets import (
    asset_statuses,
    canonical_digest as asset_digest,
    inspect_project_assets,
)


def asset(kind: str, path: str, *, required: bool = True) -> dict:
    return {
        "kind": kind,
        "path": path,
        "required": required,
        "authority": {"issuer": "test", "level": "authoritative"},
        "provenance": {"kind": "test_fixture"},
        "distribution": "internal_only",
        "availability": "included",
    }


def fact(fact_id: str, text: str, status: str) -> dict:
    return {
        "id": fact_id,
        "text": text,
        "verification_status": status,
        "authority": {"issuer": "test"},
        "provenance": {"source_id": "fixture-source"},
    }


def entity(
    entity_id: str,
    source_name: str,
    target_name: str,
    facts: list[dict],
) -> dict:
    return {
        "id": entity_id,
        "entity_type": "character",
        "names": {"source": [source_name], "target": [target_name]},
        "tags": [],
        "facts": facts,
    }


def relation(
    relation_id: str,
    source: str,
    target: str,
    status: str,
) -> dict:
    return {
        "id": relation_id,
        "from": source,
        "to": target,
        "relation_type": "project_relation",
        "attributes": {"stage": "current"},
        "verification_status": status,
        "authority": {"issuer": "test"},
        "provenance": {"source_id": "fixture-source"},
    }


def example(
    example_id: str,
    *,
    content_types=None,
    capabilities=None,
    dimensions=None,
    scope="project",
    status="reviewed",
    uses=None,
    source="Same words do not authorize a match.",
) -> dict:
    output = {
        "id": example_id,
        "dimensions": dimensions or ["accuracy"],
        "source": source,
        "rejected_target": "Rejected",
        "preferred_target": "Preferred",
        "reason": "Reviewed fixture",
        "scope": scope,
        "review_status": status,
        "authority": {"issuer": "reviewer"},
        "uses": uses or ["runtime_reference"],
        "provenance": {"source_id": "fixture-source"},
    }
    if content_types is not None:
        output["content_types"] = content_types
    if capabilities is not None:
        output["capabilities"] = capabilities
    return output


class ContextBundleFixture(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.files = {}
        self._write("checks", "checks.json", "{}")
        self._write("confirmed", "confirmed_rules.md", "# Rules\n")
        self._write("sg", "sg.md", "# Style\n")
        self._write("background", "background.md", "# Background\n")
        self._write("notes", "lang_notes.md", "# Korean notes\n")
        self._write("common", "common.md", "Common instructions")
        self._write("module", "accuracy.md", "Accuracy instructions")
        self._write("suggestions", "suggestions.md", "Suggestion instructions")
        self._write_json(
            "source_manifest",
            "input_manifest.json",
            {"schema": "fixture.input-manifest", "version": 1, "segments": 5},
        )
        self._write_json("coverage", "coverage.json", [])
        self._write_json(
            "project_sources",
            "project_sources.json",
            build_project_source_manifest(
                project="fixture/zh-ko",
                manifest_scope="internal",
                sources=[
                    {
                        "id": "fixture-source",
                        "kind": "project_reference",
                        "filename": "fixture.xlsx",
                        "sha256": "a" * 64,
                        "verification_status": "verified",
                        "distribution": "internal_only",
                        "authority": {"issuer": "test"},
                        "availability": "included",
                    }
                ],
                generated_assets=[],
                coverage={
                    "total_nonempty": 0,
                    "converted": 0,
                    "normalized": 0,
                    "ignored": 0,
                    "unmapped": 0,
                    "details_path": "coverage.json",
                },
                coverage_details=[],
            ),
        )
        self._write_json(
            "entities",
            "entities.json",
            {
                "schema": "lqe.entities",
                "version": 1,
                "entities": [
                    entity(
                        "entity.operator",
                        "操作员",
                        "운영자",
                        [
                            fact("fact.operator.verified", "Verified operator fact", "verified"),
                            fact("fact.operator.candidate", "Candidate operator fact", "candidate"),
                        ],
                    ),
                    entity(
                        "entity.supervisor",
                        "主管",
                        "관리자",
                        [
                            fact("fact.supervisor.source", "Source-backed supervisor fact", "source_backed")
                        ],
                    ),
                    entity(
                        "entity.operator.v2",
                        "操作员二",
                        "운영자2",
                        [fact("fact.near-id", "Must not prefix-match", "verified")],
                    ),
                ],
                "relations": [
                    relation(
                        "relation.operator.supervisor.verified",
                        "entity.operator",
                        "entity.supervisor",
                        "verified",
                    ),
                    relation(
                        "relation.operator.supervisor.candidate",
                        "entity.operator",
                        "entity.supervisor",
                        "candidate",
                    ),
                    relation(
                        "relation.operator.near",
                        "entity.operator",
                        "entity.operator.v2",
                        "source_backed",
                    ),
                ],
            },
        )
        self._write_json(
            "examples",
            "examples.json",
            {
                "schema": "lqe.review-examples",
                "version": 1,
                "examples": [
                    example(
                        "example.match",
                        content_types=["dialogue"],
                        capabilities=["context.dialogue@1"],
                    ),
                    example(
                        "example.wrong-content",
                        content_types=["ui_button"],
                        capabilities=["context.dialogue@1"],
                        source="请提交这份报告。",
                    ),
                    example(
                        "example.wrong-capability",
                        content_types=["dialogue"],
                        capabilities=["context.ui@1"],
                    ),
                    example(
                        "example.wrong-dimension",
                        content_types=["dialogue"],
                        capabilities=["context.dialogue@1"],
                        dimensions=["grammar"],
                    ),
                    example(
                        "example.candidate",
                        content_types=["dialogue"],
                        capabilities=["context.dialogue@1"],
                        status="candidate",
                    ),
                    example(
                        "example.regression",
                        content_types=["dialogue"],
                        capabilities=["context.dialogue@1"],
                        uses=["regression_only"],
                    ),
                    example("example.unscoped"),
                ],
            },
        )
        self._write_json(
            "context_rules",
            "context_rules.json",
            {
                "schema": "lqe.context-rules",
                "version": 1,
                "authority_rank": ["client", "language_lead"],
                "rules": [
                    {
                        "id": "rule.register.plain",
                        "capability": "language.register",
                        "provider": {"id": "ko.register", "api_version": 1},
                        "target_lang": "ko",
                        "rule_status": "confirmed",
                        "priority": 100,
                        "authority": {"issuer": "client"},
                        "valid_from": None,
                        "valid_until": None,
                        "when": {
                            "speaker_id": ["entity.operator"],
                            "addressee_ids": ["entity.supervisor"],
                        },
                        "expect": {"politeness": ["plain"]},
                        "provenance": {"confirmation_record": "fixture-confirmation"},
                    }
                ],
            },
        )
        generated_assets = []
        for asset_id, kind, key in (
            ("entities", "entity_registry", "entities"),
            ("examples", "review_examples", "examples"),
            ("context_rules", "context_rules", "context_rules"),
        ):
            generated_assets.append({
                "asset_id": asset_id,
                "kind": kind,
                "path": self.files[key].name,
                "sha256": hashlib.sha256(
                    self.files[key].read_bytes()
                ).hexdigest(),
                "derived_from": ["fixture-source"],
                "distribution": "internal_only",
                "generator": {"name": "fixture", "version": 1},
            })
        self._write_json(
            "project_sources",
            "project_sources.json",
            build_project_source_manifest(
                project="fixture/zh-ko",
                manifest_scope="internal",
                sources=[
                    {
                        "id": "fixture-source",
                        "kind": "project_reference",
                        "filename": "fixture.xlsx",
                        "sha256": "a" * 64,
                        "verification_status": "verified",
                        "distribution": "internal_only",
                        "authority": {"issuer": "test"},
                        "availability": "included",
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
                coverage_details=[],
            ),
        )
        self._write_json(
            "rogue",
            "rogue_entities.json",
            {"schema": "lqe.entities", "version": 1, "entities": [], "relations": []},
        )
        self.state = self._state()
        self.current = self.state["segments"][2]
        self.view = {
            "capabilities": ["context.core@1", "context.dialogue@1"],
            "dimensions": ["accuracy"],
            "constraint_kinds": ["language.register"],
            "neighbors": {"before": 2, "after": 2, "include_target": False},
            "limits": {
                "max_facts_per_entity": 10,
                "max_relations": 10,
                "max_runtime_examples": 10,
            },
        }

    def tearDown(self):
        self.tempdir.cleanup()

    def _write(self, key, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        self.files[key] = path
        return path

    def _write_json(self, key, name, value):
        return self._write(
            key,
            name,
            json.dumps(value, ensure_ascii=False, indent=2),
        )

    def _profile(self):
        return {
            "profile_contract_version": 2,
            "name": "fixture/zh-ko",
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
            "assets": {
                "checks": asset("checks", "checks.json"),
                "confirmed": asset("confirmed_rules", "confirmed_rules.md"),
                "entities": asset("entity_registry", "entities.json"),
                "examples": asset("review_examples", "examples.json"),
                "context_rules": asset("context_rules", "context_rules.json"),
                "project_sources": asset(
                    "project_source_manifest", "project_sources.json"
                ),
            },
            "context_pipeline": {"mode": "enforce"},
            "capabilities": {
                "context.core@1": {
                    "required": True,
                    "config": {
                        "identity": {
                            "key_columns": ["Key"],
                            "fallback": "source_coordinate",
                        }
                    },
                },
                "source_provenance@1": {"required": True},
                "context.dialogue@1": {
                    "required": True,
                    "config": {
                        "applies_when": {"content_type": ["dialogue"]}
                    },
                },
                "assets.entity_registry@1": {
                    "required": True,
                    "asset": "entities",
                },
                "assets.review_examples@1": {
                    "required": True,
                    "asset": "examples",
                },
                "language_policy.register@1": {
                    "required": True,
                    "provider": "ko.register@1",
                    "asset": "context_rules",
                },
            },
        }

    def _segment(
        self,
        segment_id,
        key,
        source,
        target,
        *,
        protected=False,
        blocked=False,
        speaker=None,
        addressees=None,
        target_verified=False,
    ):
        context = {
            "context_contract_version": 1,
            "status": "ready",
            "core": {"content_type": "dialogue", "context_note": None},
            "extensions": {},
            "provenance": {},
            "missing_required": [],
        }
        if speaker is not None:
            context["extensions"]["dialogue"] = {
                "status": "ready",
                "speaker_id": speaker,
                "addressee_ids": addressees or [],
            }
        output = {
            "id": segment_id,
            "segment_key": key,
            "key_origin": "input",
            "source": source,
            "source_digest": source_digest(source),
            "target": target,
            "input_status": "blocked" if blocked else "ready",
            "input_block_reasons": ([{"code": "FIXTURE_BLOCK"}] if blocked else []),
            "input_warnings": [],
            "protected": protected,
            "protected_reason": "fixture" if protected else None,
            "protected_texts": ["{0}"] if protected else [],
            "context": context,
            "resolved_constraints": [],
        }
        if target_verified:
            output["target_provenance_status"] = "verified"
        revision = {
            "id": segment_id,
            "key": key,
            "source": source,
            "target": target,
            "blocked": blocked,
            "protected": protected,
            "context": context,
        }
        output["segment_revision_digest"] = canonical_digest(revision)
        return output

    def _state(self, profile=None):
        profile = self._profile() if profile is None else profile
        normalized = normalize_profile(profile)
        inspection = inspect_project_assets(
            normalized, profile_dir=self.root, strict_required=True
        )
        resolution = resolve_capabilities(
            normalized,
            asset_statuses=asset_statuses(inspection["snapshot"]),
            provider_registry=trusted_provider_registry(),
        )
        registry = descriptor_registry(
            normalized, capability_resolution=resolution
        )
        segments = [
            self._segment(0, "neighbor-left", "前句", "Left"),
            self._segment(
                1,
                "protected-left",
                "保护句",
                "Protected",
                protected=True,
            ),
            self._segment(
                2,
                "current",
                "请提交这份报告。",
                "이 보고서를 제출하세요.",
                speaker="entity.operator",
                addressees=["entity.supervisor"],
            ),
            self._segment(
                3,
                "blocked-right",
                "阻断句",
                "Blocked",
                blocked=True,
            ),
            self._segment(
                4,
                "neighbor-right",
                "后句",
                "Right",
                target_verified=True,
            ),
        ]
        segments[2]["resolved_constraints"] = [
            {
                "kind": "language.register",
                "status": "resolved",
                "rule_id": "rule.register.plain",
                "expected": {"politeness": ["plain"]},
            }
        ]
        segments[2]["segment_revision_digest"] = canonical_digest(
            {
                "segment": segments[2]["segment_revision_digest"],
                "constraints": segments[2]["resolved_constraints"],
            }
        )
        paths = {
            asset_id: str(path.resolve())
            for asset_id, path in inspection["resolved_paths"].items()
        }
        return {
            "profile_digest": normalized["source_profile_digest"],
            "profile_overlay_digest": None,
            "target_lang": "ko",
            "project_asset_snapshot": inspection["snapshot"],
            "project_asset_snapshot_digest": inspection["snapshot"]["digest"],
            "project_asset_paths": paths,
            "capability_resolution": resolution,
            "capability_resolution_digest": resolution["digest"],
            "resolved_context_descriptors": registry,
            "source_manifest_path": str(self.files["source_manifest"].resolve()),
            "sg_path": str(self.files["sg"].resolve()),
            "background_path": str(self.files["background"].resolve()),
            "confirmed_rules_path": str(self.files["confirmed"].resolve()),
            "lang_notes_path": str(self.files["notes"].resolve()),
            "segments": segments,
        }

    def _rebind_asset(self, asset_id):
        path = Path(self.state["project_asset_paths"][asset_id])
        entry = self.state["project_asset_snapshot"]["assets"][asset_id]
        payload = path.read_bytes()
        entry["sha256"] = hashlib.sha256(payload).hexdigest()
        entry["size"] = len(payload)
        if asset_id != "project_sources":
            manifest_path = self.files["project_sources"]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            generated = {
                item["asset_id"]: item for item in manifest["generated_assets"]
            }
            if asset_id in generated:
                generated[asset_id]["sha256"] = entry["sha256"]
                manifest["manifest_digest"] = canonical_digest({
                    key: value
                    for key, value in manifest.items()
                    if key != "manifest_digest"
                })
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False),
                    encoding="utf-8",
                )
                manifest_entry = self.state["project_asset_snapshot"]["assets"][
                    "project_sources"
                ]
                manifest_payload = manifest_path.read_bytes()
                manifest_entry["sha256"] = hashlib.sha256(
                    manifest_payload
                ).hexdigest()
                manifest_entry["size"] = len(manifest_payload)
        snapshot = self.state["project_asset_snapshot"]
        snapshot["digest"] = asset_digest(
            {key: deepcopy(value) for key, value in snapshot.items() if key != "digest"}
        )
        self.state["project_asset_snapshot_digest"] = snapshot["digest"]
        resolution = self.state["capability_resolution"]
        for item in resolution["enabled"].values():
            if item.get("asset") == asset_id:
                item["asset_digest"] = entry["sha256"]
        resolution["digest"] = canonical_digest(
            {
                key: deepcopy(value)
                for key, value in resolution.items()
                if key != "digest"
            }
        )
        self.state["capability_resolution_digest"] = resolution["digest"]


class BoundAssetLoadingTests(ContextBundleFixture):
    def test_only_declared_present_kinds_are_loaded_and_indexed(self):
        loaded = load_project_context_assets(self.state)

        self.assertEqual(
            set(loaded["asset_bindings"]),
            {"entities", "examples", "context_rules"},
        )
        self.assertNotIn("rogue_entities", repr(loaded))
        self.assertIn("fact.operator.candidate", loaded["facts"])
        self.assertEqual(len(loaded["loaded_assets_digest"]), 64)
        self.assertEqual(validate_loaded_project_context_assets(loaded), loaded)

    def test_undeclared_path_missing_present_path_and_digest_tamper_fail_closed(self):
        undeclared = deepcopy(self.state)
        undeclared["project_asset_paths"]["rogue"] = str(self.files["rogue"])
        with self.assertRaisesRegex(ContextBundleError, "undeclared asset"):
            load_project_context_assets(undeclared)

        missing = deepcopy(self.state)
        del missing["project_asset_paths"]["entities"]
        with self.assertRaisesRegex(ContextBundleError, "no bound runtime path"):
            load_project_context_assets(missing)

        self.files["entities"].write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ContextBundleError, "digest mismatch"):
            load_project_context_assets(self.state)

    def test_external_context_asset_is_not_opened_or_activated(self):
        profile = self._profile()
        profile["assets"]["examples"]["required"] = False
        profile["assets"]["examples"]["availability"] = "external"
        del profile["capabilities"]["assets.review_examples@1"]
        state = self._state(profile)

        loaded = load_project_context_assets(state)

        self.assertEqual(loaded["review_examples"], {})
        self.assertNotIn("examples", loaded["asset_bindings"])

    def test_present_asset_without_enabled_capability_is_not_loaded_or_manifested(self):
        profile = self._profile()
        profile["assets"]["examples"]["required"] = False
        del profile["capabilities"]["assets.review_examples@1"]
        state = self._state(profile)

        self.assertEqual(
            state["project_asset_snapshot"]["assets"]["examples"]["status"],
            "present",
        )
        self.assertIn("examples", state["project_asset_paths"])
        loaded = load_project_context_assets(state)
        self.assertNotIn("examples", loaded["asset_bindings"])
        self.assertEqual(loaded["review_examples"], {})

        bundle_set = build_context_bundle_set(
            state,
            [state["segments"][2]],
            "accuracy",
            module_view=self.view,
        )
        manifest = build_worker_context_manifest(
            state,
            "accuracy",
            bundle_set,
            max_worker_bytes=1_000_000,
            common_instructions_path=self.files["common"],
            module_instructions_path=self.files["module"],
            suggestion_instructions_path=self.files["suggestions"],
        )
        visible_ids = {item["asset_id"] for item in manifest["project_assets"]}
        self.assertNotIn("examples", visible_ids)
        self.assertEqual(
            visible_ids,
            {
                "checks",
                "confirmed",
                "entities",
                "context_rules",
                "project_sources",
            },
        )

    def test_runtime_example_for_current_segment_is_rejected_as_held_out_leak(self):
        document = json.loads(self.files["examples"].read_text(encoding="utf-8"))
        leaking = example(
            "example.leak",
            content_types=["dialogue"],
            capabilities=["context.dialogue@1"],
            scope="segment",
        )
        leaking["segment_key"] = "current"
        document["examples"].append(leaking)
        self.files["examples"].write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
        self._rebind_asset("examples")

        with self.assertRaisesRegex(ContextBundleError, "leaks a held-out segment"):
            load_project_context_assets(self.state)

    def test_entity_provenance_must_reference_bound_project_source_manifest(self):
        document = json.loads(self.files["entities"].read_text(encoding="utf-8"))
        document["entities"][0]["facts"][0]["provenance"][
            "source_id"
        ] = "undeclared-source"
        self.files["entities"].write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
        self._rebind_asset("entities")

        with self.assertRaisesRegex(ContextBundleError, "undeclared source_id"):
            load_project_context_assets(self.state)

    def test_loaded_asset_cache_is_bound_to_capability_resolution(self):
        loaded = load_project_context_assets(self.state)
        changed = deepcopy(self.state)
        changed["capability_resolution"]["warnings"].append("fixture change")
        changed["capability_resolution"]["digest"] = canonical_digest(
            {
                key: value
                for key, value in changed["capability_resolution"].items()
                if key != "digest"
            }
        )
        changed["capability_resolution_digest"] = changed[
            "capability_resolution"
        ]["digest"]

        with self.assertRaisesRegex(
            ContextBundleError, "another capability resolution"
        ):
            build_context_bundle(
                changed,
                changed["segments"][2],
                "accuracy",
                loaded_assets=loaded,
                module_view=self.view,
            )

    def test_project_source_coverage_details_are_runtime_bound(self):
        self.files["coverage"].write_text(
            json.dumps({"records": []}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ContextBundleError, "details digest mismatch"):
            load_project_context_assets(self.state)

        self.files["coverage"].unlink()
        with self.assertRaisesRegex(ContextBundleError, "cannot inspect"):
            load_project_context_assets(self.state)

    def test_enabled_context_assets_require_complete_derived_from_bindings(self):
        manifest_path = self.files["project_sources"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["generated_assets"] = [
            item
            for item in manifest["generated_assets"]
            if item["asset_id"] != "entities"
        ]
        manifest["manifest_digest"] = canonical_digest({
            key: value
            for key, value in manifest.items()
            if key != "manifest_digest"
        })
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self._rebind_asset("project_sources")

        with self.assertRaisesRegex(
            ContextBundleError, "lacks derived_from records.*entities"
        ):
            load_project_context_assets(self.state)

    def test_generated_asset_digest_must_match_runtime_snapshot(self):
        manifest_path = self.files["project_sources"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        next(
            item
            for item in manifest["generated_assets"]
            if item["asset_id"] == "entities"
        )["sha256"] = "f" * 64
        manifest["manifest_digest"] = canonical_digest({
            key: value
            for key, value in manifest.items()
            if key != "manifest_digest"
        })
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self._rebind_asset("project_sources")

        with self.assertRaisesRegex(
            ContextBundleError, "generated asset 'entities' sha256 differs"
        ):
            load_project_context_assets(self.state)


class ContextBundleSelectionTests(ContextBundleFixture):
    def test_exact_entity_status_example_and_rule_selection(self):
        bundle = build_context_bundle(
            self.state, self.current, "accuracy", module_view=self.view
        )

        self.assertEqual(
            bundle["entity_fact_ids"],
            ["fact.operator.verified", "fact.supervisor.source"],
        )
        self.assertEqual(
            bundle["relation_ids"], ["relation.operator.supervisor.verified"]
        )
        self.assertNotIn("candidate", repr(bundle["entity_fact_ids"] + bundle["relation_ids"]))
        self.assertNotIn("fact.near-id", bundle["entity_fact_ids"])
        self.assertEqual(bundle["runtime_example_ids"], ["example.match"])
        self.assertEqual(
            bundle["resolved_constraints"][0]["rule_id"],
            "rule.register.plain",
        )
        self.assertEqual(bundle["segment"]["identity"]["segment_key"], "current")
        self.assertEqual(bundle["segment"]["source"], "请提交这份报告。")
        self.assertEqual(bundle["segment"]["current_target"], "이 보고서를 제출하세요.")
        self.assertEqual(bundle["segment"]["input_status"], "ready")
        self.assertEqual(
            bundle["segment"]["context"]["extensions"]["dialogue"]["speaker_id"],
            "entity.operator",
        )

    def test_relation_runtime_rule_false_is_never_worker_visible(self):
        baseline = build_context_bundle(
            self.state, self.current, "accuracy", module_view=self.view
        )
        self.assertEqual(
            baseline["relation_ids"],
            ["relation.operator.supervisor.verified"],
        )

        document = json.loads(self.files["entities"].read_text(encoding="utf-8"))
        verified = next(
            item
            for item in document["relations"]
            if item["id"] == "relation.operator.supervisor.verified"
        )
        verified["attributes"]["runtime_rule"] = False
        document["relations"].append(
            relation(
                "relation.operator.supervisor.conflict",
                "entity.operator",
                "entity.supervisor",
                "conflict",
            )
        )
        self.files["entities"].write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
        self._rebind_asset("entities")

        bundle = build_context_bundle(
            self.state, self.current, "accuracy", module_view=self.view
        )

        self.assertEqual(bundle["relation_ids"], [])
        self.assertNotIn("candidate", repr(bundle))
        self.assertNotIn("conflict", repr(bundle["relation_ids"]))

    def test_example_matching_never_uses_source_similarity(self):
        document = json.loads(self.files["examples"].read_text(encoding="utf-8"))
        document["examples"][1]["source"] = self.current["source"]
        self.files["examples"].write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
        self._rebind_asset("examples")

        bundle = build_context_bundle(
            self.state, self.current, "accuracy", module_view=self.view
        )

        self.assertEqual(bundle["runtime_example_ids"], ["example.match"])

    def test_module_view_trims_dialogue_assets_examples_constraints_and_neighbors(self):
        core_only = {
            "capabilities": ["context.core@1"],
            "dimensions": ["grammar"],
            "include_constraints": False,
            "limits": {
                "max_facts_per_entity": 99,
                "max_relations": 99,
                "max_runtime_examples": 99,
            },
        }

        bundle = build_context_bundle(
            self.state, self.current, "grammar", module_view=core_only
        )

        self.assertEqual(bundle["segment"]["context"], {"core": {"content_type": "dialogue"}})
        self.assertEqual(bundle["entity_fact_ids"], [])
        self.assertEqual(bundle["relation_ids"], [])
        self.assertEqual(bundle["runtime_example_ids"], [])
        self.assertEqual(bundle["resolved_constraints"], [])
        self.assertEqual(bundle["neighbors"], [])

        no_constraint_kinds = deepcopy(self.view)
        no_constraint_kinds["constraint_kinds"] = []
        without_constraints = build_context_bundle(
            self.state,
            self.current,
            "accuracy",
            module_view=no_constraint_kinds,
        )
        self.assertEqual(without_constraints["resolved_constraints"], [])

    def test_module_view_rejects_inactive_capability_and_requires_core(self):
        inactive = deepcopy(self.view)
        inactive["capabilities"].append("context.ui@1")
        with self.assertRaisesRegex(ContextBundleError, "inactive capabilities"):
            build_context_bundle(
                self.state, self.current, "accuracy", module_view=inactive
            )

        without_core = deepcopy(self.view)
        without_core["capabilities"].remove("context.core@1")
        with self.assertRaisesRegex(ContextBundleError, "must include context.core"):
            build_context_bundle(
                self.state, self.current, "accuracy", module_view=without_core
            )

    def test_context_status_is_scoped_to_selected_module_projection(self):
        segment = deepcopy(self.current)
        segment["context"]["extensions"]["dialogue"]["status"] = "incomplete"
        accuracy = build_context_bundle(
            self.state, segment, "accuracy", module_view=self.view
        )
        grammar_view = {
            "capabilities": ["context.core@1"],
            "dimensions": ["grammar"],
            "include_constraints": False,
            "limits": {
                "max_facts_per_entity": 0,
                "max_relations": 0,
                "max_runtime_examples": 0,
            },
        }
        grammar = build_context_bundle(
            self.state, segment, "grammar", module_view=grammar_view
        )

        self.assertEqual(accuracy["context_status"], "incomplete")
        self.assertEqual(grammar["context_status"], "ready")

    def test_protected_and_blocked_neighbors_are_excluded_and_targets_need_opt_in(self):
        omitted = build_context_bundle(
            self.state, self.current, "accuracy", module_view=self.view
        )
        include_view = deepcopy(self.view)
        include_view["neighbors"]["include_target"] = True
        included = build_context_bundle(
            self.state, self.current, "accuracy", module_view=include_view
        )

        self.assertEqual([item["id"] for item in omitted["neighbors"]], [0, 4])
        self.assertTrue(all(item["target"] is None for item in omitted["neighbors"]))
        self.assertTrue(all(item["target_status"] == "omitted" for item in omitted["neighbors"]))
        self.assertEqual([item["target"] for item in included["neighbors"]], ["Left", "Right"])
        self.assertEqual(
            [item["target_status"] for item in included["neighbors"]],
            ["unverified", "verified"],
        )

        protected = deepcopy(self.current)
        protected["protected"] = True
        protected["protected_reason"] = "fixture"
        blocked = deepcopy(self.current)
        blocked["input_status"] = "blocked"
        self.assertEqual(
            build_context_bundle(
                self.state, protected, "accuracy", module_view=include_view
            )["neighbors"],
            [],
        )
        self.assertEqual(
            build_context_bundle(
                self.state, blocked, "accuracy", module_view=include_view
            )["neighbors"],
            [],
        )

    def test_unknown_rule_reference_and_tampered_source_fail_closed(self):
        unknown = deepcopy(self.current)
        unknown["resolved_constraints"][0]["rule_id"] = "rule.missing"
        with self.assertRaisesRegex(ContextBundleError, "unknown rule id"):
            build_context_bundle(
                self.state, unknown, "accuracy", module_view=self.view
            )

        changed = deepcopy(self.current)
        changed["source"] = "Changed source"
        with self.assertRaisesRegex(ContextBundleError, "source_digest"):
            build_context_bundle(
                self.state, changed, "accuracy", module_view=self.view
            )

        changed_neighbor = deepcopy(self.state)
        changed_neighbor["segments"][0]["source"] = "Changed neighbor source"
        with self.assertRaisesRegex(ContextBundleError, "source_digest"):
            build_context_bundle(
                changed_neighbor,
                changed_neighbor["segments"][2],
                "accuracy",
                module_view=self.view,
            )

        rules = json.loads(self.files["context_rules"].read_text(encoding="utf-8"))
        rules["rules"][0]["rule_status"] = "draft"
        self.files["context_rules"].write_text(
            json.dumps(rules, ensure_ascii=False), encoding="utf-8"
        )
        self._rebind_asset("context_rules")
        with self.assertRaisesRegex(ContextBundleError, "non-confirmed rule"):
            build_context_bundle(
                self.state, self.current, "accuracy", module_view=self.view
            )

    def test_changed_neighbor_target_is_not_inherited_as_verified(self):
        changed_state = deepcopy(self.state)
        changed_state["segments"][4]["corrected"] = "Corrected Right"
        include_view = deepcopy(self.view)
        include_view["neighbors"]["include_target"] = True

        bundle = build_context_bundle(
            changed_state,
            changed_state["segments"][2],
            "accuracy",
            module_view=include_view,
        )

        right = next(item for item in bundle["neighbors"] if item["id"] == 4)
        self.assertEqual(right["target"], "Corrected Right")
        self.assertEqual(right["target_status"], "unverified")

    def test_bundle_digest_and_schema_tampering_are_rejected(self):
        bundle = build_context_bundle(
            self.state, self.current, "accuracy", module_view=self.view
        )
        self.assertEqual(validate_context_bundle(bundle), bundle)

        tampered = deepcopy(bundle)
        tampered["segment"]["current_target"] = "Tampered"
        with self.assertRaisesRegex(ContextBundleError, "digest mismatch"):
            validate_context_bundle(tampered)

        unknown = deepcopy(bundle)
        unknown["extra"] = True
        unknown["context_bundle_digest"] = canonical_digest(
            {key: value for key, value in unknown.items() if key != "context_bundle_digest"}
        )
        with self.assertRaisesRegex(ContextBundleError, "unknown property"):
            validate_context_bundle(unknown)

        noncanonical_view = deepcopy(bundle)
        noncanonical_view["module_view"]["capabilities"].reverse()
        noncanonical_view["context_bundle_digest"] = canonical_digest(
            {
                key: value
                for key, value in noncanonical_view.items()
                if key != "context_bundle_digest"
            }
        )
        with self.assertRaisesRegex(ContextBundleError, "not canonical"):
            validate_context_bundle(noncanonical_view)

        unresolved_term = deepcopy(bundle)
        unresolved_term["term_evidence_ids"] = ["term.unbound"]
        unresolved_term["context_bundle_digest"] = canonical_digest(
            {
                key: value
                for key, value in unresolved_term.items()
                if key != "context_bundle_digest"
            }
        )
        with self.assertRaisesRegex(ContextBundleError, "term evidence IDs"):
            validate_context_bundle(unresolved_term)


class BundleSetAndManifestTests(ContextBundleFixture):
    def test_shared_assets_are_exactly_resolvable_and_have_no_extras(self):
        bundle_set = build_context_bundle_set(
            self.state,
            [self.current],
            "accuracy",
            module_view=self.view,
        )
        shared = bundle_set["shared_context_assets"]

        self.assertEqual(
            set(shared["entities"]),
            {"fact.operator.verified", "fact.supervisor.source"},
        )
        self.assertEqual(
            set(shared["relations"]), {"relation.operator.supervisor.verified"}
        )
        self.assertEqual(set(shared["review_examples"]), {"example.match"})
        self.assertEqual(set(shared["constraints"]), {"rule.register.plain"})
        self.assertEqual(
            validate_shared_context_assets(shared, bundle_set["bundles"]),
            shared,
        )

        extra = deepcopy(shared)
        extra["review_examples"]["extra"] = deepcopy(
            shared["review_examples"]["example.match"]
        )
        extra["review_examples"]["extra"]["id"] = "extra"
        with self.assertRaisesRegex(ContextBundleError, r"extra=\['extra'\]"):
            validate_shared_context_assets(extra, bundle_set["bundles"])

    def test_manifest_rejects_digest_valid_but_noncanonical_shared_asset(self):
        bundle_set = build_context_bundle_set(
            self.state,
            [self.current],
            "accuracy",
            module_view=self.view,
        )
        tampered = deepcopy(bundle_set)
        fact = tampered["shared_context_assets"]["entities"][
            "fact.operator.verified"
        ]
        fact["fact"]["text"] = "Invented worker-visible fact"
        tampered["context_bundle_set_digest"] = canonical_digest(
            {
                key: value
                for key, value in tampered.items()
                if key != "context_bundle_set_digest"
            }
        )

        with self.assertRaisesRegex(
            ContextBundleError, "canonical project assets"
        ):
            build_worker_context_manifest(
                self.state,
                "accuracy",
                tampered,
                max_worker_bytes=1_000_000,
                common_instructions_path=self.files["common"],
                module_instructions_path=self.files["module"],
                suggestion_instructions_path=self.files["suggestions"],
            )

    def test_worker_manifest_binds_inputs_digests_and_complete_byte_budget(self):
        bundle_set = build_context_bundle_set(
            self.state,
            [self.current],
            "accuracy",
            module_view=self.view,
        )
        packets = [{"packet": 1, "segments": [{"id": 2}]}]
        manifest = build_worker_context_manifest(
            self.state,
            "accuracy",
            bundle_set,
            max_worker_bytes=1_000_000,
            packet_payloads=packets,
            common_instructions_path=self.files["common"],
            module_instructions_path=self.files["module"],
            suggestion_instructions_path=self.files["suggestions"],
        )

        self.assertEqual(validate_worker_context_manifest(manifest), manifest)
        self.assertEqual(manifest["profile"]["digest"], self.state["profile_digest"])
        self.assertEqual(
            manifest["capability_resolution"]["digest"],
            self.state["capability_resolution_digest"],
        )
        self.assertEqual(
            manifest["project_asset_snapshot"]["digest"],
            self.state["project_asset_snapshot_digest"],
        )
        asset_summaries = {
            item["asset_id"]: item for item in manifest["project_assets"]
        }
        self.assertEqual(
            asset_summaries["entities"]["document_digest"],
            load_project_context_assets(self.state)["asset_bindings"]["entities"][
                "document_digest"
            ],
        )
        self.assertIsNone(asset_summaries["checks"]["document_digest"])
        self.assertEqual(
            manifest["context_bundles"]["digest"],
            bundle_set["context_bundle_set_digest"],
        )
        self.assertEqual(manifest["packet_payloads"]["digest"], canonical_digest(packets))
        self.assertEqual(
            {item["id"] for item in manifest["source_manifests"]},
            {"source_manifest_path", "project_asset:project_sources"},
        )
        self.assertEqual(
            manifest["instructions"]["module"]["sha256"],
            hashlib.sha256(self.files["module"].read_bytes()).hexdigest(),
        )
        self.assertEqual(manifest["budget"]["status"], "within_budget")
        self.assertGreater(manifest["budget"]["measured_bytes"], 0)
        self.assertEqual(
            manifest["language_providers"][0]["id"], "ko.register"
        )

        with self.assertRaisesRegex(WorkerContextBudgetError, "split the batch"):
            build_worker_context_manifest(
                self.state,
                "accuracy",
                bundle_set,
                max_worker_bytes=manifest["budget"]["measured_bytes"] - 1,
                packet_payloads=packets,
                common_instructions_path=self.files["common"],
                module_instructions_path=self.files["module"],
                suggestion_instructions_path=self.files["suggestions"],
            )

    def test_budget_helpers_measure_utf8_and_never_truncate(self):
        components = ["中文", {"b": 2, "a": 1}, b"raw"]
        expected = len("中文".encode("utf-8")) + len(
            json.dumps(
                {"b": 2, "a": 1},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ) + 3

        self.assertEqual(calculate_worker_input_bytes(components), expected)
        self.assertEqual(enforce_worker_byte_budget(components, expected), expected)
        with self.assertRaises(WorkerContextBudgetError):
            enforce_worker_byte_budget(components, expected - 1)

    def test_manifest_missing_source_manifest_and_digest_tampering_fail_closed(self):
        bundle_set = build_context_bundle_set(
            self.state,
            [self.current],
            "accuracy",
            module_view=self.view,
        )
        minimal_profile = self._profile()
        for asset_id in (
            "entities",
            "examples",
            "context_rules",
            "project_sources",
        ):
            del minimal_profile["assets"][asset_id]
        for capability_id in (
            "assets.entity_registry@1",
            "assets.review_examples@1",
            "language_policy.register@1",
        ):
            del minimal_profile["capabilities"][capability_id]
        missing = self._state(minimal_profile)
        del missing["source_manifest_path"]
        missing["segments"][2]["resolved_constraints"] = []
        minimal_view = {
            "capabilities": ["context.core@1"],
            "dimensions": ["accuracy"],
            "include_constraints": False,
            "limits": {
                "max_facts_per_entity": 0,
                "max_relations": 0,
                "max_runtime_examples": 0,
            },
        }
        missing_bundle_set = build_context_bundle_set(
            missing,
            [missing["segments"][2]],
            "accuracy",
            module_view=minimal_view,
        )
        with self.assertRaisesRegex(ContextBundleError, "at least one source manifest"):
            build_worker_context_manifest(
                missing,
                "accuracy",
                missing_bundle_set,
                max_worker_bytes=1_000_000,
                common_instructions_path=self.files["common"],
                module_instructions_path=self.files["module"],
                suggestion_instructions_path=self.files["suggestions"],
            )

        manifest = build_worker_context_manifest(
            self.state,
            "accuracy",
            bundle_set,
            max_worker_bytes=1_000_000,
            common_instructions_path=self.files["common"],
            module_instructions_path=self.files["module"],
            suggestion_instructions_path=self.files["suggestions"],
        )
        manifest["budget"]["measured_bytes"] += 1
        with self.assertRaisesRegex(ContextBundleError, "digest mismatch"):
            validate_worker_context_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
