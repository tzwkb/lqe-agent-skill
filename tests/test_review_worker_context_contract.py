import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lqe_review
from lqe_context_bundle import (
    measure_complete_worker_input_bytes,
    validate_selected_context_evidence_index,
    verify_worker_context_manifest_resources,
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


class ReviewWorkerContextContractTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "checks.json").write_text("{}", encoding="utf-8")
        (self.project / "confirmed_rules.md").write_text(
            "# Confirmed rules\n", encoding="utf-8"
        )
        profile = {
            "profile_contract_version": 2,
            "name": "review-worker-context-test",
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
            "context_pipeline": {"mode": "off"},
            "capabilities": {
                "context.core@1": {"required": True},
                "source_provenance@1": {"required": True},
            },
        }
        (self.project / "profile.json").write_text(
            json.dumps(profile, ensure_ascii=False), encoding="utf-8"
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def run_script(self, script: str, *arguments: object) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), *map(str, arguments)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def build_job(self, lengths: list[int]) -> Path:
        input_path = self.root / f"input-{len(lengths)}.csv"
        with input_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Key", "Source", "Target"])
            for index, length in enumerate(lengths):
                writer.writerow(
                    [f"key-{index}", f"{index}{'文' * length}", f"Target {index}"]
                )
        job = self.root / f"job-{len(lengths)}-{max(lengths)}"
        read = self.run_script(
            "lqe_io.py",
            "read",
            "--input",
            input_path,
            "--project",
            self.project,
            "--source-col",
            "Source",
            "--target-col",
            "Target",
            "--key-col",
            "Key",
            "--no-terminology",
            "--out",
            job / "state.json",
        )
        self.assertEqual(read.returncode, 0, read.stderr)
        state = json.loads((job / "state.json").read_text(encoding="utf-8"))
        (job / "errors_precheck.json").write_text(
            json.dumps(
                [{"id": segment["id"], "issues": []} for segment in state["segments"]]
            ),
            encoding="utf-8",
        )
        split = self.run_script(
            "lqe_chunk.py",
            "split",
            "--state",
            job / "state.json",
            "--errors",
            job / "errors_precheck.json",
            "--outdir",
            job / "chunks",
            "--size",
            1,
        )
        self.assertEqual(split.returncode, 0, split.stderr)
        return job

    def test_prepare_splits_on_review_text_and_binds_each_batch(self):
        job = self.build_job([7000, 7000, 7000, 7000])
        prepared = self.run_script("lqe_review.py", "prepare", "--job", job)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)

        plan = json.loads(
            (job / "review_packets" / "batch_plan.json").read_text(encoding="utf-8")
        )
        evidence_index = json.loads(
            (job / "review_packets" / "selected_evidence_index.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            validate_selected_context_evidence_index(evidence_index),
            evidence_index,
        )
        self.assertEqual(
            plan["selected_evidence_index_digest"],
            evidence_index["index_digest"],
        )
        batches = plan["modules"]["accuracy"]
        self.assertGreater(len(batches), 1)
        self.assertEqual(sum(batch["packet_count"] for batch in batches), 4)
        for batch in batches:
            self.assertLessEqual(batch["review_text_chars"], 25_000)
            bundle_path = job / "review_packets" / batch["context_bundle_set_path"]
            manifest_path = job / "review_packets" / batch[
                "worker_context_manifest_path"
            ]
            self.assertTrue(bundle_path.is_file())
            self.assertTrue(manifest_path.is_file())
            bundle_set = json.loads(bundle_path.read_text(encoding="utf-8"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            packets = [
                json.loads(
                    (job / "review_packets" / packet_ref["path"]).read_text(
                        encoding="utf-8"
                    )
                )
                for packet_ref in batch["packets"]
            ]
            self.assertIsNone(manifest["budget"]["max_bytes"])
            self.assertEqual(manifest["budget"]["status"], "advisory")
            self.assertEqual(
                batch["worker_input_bytes"],
                measure_complete_worker_input_bytes(
                    manifest,
                    bundle_set,
                    packets,
                ),
            )
            self.assertGreater(
                batch["worker_input_bytes"],
                manifest["budget"]["measured_bytes"],
            )
            self.assertEqual(
                verify_worker_context_manifest_resources(
                    manifest,
                    job_root=job,
                ),
                manifest,
            )
            self.assertEqual(
                manifest["worker_context_manifest_digest"],
                batch["worker_context_manifest_digest"],
            )
            for packet_ref, packet in zip(batch["packets"], packets):
                self.assertEqual(packet["worker_batch_id"], batch["batch_id"])
                self.assertEqual(
                    packet["context_bundle_set_digest"],
                    batch["context_bundle_set_digest"],
                )
                self.assertEqual(
                    packet["worker_context_manifest_digest"],
                    batch["worker_context_manifest_digest"],
                )
                self.assertEqual(
                    packet["selected_evidence_index_digest"],
                    evidence_index["index_digest"],
                )

        report = json.loads(
            (job / "review_packets" / "cost_report.json").read_text(encoding="utf-8")
        )
        self.assertIn("shared assets", report["basis"])
        self.assertEqual(
            report["complete_worker_input_bytes"],
            sum(
                batch["worker_input_bytes"]
                for module_batches in plan["modules"].values()
                for batch in module_batches
            ),
        )
        self.assertEqual(
            report["largest_worker_input_bytes"],
            max(
                batch["worker_input_bytes"]
                for module_batches in plan["modules"].values()
                for batch in module_batches
            ),
        )
        accuracy_bundle_digests = {
            bundle["context_bundle_digest"]
            for batch in batches
            for bundle in json.loads(
                (
                    job
                    / "review_packets"
                    / batch["context_bundle_set_path"]
                ).read_text(encoding="utf-8")
            )["bundles"]
        }
        self.assertEqual(
            {
                entry["context_bundle_digest"]
                for entry in evidence_index["entries"]
                if entry["module"] == "accuracy"
            },
            accuracy_bundle_digests,
        )

    def test_unprofiled_job_has_empty_assets_and_foundation_bindings(self):
        input_path = self.root / "unprofiled.csv"
        input_path.write_text(
            "Key,Source,Target\nkey-0,打开,Open\n", encoding="utf-8"
        )
        job = self.root / "unprofiled-job"
        read = self.run_script(
            "lqe_io.py",
            "read",
            "--input",
            input_path,
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
            "--out",
            job / "state.json",
        )
        self.assertEqual(read.returncode, 0, read.stderr)
        state = json.loads((job / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["project_asset_snapshot"]["assets"], {})
        self.assertEqual(state["project_asset_paths"], {})
        self.assertEqual(
            set(state["capability_resolution"]["enabled"]),
            {"context.core@1", "source_provenance@1"},
        )

    def test_publish_requires_live_worker_manifest_binding(self):
        job = self.build_job([100])
        prepared = self.run_script("lqe_review.py", "prepare", "--job", job)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        packet = json.loads(
            (job / "review_packets" / "accuracy" / "chunk_00.json").read_text(
                encoding="utf-8"
            )
        )
        draft = {
            "schema": "lqe.compact-module-draft",
            "version": 1,
            "module": "accuracy",
            "chunk_id": 0,
            "packet_digest": packet["packet_digest"],
            "worker_batch_id": packet["worker_batch_id"],
            "worker_packet_basis_digest": packet["worker_packet_basis_digest"],
            "context_bundle_set_digest": packet["context_bundle_set_digest"],
            "worker_context_manifest_digest": packet[
                "worker_context_manifest_digest"
            ],
            "selected_evidence_index_path": packet[
                "selected_evidence_index_path"
            ],
            "selected_evidence_index_digest": packet[
                "selected_evidence_index_digest"
            ],
            "worker_receipt": {"worker_id": "checker.accuracy", "run_id": "run.1"},
            "reviewed_ids": packet["reviewed_ids"],
            "findings": [],
        }
        draft_path = job / "accuracy.draft.json"
        stale = dict(draft)
        stale["worker_context_manifest_digest"] = "f" * 64
        draft_path.write_text(json.dumps(stale), encoding="utf-8")
        rejected = self.run_script(
            "lqe_review.py",
            "publish",
            "--job",
            job,
            "--chunk",
            0,
            "--module",
            "accuracy",
            "--input",
            draft_path,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("worker_context_manifest_digest mismatch", rejected.stderr)

        draft_path.write_text(json.dumps(draft), encoding="utf-8")
        published = self.run_script(
            "lqe_review.py",
            "publish",
            "--job",
            job,
            "--chunk",
            0,
            "--module",
            "accuracy",
            "--input",
            draft_path,
        )
        self.assertEqual(published.returncode, 0, published.stderr)
        receipt = json.loads(
            (job / "chunks" / "chunk_00.accuracy.receipt.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            receipt["review_provenance"]["worker_receipt"],
            draft["worker_receipt"],
        )
        self.assertEqual(
            receipt["review_provenance"]["selected_evidence_index_digest"],
            packet["selected_evidence_index_digest"],
        )

    def test_publish_rejects_missing_or_tampered_prepared_context(self):
        job = self.build_job([100])
        prepared = self.run_script("lqe_review.py", "prepare", "--job", job)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        packet = json.loads(
            (job / "review_packets" / "accuracy" / "chunk_00.json").read_text(
                encoding="utf-8"
            )
        )
        draft = {
            "schema": "lqe.compact-module-draft",
            "version": 1,
            "module": "accuracy",
            "chunk_id": 0,
            "packet_digest": packet["packet_digest"],
            "worker_batch_id": packet["worker_batch_id"],
            "worker_packet_basis_digest": packet["worker_packet_basis_digest"],
            "context_bundle_set_digest": packet["context_bundle_set_digest"],
            "worker_context_manifest_digest": packet[
                "worker_context_manifest_digest"
            ],
            "selected_evidence_index_path": packet[
                "selected_evidence_index_path"
            ],
            "selected_evidence_index_digest": packet[
                "selected_evidence_index_digest"
            ],
            "worker_receipt": {"worker_id": "checker.accuracy", "run_id": "run.2"},
            "reviewed_ids": packet["reviewed_ids"],
            "findings": [],
        }
        draft_path = job / "accuracy.draft.json"
        draft_path.write_text(json.dumps(draft), encoding="utf-8")
        manifest_path = (
            job
            / "review_packets"
            / "context"
            / "accuracy"
            / "batch_00"
            / "worker_manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["budget"]["measured_bytes"] += 1
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        rejected = self.run_script(
            "lqe_review.py",
            "publish",
            "--job",
            job,
            "--chunk",
            0,
            "--module",
            "accuracy",
            "--input",
            draft_path,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("packet tree is missing or stale", rejected.stderr)

    def test_publish_revalidates_packet_tree_after_draft_load(self):
        job = self.build_job([100])
        prepared = self.run_script("lqe_review.py", "prepare", "--job", job)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        packet = json.loads(
            (job / "review_packets" / "accuracy" / "chunk_00.json").read_text(
                encoding="utf-8"
            )
        )
        draft_path = job / "accuracy.race.draft.json"
        draft_path.write_text(
            json.dumps(
                {
                    "schema": "lqe.compact-module-draft",
                    "version": 1,
                    "module": "accuracy",
                    "chunk_id": 0,
                    "packet_digest": packet["packet_digest"],
                    "worker_batch_id": packet["worker_batch_id"],
                    "worker_packet_basis_digest": packet[
                        "worker_packet_basis_digest"
                    ],
                    "context_bundle_set_digest": packet[
                        "context_bundle_set_digest"
                    ],
                    "worker_context_manifest_digest": packet[
                        "worker_context_manifest_digest"
                    ],
                    "selected_evidence_index_path": packet[
                        "selected_evidence_index_path"
                    ],
                    "selected_evidence_index_digest": packet[
                        "selected_evidence_index_digest"
                    ],
                    "worker_receipt": {
                        "worker_id": "checker.accuracy",
                        "run_id": "run.3",
                    },
                    "reviewed_ids": packet["reviewed_ids"],
                    "findings": [],
                }
            ),
            encoding="utf-8",
        )
        manifest_path = (
            job
            / "review_packets"
            / "context"
            / "accuracy"
            / "batch_00"
            / "worker_manifest.json"
        )
        original_loader = lqe_review._load_compact_draft

        def replace_after_load(path, live_packet, **kwargs):
            findings = original_loader(path, live_packet, **kwargs)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["budget"]["measured_bytes"] += 1
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            return findings

        with mock.patch.object(
            lqe_review,
            "_load_compact_draft",
            side_effect=replace_after_load,
        ):
            with self.assertRaisesRegex(SystemExit, "packet tree is missing or stale"):
                lqe_review.cmd_publish(
                    type(
                        "Args",
                        (),
                        {
                            "job": str(job),
                            "chunk": 0,
                            "module": "accuracy",
                            "input": str(draft_path),
                        },
                    )()
                )
        self.assertFalse((job / "chunks" / "chunk_00.accuracy.json").exists())
        self.assertFalse(
            (job / "chunks" / "chunk_00.accuracy.receipt.json").exists()
        )

    def test_prepare_and_auto_publish_fail_without_v2_bindings_or_packet_tree(self):
        job = self.build_job([100])
        state_path = job / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.pop("project_asset_snapshot")
        state_path.write_text(json.dumps(state), encoding="utf-8")
        prepared = self.run_script("lqe_review.py", "prepare", "--job", job)
        self.assertNotEqual(prepared.returncode, 0)
        self.assertIn("project asset snapshot", prepared.stderr)

        clean_job = self.build_job([101])
        auto = self.run_script(
            "lqe_review.py", "auto-publish", "--job", clean_job
        )
        self.assertNotEqual(auto.returncode, 0)
        self.assertIn("packet tree is missing or stale", auto.stderr)

    def test_single_packet_over_legacy_byte_budget_is_measured_not_rejected(self):
        job = self.build_job([20_000])
        prepared = self.run_script("lqe_review.py", "prepare", "--job", job)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)

        plan = json.loads(
            (job / "review_packets" / "batch_plan.json").read_text(
                encoding="utf-8"
            )
        )
        batch = plan["modules"]["accuracy"][0]
        self.assertEqual(batch["packet_count"], 1)
        self.assertGreater(batch["worker_input_bytes"], 100_000)
        self.assertEqual(
            plan["policy"]["worker_input_bytes"],
            {
                "mode": "advisory",
                "measurement": "complete_utf8_bytes",
            },
        )

        manifest = json.loads(
            (
                job
                / "review_packets"
                / batch["worker_context_manifest_path"]
            ).read_text(encoding="utf-8")
        )
        self.assertIsNone(manifest["budget"]["max_bytes"])
        self.assertEqual(manifest["budget"]["status"], "advisory")


if __name__ == "__main__":
    unittest.main()
