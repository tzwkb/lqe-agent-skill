import json
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lqe_capabilities import normalize_profile, resolve_capabilities
from lqe_context import descriptor_registry
from lqe_context_bundle import canonical_digest
from lqe_profile_ingest import source_digest
from lqe_project_assets import asset_statuses, inspect_project_assets
import lqe_suggestion_review
import lqe_suggestions
from lqe_suggestion_review import (
    build_final_artifact,
    build_review_artifact,
    build_review_packet,
    validate_review_artifact,
)
from lqe_suggestions import validate_suggestion_artifact


SUGGESTIONS = SCRIPTS / "lqe_suggestions.py"
REVIEW = SCRIPTS / "lqe_suggestion_review.py"


def source_semantics():
    return {
        "subjects": ["source subject"],
        "actions": ["source action"],
        "objects": [],
        "negation": {"present": False, "scope": None},
        "polarity": "affirmative",
        "modality": [],
        "speech_act": "statement",
        "text_function": "inform",
        "intensity": "neutral",
        "omitted_source_elements": [],
        "unsupported_additions": [],
    }


def tone_decision():
    return {
        "register": "neutral",
        "politeness": "neutral",
        "depends_on_dialogue_context": False,
        "evidence": [{"type": "source_form", "value": "neutral source"}],
        "uncertainties": [],
    }


def generation_entry(segment_id, reference_target):
    return {
        "id": segment_id,
        "reference_target": reference_target,
        "source_semantics": source_semantics(),
        "tone_decision": tone_decision(),
    }


def semantic_verification(status="pass"):
    fields = (
        "subjects",
        "actions",
        "objects",
        "polarity_negation",
        "modality",
        "speech_act",
        "text_function",
        "intensity",
        "omissions",
        "unsupported_additions",
        "tone",
    )
    return {
        field: {"status": status, "evidence": f"{field} checked against source."}
        for field in fields
    }


def write_json(path: Path, value: object) -> None:
    if (
        path.name == "state.json"
        and isinstance(value, dict)
        and isinstance(value.get("segments"), list)
        and "job_runtime_contract_version" not in value
    ):
        value = {**value, "job_runtime_contract_version": 2}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def issue(category: str, comment: str, *, edit=None, needs_confirmation=True) -> dict:
    return {
        "category": category,
        "severity": "Major",
        "comment": comment,
        "needs_confirmation": needs_confirmation,
        "edit": edit,
    }


class SuggestionReviewCliE2ETests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.job = Path(self.tempdir.name) / "job"
        self.job.mkdir(parents=True)
        (self.job / "checks.json").write_text("{}", encoding="utf-8")
        (self.job / "sg.md").write_text("# Test style\n", encoding="utf-8")
        (self.job / "confirmed_rules.md").write_text(
            "# Confirmed test rules\n",
            encoding="utf-8",
        )
        write_json(
            self.job / "input_manifest.json",
            {"schema": "fixture.input-manifest", "version": 1},
        )
        segments = [
            {"id": 0, "source": "Correct meaning", "target": "Wrong {0}", "protected_texts": []},
            {"id": 1, "source": "Fix spelling", "target": "bad", "protected_texts": []},
            {"id": 2, "source": "Complete meaning", "target": "Partial", "protected_texts": []},
            {"id": 3, "source": "Improve style", "target": "Awkward", "protected_texts": []},
        ]
        for segment in segments:
            segment["segment_key"] = f"segment-{segment['id']}"
            segment["key_origin"] = "generated"
            segment["source_digest"] = source_digest(segment["source"])
            segment["input_status"] = "ready"
            segment["input_block_reasons"] = []
            segment["input_warnings"] = []
            segment["protected"] = False
            segment["protected_reason"] = None
            segment["context"] = {
                "context_contract_version": 1,
                "status": "ready",
                "core": {"content_type": "general", "context_note": None},
                "extensions": {},
                "provenance": {},
                "missing_required": [],
            }
            segment["resolved_constraints"] = []
            segment["segment_revision_digest"] = canonical_digest({
                "id": segment["id"],
                "source": segment["source"],
                "target": segment["target"],
                "context": segment["context"],
            })
        def declared_asset(kind: str, path: str) -> dict:
            return {
                "kind": kind,
                "path": path,
                "required": True,
                "authority": {"issuer": "test", "level": "authoritative"},
                "provenance": {"kind": "test_fixture"},
                "distribution": "internal_only",
                "availability": "included",
            }

        profile = normalize_profile({
            "profile_contract_version": 2,
            "name": "fixture/en-en",
            "language_pair": "en-en",
            "source_lang": "en",
            "target_lang": "en",
            "wordcount_basis": "source-chars",
            "scoring_policy": {
                "threshold": 98,
                "scorecard_profile": "legacy",
                "severity_scale": "lisa",
                "critical_gate": False,
                "repeat_dedup": True,
            },
            "assets": {
                "checks": declared_asset("checks", "checks.json"),
                "style": declared_asset("style_guide", "sg.md"),
                "confirmed": declared_asset(
                    "confirmed_rules", "confirmed_rules.md"
                ),
            },
            "context_pipeline": {"mode": "off"},
            "capabilities": {
                "context.core@1": {
                    "required": True,
                    "config": {
                        "identity": {
                            "key_columns": ["key"],
                            "fallback": "source_coordinate",
                        }
                    },
                },
                "source_provenance@1": {"required": True},
            },
        })
        inspection = inspect_project_assets(
            profile,
            profile_dir=self.job,
            strict_required=True,
        )
        resolution = resolve_capabilities(
            profile,
            asset_statuses=asset_statuses(inspection["snapshot"]),
        )
        state = {
            "profile_digest": profile["source_profile_digest"],
            "profile_overlay_digest": None,
            "project_asset_snapshot": inspection["snapshot"],
            "project_asset_snapshot_digest": inspection["snapshot"]["digest"],
            "project_asset_paths": {
                asset_id: str(path.resolve())
                for asset_id, path in inspection["resolved_paths"].items()
            },
            "capability_resolution": resolution,
            "capability_resolution_digest": resolution["digest"],
            "resolved_context_descriptors": descriptor_registry(
                profile,
                capability_resolution=resolution,
            ),
            "source_manifest_path": str(
                (self.job / "input_manifest.json").resolve()
            ),
            "sg_path": str((self.job / "sg.md").resolve()),
            "confirmed_rules_path": str(
                (self.job / "confirmed_rules.md").resolve()
            ),
            "input_format": "tabular",
            "input_path": str(self.job / "input.csv"),
            "source_lang": "en",
            "target_lang": "en",
            "wordcount": 20,
            "iteration": 0,
            "segments": segments,
        }
        errors = [
            {"id": 0, "errors": [issue("Mistranslation", "Meaning is wrong.")], "corrected": None},
            {
                "id": 1,
                "errors": [issue(
                    "Grammar",
                    "Spelling is wrong.",
                    needs_confirmation=False,
                    edit={"from": "bad", "to": "good", "evidence": None},
                )],
                "corrected": "good",
            },
            {"id": 2, "errors": [issue("Omission", "Meaning is incomplete.")], "corrected": None},
            {"id": 3, "errors": [issue("Unidiomatic", "Style is awkward.")], "corrected": None},
        ]
        write_json(self.job / "state.json", state)
        write_json(self.job / "errors.json", errors)

    def tearDown(self):
        self.tempdir.cleanup()

    def run_script(self, script: Path, *args: object) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(script), *map(str, args)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def assert_ok(self, result: subprocess.CompletedProcess) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)

    def prepare_candidates(self) -> tuple[dict, dict]:
        prepared = self.run_script(SUGGESTIONS, "prepare", "--job", self.job)
        self.assert_ok(prepared)
        packet = json.loads(
            (self.job / "reference_suggestions.packet.json").read_text(encoding="utf-8")
        )
        draft = {
            "schema": "lqe.reference-suggestion-generation-draft",
            "version": 5,
            "packet_digest": packet["packet_digest"],
            "worker_context_manifest_digest": packet[
                "worker_context_manifest_digest"
            ],
            "worker_receipt": {
                "worker_id": "generation-worker",
                "run_id": "generation-run",
            },
            "selection": packet["selection"],
            "reviewed_ids": packet["reviewed_ids"],
            "entries": [
                generation_entry(0, "Right {0}"),
                generation_entry(2, "Complete"),
            ],
            "abstained_ids": [1, 3],
            "abstention_reasons": [
                {
                    "id": segment_id,
                    "reason_codes": ["SOURCE_INTENT_UNCERTAIN"],
                    "evidence": "The available source/context evidence is insufficient.",
                }
                for segment_id in (1, 3)
            ],
        }
        draft_path = self.job / "reference_suggestions.draft.json"
        write_json(draft_path, draft)
        published = self.run_script(
            SUGGESTIONS,
            "publish-candidates",
            "--job",
            self.job,
            "--input",
            draft_path,
        )
        self.assert_ok(published)
        candidates = json.loads(
            (self.job / "reference_suggestions.candidates.json").read_text(encoding="utf-8")
        )
        return packet, candidates

    def test_candidate_review_final_chain_and_live_validation(self):
        generation_packet, candidates = self.prepare_candidates()
        worker_manifest = json.loads(
            (self.job / "suggestion_context" / "worker_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        context_digest = generation_packet["worker_context_manifest_digest"]
        self.assertEqual(
            context_digest,
            worker_manifest["worker_context_manifest_digest"],
        )
        self.assertEqual(worker_manifest["budget"]["max_bytes"], 100_000)
        self.assertLessEqual(worker_manifest["budget"]["measured_bytes"], 100_000)
        self.assertTrue(worker_manifest["worker_documents"])
        self.assertEqual(
            {
                item["id"]: item["locator"]["kind"]
                for item in worker_manifest["worker_documents"]
            },
            {
                "confirmed_rules_path": "job_relative",
                "sg_path": "job_relative",
            },
        )
        self.assertEqual(candidates["worker_context_manifest_digest"], context_digest)
        self.assertEqual(
            [route["risk_route"] for route in candidates["routes"]],
            [
                "independent_verifier",
                "hard_reject",
                "independent_verifier",
                "hard_reject",
            ],
        )
        self.assertEqual(
            [item["id"] for item in candidates["abstention_reasons"]],
            [1, 3],
        )
        self.assertEqual(
            candidates["routes"][1]["reason_codes"],
            ["WORKER_ABSTAINED", "SOURCE_INTENT_UNCERTAIN"],
        )

        prepared = self.run_script(REVIEW, "prepare", "--job", self.job)
        self.assert_ok(prepared)
        packet = json.loads(
            (self.job / "suggestion_review.packet.json").read_text(encoding="utf-8")
        )
        self.assertEqual(packet["reviewed_ids"], [0, 2])
        self.assertEqual(packet["worker_context_manifest_digest"], context_digest)
        self.assertTrue(
            packet["instructions"]["worker_context"][
                "same_evidence_as_generation"
            ]
        )
        verifier_instructions = packet["instructions"]["verifier_instructions"]
        verifier_payload = (ROOT / "references" / "suggestion_review.md").read_bytes()
        self.assertEqual(
            verifier_instructions,
            {
                "path": "references/suggestion_review.md",
                "sha256": hashlib.sha256(verifier_payload).hexdigest(),
                "bytes": len(verifier_payload),
            },
        )
        self.assertEqual(
            packet["entries"][0]["context_bundle_digest"],
            generation_packet["segments"][0]["context_bundle_digest"],
        )
        self.assertIn("context_projection", packet["entries"][0])
        draft = {
            "schema": "lqe.suggestion-review-draft",
            "version": 1,
            "review_packet_digest": packet["packet_digest"],
            "worker_context_manifest_digest": packet[
                "worker_context_manifest_digest"
            ],
            "worker_receipt": {
                "worker_id": "review-worker",
                "run_id": "review-run",
            },
            "reviewed_ids": packet["reviewed_ids"],
            "verdicts": [
                {
                    "id": 0,
                    "candidate_digest": packet["entries"][0]["candidate_digest"],
                    "decision": "accept",
                    "reason_codes": [],
                    "evidence": "Meaning, issues, placeholders and constraints pass.",
                    "semantic_verification": semantic_verification(),
                },
                {
                    "id": 2,
                    "candidate_digest": packet["entries"][1]["candidate_digest"],
                    "decision": "reject",
                    "reason_codes": ["OMISSION_REMAINS"],
                    "evidence": "The candidate still loses source detail.",
                    "semantic_verification": semantic_verification("fail"),
                },
            ],
        }
        write_json(self.job / "suggestion_review.draft.json", draft)
        self.assert_ok(self.run_script(REVIEW, "publish-review", "--job", self.job))
        self.assert_ok(self.run_script(REVIEW, "publish-final", "--job", self.job))
        self.assert_ok(self.run_script(SUGGESTIONS, "validate", "--job", self.job))

        review = json.loads(
            (self.job / "suggestion_review.json").read_text(encoding="utf-8")
        )

        final = json.loads(
            (self.job / "reference_suggestions.json").read_text(encoding="utf-8")
        )
        self.assertEqual(review["worker_context_manifest_digest"], context_digest)
        self.assertEqual(final["worker_context_manifest_digest"], context_digest)
        self.assertEqual([entry["id"] for entry in final["final_entries"]], [0])
        self.assertEqual([entry["id"] for entry in final["excluded_ids"]], [1, 2, 3])

        state = json.loads((self.job / "state.json").read_text(encoding="utf-8"))
        state["segments"][0]["target"] = "Changed {0}"
        write_json(self.job / "state.json", state)
        stale = self.run_script(SUGGESTIONS, "validate", "--job", self.job)
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("stale", stale.stderr)

    def test_suggestion_context_is_required_module_union_and_has_readable_index(self):
        prepared = self.run_script(SUGGESTIONS, "prepare", "--job", self.job)
        self.assert_ok(prepared)
        packet = json.loads(
            (self.job / "reference_suggestions.packet.json").read_text(
                encoding="utf-8"
            )
        )
        basis = packet["context_view_basis"]
        self.assertEqual(
            basis["source_modules"],
            ["terminology", "accuracy", "grammar", "naturalness", "suggestions"],
        )
        self.assertEqual(
            set(basis["merged_view"]["dimensions"]),
            {"terminology", "accuracy", "grammar", "naturalness", "suggestions"},
        )
        index_path = self.job / packet["content_index"]["path"]
        index = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertEqual(
            packet["content_index"]["digest"],
            index["content_index_digest"],
        )
        self.assertTrue(index["resources"])
        self.assertTrue(all(
            resource["delivery"] in {"job_relative_path", "embedded_text"}
            for resource in index["resources"]
        ))

    def test_review_worker_must_be_independent_and_accept_all_semantic_checks(self):
        self.prepare_candidates()
        self.assert_ok(self.run_script(REVIEW, "prepare", "--job", self.job))
        packet = json.loads(
            (self.job / "suggestion_review.packet.json").read_text(encoding="utf-8")
        )
        verdicts = [{
            "id": entry["id"],
            "candidate_digest": entry["candidate_digest"],
            "decision": "accept",
            "reason_codes": [],
            "evidence": "Checked independently.",
            "semantic_verification": semantic_verification(),
        } for entry in packet["entries"]]
        same_worker = {
            "schema": "lqe.suggestion-review-draft",
            "version": 1,
            "review_packet_digest": packet["packet_digest"],
            "worker_context_manifest_digest": packet[
                "worker_context_manifest_digest"
            ],
            "worker_receipt": {
                "worker_id": "generation-worker",
                "run_id": "review-run-2",
            },
            "reviewed_ids": packet["reviewed_ids"],
            "verdicts": verdicts,
        }
        with self.assertRaisesRegex(ValueError, "worker_id must differ"):
            lqe_suggestion_review.validate_review_draft(same_worker, packet)

        failed_check = json.loads(json.dumps(same_worker))
        failed_check["worker_receipt"] = {
            "worker_id": "review-worker-2",
            "run_id": "review-run-2",
        }
        failed_check["verdicts"][0]["semantic_verification"]["unsupported_additions"][
            "status"
        ] = "fail"
        with self.assertRaisesRegex(ValueError, "without passing every"):
            lqe_suggestion_review.validate_review_draft(failed_check, packet)

    def test_oversized_suggestions_are_batched_and_merged_without_truncation(self):
        state = json.loads((self.job / "state.json").read_text(encoding="utf-8"))
        segments = []
        errors = []
        for segment_id in range(14):
            source = f"Source {segment_id} " + ("x" * 8_000)
            target = f"Target {segment_id}"
            segment = {
                "id": segment_id,
                "source": source,
                "target": target,
                "protected_texts": [],
                "segment_key": f"segment-{segment_id}",
                "key_origin": "generated",
                "source_digest": source_digest(source),
                "input_status": "ready",
                "input_block_reasons": [],
                "input_warnings": [],
                "protected": False,
                "protected_reason": None,
                "context": {
                    "context_contract_version": 1,
                    "status": "ready",
                    "core": {"content_type": "general", "context_note": None},
                    "extensions": {},
                    "provenance": {},
                    "missing_required": [],
                },
                "resolved_constraints": [],
            }
            segment["segment_revision_digest"] = canonical_digest({
                "id": segment_id,
                "source": source,
                "target": target,
                "context": segment["context"],
            })
            segments.append(segment)
            errors.append({
                "id": segment_id,
                "errors": [issue("Mistranslation", "Meaning is wrong.")],
                "corrected": None,
            })
        state["segments"] = segments
        state["wordcount"] = sum(len(segment["source"]) for segment in segments)
        write_json(self.job / "state.json", state)
        write_json(self.job / "errors.json", errors)

        prepared = self.run_script(SUGGESTIONS, "prepare", "--job", self.job)
        self.assert_ok(prepared)
        root_packet = json.loads(
            (self.job / "reference_suggestions.packet.json").read_text(
                encoding="utf-8"
            )
        )
        plan_path = self.job / root_packet["batch_plan"]["path"]
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertGreater(len(plan["batches"]), 1)
        covered = [
            segment_id
            for batch in plan["batches"]
            for segment_id in batch["reviewed_ids"]
        ]
        self.assertEqual(covered, list(range(14)))
        for batch_index, record in enumerate(plan["batches"]):
            packet = json.loads(
                (self.job / record["packet_path"]).read_text(encoding="utf-8")
            )
            draft = {
                "schema": "lqe.reference-suggestion-generation-draft",
                "version": 5,
                "packet_digest": packet["packet_digest"],
                "worker_context_manifest_digest": packet[
                    "worker_context_manifest_digest"
                ],
                "worker_receipt": {
                    "worker_id": f"generation-worker-{batch_index}",
                    "run_id": f"generation-run-{batch_index}",
                },
                "selection": packet["selection"],
                "reviewed_ids": packet["reviewed_ids"],
                "entries": [
                    generation_entry(segment["id"], f"Reviewed {segment['id']}")
                    for segment in packet["segments"]
                ],
                "abstained_ids": [],
                "abstention_reasons": [],
            }
            write_json(self.job / record["draft_path"], draft)
        published = self.run_script(
            SUGGESTIONS,
            "publish-candidates",
            "--job",
            self.job,
            "--input",
            self.job / "suggestion_context" / "batches",
        )
        self.assert_ok(published)
        candidates = json.loads(
            (self.job / "reference_suggestions.candidates.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(len(candidates["routes"]), 14)
        self.assertEqual(len(candidates["generation_worker_receipts"]), len(plan["batches"]))

        self.assert_ok(self.run_script(REVIEW, "prepare", "--job", self.job))
        review_root = json.loads(
            (self.job / "suggestion_review.packet.json").read_text(encoding="utf-8")
        )
        review_plan = json.loads(
            (self.job / review_root["batch_plan"]["path"]).read_text(encoding="utf-8")
        )
        review_covered = [
            segment_id
            for batch in review_plan["batches"]
            for segment_id in batch["reviewed_ids"]
        ]
        self.assertEqual(review_covered, list(range(14)))
        for batch_index, record in enumerate(review_plan["batches"]):
            packet = json.loads(
                (self.job / record["packet_path"]).read_text(encoding="utf-8")
            )
            draft = {
                "schema": "lqe.suggestion-review-draft",
                "version": 1,
                "review_packet_digest": packet["packet_digest"],
                "worker_context_manifest_digest": packet[
                    "worker_context_manifest_digest"
                ],
                "worker_receipt": {
                    "worker_id": f"review-worker-{batch_index}",
                    "run_id": f"review-run-{batch_index}",
                },
                "reviewed_ids": packet["reviewed_ids"],
                "verdicts": [{
                    "id": entry["id"],
                    "candidate_digest": entry["candidate_digest"],
                    "decision": "accept",
                    "reason_codes": [],
                    "evidence": "Every semantic and tone field matches.",
                    "semantic_verification": semantic_verification(),
                } for entry in packet["entries"]],
            }
            write_json(self.job / record["draft_path"], draft)
        self.assert_ok(self.run_script(REVIEW, "publish-review", "--job", self.job))
        self.assert_ok(self.run_script(REVIEW, "publish-final", "--job", self.job))
        self.assert_ok(self.run_script(SUGGESTIONS, "validate", "--job", self.job))
        final = json.loads(
            (self.job / "reference_suggestions.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [entry["id"] for entry in final["final_entries"]],
            list(range(14)),
        )

    def test_verifier_instruction_change_stales_review_and_final(self):
        generation_packet, candidates = self.prepare_candidates()
        packet = build_review_packet(generation_packet, candidates)
        draft = {
            "schema": "lqe.suggestion-review-draft",
            "version": 1,
            "review_packet_digest": packet["packet_digest"],
            "worker_context_manifest_digest": packet[
                "worker_context_manifest_digest"
            ],
            "worker_receipt": {
                "worker_id": "review-worker",
                "run_id": "review-run",
            },
            "reviewed_ids": packet["reviewed_ids"],
            "verdicts": [
                {
                    "id": entry["id"],
                    "candidate_digest": entry["candidate_digest"],
                    "decision": "accept",
                    "reason_codes": [],
                    "evidence": "Source meaning and constraints pass.",
                    "semantic_verification": semantic_verification(),
                }
                for entry in packet["entries"]
            ],
        }
        review = build_review_artifact(packet, draft)
        final = build_final_artifact(generation_packet, candidates, review)
        state = json.loads((self.job / "state.json").read_text(encoding="utf-8"))
        changed = Path(self.tempdir.name) / "changed_suggestion_review.md"
        changed.write_text("changed verifier contract", encoding="utf-8")

        with patch.object(
            lqe_suggestions,
            "SUGGESTION_REVIEW_INSTRUCTIONS_PATH",
            changed,
        ):
            live_packet = build_review_packet(generation_packet, candidates)
            with self.assertRaisesRegex(ValueError, "stale"):
                validate_review_artifact(review, live_packet)
            with self.assertRaisesRegex(ValueError, "verifier instructions are stale"):
                validate_suggestion_artifact(
                    final,
                    generation_packet,
                    state["segments"],
                    candidate_artifact=candidates,
                    review_artifact=review,
                )

    def test_verifier_instruction_bytes_count_toward_worker_budget(self):
        self.prepare_candidates()
        oversized = Path(self.tempdir.name) / "oversized_suggestion_review.md"
        oversized.write_bytes(b"x" * 100_000)

        with patch.object(
            lqe_suggestions,
            "SUGGESTION_REVIEW_INSTRUCTIONS_PATH",
            oversized,
        ):
            with self.assertRaisesRegex(ValueError, "exceeding budget 100000"):
                lqe_suggestion_review._load_live_chain(
                    self.job,
                    "state.json",
                    "errors.json",
                    command="suggestion-review-prepare",
                )

    def test_publish_rebuilds_and_rejects_tampered_worker_context(self):
        prepared = self.run_script(SUGGESTIONS, "prepare", "--job", self.job)
        self.assert_ok(prepared)
        packet = json.loads(
            (self.job / "reference_suggestions.packet.json").read_text(
                encoding="utf-8"
            )
        )
        draft = {
            "schema": "lqe.reference-suggestion-generation-draft",
            "version": 5,
            "packet_digest": packet["packet_digest"],
            "worker_context_manifest_digest": packet[
                "worker_context_manifest_digest"
            ],
            "worker_receipt": {
                "worker_id": "generation-worker",
                "run_id": "generation-run",
            },
            "selection": packet["selection"],
            "reviewed_ids": packet["reviewed_ids"],
            "entries": [],
            "abstained_ids": [entry["id"] for entry in packet["segments"]],
            "abstention_reasons": [
                {
                    "id": entry["id"],
                    "reason_codes": ["SOURCE_INTENT_UNCERTAIN"],
                    "evidence": "The worker cannot establish source intent.",
                }
                for entry in packet["segments"]
            ],
        }
        draft_path = self.job / "reference_suggestions.draft.json"
        write_json(draft_path, draft)

        manifest_path = self.job / "suggestion_context" / "worker_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["budget"]["measured_bytes"] += 1
        write_json(manifest_path, manifest)
        result = self.run_script(
            SUGGESTIONS,
            "publish-candidates",
            "--job",
            self.job,
            "--input",
            draft_path,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manifest digest mismatch", result.stderr)

    def test_generation_total_worker_input_budget_is_100000_bytes(self):
        (self.job / "sg.md").write_text("x" * 100_001, encoding="utf-8")
        state = json.loads((self.job / "state.json").read_text(encoding="utf-8"))
        payload = (self.job / "sg.md").read_bytes()
        style = state["project_asset_snapshot"]["assets"]["style"]
        import hashlib

        style["sha256"] = hashlib.sha256(payload).hexdigest()
        style["size"] = len(payload)
        snapshot = state["project_asset_snapshot"]
        snapshot["digest"] = canonical_digest({
            key: value for key, value in snapshot.items() if key != "digest"
        })
        state["project_asset_snapshot_digest"] = snapshot["digest"]
        write_json(self.job / "state.json", state)

        result = self.run_script(SUGGESTIONS, "prepare", "--job", self.job)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exceeding budget 100000", result.stderr)

    def test_verifier_draft_cannot_modify_candidate_text(self):
        self.prepare_candidates()
        self.assert_ok(self.run_script(REVIEW, "prepare", "--job", self.job))
        packet = json.loads(
            (self.job / "suggestion_review.packet.json").read_text(encoding="utf-8")
        )
        verdicts = []
        for entry in packet["entries"]:
            verdicts.append({
                "id": entry["id"],
                "candidate_digest": entry["candidate_digest"],
                "decision": "accept",
                "reason_codes": [],
                "evidence": "Passes all checks.",
                "semantic_verification": semantic_verification(),
                "reference_target": "Tampered",
            })
        write_json(
            self.job / "suggestion_review.draft.json",
            {
                "schema": "lqe.suggestion-review-draft",
                "version": 1,
                "review_packet_digest": packet["packet_digest"],
                "worker_context_manifest_digest": packet[
                    "worker_context_manifest_digest"
                ],
                "worker_receipt": {
                    "worker_id": "review-worker",
                    "run_id": "review-run",
                },
                "reviewed_ids": packet["reviewed_ids"],
                "verdicts": verdicts,
            },
        )
        result = self.run_script(REVIEW, "publish-review", "--job", self.job)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Additional properties", result.stderr)

    def test_final_requires_independent_review_and_v4_is_rejected(self):
        self.prepare_candidates()
        missing = self.run_script(REVIEW, "publish-final", "--job", self.job)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("review artifact is required", missing.stderr)

        write_json(
            self.job / "reference_suggestions.json",
            {"schema": "lqe.reference-suggestions", "version": 4},
        )
        legacy = self.run_script(SUGGESTIONS, "validate", "--job", self.job)
        self.assertNotEqual(legacy.returncode, 0)
        self.assertIn("only independently reviewed v5", legacy.stderr)


if __name__ == "__main__":
    unittest.main()
