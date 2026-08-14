import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MASTERTB = SCRIPTS / "mastertb_prep.py"
LQE_CHUNK = SCRIPTS / "lqe_chunk.py"
LQE_IO = SCRIPTS / "lqe_io.py"
LQE_REVIEW = SCRIPTS / "lqe_review.py"


def write_json(path: Path, value) -> None:
    if (
        path.name == "state.json"
        and isinstance(value, dict)
        and isinstance(value.get("segments"), list)
        and "job_runtime_contract_version" not in value
    ):
        value["job_runtime_contract_version"] = 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


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


class MasterTBModuleContractTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.job = Path(self.tempdir.name) / "mastertb"
        write_json(
            self.job / "state.json",
            {
                "segments": [
                    {"id": 0, "source": "花衣蝶", "target": "ผีเสื้อบุปผา"}
                ]
            },
        )
        write_json(
            self.job / "context.json",
            {
                "0": {
                    "zhcn": "花衣蝶",
                    "en": "Floral Butterfly",
                    "definition": "Named creature",
                    "category": "Creature Species",
                    "gender": "",
                    "former": "",
                    "th": "ผีเสื้อบุปผา",
                    "th_comment": "",
                    "th_status": "Approved",
                    "scope": "Creatures",
                }
            },
        )
        self.precheck_issue = {
            "category": "Punctuation",
            "severity": "Minor",
            "comment": "Check punctuation",
            "needs_confirmation": True,
            "edit": None,
        }
        write_json(
            self.job / "errors_precheck.json",
            [{"id": 0, "issues": [self.precheck_issue]}],
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def run_script(self, script: Path, *args):
        return subprocess.run(
            [sys.executable, str(script), *map(str, args)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def test_chunks_use_current_check_module_contract(self):
        result = self.run_script(
            MASTERTB,
            "chunks",
            "--job-dir",
            self.job,
            "--size",
            "10",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        chunk = json.loads(
            (self.job / "chunks" / "chunk_00.json").read_text(encoding="utf-8")
        )
        self.assertIn("chunk_id", chunk)
        self.assertEqual(chunk["chunk_id"], 0)
        self.assertNotIn("terms", chunk)
        self.assertEqual(chunk["resolved_context_descriptors"], {})
        segment = chunk["segments"][0]
        precheck = dict(segment["precheck"][0])
        precheck_ref = precheck.pop("precheck_ref")
        self.assertTrue(precheck_ref.startswith("precheck:0:"))
        self.assertEqual(precheck, self.precheck_issue)
        self.assertEqual(
            {key: segment[key] for key in ("id", "source", "target", "kind")},
            {
                "id": 0,
                "source": "花衣蝶",
                "target": "ผีเสื้อบุปผา",
                "kind": "name",
            },
        )
        self.assertEqual(
            segment["context_note"],
            "EN: Floral Butterfly\n"
            "Definition: Named creature\n"
            "Category: Creature Species\n"
            "Status: Approved\n"
            "Scope: Creatures",
        )
        self.assertEqual(segment["term_hits"], [])
        self.assertEqual(segment["term_near"], [])
        self.assertFalse(segment["protected"])
        for legacy_field in (
            "en",
            "definition",
            "category",
            "gender",
            "former",
            "target_comment",
            "target_status",
            "scope",
        ):
            self.assertNotIn(legacy_field, segment)
        for module in ("terminology", "accuracy", "grammar", "naturalness"):
            self.assertIn(f"chunk_NN.{module}.json", result.stdout)
        self.assertIn("chunk_NN.proper_names.json", result.stdout)
        self.assertIn("lqe_review.py prepare", result.stdout)
        self.assertIn("lqe_review.py publish", result.stdout)
        self.assertIn("validate-checks --job", result.stdout)
        self.assertIn("merge-checks --job", result.stdout)

    def test_runtime_v2_compact_review_preserves_context_and_validates(self):
        root = Path(self.tempdir.name) / "bound-runtime"
        project = root / "project"
        project.mkdir(parents=True)
        (project / "checks.json").write_text("{}", encoding="utf-8")
        (project / "confirmed_rules.md").write_text(
            "# Confirmed rules\n", encoding="utf-8"
        )
        write_json(
            project / "terms.json",
            [
                {
                    "source": "无关词",
                    "target": "คำอื่น",
                    "confirmed": True,
                    "protected": False,
                }
            ],
        )
        write_json(
            project / "profile.json",
            {
                "profile_contract_version": 2,
                "name": "mastertb-test/zh-th",
                "language_pair": "zh-th",
                "source_lang": "zh",
                "target_lang": "th",
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
                    "rules": asset("confirmed_rules", "confirmed_rules.md"),
                    "terms": asset("terminology", "terms.json"),
                },
                "context_pipeline": {"mode": "off"},
                "capabilities": {
                    "context.core@1": {"required": True},
                    "source_provenance@1": {"required": True},
                },
            },
        )
        input_path = root / "input.csv"
        input_path.write_text(
            "Key,Source,Target\nterm-0,花衣蝶,ผีเสื้อบุปผา\n",
            encoding="utf-8",
        )
        job = root / "job"
        read = self.run_script(
            LQE_IO,
            "read",
            "--input",
            input_path,
            "--project",
            project,
            "--source-col",
            "Source",
            "--target-col",
            "Target",
            "--key-col",
            "Key",
            "--out",
            job / "state.json",
        )
        self.assertEqual(read.returncode, 0, read.stderr)
        state = json.loads((job / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["artifact_contract_version"], 1)
        self.assertEqual(state["job_runtime_contract_version"], 2)
        write_json(
            job / "context.json",
            {
                "0": {
                    "zhcn": "花衣蝶",
                    "en": "Floral Butterfly",
                    "definition": "A named creature used in the bestiary.",
                    "category": "Creature Species",
                    "gender": "Neutral",
                    "former": "Old Butterfly",
                    "th": "ผีเสื้อบุปผา",
                    "th_comment": "Keep the floral image.",
                    "th_status": "Approved",
                    "scope": "Bestiary",
                }
            },
        )
        write_json(job / "errors_precheck.json", [{"id": 0, "issues": []}])

        chunks = self.run_script(
            MASTERTB,
            "chunks",
            "--job-dir",
            job,
            "--size",
            10,
        )
        self.assertEqual(chunks.returncode, 0, chunks.stderr)
        chunk = json.loads(
            (job / "chunks" / "chunk_00.json").read_text(encoding="utf-8")
        )
        segment = chunk["segments"][0]
        source_segment = state["segments"][0]
        self.assertEqual(
            chunk["resolved_context_descriptors"],
            state["resolved_context_descriptors"],
        )
        for field in (
            "segment_key",
            "key_origin",
            "source_digest",
            "source_provenance",
            "input_status",
            "context",
            "context_provenance",
            "context_status",
            "context_missing_required",
            "resolved_constraints",
            "segment_revision_digest",
            "module_review_equivalence_keys",
        ):
            self.assertEqual(segment[field], source_segment[field], field)

        prepared = self.run_script(LQE_REVIEW, "prepare", "--job", job)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        accuracy_packet = json.loads(
            (job / "review_packets" / "accuracy" / "chunk_00.json").read_text(
                encoding="utf-8"
            )
        )
        packet_segment = accuracy_packet["segments"][0]
        self.assertEqual(packet_segment["segment_key"], "term-0")
        self.assertEqual(
            packet_segment["segment_revision_digest"],
            source_segment["segment_revision_digest"],
        )
        for expected in (
            "EN: Floral Butterfly",
            "Definition: A named creature used in the bestiary.",
            "Category: Creature Species",
            "Gender: Neutral",
            "Former: Old Butterfly",
            "Target comment: Keep the floral image.",
            "Status: Approved",
            "Scope: Bestiary",
        ):
            self.assertIn(expected, packet_segment["context_note"])

        for module in ("terminology", "accuracy", "grammar", "naturalness"):
            packet = json.loads(
                (job / "review_packets" / module / "chunk_00.json").read_text(
                    encoding="utf-8"
                )
            )
            findings = []
            if module == "accuracy":
                findings = [
                    {
                        "id": 0,
                        "issues": [
                            {
                                "category": "Mistranslation",
                                "severity": "Major",
                                "comment": "The candidate meaning needs human confirmation.",
                                "needs_confirmation": True,
                                "edit": None,
                            }
                        ],
                    }
                ]
            draft = {
                "schema": "lqe.compact-module-draft",
                "version": 1,
                "module": module,
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
                    "worker_id": f"test-checker.{module}",
                    "run_id": "chunk-0",
                },
                "reviewed_ids": packet["reviewed_ids"],
                "findings": findings,
            }
            draft_path = job / f"{module}.draft.json"
            write_json(draft_path, draft)
            published = self.run_script(
                LQE_REVIEW,
                "publish",
                "--job",
                job,
                "--chunk",
                0,
                "--module",
                module,
                "--input",
                draft_path,
            )
            self.assertEqual(published.returncode, 0, published.stderr)

        validated = self.run_script(
            LQE_CHUNK,
            "validate-checks",
            "--job",
            job,
        )
        self.assertEqual(validated.returncode, 0, validated.stderr)

    def test_current_modules_validate_merge_and_mark_complete(self):
        chunks = self.run_script(
            MASTERTB,
            "chunks",
            "--job-dir",
            self.job,
            "--size",
            "10",
        )
        self.assertEqual(chunks.returncode, 0, chunks.stderr)
        for module in ("terminology", "accuracy", "grammar", "naturalness"):
            write_json(
                self.job / "chunks" / f"chunk_00.{module}.json",
                [{"id": 0, "issues": []}],
            )

        validated = self.run_script(
            LQE_CHUNK, "validate-checks", "--job", self.job
        )
        self.assertEqual(validated.returncode, 0, validated.stderr)
        merged_modules = self.run_script(
            LQE_CHUNK, "merge-checks", "--job", self.job
        )
        self.assertEqual(merged_modules.returncode, 0, merged_modules.stderr)
        self.assertFalse(
            (self.job / "chunks" / "chunk_00.proper_names.json").exists()
        )

        merged_job = self.run_script(
            MASTERTB,
            "merge",
            "--job-dir",
            self.job,
            "--no-consistency",
        )

        self.assertEqual(merged_job.returncode, 0, merged_job.stderr)
        status = json.loads(
            (self.job / "recall_status.json").read_text(encoding="utf-8")
        )
        self.assertTrue(status["checks_complete"])
        self.assertTrue(status["verdict_allowed"])
        self.assertEqual(status["incomplete_chunks"], [])
        errors = json.loads(
            (self.job / "errors.json").read_text(encoding="utf-8")
        )
        self.assertEqual(errors, [{"id": 0, "errors": [], "corrected": None}])

    def test_chunks_write_current_check_context(self):
        result = self.run_script(
            MASTERTB,
            "chunks",
            "--job-dir",
            self.job,
            "--size",
            "10",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        context_path = self.job / "chunks" / "_CHECK_CONTEXT.md"
        self.assertTrue(context_path.is_file())
        context = context_path.read_text(encoding="utf-8")
        self.assertIn("`segments[]`", context)
        self.assertIn("references/check_modules/common.md", context)
        self.assertIn("references/check_modules/term_audit.md", context)
        self.assertIn('"issues"', context)
        self.assertIn('"needs_confirmation"', context)
        self.assertIn('"edit"', context)
        self.assertIn('"findings"', context)
        self.assertIn("`context_note`", context)
        self.assertIn("lqe_review.py prepare", context)
        self.assertIn("lqe_review.py publish", context)
        self.assertNotIn("lqe_chunk.py publish-module", context)
        self.assertNotIn("`terms[]`", context)
        self.assertNotIn("reviewed_first", context)

    def test_merge_stops_when_merge_checks_was_not_run(self):
        chunks = self.run_script(
            MASTERTB,
            "chunks",
            "--job-dir",
            self.job,
            "--size",
            "10",
        )
        self.assertEqual(chunks.returncode, 0, chunks.stderr)
        for module in ("terminology", "accuracy", "grammar", "naturalness"):
            write_json(
                self.job / "chunks" / f"chunk_00.{module}.json",
                [{"id": 0, "issues": []}],
            )

        result = self.run_script(
            MASTERTB,
            "merge",
            "--job-dir",
            self.job,
            "--no-consistency",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("merge-checks", result.stderr)
        self.assertFalse((self.job / "errors.json").exists())

    def test_merge_stops_when_merged_output_has_missing_ids(self):
        chunks = self.run_script(
            MASTERTB,
            "chunks",
            "--job-dir",
            self.job,
            "--size",
            "10",
        )
        self.assertEqual(chunks.returncode, 0, chunks.stderr)
        for module in ("terminology", "accuracy", "grammar", "naturalness"):
            write_json(
                self.job / "chunks" / f"chunk_00.{module}.json",
                [{"id": 0, "issues": []}],
            )
        write_json(self.job / "chunks" / "chunk_00.out.json", [])

        result = self.run_script(
            MASTERTB,
            "merge",
            "--job-dir",
            self.job,
            "--no-consistency",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing=[0]", result.stderr)
        self.assertFalse((self.job / "errors.json").exists())

    def test_merge_rejects_legacy_findings_wrapper(self):
        chunks = self.run_script(
            MASTERTB,
            "chunks",
            "--job-dir",
            self.job,
            "--size",
            "10",
        )
        self.assertEqual(chunks.returncode, 0, chunks.stderr)
        for module in ("terminology", "accuracy", "grammar", "naturalness"):
            write_json(
                self.job / "chunks" / f"chunk_00.{module}.json",
                [{"id": 0, "issues": []}],
            )
        write_json(
            self.job / "chunks" / "chunk_00.out.json",
            {"findings": [{"id": 0, "issues": []}]},
        )

        result = self.run_script(
            MASTERTB,
            "merge",
            "--job-dir",
            self.job,
            "--no-consistency",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("module output envelope fields are invalid", result.stderr)
        self.assertFalse((self.job / "errors.json").exists())


if __name__ == "__main__":
    unittest.main()
