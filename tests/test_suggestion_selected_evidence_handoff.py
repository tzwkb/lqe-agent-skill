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

from lqe_split_contract import canonical_digest
from lqe_suggestions import (
    _strict_evidence_union,
    build_suggestion_content_index,
    validate_generation_draft,
)
from lqe_suggestion_review import validate_review_draft
from tests.runtime_helpers import publish_compact_modules


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def issue() -> dict:
    return {
        "category": "Mistranslation",
        "severity": "Major",
        "comment": "The target changes the source meaning.",
        "needs_confirmation": True,
        "edit": None,
    }


def source_semantics() -> dict:
    return {
        "subjects": ["implicit addressee"],
        "actions": ["go home"],
        "objects": ["home"],
        "negation": {"present": False, "scope": None},
        "polarity": "affirmative",
        "modality": ["imperative"],
        "speech_act": "command",
        "text_function": "dialogue",
        "intensity": "strong",
        "omitted_source_elements": [],
        "unsupported_additions": [],
    }


def tone_decision() -> dict:
    return {
        "register": "direct",
        "politeness": "plain imperative",
        "depends_on_dialogue_context": False,
        "evidence": [{"type": "source_form", "value": "立刻 and imperative punctuation"}],
        "uncertainties": [],
    }


def semantic_verification() -> dict:
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
        field: {"status": "pass", "evidence": f"{field} matches the source."}
        for field in fields
    }


class SuggestionSelectedEvidenceHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.job = self.root / "job"
        source = self.root / "input.csv"
        with source.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Key", "Source", "Target"])
            writer.writerow(["line-0", "立刻回家！", "Please wait."])
        self.assert_ok(self.run_script(
            "lqe_io.py",
            "read",
            "--input",
            source,
            "--source-col",
            "Source",
            "--target-col",
            "Target",
            "--key-col",
            "Key",
            "--source-lang",
            "zh",
            "--target-lang",
            "en",
            "--no-terminology",
            "--review-mode",
            "full",
            "--out",
            self.job / "state.json",
        ))
        write_json(
            self.job / "errors_precheck.json",
            [{"id": 0, "issues": []}],
        )
        self.assert_ok(self.run_script(
            "lqe_chunk.py",
            "split",
            "--state",
            self.job / "state.json",
            "--errors",
            self.job / "errors_precheck.json",
            "--outdir",
            self.job / "chunks",
            "--size",
            1,
        ))
        publish_compact_modules(
            self.job,
            {
                "precheck_review": [],
                "accuracy": [{"id": 0, "issues": [issue()]}],
                "grammar": [],
                "naturalness": [],
            },
        )
        for command in ("validate-checks", "merge-checks", "reconcile"):
            self.assert_ok(self.run_script(
                "lqe_chunk.py", command, "--job", self.job
            ))
        self.assert_ok(self.run_script(
            "lqe_chunk.py",
            "merge",
            "--state",
            self.job / "state.json",
            "--errors",
            self.job / "errors_precheck.json",
            "--outdir",
            self.job / "chunks",
            "--out",
            self.job / "errors.json",
        ))

    def tearDown(self):
        self.tempdir.cleanup()

    def run_script(self, script: str, *arguments: object) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), *map(str, arguments)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def assert_ok(self, result: subprocess.CompletedProcess) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_generation_uses_exact_checker_union_and_separate_worker(self):
        self.assert_ok(self.run_script(
            "lqe_suggestions.py", "prepare", "--job", self.job
        ))
        packet = json.loads(
            (self.job / "reference_suggestions.packet.json").read_text(
                encoding="utf-8"
            )
        )
        evidence_index = json.loads(
            (self.job / packet["selected_evidence_index"]["path"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            packet["selected_evidence_index"]["digest"],
            evidence_index["index_digest"],
        )
        self.assertTrue(packet["checker_worker_receipts"])
        segment = packet["segments"][0]
        evidence = segment["checker_selected_evidence"]
        self.assertEqual(evidence["reporting_checker_modules"], ["accuracy"])
        selected_modules = {
            item["module"]
            for item in evidence["strict_union"]["module_selections"]
        }
        self.assertIn("accuracy", selected_modules)
        self.assertEqual(
            packet["context_view_basis"]["evidence_mode"],
            "checker_selected_strict_union",
        )
        self.assertEqual(
            packet["context_view_basis"]["projection_only_view"]["limits"],
            {
                "max_facts_per_entity": 0,
                "max_relations": 0,
                "max_runtime_examples": 0,
            },
        )
        self.assertEqual(
            packet["context_view_basis"]["projection_only_view"]["capabilities"],
            ["context.core@1"],
        )
        self.assertEqual(
            segment["context_projection"],
            evidence["strict_union"]["context_projection"],
        )

        checker = next(
            item
            for item in packet["checker_worker_receipts"]
            if item["worker_id"] == "test-checker.accuracy"
        )
        draft = {
            "schema": "lqe.reference-suggestion-generation-draft",
            "version": 5,
            "packet_digest": packet["packet_digest"],
            "worker_context_manifest_digest": packet[
                "worker_context_manifest_digest"
            ],
            "selected_evidence_index_digest": packet[
                "selected_evidence_index"
            ]["digest"],
            "worker_receipt": checker,
            "selection": packet["selection"],
            "reviewed_ids": packet["reviewed_ids"],
            "entries": [],
            "abstained_ids": [0],
            "abstention_reasons": [{
                "id": 0,
                "reason_codes": ["SOURCE_INTENT_UNCERTAIN"],
                "evidence": "The generation worker abstained.",
            }],
        }
        with self.assertRaisesRegex(ValueError, "differ from checker workers"):
            validate_generation_draft(draft, packet)
        for colliding_receipt in (
            {
                "worker_id": checker["worker_id"],
                "run_id": "different-generation-run",
            },
            {
                "worker_id": "different-generation-worker",
                "run_id": checker["run_id"],
            },
        ):
            draft["worker_receipt"] = colliding_receipt
            with self.assertRaisesRegex(ValueError, "differ from checker workers"):
                validate_generation_draft(draft, packet)

        draft["worker_receipt"] = {
            "worker_id": "suggestion-generation",
            "run_id": "suggestion-generation-run",
        }
        self.assertEqual(validate_generation_draft(draft, packet), draft)

        generation = {
            **draft,
            "entries": [{
                "id": 0,
                "reference_target": "Go home now!",
                "source_semantics": source_semantics(),
                "tone_decision": tone_decision(),
            }],
            "abstained_ids": [],
            "abstention_reasons": [],
        }
        generation_path = self.job / "reference_suggestions.draft.json"
        write_json(generation_path, generation)
        self.assert_ok(self.run_script(
            "lqe_suggestions.py",
            "publish-candidates",
            "--job",
            self.job,
            "--input",
            generation_path,
        ))
        candidates = json.loads(
            (self.job / "reference_suggestions.candidates.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            candidates["selected_evidence_index"],
            packet["selected_evidence_index"],
        )
        self.assertEqual(
            candidates["checker_worker_receipts"],
            packet["checker_worker_receipts"],
        )

        self.assert_ok(self.run_script(
            "lqe_suggestion_review.py", "prepare", "--job", self.job
        ))
        review_packet = json.loads(
            (self.job / "suggestion_review.packet.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            review_packet["selected_evidence_index"],
            packet["selected_evidence_index"],
        )
        review_draft = {
            "schema": "lqe.suggestion-review-draft",
            "version": 1,
            "review_packet_digest": review_packet["packet_digest"],
            "worker_context_manifest_digest": review_packet[
                "worker_context_manifest_digest"
            ],
            "selected_evidence_index_digest": review_packet[
                "selected_evidence_index"
            ]["digest"],
            "worker_receipt": {
                "worker_id": "suggestion-verifier",
                "run_id": "suggestion-verifier-run",
            },
            "reviewed_ids": review_packet["reviewed_ids"],
            "verdicts": [{
                "id": 0,
                "candidate_digest": review_packet["entries"][0][
                    "candidate_digest"
                ],
                "decision": "accept",
                "reason_codes": [],
                "evidence": "The candidate matches all source and context evidence.",
                "semantic_verification": semantic_verification(),
                "rule_verifications": [
                    {
                        "rule_id": assertion["rule_id"],
                        "status": "pass",
                        "evidence": "The applicable rule is satisfied.",
                    }
                    for assertion in review_packet["entries"][0].get(
                        "applicable_rule_assertions", []
                    )
                ],
            }],
        }
        colliding_review = {
            **review_draft,
            "worker_receipt": checker,
        }
        with self.assertRaisesRegex(ValueError, "differ from checker workers"):
            validate_review_draft(colliding_review, review_packet)
        for colliding_receipt in (
            {
                "worker_id": checker["worker_id"],
                "run_id": "different-verifier-run",
            },
            {
                "worker_id": "different-verifier-worker",
                "run_id": checker["run_id"],
            },
        ):
            colliding_review["worker_receipt"] = colliding_receipt
            with self.assertRaisesRegex(ValueError, "differ from checker workers"):
                validate_review_draft(colliding_review, review_packet)
        write_json(self.job / "suggestion_review.draft.json", review_draft)
        self.assert_ok(self.run_script(
            "lqe_suggestion_review.py", "publish-review", "--job", self.job
        ))
        self.assert_ok(self.run_script(
            "lqe_suggestion_review.py", "publish-final", "--job", self.job
        ))
        self.assert_ok(self.run_script(
            "lqe_suggestions.py", "validate", "--job", self.job
        ))
        for name in ("suggestion_review.json", "reference_suggestions.json"):
            artifact = json.loads((self.job / name).read_text(encoding="utf-8"))
            self.assertEqual(
                artifact["selected_evidence_index"],
                packet["selected_evidence_index"],
            )
            self.assertEqual(
                artifact["checker_worker_receipts"],
                packet["checker_worker_receipts"],
            )

    def test_tampered_checker_index_fails_live_prepare(self):
        index_path = self.job / "review_packets" / "selected_evidence_index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["entries"][0]["context_status"] = (
            "ready"
            if index["entries"][0]["context_status"] != "ready"
            else "unknown"
        )
        write_json(index_path, index)
        result = self.run_script(
            "lqe_suggestions.py", "prepare", "--job", self.job
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("digest mismatch", result.stderr)


class StrictEvidenceUnionUnitTests(unittest.TestCase):
    def test_union_contains_only_checker_selected_records(self):
        constraint = {"kind": "context.dialogue@1", "value": "plain"}
        context_projection = {
            "core": {"content_type": "dialogue"},
            "extensions": {
                "dialogue": {
                    "status": "ready",
                    "speaker_id": "speaker",
                }
            },
        }
        neighbor = {
            "id": 8,
            "segment_key": "neighbor-8",
            "source": "Prior line",
            "input_status": "ready",
            "protected": False,
            "context": {"core": {"content_type": "dialogue"}},
            "target": None,
            "target_status": "omitted",
        }

        def record(module: str, fact_id: str, example_id: str) -> dict:
            entry = {
                "module": module,
                "entity_fact_ids": [fact_id],
                "relation_ids": ["relation-1"],
                "runtime_example_ids": [example_id],
                "term_evidence_ids": [],
                "resolved_constraint_digests": [canonical_digest(constraint)],
                "context_projection_digest": canonical_digest(context_projection),
                "selection_digest": canonical_digest({"module": module}),
            }
            return {
                "entry": entry,
                "bundle": {
                    "segment": {"context": context_projection},
                    "resolved_constraints": [constraint],
                    "neighbors": [neighbor],
                },
                "shared_context_assets": {
                    "entities": {
                        fact_id: {"entity_id": "speaker", "fact": {"id": fact_id}}
                    },
                    "relations": {
                        "relation-1": {"id": "relation-1", "from": "speaker", "to": "listener"}
                    },
                    "review_examples": {
                        example_id: {"id": example_id, "source": "Example"}
                    },
                    "constraints": {},
                },
                "context_bundle_set_path": f"review_packets/{module}/bundle_set.json",
                "context_bundle_set_digest": canonical_digest(module),
            }

        union = _strict_evidence_union([
            record("accuracy", "fact-1", "example-1"),
            record("naturalness", "fact-2", "example-2"),
        ])
        self.assertEqual(set(union["entity_facts"]), {"fact-1", "fact-2"})
        self.assertEqual(set(union["relations"]), {"relation-1"})
        self.assertEqual(
            set(union["runtime_examples"]), {"example-1", "example-2"}
        )
        self.assertEqual(union["resolved_constraints"], [constraint])
        self.assertEqual(union["context_projection"], context_projection)
        self.assertEqual(
            [item["module"] for item in union["context_projections"]],
            ["accuracy", "naturalness"],
        )
        self.assertEqual(len(union["neighbors"]), 1)
        self.assertEqual(
            union["neighbors"][0]["selected_by_modules"],
            ["accuracy", "naturalness"],
        )
        self.assertNotIn("unselected", union["entity_facts"])
        self.assertEqual(
            union["strict_union_digest"],
            canonical_digest({
                key: value
                for key, value in union.items()
                if key != "strict_union_digest"
            }),
        )

    def test_content_index_does_not_expose_full_source_manifests(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            sg = root / "sg.md"
            source_manifest = root / "source_manifest.json"
            sg.write_text("style guidance", encoding="utf-8")
            source_manifest.write_text('{"rows": ["full source"]}', encoding="utf-8")
            state = {
                "sg_path": str(sg),
                "source_manifest_path": str(source_manifest),
                "source_manifest_paths": {"raw": str(source_manifest)},
            }
            indexes = [
                build_suggestion_content_index(root, state),
                build_suggestion_content_index(
                    root,
                    state,
                    selected_evidence_index={
                        "path": "review_packets/selected_evidence_index.json",
                        "digest": "a" * 64,
                    },
                ),
            ]
        for index in indexes:
            resource_ids = [item["id"] for item in index["resources"]]
            self.assertEqual(resource_ids, ["sg_path"])
            self.assertNotIn("full source", json.dumps(index, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
