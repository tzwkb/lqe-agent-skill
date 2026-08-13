import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CHUNK_SCRIPT = SCRIPTS / "lqe_chunk.py"
REVIEW_SCRIPT = SCRIPTS / "lqe_review.py"
sys.path.insert(0, str(SCRIPTS))

from lqe_review import build_review_packet, build_worker_batch_plan


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def issue(category: str, comment: str) -> dict:
    return {
        "category": category,
        "severity": "Major",
        "comment": comment,
        "needs_confirmation": True,
        "edit": None,
    }


class CompactReviewPacketTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.job = Path(self.tempdir.name) / "job"
        self.state_path = self.job / "state.json"
        self.precheck_path = self.job / "errors_precheck.json"
        source = Path(self.tempdir.name) / "input.csv"
        with source.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["Key", "Source", "Target", "content_type", "context_note"]
            )
            writer.writerow(
                ["key-0", "修复错误", "Fix the eror", "UI/界面文本", "Button tooltip"]
            )
            writer.writerow(["key-1", "锁定文本", "Locked text", "", ""])
            writer.writerow(["key-2", "保留标签", "Keep tag", "", ""])
        read = self.run_script(
            SCRIPTS / "lqe_io.py",
            "read",
            "--input",
            source,
            "--source-col",
            "Source",
            "--target-col",
            "Target",
            "--key-col",
            "Key",
            "--content-type-col",
            "content_type",
            "--context-note-col",
            "context_note",
            "--source-lang",
            "zh",
            "--target-lang",
            "en",
            "--no-terminology",
            "--out",
            self.state_path,
        )
        self.assertEqual(read.returncode, 0, read.stderr)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["segments"][0]["protected_texts"] = ["{name}"]
        state["segments"][1]["protected"] = True
        state["segments"][1]["protected_reason"] = "SOURCE_LOCKED"
        state["segments"][2]["text_type_context"] = "战斗/操作提示"
        write_json(self.state_path, state)
        self.precheck = [
            {
                "id": 0,
                "issues": [
                    issue("Untranslated", "Source and target are identical.")
                ],
            },
            {"id": 1, "issues": []},
            {
                "id": 2,
                "issues": [issue("Markup", "The target drops one tag.")],
            },
        ]
        write_json(self.precheck_path, self.precheck)
        split = self.run_script(
            CHUNK_SCRIPT,
            "split",
            "--state",
            self.state_path,
            "--errors",
            self.precheck_path,
            "--outdir",
            self.job / "chunks",
            "--size",
            100,
        )
        self.assertEqual(split.returncode, 0, split.stderr)

    def tearDown(self):
        self.tempdir.cleanup()

    def run_script(
        self,
        script: Path,
        *arguments: object,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(script), *map(str, arguments)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def prepare(self) -> subprocess.CompletedProcess:
        return self.run_script(REVIEW_SCRIPT, "prepare", "--job", self.job)

    def packet(self, module: str) -> dict:
        return json.loads(
            (
                self.job
                / "review_packets"
                / module
                / "chunk_00.json"
            ).read_text(encoding="utf-8")
        )

    def draft(self, packet: dict, findings: list[dict], *, reviewed_ids=None) -> dict:
        return {
            "schema": "lqe.compact-module-draft",
            "version": 1,
            "module": packet["module"],
            "chunk_id": packet["chunk_id"],
            "packet_digest": packet["packet_digest"],
            "worker_batch_id": packet["worker_batch_id"],
            "worker_packet_basis_digest": packet["worker_packet_basis_digest"],
            "context_bundle_set_digest": packet["context_bundle_set_digest"],
            "worker_context_manifest_digest": packet[
                "worker_context_manifest_digest"
            ],
            "reviewed_ids": (
                packet["reviewed_ids"] if reviewed_ids is None else reviewed_ids
            ),
            "findings": findings,
        }

    def test_prepare_projects_only_module_relevant_context(self):
        prepared = self.prepare()
        self.assertEqual(prepared.returncode, 0, prepared.stderr)

        precheck = self.packet("precheck_review")
        self.assertEqual(precheck["reviewed_ids"], [2])
        self.assertEqual(
            [item["category"] for item in precheck["segments"][0]["precheck"]],
            ["Markup"],
        )
        self.assertEqual(precheck["auto_empty"]["protected"], 1)
        self.assertEqual(precheck["auto_empty"]["not_applicable"], 1)

        accuracy = self.packet("accuracy")
        self.assertEqual(accuracy["reviewed_ids"], [0, 2])
        self.assertEqual(
            set(accuracy["segments"][0]),
            {
                "id",
                "source",
                "target",
                "content_type",
                "kind",
                "protected_texts",
                "input_status",
                "module_review_equivalence_keys",
                "segment_key",
                "segment_revision_digest",
                "context_projection",
            },
        )
        self.assertEqual(
            accuracy["segments"][0]["context_projection"]["core"]["context_note"],
            "Button tooltip",
        )
        self.assertNotIn("precheck", accuracy["segments"][0])
        self.assertNotIn("term_hits", accuracy["segments"][0])
        self.assertNotIn("source_ref", accuracy["segments"][0])
        self.assertEqual(
            accuracy["segments"][1]["text_type_context"],
            "战斗/操作提示",
        )

        report = json.loads(
            (
                self.job / "review_packets" / "cost_report.json"
            ).read_text(encoding="utf-8")
        )
        batch_plan = json.loads(
            (
                self.job / "review_packets" / "batch_plan.json"
            ).read_text(encoding="utf-8")
        )
        self.assertGreater(report["input_reduction_percent"], 0)
        self.assertEqual(report["segment_reviews_before"], 12)
        self.assertEqual(report["segment_reviews_after"], 7)
        self.assertEqual(report["worker_batches"], 4)
        self.assertEqual(
            batch_plan["policy"],
            {
                "max_packets_per_worker": 4,
                "max_review_text_chars_per_worker": 25_000,
                "max_worker_input_bytes": 100_000,
                "single_minimum_over_budget_fails": True,
            },
        )

    def test_batch_plan_restarts_worker_after_four_packets(self):
        packets = [
            build_review_packet(
                {
                    "chunk_id": chunk_id,
                    "iteration": 0,
                    "split_fingerprint": "split",
                    "payload_digest": f"chunk-{chunk_id}",
                    "segments": [
                        {
                            "id": chunk_id,
                            "source": "源文",
                            "target": "Target",
                            "kind": "name",
                        }
                    ],
                },
                "accuracy",
            )
            for chunk_id in range(5)
        ]

        plan = build_worker_batch_plan(
            {"split_fingerprint": "split"},
            ["accuracy"],
            packets,
        )

        self.assertEqual(
            [batch["packet_count"] for batch in plan["modules"]["accuracy"]],
            [4, 1],
        )
        self.assertEqual(
            [
                item["chunk_id"]
                for batch in plan["modules"]["accuracy"]
                for item in batch["packets"]
            ],
            [0, 1, 2, 3, 4],
        )

    def test_sparse_draft_expands_to_formal_full_coverage(self):
        prepared = self.prepare()
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        packet = self.packet("grammar")
        draft = self.job / "grammar.compact.json"
        write_json(
            draft,
            self.draft(
                packet,
                [
                    {
                        "id": 0,
                        "issues": [
                            {
                                "category": "Spelling",
                                "severity": "Major",
                                "comment": "The word is misspelled.",
                                "needs_confirmation": False,
                                "edit": {
                                    "from": "eror",
                                    "to": "error",
                                    "evidence": None,
                                },
                            }
                        ],
                    }
                ],
            ),
        )

        published = self.run_script(
            REVIEW_SCRIPT,
            "publish",
            "--job",
            self.job,
            "--chunk",
            0,
            "--module",
            "grammar",
            "--input",
            draft,
        )

        self.assertEqual(published.returncode, 0, published.stderr)
        formal_path = self.job / "chunks" / "chunk_00.grammar.json"
        formal = json.loads(formal_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [entry["id"] for entry in formal["entries"]],
            [0, 1, 2],
        )
        self.assertEqual(len(formal["entries"][0]["issues"]), 1)
        self.assertEqual(formal["entries"][1]["issues"], [])
        self.assertEqual(formal["entries"][2]["issues"], [])
        self.assertTrue(
            (
                self.job
                / "chunks"
                / "chunk_00.grammar.receipt.json"
            ).is_file()
        )

    def test_publish_rejects_minor_edit(self):
        prepared = self.prepare()
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        packet = self.packet("grammar")
        draft = self.job / "grammar.minor-edit.json"
        write_json(
            draft,
            self.draft(
                packet,
                [
                    {
                        "id": 0,
                        "issues": [
                            {
                                "category": "Spelling",
                                "severity": "Minor",
                                "comment": "The word is misspelled.",
                                "needs_confirmation": False,
                                "edit": {
                                    "from": "eror",
                                    "to": "error",
                                    "evidence": None,
                                },
                            }
                        ],
                    }
                ],
            ),
        )

        published = self.run_script(
            REVIEW_SCRIPT,
            "publish",
            "--job",
            self.job,
            "--chunk",
            0,
            "--module",
            "grammar",
            "--input",
            draft,
        )

        self.assertNotEqual(published.returncode, 0)
        self.assertIn("Minor issue", published.stderr)
        self.assertFalse(
            (self.job / "chunks" / "chunk_00.grammar.json").exists()
        )

    def test_publish_rejects_incomplete_review_proof(self):
        prepared = self.prepare()
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        packet = self.packet("naturalness")
        draft = self.job / "naturalness.compact.json"
        write_json(
            draft,
            self.draft(packet, [], reviewed_ids=packet["reviewed_ids"][:-1]),
        )

        published = self.run_script(
            REVIEW_SCRIPT,
            "publish",
            "--job",
            self.job,
            "--chunk",
            0,
            "--module",
            "naturalness",
            "--input",
            draft,
        )

        self.assertNotEqual(published.returncode, 0)
        self.assertIn("reviewed_ids", published.stderr)
        self.assertFalse(
            (self.job / "chunks" / "chunk_00.naturalness.json").exists()
        )

    def test_auto_publish_only_handles_zero_review_packets(self):
        self.precheck[2]["issues"] = []
        write_json(self.precheck_path, self.precheck)
        resplit = self.run_script(
            CHUNK_SCRIPT,
            "split",
            "--state",
            self.state_path,
            "--errors",
            self.precheck_path,
            "--outdir",
            self.job / "chunks",
            "--size",
            100,
        )
        self.assertEqual(resplit.returncode, 0, resplit.stderr)
        prepared = self.prepare()
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.assertFalse(self.packet("precheck_review")["requires_ai"])

        published = self.run_script(
            REVIEW_SCRIPT,
            "auto-publish",
            "--job",
            self.job,
        )

        self.assertEqual(published.returncode, 0, published.stderr)
        self.assertIn("published 1", published.stdout)
        formal = json.loads(
            (
                self.job
                / "chunks"
                / "chunk_00.precheck_review.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            formal["entries"],
            [
                {"id": 0, "issues": []},
                {"id": 1, "issues": []},
                {"id": 2, "issues": []},
            ],
        )
        self.assertFalse(
            (self.job / "chunks" / "chunk_00.accuracy.json").exists()
        )


if __name__ == "__main__":
    unittest.main()
