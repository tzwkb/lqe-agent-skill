import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import openpyxl
from openpyxl.styles import PatternFill


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CHUNK_SCRIPT = SCRIPTS / "lqe_chunk.py"
IO_SCRIPT = SCRIPTS / "lqe_io.py"
CALC_SCRIPT = SCRIPTS / "lqe_calc.py"
BATCH_SCRIPT = SCRIPTS / "lqe_batch.py"
MASTERTB_SCRIPT = SCRIPTS / "mastertb_prep.py"
MASTERTB_TO_TERMS_SCRIPT = SCRIPTS / "mastertb_to_terms.py"
FINALIZE_SCRIPT = SCRIPTS / "finalize_job.sh"
AGGREGATE_SCRIPT = SCRIPTS / "aggregate_sheets.py"
REQUIRED_MODULES = ("terminology", "accuracy", "grammar", "naturalness")

sys.path.insert(0, str(SCRIPTS))

from lqe_checks import run_pre_check
import aggregate_sheets
import lqe_chunk
import lqe_io
from lqe_report_contract import attach_report_contract, validate_report_contract


def write_json(path, value):
    if (
        (path.name == "state.json" or path.name.endswith("-state.json"))
        and isinstance(value, dict)
        and isinstance(value.get("segments"), list)
        and "job_runtime_contract_version" not in value
    ):
        value["job_runtime_contract_version"] = 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def check_issue(
    category="Grammar",
    severity="Major",
    comment="check",
    *,
    needs_confirmation=False,
    edit=None,
):
    return {
        "category": category,
        "severity": severity,
        "comment": comment,
        "needs_confirmation": needs_confirmation,
        "edit": edit,
    }


def exact_spans(text, value):
    spans = []
    start = text.find(value)
    while start >= 0:
        end = start + len(value)
        spans.append({"start": start, "end": end, "text": value})
        start = text.find(value, end)
    return spans


def term_issue(
    term_source,
    expected_targets,
    source_text,
    target_text,
    *,
    target_terms=(),
    **kwargs,
):
    value = check_issue(category="Terminology", **kwargs)
    target_spans = [
        span
        for target_term in target_terms
        for span in exact_spans(target_text, target_term)
    ]
    target_spans.sort(key=lambda span: (span["start"], span["end"], span["text"]))
    value.update(
        {
            "term_source": term_source,
            "expected_targets": list(expected_targets),
            "term_spans": {
                "source": exact_spans(source_text, term_source),
                "target": target_spans,
            },
        }
    )
    return value


def replacement(frm, to, *, evidence=None, start=None, end=None):
    value = {"from": frm, "to": to, "evidence": evidence}
    if start is not None or end is not None:
        value["start"] = start
        value["end"] = end
    return value


def confirmed_evidence(source, target):
    return {"type": "confirmed_term", "source": source, "target": target}


def call_quietly(function, *args, **kwargs):
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        return function(*args, **kwargs)


class CorrectedOwnershipChunkTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.job = Path(self.tempdir.name) / "job"
        self.chunks = self.job / "chunks"
        self.chunks.mkdir(parents=True)

    def tearDown(self):
        self.tempdir.cleanup()

    def run_chunk(self, *args):
        return subprocess.run(
            [sys.executable, str(CHUNK_SCRIPT), *map(str, args)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def make_job(self, segments, *, representatives=None, dedup_map=None, pre=None):
        terms = {}
        for segment in segments:
            for hit in segment.get("term_hits", []):
                source = hit.get("source")
                if not source:
                    continue
                sense = {key: value for key, value in hit.items() if key != "source"}
                if sense not in terms.setdefault(source, []):
                    terms[source].append(sense)
        state = {"segments": segments}
        if terms:
            state["terminology"] = [
                {"source": source, "senses": senses}
                for source, senses in terms.items()
            ]
        write_json(self.job / "state.json", state)
        if pre is None:
            pre = [{"id": segment["id"], "issues": []} for segment in segments]
        write_json(self.job / "errors_precheck.json", pre)

        result = self.run_chunk(
            "split",
            "--state",
            self.job / "state.json",
            "--errors",
            self.job / "errors_precheck.json",
            "--outdir",
            self.chunks,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        chunk_segments = read_json(self.chunks / "chunk_00.json")["segments"]
        if representatives is not None:
            self.assertEqual(
                [segment["id"] for segment in chunk_segments],
                [segment["id"] for segment in representatives],
            )
        if dedup_map is not None:
            self.assertEqual(read_json(self.chunks / "dedup_map.json"), dedup_map)

    def write_modules(self, ids, *, issues_by_module=None):
        issues_by_module = issues_by_module or {}
        for module in REQUIRED_MODULES:
            rows = [
                {
                    "id": segment_id,
                    "issues": issues_by_module.get(module, {}).get(segment_id, []),
                }
                for segment_id in ids
            ]
            write_json(self.chunks / f"chunk_00.{module}.json", rows)

    def merge_checks(self):
        result = self.run_chunk("merge-checks", "--job", self.job)
        self.assertEqual(result.returncode, 0, result.stderr)
        return read_json(self.chunks / "chunk_00.out.json")

    def merge_final(self):
        output = self.job / "errors.json"
        result = self.run_chunk(
            "merge",
            "--state",
            self.job / "state.json",
            "--errors",
            self.job / "errors_precheck.json",
            "--outdir",
            self.chunks,
            "--out",
            output,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return read_json(output)

    def test_validate_checks_rejects_top_level_corrected(self):
        self.make_job([{"id": 0, "source": "x", "target": "x"}])
        self.write_modules([0])
        path = self.chunks / "chunk_00.terminology.json"
        write_json(path, [{"id": 0, "issues": [], "corrected": "model text"}])

        result = self.run_chunk("validate-checks", "--job", self.job)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("corrected is not allowed", result.stderr)

    def test_ckpt_append_rejects_top_level_corrected(self):
        checkpoint = self.chunks / "chunk_00.terminology.ckpt.jsonl"
        entry = json.dumps(
            {"id": 0, "issues": [], "corrected": "model text"},
            ensure_ascii=False,
        )

        result = self.run_chunk(
            "ckpt-append", "--file", checkpoint, "--entry", entry
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("corrected is not allowed", result.stderr)
        self.assertFalse(checkpoint.exists())

    def test_merge_checks_rejects_top_level_corrected(self):
        self.make_job([{"id": 0, "source": "x", "target": "x"}])
        self.write_modules([0])
        path = self.chunks / "chunk_00.naturalness.json"
        write_json(path, [{"id": 0, "issues": [], "corrected": "model text"}])

        result = self.run_chunk("merge-checks", "--job", self.job)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("corrected is not allowed", result.stderr)
        self.assertFalse((self.chunks / "chunk_00.out.json").exists())

    def test_validate_checks_requires_modules_and_complete_ids(self):
        segments = [
            {"id": 0, "source": "a", "target": "a"},
            {"id": 1, "source": "b", "target": "b"},
        ]
        self.make_job(segments)
        self.write_modules([0, 1])
        (self.chunks / "chunk_00.grammar.json").unlink()
        write_json(
            self.chunks / "chunk_00.terminology.json",
            [{"id": 0, "issues": []}],
        )

        result = self.run_chunk("validate-checks", "--job", self.job)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("grammar", result.stderr)
        self.assertIn("1", result.stderr)

    def test_merge_checks_unions_valid_module_owned_issues(self):
        self.make_job([{"id": 0, "source": "x", "target": "x"}])
        punctuation = check_issue(
            category="Punctuation",
            comment="same",
            edit=replacement("x", "X"),
        )
        terminology = term_issue(
            "x",
            ["X"],
            "x",
            "x",
            severity="Major",
            comment="terminology finding",
            needs_confirmation=True,
            target_terms=("x",),
        )
        terminology["resolution_status"] = "reference_allowed"
        accuracy = check_issue(
            category="Omission",
            severity="Major",
            comment="accuracy confirmed",
            needs_confirmation=True,
        )
        self.write_modules(
            [0],
            issues_by_module={
                "terminology": {0: [terminology]},
                "accuracy": {0: [accuracy]},
                "grammar": {0: [punctuation]},
            },
        )

        merged = self.merge_checks()

        self.assertEqual(
            merged,
            [{"id": 0, "issues": [terminology, accuracy, punctuation]}],
        )
        self.assertNotIn("corrected", merged[0])
        self.assertNotIn("errors", merged[0])

    def test_accuracy_owned_issue_from_terminology_module_is_rejected(self):
        segment = {
            "id": 0,
            "source": "source",
            "target": "bad",
            "kind": "desc",
            "term_hits": [],
            "protected_texts": [],
        }
        terminology_issue = check_issue(
            category="Mistranslation",
            severity="Major",
            comment="terminology rewrite",
            edit=replacement("bad", "term guess"),
        )
        accuracy_issue = check_issue(
            category="Mistranslation",
            severity="Major",
            comment="accuracy rewrite",
            edit=replacement("bad", "accurate"),
        )
        self.make_job([segment])
        self.write_modules(
            [0],
            issues_by_module={
                "terminology": {0: [terminology_issue]},
                "accuracy": {0: [accuracy_issue]},
            },
        )

        result = self.run_chunk("merge-checks", "--job", self.job)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("terminology cannot own category 'Mistranslation'", result.stderr)
        self.assertFalse((self.chunks / "chunk_00.out.json").exists())

    def test_precheck_reports_minor_without_a_local_edit(self):
        state_path = self.job / "state.json"
        output_path = self.job / "errors_precheck.json"
        write_json(
            state_path,
            {
                "source_lang": "en",
                "target_lang": "en",
                "segments": [{"id": 0, "source": "A-B", "target": "A—B"}],
            },
        )

        call_quietly(run_pre_check, state_path, output_path)
        result = read_json(output_path)

        self.assertEqual(set(result[0]), {"id", "issues"})
        em_dash = next(
            issue for issue in result[0]["issues"] if "Em dash" in issue["comment"]
        )
        self.assertTrue(em_dash["needs_confirmation"])
        self.assertIsNone(em_dash["edit"])

    def test_em_dash_variants_do_not_generate_minor_corrected_text(self):
        variants = ("A—B", "A — B", "A— B", "A —B")
        for target in variants:
            with self.subTest(target=target):
                state_path = self.job / "state.json"
                output_path = self.job / "errors_precheck.json"
                write_json(
                    state_path,
                    {
                        "source_lang": "en",
                        "target_lang": "en",
                        "segments": [
                            {"id": 0, "source": "A-B", "target": target}
                        ],
                    },
                )
                call_quietly(run_pre_check, state_path, output_path)
                issues = read_json(output_path)[0]["issues"]

                result = lqe_chunk.build_segment_result(
                    {
                        "id": 0,
                        "target": target,
                        "kind": "desc",
                        "term_hits": [],
                        "protected_texts": [],
                    },
                    issues,
                )

                self.assertIsNone(result["corrected"])
                self.assertTrue(
                    all(issue["needs_confirmation"] for issue in result["errors"])
                )
                self.assertTrue(
                    all(issue["edit"] is None for issue in result["errors"])
                )

    def test_precheck_issue_schema_is_complete_for_all_output_modes(self):
        cases = (
            ("empty", "Text", ""),
            ("reminder", "Hello {name}", "Hello"),
            ("double_space", "A B", "A  B"),
            ("fullwidth", "A,B", "A，B"),
        )
        required = {
            "category",
            "severity",
            "comment",
            "needs_confirmation",
            "edit",
        }
        for label, source, target in cases:
            with self.subTest(case=label):
                state_path = self.job / "state.json"
                output_path = self.job / "errors_precheck.json"
                write_json(
                    state_path,
                    {
                        "source_lang": "en",
                        "target_lang": "en",
                        "segments": [
                            {"id": 0, "source": source, "target": target}
                        ],
                    },
                )

                call_quietly(run_pre_check, state_path, output_path)
                issues = read_json(output_path)[0]["issues"]

                self.assertTrue(issues)
                for issue in issues:
                    self.assertTrue(required.issubset(issue))
                    self.assertTrue(
                        issue["edit"] is None or isinstance(issue["edit"], dict)
                    )

    def test_protected_segment_still_uses_correction_builder(self):
        segment = {
            "id": 0,
            "source": "protected",
            "target": "original",
            "protected": True,
            "kind": "desc",
            "term_hits": [],
            "protected_texts": ["original"],
        }
        self.make_job([segment])
        write_json(
            self.chunks / "chunk_00.out.json",
            [{"id": 0, "issues": []}],
        )
        output = self.job / "errors.json"

        with mock.patch.object(
            lqe_chunk,
            "build_segment_result",
            wraps=lqe_chunk.build_segment_result,
        ) as builder:
            call_quietly(
                lqe_chunk.cmd_merge,
                SimpleNamespace(
                    state=str(self.job / "state.json"),
                    errors=str(self.job / "errors_precheck.json"),
                    outdir=str(self.chunks),
                    out=str(output),
                ),
            )

        builder.assert_called_once()
        called_segment, called_issues = builder.call_args.args
        self.assertEqual(called_segment["id"], 0)
        self.assertEqual(called_issues, [])
        self.assertEqual(
            read_json(output),
            [{"id": 0, "errors": [], "corrected": None}],
        )

    def test_term_hits_flatten_senses_and_preserve_explicit_flags(self):
        from lqe_chunk import _term_hits

        hits = _term_hits(
            "魔草巫灵",
            [
                (
                    "魔草巫灵",
                    [
                        {"target": "Verdrel", "confirmed": True, "protected": True},
                        {"target": "Reference", "confirmed": False},
                    ],
                )
            ],
        )

        self.assertEqual(
            hits,
            [
                {
                    "source": "魔草巫灵",
                    "target": "Verdrel",
                    "confirmed": True,
                    "protected": True,
                },
                {
                    "source": "魔草巫灵",
                    "target": "Reference",
                    "confirmed": False,
                    "protected": False,
                },
            ],
        )

    def test_unconfirmed_names_do_not_generate_new_translations(self):
        cases = [
            ("深蓝鲸", "ธาลูน", "Thalune"),
            ("伊里斯", "Iris", "Irys"),
            ("星光狮", "Old Lion", "Blitzmane"),
        ]
        segments = []
        terminology = {}
        for segment_id, (source, original, proposed) in enumerate(cases):
            segments.append(
                {
                    "id": segment_id,
                    "source": source,
                    "target": original,
                    "kind": "name",
                    "term_hits": [
                        {
                            "source": source,
                            "target": proposed,
                            "confirmed": False,
                        }
                    ],
                    "protected_texts": [],
                }
            )
            terminology[segment_id] = [
                term_issue(
                    source,
                    [proposed],
                    source,
                    original,
                    severity="Major",
                    comment=f"{source} proposed name",
                    target_terms=(original,),
                    edit=replacement(
                        original,
                        proposed,
                        evidence=confirmed_evidence(source, proposed),
                    ),
                )
            ]
        self.make_job(segments)
        self.write_modules(range(len(segments)), issues_by_module={"terminology": terminology})

        self.merge_checks()
        merged = self.merge_final()

        self.assertEqual([row["corrected"] for row in merged], [None, None, None])
        for row in merged:
            self.assertTrue(row["errors"][0]["needs_confirmation"])
            self.assertIsNone(row["errors"][0]["edit"])

    def test_term_replacement_requires_confirmed_true(self):
        terms = [
            ("魔草巫灵", "Old Verdrel", "Verdrel"),
            ("奇丽花", "Old Florora", "Florora"),
        ]
        for source, original, target in terms:
            for confirmed in (False, True):
                with self.subTest(source=source, confirmed=confirmed):
                    segment = {
                        "id": 0,
                        "source": source,
                        "target": original,
                        "kind": "name",
                        "term_hits": [
                            {
                                "source": source,
                                "target": target,
                                "confirmed": confirmed,
                            }
                        ],
                        "protected_texts": [],
                    }
                    issue = term_issue(
                        source,
                        [target],
                        source,
                        original,
                        severity="Major",
                        comment="use confirmed term",
                        target_terms=(original,),
                        edit=replacement(
                            original,
                            target,
                            evidence=confirmed_evidence(source, target),
                        ),
                    )
                    self.make_job([segment])
                    self.write_modules(
                        [0], issues_by_module={"terminology": {0: [issue]}}
                    )

                    self.merge_checks()
                    merged = self.merge_final()

                    self.assertEqual(
                        merged[0]["corrected"], target if confirmed else None
                    )

    def test_conflicting_flower_butterfly_edits_do_not_land(self):
        source = "花衣蝶登场"
        original = "Old Butterfly appears"
        first = "Sprigella"
        second = "Hanagoromia"
        segment = {
            "id": 0,
            "source": source,
            "target": original,
            "kind": "desc",
            "term_hits": [
                {
                    "source": "花衣蝶",
                    "target": first,
                    "confirmed": True,
                    "matched_text": "Old Butterfly",
                },
                {
                    "source": "花衣蝶",
                    "target": second,
                    "confirmed": True,
                    "matched_text": "Old Butterfly",
                },
            ],
            "protected_texts": [],
        }
        first_issue = term_issue(
            "花衣蝶",
            [first],
            source,
            original,
            severity="Major",
            comment="first name",
            target_terms=("Old Butterfly",),
            edit=replacement(
                "Old Butterfly",
                first,
                evidence=confirmed_evidence("花衣蝶", first),
            ),
        )
        second_issue = term_issue(
            "花衣蝶",
            [second],
            source,
            original,
            severity="Major",
            comment="second name",
            target_terms=("Old Butterfly",),
            edit=replacement(
                "Old Butterfly",
                second,
                evidence=confirmed_evidence("花衣蝶", second),
            ),
        )
        self.make_job([segment])
        self.write_modules(
            [0],
            issues_by_module={
                "terminology": {0: [first_issue, second_issue]},
            },
        )

        self.merge_checks()
        merged = self.merge_final()

        self.assertIsNone(merged[0]["corrected"])
        self.assertEqual(len(merged[0]["errors"]), 2)
        self.assertTrue(
            all(issue["needs_confirmation"] for issue in merged[0]["errors"])
        )
        self.assertTrue(all(issue["edit"] is None for issue in merged[0]["errors"]))

    def test_mixed_row_only_applies_safe_edit(self):
        original = "สไตล์ฟลอโรรา- รองเท้า"
        segment = {
            "id": 0,
            "source": "奇丽花印象-鞋子",
            "target": original,
            "kind": "desc",
            "term_hits": [
                {"source": "奇丽花", "target": "Florora", "confirmed": False}
            ],
            "protected_texts": [],
        }
        pending_name = term_issue(
            "奇丽花",
            ["Florora"],
            segment["source"],
            original,
            severity="Major",
            comment="proper name needs confirmation",
            needs_confirmation=True,
            target_terms=("ฟลอโรรา",),
        )
        pending_name["resolution_status"] = "reference_allowed"
        spacing = check_issue(
            category="Punctuation",
            comment="space before hyphen",
            edit=replacement("-", " -"),
        )
        self.make_job([segment])
        self.write_modules(
            [0],
            issues_by_module={"terminology": {0: [pending_name]}, "grammar": {0: [spacing]}},
        )

        self.merge_checks()
        merged = self.merge_final()

        self.assertEqual(merged[0]["corrected"], "สไตล์ฟลอโรรา - รองเท้า")
        self.assertTrue(
            any(issue["needs_confirmation"] for issue in merged[0]["errors"])
        )

    def test_duplicate_broadcast_keeps_errors_and_generated_corrected(self):
        segments = [
            {"id": 0, "source": "same source", "target": "bad-text"},
            {"id": 1, "source": "same source", "target": "bad-text"},
        ]
        representative = {
            "id": 0,
            "source": "same source",
            "target": "bad-text",
            "kind": "desc",
            "term_hits": [],
            "protected_texts": [],
        }
        safe = check_issue(
            category="Punctuation",
            comment="replace hyphen",
            edit=replacement("-", " "),
        )
        self.make_job(
            segments,
            representatives=[representative],
            dedup_map={"0": [0, 1]},
        )
        self.write_modules([0], issues_by_module={"grammar": {0: [safe]}})

        self.merge_checks()
        merged = self.merge_final()

        self.assertEqual([row["id"] for row in merged], [0, 1])
        self.assertEqual([row["corrected"] for row in merged], ["bad text", "bad text"])
        self.assertEqual(merged[0]["errors"], merged[1]["errors"])

    def test_reconcile_writes_only_id_and_issues(self):
        self.make_job([{"id": 0, "source": "x", "target": "x"}])
        accuracy_issue = check_issue(
            category="Mistranslation",
            severity="Major",
            comment="confirmed by accuracy",
            edit=replacement("x", "accurate"),
        )
        dropped_issue = check_issue(
            category="Mistranslation",
            severity="Major",
            comment="other module guess",
            edit=replacement("x", "guess"),
        )
        self.write_modules([0], issues_by_module={"accuracy": {0: [accuracy_issue]}})
        write_json(
            self.chunks / "chunk_00.out.json",
            [{"id": 0, "issues": [accuracy_issue, dropped_issue]}],
        )

        result = self.run_chunk("reconcile", "--job", self.job)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            read_json(self.chunks / "chunk_00.out.json"),
            [{"id": 0, "issues": [accuracy_issue]}],
        )

    def test_split_half_uses_module_checkpoint_name(self):
        self.make_job(
            [
                {"id": 0, "source": "a", "target": "a"},
                {"id": 1, "source": "b", "target": "b"},
            ]
        )
        checkpoint = self.chunks / "chunk_00.grammar.ckpt.jsonl"
        checkpoint.write_text(
            json.dumps({"id": 0, "issues": []})
            + "\n"
            + json.dumps({"id": 1, "issues": []})
            + "\n",
            encoding="utf-8",
        )

        result = self.run_chunk(
            "split-half",
            "--job",
            self.job,
            "--chunk",
            "0",
            "--module",
            "grammar",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.chunks / "chunk_00_p1.grammar.ckpt.jsonl").exists())
        self.assertTrue((self.chunks / "chunk_00_p2.grammar.ckpt.jsonl").exists())

    def test_old_check_command_names_and_lens_flag_are_removed(self):
        source = CHUNK_SCRIPT.read_text(encoding="utf-8")

        self.assertNotIn("merge-lenses", source)
        self.assertNotIn("validate-lenses", source)
        self.assertNotIn('"--lens"', source)


class CorrectedOwnershipPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def run_script(self, script, *args):
        return subprocess.run(
            [sys.executable, str(script), *map(str, args)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def make_aggregate_job(self, *, errors, workbook=None, segments=None, name="multi"):
        job = self.root / name
        sheet_job = job / "Sheet1"
        source = self.root / f"{name}-source.xlsx"
        workbook = workbook or openpyxl.Workbook()
        worksheet = workbook["Sheet1"] if "Sheet1" in workbook.sheetnames else workbook.active
        worksheet.title = "Sheet1"
        if worksheet.max_row == 1 and worksheet.max_column == 1 and worksheet["A1"].value is None:
            worksheet["A1"] = "Source"
            worksheet["B1"] = "Target"
            worksheet["A2"] = "原文"
            worksheet["B2"] = "原译"
        workbook.save(source)
        segments = segments or [
            {
                "id": 0,
                "row_index": 0,
                "source": "原文",
                "target": "原译",
                "kind": "desc",
                "term_hits": [],
                "protected_texts": [],
            }
        ]
        state_data = {
            "input_path": str(source),
            "target_col": 1,
            "headers": ["Source", "Target"],
            "wordcount": 1,
            "segments": segments,
        }
        write_json(sheet_job / "state.json", state_data)
        write_json(sheet_job / "errors.json", errors)
        report = openpyxl.Workbook()
        report.active.title = "LQE Results"
        report.active.append(["原译", "建议译文"])
        attach_report_contract(report, state_data, errors)
        report.save(sheet_job / "Sheet1_lqe.xlsx")
        report.close()
        return job, source

    def run_aggregate(self, job):
        for child in job.iterdir():
            if not child.is_dir():
                continue
            report_path = child / f"{child.name}_lqe.xlsx"
            if not report_path.is_file():
                continue
            workbook = openpyxl.load_workbook(report_path)
            try:
                attach_report_contract(
                    workbook,
                    read_json(child / "state.json"),
                    read_json(child / "errors.json"),
                )
                workbook.save(report_path)
            finally:
                workbook.close()
        return self.run_script(AGGREGATE_SCRIPT, "--job", job)

    def test_build_results_is_required_before_apply(self):
        state = self.root / "state.json"
        checks = self.root / "checks.json"
        errors = self.root / "errors.json"
        write_json(
            state,
            {
                "segments": [
                    {
                        "id": 0,
                        "source": "ABC",
                        "target": "A“B”C",
                        "kind": "desc",
                        "term_hits": [],
                        "protected_texts": [],
                    }
                ]
            },
        )
        write_json(
            checks,
            [
                {
                    "id": 0,
                    "issues": [
                        check_issue(edit=replacement("“B”", '\"B\"'))
                    ],
                }
            ],
        )

        result = self.run_script(
            IO_SCRIPT,
            "build-results",
            "--state",
            state,
            "--checks",
            checks,
            "--out",
            errors,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read_json(errors)[0]["corrected"], 'A"B"C')

    def test_build_results_rejects_missing_and_extra_check_ids(self):
        state = self.root / "state.json"
        write_json(
            state,
            {
                "segments": [
                    {"id": 0, "source": "a", "target": "a"},
                    {"id": 1, "source": "b", "target": "b"},
                ]
            },
        )
        cases = {
            "missing": ([{"id": 0, "issues": []}], "missing=[1]"),
            "extra": (
                [
                    {"id": 0, "issues": []},
                    {"id": 1, "issues": []},
                    {"id": 2, "issues": []},
                ],
                "extra=[2]",
            ),
        }
        for name, (entries, message) in cases.items():
            with self.subTest(name=name):
                checks = self.root / f"{name}.json"
                errors = self.root / f"{name}-errors.json"
                write_json(checks, entries)

                result = self.run_script(
                    IO_SCRIPT,
                    "build-results",
                    "--state",
                    state,
                    "--checks",
                    checks,
                    "--out",
                    errors,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse(errors.exists())

    def test_batch_merge_rejects_model_corrected(self):
        job = self.root / "batch"
        write_json(
            job / "state.json",
            {"segments": [{"id": 0, "source": "x", "target": "x"}]},
        )
        write_json(
            job / "evals" / "eval_00.json",
            [{"id": 0, "issues": [], "corrected": "model text"}],
        )

        result = self.run_script(BATCH_SCRIPT, "merge", "--job", job)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("corrected", result.stderr)

    def test_batch_plan_reads_precheck_issue_contract(self):
        job = self.root / "batch-plan"
        write_json(
            job / "state.json",
            {"segments": [{"id": 0, "source": "原文", "target": "target"}]},
        )
        write_json(
            job / "errors_precheck.json",
            [{"id": 0, "issues": [check_issue(comment="precheck flag")]}],
        )

        result = self.run_script(
            BATCH_SCRIPT,
            "plan",
            "--job",
            job,
            "--output-budget",
            "400",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = (job / "batches" / "batch_00.txt").read_text(encoding="utf-8")
        self.assertIn("PRECHECK: Grammar:precheck flag", prompt)

    def test_batch_plan_flattens_multisense_terms_with_explicit_flags(self):
        job = self.root / "batch-terms"
        write_json(
            job / "state.json",
            {
                "segments": [{"id": 0, "source": "里奥出现", "target": "Rio"}],
                "terms_path": str(job / "terms.json"),
            },
        )
        write_json(
            job / "terms.json",
            [
                {
                    "source": "里奥",
                    "senses": [
                        {
                            "target": "ลีโอ",
                            "confirmed": True,
                            "protected": False,
                        },
                        {
                            "target": "ไลเอล",
                            "confirmed": False,
                            "protected": True,
                        },
                    ],
                }
            ],
        )

        result = self.run_script(BATCH_SCRIPT, "plan", "--job", job)

        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = (job / "batches" / "batch_00.txt").read_text(encoding="utf-8")
        self.assertIn("里奥=ลีโอ[confirmed=true, protected=false]", prompt)
        self.assertIn("里奥=ไลเอล[confirmed=false, protected=true]", prompt)

    def test_batch_merge_rejects_incomplete_id_coverage(self):
        job = self.root / "batch-missing"
        write_json(
            job / "state.json",
            {
                "segments": [
                    {"id": 0, "source": "a", "target": "a"},
                    {"id": 1, "source": "b", "target": "b"},
                ]
            },
        )
        write_json(job / "evals" / "eval_00.json", [{"id": 0, "issues": []}])

        result = self.run_script(BATCH_SCRIPT, "merge", "--job", job)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing=[1]", result.stderr)
        self.assertFalse((job / "errors.json").exists())

    def test_mastertb_merge_rejects_model_corrected(self):
        job = self.root / "mastertb"
        segment = {"id": 0, "source": "x", "target": "x"}
        write_json(
            job / "state.json",
            {"segments": [segment]},
        )
        write_json(
            job / "chunks" / "chunk_00.json",
            {"chunk_id": 0, "segments": [segment]},
        )
        for module in REQUIRED_MODULES:
            write_json(
                job / "chunks" / f"chunk_00.{module}.json",
                [{"id": 0, "issues": []}],
            )
        write_json(
            job / "chunks" / "chunk_00.out.json",
            [{"id": 0, "issues": [], "corrected": "model text"}],
        )

        result = self.run_script(
            MASTERTB_SCRIPT,
            "merge",
            "--job-dir",
            job,
            "--no-consistency",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("corrected", result.stderr)

    def test_aggregate_preserves_legacy_state_correction_fallback(self):
        job, _ = self.make_aggregate_job(
            errors=[{"id": 0, "errors": [], "corrected": None}],
            segments=[
                {
                    "id": 0,
                    "row_index": 0,
                    "source": "原文",
                    "target": "原译",
                    "corrected": "旧状态修正",
                    "kind": "desc",
                    "term_hits": [],
                    "protected_texts": [],
                }
            ],
        )

        result = self.run_aggregate(job)

        self.assertEqual(result.returncode, 0, result.stderr)
        output = openpyxl.load_workbook(job / f"{job.name}_corrected.xlsx")
        try:
            self.assertEqual(output["Sheet1"]["B2"].value, "旧状态修正")
        finally:
            output.close()

    def test_aggregate_uses_only_standard_parent_workbook_names(self):
        job, _ = self.make_aggregate_job(
            errors=[{"id": 0, "errors": [], "corrected": None}],
        )

        result = self.run_aggregate(job)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            sorted(path.name for path in job.glob("*.xlsx")),
            ["multi_corrected.xlsx", "multi_lqe.xlsx"],
        )

    def test_aggregate_rejects_forged_corrected_before_writing_outputs(self):
        job, _ = self.make_aggregate_job(
            errors=[{"id": 0, "errors": [], "corrected": "伪造修正"}]
        )
        outputs = [job / "multi_corrected.xlsx", job / "multi_lqe.xlsx"]
        for output in outputs:
            output.write_bytes(b"existing output")

        result = self.run_aggregate(job)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("corrected", result.stderr)
        for output in outputs:
            self.assertEqual(output.read_bytes(), b"existing output")

    def test_aggregate_requires_errors_field(self):
        job, _ = self.make_aggregate_job(
            errors=[{"id": 0, "corrected": None}],
        )

        result = self.run_aggregate(job)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("errors", result.stderr)
        self.assertFalse((job / "multi_corrected.xlsx").exists())
        self.assertFalse((job / "multi_lqe.xlsx").exists())

    def test_aggregate_requires_exact_result_id_coverage(self):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Sheet1"
        sheet.append(["Source", "Target"])
        sheet.append(["one", "one"])
        sheet.append(["two", "two"])
        segments = [
            {"id": 0, "row_index": 0, "source": "one", "target": "one"},
            {"id": 1, "row_index": 1, "source": "two", "target": "two"},
        ]
        cases = {
            "missing": (
                [{"id": 0, "errors": [], "corrected": None}],
                "missing=[1]",
            ),
            "extra": (
                [
                    {"id": 0, "errors": [], "corrected": None},
                    {"id": 1, "errors": [], "corrected": None},
                    {"id": 2, "errors": [], "corrected": None},
                ],
                "extra=[2]",
            ),
        }
        for name, (errors, message) in cases.items():
            with self.subTest(name=name):
                job, _ = self.make_aggregate_job(
                    errors=errors,
                    workbook=workbook,
                    segments=segments,
                    name=name,
                )

                result = self.run_aggregate(job)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse((job / f"{name}_corrected.xlsx").exists())
                self.assertFalse((job / f"{name}_lqe.xlsx").exists())

    def test_aggregate_rejects_sdlxliff_child_state(self):
        job = self.root / "sdl-parent"
        child = job / "child"
        write_json(
            child / "state.json",
            {
                "input_format": "sdlxliff",
                "input_path": str(self.root / "source.sdlxliff"),
                "segments": [],
            },
        )
        write_json(child / "errors.json", [])

        result = self.run_aggregate(job)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "SDLXLIFF jobs are not multi-sheet workbooks", result.stderr
        )
        self.assertFalse((job / "sdl-parent_corrected.xlsx").exists())
        self.assertFalse((job / "sdl-parent_lqe.xlsx").exists())

    def test_aggregate_preserves_complete_source_workbook(self):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Sheet1"
        sheet.append(["Source", "Target", "Formula", "Merged", None])
        sheet.append(["原文", "原译", "=LEN(B2)", "keep", None])
        sheet.merge_cells("D1:E1")
        sheet.column_dimensions["A"].width = 18
        sheet.column_dimensions["B"].width = 24
        sheet["B2"].fill = PatternFill(fill_type="solid", fgColor="00FF00")
        notes = workbook.create_sheet("Notes")
        notes["A1"] = "untouched"
        error = check_issue(
            edit=replacement("原", "新"),
        )
        job, source = self.make_aggregate_job(
            errors=[{"id": 0, "errors": [error], "corrected": "新译"}],
            workbook=workbook,
        )

        result = self.run_aggregate(job)

        self.assertEqual(result.returncode, 0, result.stderr)
        original = openpyxl.load_workbook(source, data_only=False)
        output = openpyxl.load_workbook(job / "multi_corrected.xlsx", data_only=False)
        try:
            self.assertEqual(output.sheetnames, original.sheetnames)
            self.assertEqual(output["Sheet1"]["B2"].value, "新译")
            self.assertEqual(
                output["Sheet1"]["B2"].style_id,
                original["Sheet1"]["B2"].style_id,
            )
            self.assertEqual(output["Sheet1"]["C2"].value, "=LEN(B2)")
            self.assertEqual(
                list(output["Sheet1"].merged_cells.ranges),
                list(original["Sheet1"].merged_cells.ranges),
            )
            self.assertEqual(output["Sheet1"].column_dimensions["A"].width, 18)
            self.assertEqual(output["Sheet1"].column_dimensions["B"].width, 24)
            self.assertEqual(output["Notes"]["A1"].value, "untouched")
        finally:
            original.close()
            output.close()

    def test_finalize_uses_only_new_check_commands(self):
        syntax = subprocess.run(
            ["bash", "-n", str(FINALIZE_SCRIPT)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

        job = self.root / "finalize"
        (job / "chunks").mkdir(parents=True)
        write_json(job / "state.json", {})
        write_json(job / "chunks" / "chunk_00.json", {})
        log = self.root / "python-calls.log"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        python_stub = bin_dir / "python3"
        python_stub.write_text(
            """#!/bin/sh
printf '%s\\n' "$*" >> "$CALL_LOG"
case "$1" in
  -)
    script=$(cat)
    case "$script" in
      *enabled_modules*) printf 'terminology, accuracy, grammar, naturalness\\n' ;;
      *) printf '98\\n' ;;
    esac
    ;;
  *lqe_calc.py) printf '{"score":100,"status":"PASS","errors":0,"wordcount":1,"critical":0,"npt":0}\\n' ;;
  -c)
    case "$2" in
      *score*) printf '100\\n' ;;
      *status*) printf 'PASS\\n' ;;
    esac
    ;;
esac
""",
            encoding="utf-8",
        )
        python_stub.chmod(0o755)
        env = dict(os.environ)
        env["CALL_LOG"] = str(log)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"

        result = subprocess.run(
            ["bash", str(FINALIZE_SCRIPT), str(job), "1", "single"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env=env,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "enabled modules: terminology, accuracy, grammar, naturalness",
            result.stdout,
        )
        calls = [
            line.split()
            for line in log.read_text(encoding="utf-8").splitlines()
            if "lqe_chunk.py" in line
        ]
        self.assertEqual(
            [call[1] for call in calls],
            ["validate-checks", "merge-checks", "reconcile", "merge"],
        )
        for call in calls[:3]:
            self.assertEqual(call[2:4], ["--job", str(job)])
        self.assertEqual(
            calls[3][2:],
            [
                "--state", str(job / "state.json"),
                "--errors", str(job / "errors_precheck.json"),
                "--outdir", str(job / "chunks"),
                "--out", str(job / "errors.json"),
            ],
        )

    def test_finalize_defaults_to_single_when_mode_is_omitted(self):
        job = self.root / "finalize-default"
        (job / "chunks").mkdir(parents=True)
        write_json(job / "state.json", {})
        write_json(job / "chunks" / "chunk_00.json", {})
        log = self.root / "python-calls-default.log"
        bin_dir = self.root / "bin-default"
        bin_dir.mkdir()
        python_stub = bin_dir / "python3"
        python_stub.write_text(
            """#!/bin/sh
printf '%s\n' "$*" >> "$CALL_LOG"
case "$1" in
  -)
    script=$(cat)
    case "$script" in
      *enabled_modules*) printf 'terminology, accuracy, grammar, naturalness\n' ;;
      *) printf '98\n' ;;
    esac
    ;;
  *lqe_calc.py) printf '{"score":90,"status":"FAIL","errors":1,"wordcount":1,"critical":0,"npt":0}\n' ;;
  -c)
    case "$2" in
      *score*) printf '90\n' ;;
      *status*) printf 'FAIL\n' ;;
    esac
    ;;
esac
""",
            encoding="utf-8",
        )
        python_stub.chmod(0o755)
        env = dict(os.environ)
        env["CALL_LOG"] = str(log)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"

        result = subprocess.run(
            ["bash", str(FINALIZE_SCRIPT), str(job), "1"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env=env,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = log.read_text(encoding="utf-8").splitlines()
        self.assertTrue(any("lqe_io.py write" in call for call in calls), calls)
        self.assertFalse(any("lqe_io.py apply-fixes" in call for call in calls), calls)
        self.assertIn(
            "enabled modules: terminology, accuracy, grammar, naturalness",
            result.stdout,
        )
        self.assertIn("MODE=single", result.stdout)

    def test_finalize_stops_before_marker_when_a_required_stage_fails(self):
        bin_dir = self.root / "bin-stage-failures"
        bin_dir.mkdir()
        python_stub = bin_dir / "python3"
        python_stub.write_text(
            """#!/bin/sh
case "$*" in
  *"$FAIL_TOKEN"*) exit 9 ;;
esac
case "$1" in
  -)
    script=$(cat)
    case "$script" in
      *enabled_modules*) printf 'terminology, accuracy, grammar, naturalness\n' ;;
      *) printf '98\n' ;;
    esac
    ;;
  *lqe_calc.py) printf '{"score":100,"status":"PASS","errors":0,"wordcount":1,"critical":0,"npt":0}\n' ;;
  -c)
    case "$2" in
      *score*) printf '100\n' ;;
      *status*) printf 'PASS\n' ;;
    esac
    ;;
esac
""",
            encoding="utf-8",
        )
        python_stub.chmod(0o755)

        for index, token in enumerate(
            (
                "lqe_chunk.py reconcile",
                "lqe_calc.py",
                "lqe_io.py write",
                "lqe_io.py export",
            )
        ):
            with self.subTest(token=token):
                job = self.root / f"stage-failure-{index}"
                (job / "chunks").mkdir(parents=True)
                write_json(job / "state.json", {})
                write_json(job / "chunks" / "chunk_00.json", {})
                env = dict(os.environ)
                env["FAIL_TOKEN"] = token
                env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"

                result = subprocess.run(
                    ["bash", str(FINALIZE_SCRIPT), str(job), "1", "single"],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    env=env,
                )

                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse((job / ".finalized").exists())
                self.assertNotIn("FINALIZED", result.stdout)


class CorrectedOwnershipOutputTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def run_io(self, *args):
        return subprocess.run(
            [sys.executable, str(IO_SCRIPT), *map(str, args)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def make_export_fixture(self, name, segments, errors):
        job = self.root / name
        job.mkdir()
        source = job / "source.xlsx"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["Source", "Target"])
        for segment in segments:
            sheet.append([segment["source"], segment["target"]])
        workbook.save(source)
        workbook.close()
        state = job / "state.json"
        results = job / "errors.json"
        write_json(
            state,
            {
                "input_path": str(source),
                "source_col": "Source",
                "target_col": 1,
                "headers": ["Source", "Target"],
                "segments": segments,
            },
        )
        write_json(results, errors)
        return state, results, job / f"{name}_corrected.xlsx"

    def test_export_rejects_forged_corrected_before_touching_output(self):
        segments = [
            {"id": 0, "row_index": 0, "source": "Source", "target": "原译"}
        ]
        state, errors, output = self.make_export_fixture(
            "forged-export",
            segments,
            [{"id": 0, "errors": [], "corrected": "伪造修正"}],
        )
        output.write_bytes(b"existing output")

        result = self.run_io("export", "--state", state, "--errors", errors)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("corrected", result.stderr)
        self.assertEqual(output.read_bytes(), b"existing output")

    def test_export_verifies_confirmed_term_edit_with_chunk_context(self):
        segments = [
            {"id": 0, "row_index": 0, "source": "世界", "target": "World"}
        ]
        issue = term_issue(
            "世界",
            ["Realm"],
            segments[0]["source"],
            segments[0]["target"],
            severity="Major",
            comment="Use the confirmed project term.",
            target_terms=("World",),
            edit=replacement(
                "World",
                "Realm",
                evidence=confirmed_evidence("世界", "Realm"),
            ),
        )
        state, errors, output = self.make_export_fixture(
            "confirmed-term-export",
            segments,
            [{"id": 0, "errors": [issue], "corrected": "Realm"}],
        )
        state_data = read_json(state)
        state_data["terminology"] = [
            {
                "source": "世界",
                "target": "Realm",
                "confirmed": True,
                "protected": False,
            }
        ]
        write_json(state, state_data)
        precheck = state.parent / "errors_precheck.json"
        write_json(precheck, [{"id": 0, "issues": []}])
        split = subprocess.run(
            [
                sys.executable,
                str(CHUNK_SCRIPT),
                "split",
                "--state",
                str(state),
                "--errors",
                str(precheck),
                "--outdir",
                str(state.parent / "chunks"),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(split.returncode, 0, split.stderr)

        result = self.run_io("export", "--state", state, "--errors", errors)

        self.assertEqual(result.returncode, 0, result.stderr)
        workbook = openpyxl.load_workbook(output, data_only=False)
        try:
            self.assertEqual(workbook.active["B2"].value, "Realm")
        finally:
            workbook.close()

    def test_export_requires_exact_result_id_coverage_before_touching_output(self):
        segments = [
            {"id": 0, "row_index": 0, "source": "one", "target": "one"},
            {"id": 1, "row_index": 1, "source": "two", "target": "two"},
        ]
        cases = {
            "missing": (
                [{"id": 0, "errors": [], "corrected": None}],
                "missing=[1]",
            ),
            "extra": (
                [
                    {"id": 0, "errors": [], "corrected": None},
                    {"id": 1, "errors": [], "corrected": None},
                    {"id": 2, "errors": [], "corrected": None},
                ],
                "extra=[2]",
            ),
        }
        for name, (entries, message) in cases.items():
            with self.subTest(name=name):
                state, errors, output = self.make_export_fixture(
                    f"{name}-export", segments, entries
                )
                output.write_bytes(b"existing output")

                result = self.run_io("export", "--state", state, "--errors", errors)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertEqual(output.read_bytes(), b"existing output")

    def test_apply_fixes_does_not_apply_or_advance_for_protected_row(self):
        protected = check_issue(edit=replacement("原译", "篡改"))
        protected["protected"] = True
        segments = [
            {
                "id": 0,
                "row_index": 0,
                "source": "Source",
                "target": "原译",
                "kind": "desc",
                "term_hits": [],
                "protected_texts": [],
            }
        ]
        state, errors, _ = self.make_export_fixture(
            "protected-apply",
            segments,
            [{"id": 0, "errors": [protected], "corrected": "篡改"}],
        )
        state_data = read_json(state)
        state_data["rows_raw"] = [["Source", "原译"]]
        state_data["wordcount"] = 1
        state_data["iteration"] = 0
        write_json(state, state_data)

        result = self.run_io("apply-fixes", "--state", state, "--errors", errors)

        self.assertEqual(result.returncode, 0, result.stderr)
        updated = read_json(state)
        self.assertIsNone(updated["segments"][0].get("corrected"))
        self.assertEqual(updated["iteration"], 0)

    def test_export_protects_rows_but_applies_mixed_safe_correction(self):
        protected = check_issue(edit=replacement("原译一", "篡改"))
        protected["protected"] = True
        confirmation = check_issue(needs_confirmation=True)
        safe = check_issue(edit=replacement("原译二", "安全修改"))
        segments = [
            {
                "id": 0,
                "row_index": 0,
                "source": "one",
                "target": "原译一",
                "kind": "desc",
                "term_hits": [],
                "protected_texts": [],
            },
            {
                "id": 1,
                "row_index": 1,
                "source": "two",
                "target": "原译二",
                "kind": "desc",
                "term_hits": [],
                "protected_texts": [],
            },
        ]
        state, errors, output = self.make_export_fixture(
            "protected-export",
            segments,
            [
                {"id": 0, "errors": [protected], "corrected": "篡改"},
                {
                    "id": 1,
                    "errors": [confirmation, safe],
                    "corrected": "安全修改",
                },
            ],
        )

        result = self.run_io("export", "--state", state, "--errors", errors)

        self.assertEqual(result.returncode, 0, result.stderr)
        workbook = openpyxl.load_workbook(output)
        try:
            self.assertEqual(workbook.active["B2"].value, "原译一")
            self.assertEqual(workbook.active["B3"].value, "安全修改")
        finally:
            workbook.close()

    def test_tabular_corrected_xlsx_keeps_formula_like_translation_as_text(self):
        segments = [
            {"id": 0, "row_index": 0, "source": "Formula", "target": "原译"}
        ]
        corrected = "=SUM(1,2)"
        state, errors, output = self.make_export_fixture(
            "literal-formula-export",
            segments,
            [
                {
                    "id": 0,
                    "errors": [
                        check_issue(edit=replacement("原译", corrected))
                    ],
                    "corrected": corrected,
                }
            ],
        )

        result = self.run_io("export", "--state", state, "--errors", errors)

        self.assertEqual(result.returncode, 0, result.stderr)
        workbook = openpyxl.load_workbook(output, data_only=False)
        try:
            cell = workbook.active["B2"]
            self.assertEqual(cell.value, corrected)
            self.assertEqual(cell.data_type, "s")
        finally:
            workbook.close()
        workbook = openpyxl.load_workbook(output, data_only=True)
        try:
            self.assertEqual(workbook.active["B2"].value, corrected)
        finally:
            workbook.close()

    def test_sdl_corrected_xlsx_keeps_formula_like_text_literal(self):
        job = self.root / "sdl-literal-formula"
        job.mkdir()
        state_path = job / "state.json"
        state = {
            "input_format": "sdlxliff",
            "input_path": str(job / "source.sdlxliff"),
            "segments": [
                {
                    "id": 0,
                    "source": "=SOURCE()",
                    "target": "=TARGET()",
                    "source_ref": {
                        "relative_path": "source.sdlxliff",
                        "tu_id": "1",
                        "sdl_segment_id": "1",
                    },
                    "metadata": {"sdlxliff": {"file_original": "ui.xml"}},
                }
            ],
        }
        corrected = "=CORRECTED()"

        output = lqe_io._export_sdlxliff_xlsx(
            state_path,
            state,
            {0: {"errors": [], "corrected": corrected}},
        )

        workbook = openpyxl.load_workbook(output, data_only=False)
        try:
            row = workbook.active[2]
            self.assertEqual([cell.value for cell in row][3:], ["=SOURCE()", corrected])
            self.assertEqual([cell.data_type for cell in row][3:], ["s", "s"])
        finally:
            workbook.close()
        workbook = openpyxl.load_workbook(output, data_only=True)
        try:
            self.assertEqual(
                [cell.value for cell in workbook.active[2]][3:],
                ["=SOURCE()", corrected],
            )
        finally:
            workbook.close()

    @staticmethod
    def workbook_signature(path):
        workbook = openpyxl.load_workbook(path, data_only=False)
        try:
            return {
                "sheetnames": workbook.sheetnames,
                "dimensions": {
                    sheet.title: sheet.calculate_dimension() for sheet in workbook
                },
                "headers": {
                    sheet.title: [cell.value for cell in sheet[1]]
                    for sheet in workbook
                },
            }
        finally:
            workbook.close()

    def test_report_uses_plain_processing_labels(self):
        output = self.root / "sample_lqe.xlsx"
        targets = ["原译一", "原译二", "原译三", "原译四", "原译五"]
        state = {
            "input_path": str(self.root / "sample.xlsx"),
            "headers": ["原文", "译文"],
            "rows_raw": [
                [f"Source {index}", target]
                for index, target in enumerate(targets)
            ],
            "segments": [
                {
                    "id": index,
                    "source": f"Source {index}",
                    "target": target,
                    "kind": "desc",
                }
                for index, target in enumerate(targets)
            ],
            "wordcount": len(targets),
        }
        protected = check_issue()
        protected["protected"] = True
        history = [
            {
                "iteration": 0,
                "errors": [
                    {
                        "id": 0,
                        "errors": [check_issue()],
                        "corrected": "建议译文一",
                    },
                    {
                        "id": 1,
                        "errors": [check_issue(needs_confirmation=True)],
                        "corrected": "安全修改二",
                    },
                    {"id": 2, "errors": [check_issue()], "corrected": None},
                    {"id": 3, "errors": [], "corrected": None},
                    {"id": 4, "errors": [protected], "corrected": None},
                ],
            }
        ]

        call_quietly(lqe_io._build_xlsx, state, history, 99, 98, output)

        workbook = openpyxl.load_workbook(output)
        try:
            headers = [cell.value for cell in workbook["LQE Results"][1]]
            self.assertIn("原译", headers)
            self.assertIn("AI/建议译文", headers)
            self.assertIn("处理方式", headers)
            processing_col = headers.index("处理方式") + 1
            labels = [
                workbook["LQE Results"].cell(row=row, column=processing_col).value
                for row in range(2, 7)
            ]
            self.assertEqual(
                labels,
                ["建议修改", "需要人工确认", "仅提醒", "无需修改", "已保护，不修改"],
            )
        finally:
            workbook.close()

    def test_report_groups_one_row_per_issue_and_shows_ai_provenance(self):
        output = self.root / "grouped_issues_lqe.xlsx"
        state = {
            "input_path": str(self.root / "sample.xlsx"),
            "headers": ["原文", "译文"],
            "rows_raw": [
                [f"Source {index}", f"Target {index}"] for index in range(4)
            ],
            "segments": [
                {
                    "id": index,
                    "source": f"Source {index}",
                    "target": f"Target {index}",
                    "kind": "desc",
                }
                for index in range(4)
            ],
            "wordcount": 4,
        }
        edited = check_issue(comment="AI edited this issue.")
        edited["review_provenance"] = {
            "finding_origin": "ai_module",
            "ai_reviewed": True,
            "ai_edited": True,
            "review_module": "grammar",
            "reviewed_segment_id": 0,
            "edit_origin": "ai_module",
        }
        reviewed = check_issue(comment="AI reviewed without editing.")
        reviewed["review_provenance"] = {
            "finding_origin": "ai_module",
            "ai_reviewed": True,
            "ai_edited": False,
            "review_module": "accuracy",
            "reviewed_segment_id": 0,
            "edit_origin": None,
        }
        machine = check_issue(comment="Machine precheck only.")
        machine["review_provenance"] = {
            "finding_origin": "machine_precheck",
            "ai_reviewed": False,
            "ai_edited": True,
            "review_module": None,
            "reviewed_segment_id": None,
            "edit_origin": None,
        }
        legacy = check_issue(comment="Legacy issue without provenance.")
        history = [{
            "iteration": 0,
            "errors": [
                {
                    "id": 0,
                    "errors": [edited, reviewed],
                    "corrected": "Target 0 fixed",
                },
                {"id": 1, "errors": [machine], "corrected": None},
                {"id": 2, "errors": [legacy], "corrected": None},
                {"id": 3, "errors": [], "corrected": None},
            ],
        }]

        call_quietly(lqe_io._build_xlsx, state, history, 99, 98, output)

        workbook = openpyxl.load_workbook(output, data_only=True)
        try:
            sheet = workbook["LQE Results"]
            headers = [cell.value for cell in sheet[1]]
            rows = [
                {
                    header: sheet.cell(row=row, column=column).value
                    for column, header in enumerate(headers, start=1)
                }
                for row in range(2, sheet.max_row + 1)
            ]
            self.assertEqual([row["LQE Segment ID"] for row in rows], [0, 0, 1, 2, 3])
            self.assertEqual([row["LQE 错误序号"] for row in rows], [1, 2, 1, 1, None])
            self.assertEqual(
                [row["LQE AI 复核状态"] for row in rows],
                [
                    "已复核（AI 模块记录）",
                    "已复核（AI 模块记录）",
                    "未确认（机器预检保留）",
                    "未知（旧流程未记录）",
                    "不适用",
                ],
            )
            self.assertEqual(
                [row["LQE AI 编辑状态"] for row in rows],
                [
                    "已生成并验证建议（AI 模块记录）",
                    "已复核、未生成 AI 建议",
                    "未由 AI 编辑",
                    "未知（旧流程未记录）",
                    "不适用",
                ],
            )
            self.assertEqual(
                [row["LQE 检查来源"] for row in rows],
                [
                    "AI 模块：grammar",
                    "AI 模块：accuracy",
                    "机器预检",
                    "旧流程（来源未记录）",
                    "不适用",
                ],
            )
            self.assertEqual(
                [row["错误详情"] for row in rows],
                [
                    "[Grammar · Major] AI edited this issue.",
                    "[Grammar · Major] AI reviewed without editing.",
                    "[Grammar · Major] Machine precheck only.",
                    "[Grammar · Major] Legacy issue without provenance.",
                    None,
                ],
            )
            self.assertEqual(
                [row["AI/建议译文"] for row in rows[:2]],
                ["Target 0 fixed", None],
            )
            self.assertTrue(sheet.row_dimensions[3].hidden)
            scorecard = workbook["LQA Scorecard"]
            header_row = next(
                row
                for row in range(1, scorecard.max_row + 1)
                if scorecard.cell(row, 1).value == "Segment ID"
            )
            score_headers = [cell.value for cell in scorecard[header_row]]
            self.assertEqual(
                [
                    "Segment ID",
                    "原文",
                    "原译",
                    "AI/建议译文",
                    "建议状态",
                    "错误类别",
                    "严重度",
                    "问题说明",
                    "审校结论",
                    "审校终稿或备注",
                ],
                score_headers,
            )
        finally:
            workbook.close()

    def test_current_report_disambiguates_source_headers_and_rejects_forged_audit_rows(self):
        output = self.root / "audit-contract_lqe.xlsx"
        state = {
            "artifact_contract_version": 1,
            "input_path": str(self.root / "audit-source.xlsx"),
            "headers": ["LQE Segment ID", "LQE AI 复核状态", "译文"],
            "rows_raw": [["source-id", "source-status", "Target"]],
            "source_col": 0,
            "target_col": 2,
            "segments": [
                {
                    "id": 0,
                    "source": "Source",
                    "target": "Target",
                    "kind": "desc",
                }
            ],
            "wordcount": 1,
        }
        reviewed = check_issue(comment="Reviewed issue " + "detail " * 320)
        reviewed["review_provenance"] = {
            "finding_origin": "ai_module",
            "ai_reviewed": True,
            "ai_edited": False,
            "review_module": "grammar",
            "reviewed_segment_id": 0,
            "edit_origin": None,
        }
        results = [{"id": 0, "errors": [reviewed], "corrected": None}]
        history = [{"iteration": 0, "errors": results}]

        call_quietly(
            lqe_io._build_xlsx,
            state,
            history,
            99,
            98,
            output,
            report_contract_results=results,
        )

        workbook = openpyxl.load_workbook(output, rich_text=True)
        try:
            sheet = workbook["LQE Results"]
            headers = [cell.value for cell in sheet[1]]
            self.assertEqual(len(headers), len(set(headers)))
            self.assertEqual(headers[-1], "LQE_Iter")
            generated_segment = "LQE Segment ID"
            generated_review = "LQE AI 复核状态"
            self.assertIn(generated_segment, headers)
            self.assertIn(generated_review, headers)
            self.assertIn("LQE Segment ID（原始数据）", headers)
            self.assertIn("LQE AI 复核状态（原始数据）", headers)
            self.assertEqual(
                sheet.cell(2, headers.index(generated_segment) + 1).value,
                0,
            )
            sheet.cell(2, headers.index(generated_review) + 1).value = "已编辑"
            with self.assertRaisesRegex(
                ValueError,
                "rows do not match",
            ):
                attach_report_contract(workbook, state, results)
        finally:
            workbook.close()

        workbook = openpyxl.load_workbook(output, rich_text=True)
        try:
            scorecard = workbook["LQA Scorecard"]
            header_row = next(
                row
                for row in range(1, scorecard.max_row + 1)
                if scorecard.cell(row, 1).value == "Segment ID"
            )
            headers = [cell.value for cell in scorecard[header_row]]
            comment_column = headers.index("问题说明") + 1
            detail_row = header_row + 1
            scorecard.cell(detail_row, comment_column).value = "伪造：已复核"
            with self.assertRaisesRegex(
                ValueError,
                "visible_scorecard_digest",
            ):
                validate_report_contract(workbook, state, results)
        finally:
            workbook.close()

    def test_scorecard_detail_uses_each_segments_source_relative_path(self):
        output = self.root / "multi_source_lqe.xlsx"
        state = {
            "input_path": str(self.root / "aggregate_review_source.xlsx"),
            "headers": ["来源相对路径", "原文", "译文"],
            "rows_raw": [
                ["day1/first.xlsx.sdlxliff", "Source one", "Target one"],
                ["day2/second.xlsx.sdlxliff", "Source two", "Target two"],
            ],
            "segments": [
                {"id": 0, "source": "Source one", "target": "Target one"},
                {"id": 1, "source": "Source two", "target": "Target two"},
            ],
            "wordcount": 2,
        }
        history = [
            {
                "iteration": 0,
                "errors": [
                    {"id": 0, "errors": [check_issue()], "corrected": None},
                    {"id": 1, "errors": [check_issue()], "corrected": None},
                ],
            }
        ]

        call_quietly(lqe_io._build_xlsx, state, history, 99, 98, output)

        workbook = openpyxl.load_workbook(output)
        try:
            results = workbook["LQE Results"]
            headers = [cell.value for cell in results[1]]
            source_path_column = headers.index("来源相对路径") + 1
            self.assertEqual(
                [
                    results.cell(row=row, column=source_path_column).value
                    for row in range(2, 4)
                ],
                ["day1/first.xlsx.sdlxliff", "day2/second.xlsx.sdlxliff"],
            )
        finally:
            workbook.close()

    def test_scorecard_source_relative_path_column_is_wide_enough(self):
        output = self.root / "source_path_width_lqe.xlsx"
        state = {
            "input_path": str(self.root / "aggregate_review_source.xlsx"),
            "headers": ["来源相对路径", "原文", "译文"],
            "rows_raw": [
                [
                    "day/01_dialogs_words_overrided_name_result.xlsx.sdlxliff",
                    "Source",
                    "Target",
                ]
            ],
            "segments": [{"id": 0, "source": "Source", "target": "Target"}],
            "wordcount": 1,
        }
        history = [
            {
                "iteration": 0,
                "errors": [
                    {"id": 0, "errors": [check_issue()], "corrected": None}
                ],
            }
        ]

        call_quietly(lqe_io._build_xlsx, state, history, 99, 98, output)

        workbook = openpyxl.load_workbook(output)
        try:
            scorecard = workbook["LQA Scorecard"]
            self.assertEqual(scorecard.max_column, 10)
            self.assertTrue(
                all(
                    not scorecard.column_dimensions[
                        openpyxl.utils.get_column_letter(column)
                    ].hidden
                    for column in range(1, 11)
                )
            )
        finally:
            workbook.close()

    def test_lqe_report_keeps_formula_like_external_text_literal(self):
        output = self.root / "literal_report_lqe.xlsx"
        state = {
            "input_path": str(self.root / "=INPUT.xlsx"),
            "headers": ["来源相对路径", "原文", "译文"],
            "rows_raw": [["=FILE()", "=SOURCE()", "=TARGET()"]],
            "segments": [
                {
                    "id": 0,
                    "source": "=SOURCE()",
                    "target": "=TARGET()",
                }
            ],
            "wordcount": 1,
        }
        history = [
            {
                "iteration": 0,
                "errors": [
                    {
                        "id": 0,
                        "errors": [
                            check_issue(comment="=COMMENT()")
                        ],
                        "corrected": "=CORRECTED()",
                    }
                ],
            }
        ]

        call_quietly(lqe_io._build_xlsx, state, history, 99, 98, output)

        workbook = openpyxl.load_workbook(output, data_only=False)
        try:
            scorecard = workbook["LQA Scorecard"]
            header_row = next(
                row
                for row in range(1, scorecard.max_row + 1)
                if scorecard.cell(row=row, column=1).value == "Segment ID"
            )
            detail = scorecard[header_row + 1]
            self.assertEqual(
                [detail[index].value for index in (1, 2, 3, 7)],
                [
                    "=SOURCE()",
                    "=TARGET()",
                    "=CORRECTED()",
                    "=COMMENT()",
                ],
            )
            self.assertEqual(
                [detail[index].data_type for index in (1, 2, 3, 7)],
                ["s"] * 4,
            )
            result_row = workbook["LQE Results"][2]
            self.assertEqual(
                [result_row[index].value for index in (0, 1, 2, 3)],
                [0, "=SOURCE()", "=TARGET()", "=CORRECTED()"],
            )
            self.assertEqual(
                [result_row[index].data_type for index in (0, 1, 2, 3)],
                ["n", "s", "s", "s"],
            )
            result_headers = [
                cell.value for cell in workbook["LQE Results"][1]
            ]
            file_column = result_headers.index("来源相对路径") + 1
            self.assertEqual(
                workbook["LQE Results"].cell(2, file_column).value,
                "=FILE()",
            )
            self.assertEqual(
                workbook["LQE Results"].cell(2, file_column).data_type,
                "s",
            )
        finally:
            workbook.close()
        workbook = openpyxl.load_workbook(output, data_only=True)
        try:
            scorecard = workbook["LQA Scorecard"]
            header_row = next(
                row
                for row in range(1, scorecard.max_row + 1)
                if scorecard.cell(row=row, column=1).value == "Segment ID"
            )
            self.assertEqual(scorecard.cell(header_row + 1, 3).value, "=TARGET()")
            self.assertEqual(
                workbook["LQE Results"].cell(row=2, column=4).value,
                "=CORRECTED()",
            )
        finally:
            workbook.close()

    def test_processing_label_uses_only_errors_and_corrected(self):
        processing_label = getattr(lqe_io, "_processing_label", None)
        self.assertIsNotNone(processing_label)
        protected = check_issue()
        protected["protected"] = True
        cases = [
            ({"errors": [protected], "corrected": "ignored"}, "已保护，不修改"),
            (
                {
                    "errors": [check_issue(needs_confirmation=True)],
                    "corrected": "安全修改",
                },
                "需要人工确认",
            ),
            ({"errors": [check_issue()], "corrected": "建议译文"}, "建议修改"),
            ({"errors": [check_issue()], "corrected": None}, "仅提醒"),
            ({"errors": [], "corrected": None}, "无需修改"),
        ]
        for entry, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(processing_label(entry), expected)

    def test_errors_without_corrected_are_valid(self):
        issues = lqe_io._validate_errors(
            [{"id": 0, "errors": [check_issue()], "corrected": None}],
            {0},
        )

        self.assertFalse(
            any("corrected=null" in issue for issue in issues),
            issues,
        )

    def test_corrected_workbook_has_source_structure(self):
        source = self.root / "sample.xlsx"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Content"
        sheet.append(["Source", "Target", "Formula"])
        sheet.append(["Source", "原译", "=LEN(B2)"])
        sheet.append(["Reminder", "保留原译", "=LEN(B3)"])
        notes = workbook.create_sheet("Notes")
        notes["A1"] = "untouched"
        workbook.save(source)
        workbook.close()
        state_path = self.root / "state.json"
        errors_path = self.root / "errors.json"
        write_json(
            state_path,
            {
                "input_path": str(source),
                "source_col": "Source",
                "target_col": 1,
                "headers": ["Source", "Target", "Formula"],
                "segments": [
                    {
                        "id": 0,
                        "row_index": 0,
                        "source": "Source",
                        "target": "原译",
                        "kind": "desc",
                        "term_hits": [],
                        "protected_texts": [],
                    },
                    {
                        "id": 1,
                        "row_index": 1,
                        "source": "Reminder",
                        "target": "保留原译",
                        "kind": "desc",
                        "term_hits": [],
                        "protected_texts": [],
                    },
                ],
            },
        )
        write_json(
            errors_path,
            [
                {
                    "id": 0,
                    "errors": [
                        check_issue(edit=replacement("原译", "建议译文"))
                    ],
                    "corrected": "建议译文",
                },
                {"id": 1, "errors": [check_issue()], "corrected": None},
            ],
        )
        before = self.workbook_signature(source)

        call_quietly(
            lqe_io.cmd_export,
            SimpleNamespace(state=str(state_path), errors=str(errors_path)),
        )

        output = next(self.root.glob("*_corrected.xlsx"))
        after = self.workbook_signature(output)
        self.assertEqual(after["sheetnames"], before["sheetnames"])
        self.assertEqual(after["dimensions"], before["dimensions"])
        self.assertEqual(after["headers"], before["headers"])
        output_workbook = openpyxl.load_workbook(output, data_only=False)
        try:
            self.assertEqual(output_workbook["Content"]["B2"].value, "建议译文")
            self.assertEqual(output_workbook["Content"]["B3"].value, "保留原译")
        finally:
            output_workbook.close()

    def test_export_prints_only_four_plain_counts(self):
        source = self.root / "counts.xlsx"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["Source", "Target"])
        for index in range(4):
            sheet.append([f"Source {index}", f"原译{index}"])
        workbook.save(source)
        workbook.close()
        state_path = self.root / "counts-state.json"
        errors_path = self.root / "counts-errors.json"
        segments = [
            {
                "id": index,
                "row_index": index,
                "source": f"Source {index}",
                "target": f"原译{index}",
            }
            for index in range(4)
        ]
        segments[3]["protected"] = True
        write_json(
            state_path,
            {
                "input_path": str(source),
                "source_col": "Source",
                "target_col": 1,
                "headers": ["Source", "Target"],
                "segments": segments,
            },
        )
        write_json(
            errors_path,
            [
                {
                    "id": 0,
                    "errors": [
                        check_issue(edit=replacement("原译0", "建议译文"))
                    ],
                    "corrected": "建议译文",
                },
                {
                    "id": 1,
                    "errors": [
                        check_issue(needs_confirmation=True),
                        check_issue(edit=replacement("原译1", "安全修改")),
                    ],
                    "corrected": "安全修改",
                },
                {"id": 2, "errors": [], "corrected": None},
                {"id": 3, "errors": [], "corrected": None},
            ],
        )
        stdout = StringIO()

        with redirect_stdout(stdout):
            lqe_io.cmd_export(
                SimpleNamespace(state=str(state_path), errors=str(errors_path))
            )

        summary = stdout.getvalue()
        for expected in [
            "建议修改 1",
            "需要人工确认 1",
            "保持原译 1",
            "已保护 1",
        ]:
            self.assertIn(expected, summary)
        for legacy in ["AI修正", "人工批准", "待人工裁决", "未改", "TM保护"]:
            self.assertNotIn(legacy, summary)


class CorrectedOwnershipRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def run_script(self, script, *args):
        return subprocess.run(
            [sys.executable, str(script), *map(str, args)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def score_result(self, name, errors):
        state_path = self.root / f"{name}-state.json"
        errors_path = self.root / f"{name}-errors.json"
        write_json(
            state_path,
            {
                "wordcount": 100,
                "segments": [
                    {"id": 0, "source": "甲。", "target": "A."},
                    {"id": 1, "source": "乙。", "target": "B."},
                ],
            },
        )
        write_json(errors_path, errors)
        result = self.run_script(
            CALC_SCRIPT,
            "--state",
            state_path,
            "--errors",
            errors_path,
            "--threshold",
            "98",
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_scoring_is_identical_when_only_correction_metadata_changes(self):
        base = [
            {
                "id": 0,
                "errors": [
                    {
                        "category": "Mistranslation",
                        "severity": "Major",
                        "comment": "wrong",
                    }
                ],
                "corrected": None,
            },
            {
                "id": 1,
                "errors": [
                    {
                        "category": "Punctuation",
                        "severity": "Major",
                        "comment": "punctuation",
                    }
                ],
                "corrected": None,
            },
        ]
        with_metadata = json.loads(json.dumps(base))
        for entry in with_metadata:
            for issue in entry["errors"]:
                issue["needs_confirmation"] = False
                issue["edit"] = {
                    "from": "old",
                    "to": "new",
                    "evidence": None,
                }
        expected = {
            "score": 87.5,
            "status": "FAIL",
            "errors": 2,
            "wordcount": 100,
            "critical": 0,
            "repeated": 0,
            "npt": 125.0,
            "critical_gate": False,
            "blocked_segments": 0,
            "total_wordcount": 100,
        }

        self.assertEqual(self.score_result("base", base), expected)
        self.assertEqual(
            self.score_result("with-metadata", with_metadata), expected
        )

    def test_standard_job_has_only_two_xlsx_outputs(self):
        source = self.root / "source.xlsx"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["Source", "Target"])
        sheet.append(["Open the door.", "Open teh door."])
        workbook.save(source)
        workbook.close()
        job = self.root / "fixture"
        state = job / "state.json"
        checks = job / "checks.json"
        errors = job / "errors.json"

        read = self.run_script(
            IO_SCRIPT,
            "read",
            "--input",
            source,
            "--source-col",
            "Source",
            "--target-col",
            "Target",
            "--source-lang",
            "en",
            "--target-lang",
            "en",
            "--out",
            state,
        )
        self.assertEqual(read.returncode, 0, read.stderr)
        read_state = read_json(state)
        read_state.pop("artifact_contract_version", None)
        write_json(state, read_state)
        write_json(
            checks,
            [
                {
                    "id": 0,
                    "issues": [
                        check_issue(
                            category="Spelling",
                            comment="Fix the local typo.",
                            edit=replacement("teh", "the"),
                        )
                    ],
                }
            ],
        )
        built = self.run_script(
            IO_SCRIPT,
            "build-results",
            "--state",
            state,
            "--checks",
            checks,
            "--out",
            errors,
        )
        self.assertEqual(built.returncode, 0, built.stderr)
        report = self.run_script(
            IO_SCRIPT,
            "write",
            "--state",
            state,
            "--errors",
            errors,
            "--score",
            "99",
            "--threshold",
            "98",
        )
        self.assertEqual(report.returncode, 0, report.stderr)
        exported = self.run_script(
            IO_SCRIPT,
            "export",
            "--state",
            state,
            "--errors",
            errors,
        )
        self.assertEqual(exported.returncode, 0, exported.stderr)

        self.assertEqual(
            sorted(path.name for path in job.glob("*.xlsx")),
            ["fixture_corrected.xlsx", "fixture_lqe.xlsx"],
        )


class CorrectedOwnershipProjectTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def run_io(self, *args):
        return subprocess.run(
            [sys.executable, str(IO_SCRIPT), *map(str, args)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def make_candidate_protection_job(self):
        job = self.root / "candidate-job"
        state_path = job / "state.json"
        candidate_path = job / "tm_candidates.json"
        source_ref = {
            "relative_path": "source.sdlxliff",
            "file_index": 0,
            "tu_id": "tu-1",
            "tu_index": 0,
            "sdl_segment_id": "1",
            "segment_index": 0,
        }
        write_json(
            state_path,
            {
                "input_format": "sdlxliff",
                "tm_candidates_path": str(candidate_path),
                "segments": [
                    {
                        "id": 0,
                        "source": "甲",
                        "target": "Alpha",
                        "corrected": None,
                        "source_ref": source_ref,
                        "metadata": {
                            "sdlxliff": {
                                "origin": "tm",
                                "match_percent": "100",
                                "text_match": "SourceAndTarget",
                            }
                        },
                    }
                ],
            },
        )
        payload = {
            "candidate_ids": [0],
            "segments": [
                {
                    "id": 0,
                    "source_ref": source_ref,
                    "evidence": {
                        "origin": "tm",
                        "match_percent": "100",
                        "text_match": "SourceAndTarget",
                        "origin_is_tm": True,
                        "percent_is_100": True,
                        "text_match_is_source_and_target": True,
                    },
                }
            ],
        }
        write_json(candidate_path, payload)
        return job, state_path, candidate_path, payload

    def test_clean_terms_preserves_explicit_flags_and_rejects_missing_flags(self):
        terms = lqe_io._clean_terms(
            [
                {
                    "source": "已确认",
                    "target": "Confirmed",
                    "status": "Approved",
                    "category": "Name",
                    "definition": "A confirmed name",
                    "confirmed": True,
                    "protected": True,
                }
            ]
        )

        self.assertEqual(
            terms,
            [
                {
                    "source": "已确认",
                    "target": "Confirmed",
                    "status": "Approved",
                    "category": "Name",
                    "definition": "A confirmed name",
                    "confirmed": True,
                    "protected": True,
                },
            ],
        )
        with self.assertRaisesRegex(ValueError, "confirmation mapping"):
            lqe_io._clean_terms(
                [
                    {
                        "source": "未标记",
                        "target": "Unmarked",
                        "status": "Locked",
                    }
                ]
            )

    def test_mastertb_output_writes_explicit_term_flags(self):
        source = self.root / "master.xlsx"
        output = self.root / "terms.json"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["术语 ZHCN", "TH", "Status"])
        sheet.append(["小花仙", "ดอกบ้องแบ๊ว", "Approved"])
        workbook.save(source)
        workbook.close()

        result = subprocess.run(
            [
                sys.executable,
                str(MASTERTB_TO_TERMS_SCRIPT),
                "--input",
                str(source),
                "--out",
                str(output),
                "--target-col",
                "TH",
                "--approved-statuses",
                "",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            read_json(output),
            [
                {
                    "source": "小花仙",
                    "target": "ดอกบ้องแบ๊ว",
                    "status": "Approved",
                    "confirmed": False,
                    "protected": False,
                }
            ],
        )

    def test_mastertb_multisense_and_backfill_keep_explicit_flags(self):
        source = self.root / "master-flags.xlsx"
        output = self.root / "terms-flags.json"
        backfill = self.root / "backfill.json"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["术语 ZHCN", "TH", "Status"])
        sheet.append(["多义", "A", "Approved"])
        sheet.append(["多义", "B", "Draft"])
        sheet.append(["空缺", "", "Approved"])
        workbook.save(source)
        workbook.close()
        write_json(
            backfill,
            [
                {
                    "source": "空缺",
                    "target": "Legacy",
                    "confirmed": True,
                    "protected": True,
                }
            ],
        )

        result = subprocess.run(
            [
                sys.executable,
                str(MASTERTB_TO_TERMS_SCRIPT),
                "--input",
                str(source),
                "--out",
                str(output),
                "--target-col",
                "TH",
                "--approved-statuses",
                "",
                "--backfill",
                str(backfill),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        by_source = {item["source"]: item for item in read_json(output)}
        self.assertEqual(
            by_source["多义"]["senses"],
            [
                {
                    "target": "A",
                    "confirmed": False,
                    "protected": False,
                    "status": "Approved",
                },
                {
                    "target": "B",
                    "confirmed": False,
                    "protected": False,
                    "status": "Draft",
                },
            ],
        )
        self.assertEqual(
            by_source["空缺"],
            {
                "source": "空缺",
                "target": "Legacy",
                "confirmed": True,
                "protected": True,
            },
        )

    def test_read_state_uses_confirmed_rules_path(self):
        project = self.root / "game" / "zh-en"
        write_json(
            project / "profile.json",
            {
                "name": "fixture/zh-en",
                "language_pair": "zh-en",
                "source_lang": "zh",
                "target_lang": "en",
                "confirmed_rules": "confirmed_rules.md",
            },
        )
        (project / "confirmed_rules.md").write_text("# Confirmed\n", encoding="utf-8")
        common = self.root / "game" / "common" / "confirmed_rules_common.md"
        common.parent.mkdir(parents=True)
        common.write_text("# Common\n", encoding="utf-8")
        source = self.root / "source.csv"
        source.write_text("Source,Target\n源文,Target\n", encoding="utf-8")
        state_path = self.root / "job" / "state.json"

        result = subprocess.run(
            [
                sys.executable,
                str(IO_SCRIPT),
                "read",
                "--input",
                str(source),
                "--project",
                str(project / "profile.json"),
                "--source-col",
                "Source",
                "--target-col",
                "Target",
                "--out",
                str(state_path),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        state = read_json(state_path)
        self.assertTrue(state["confirmed_rules_path"].endswith("confirmed_rules.md"))
        self.assertNotIn("adjudications_path", state)
        merged = Path(state["confirmed_rules_path"]).read_text(encoding="utf-8")
        self.assertIn("# Common", merged)
        self.assertIn("# Confirmed", merged)

    def test_profile_protected_term_statuses_mark_only_configured_terms(self):
        project = self.root / "project-statuses"
        write_json(
            project / "profile.json",
            {
                "name": "fixture/zh-en",
                "language_pair": "zh-en",
                "source_lang": "zh",
                "target_lang": "en",
                "terminology": "terms.json",
                "term_status_map": {
                    "Approved": "unconfirmed",
                    "Draft": "unconfirmed",
                },
                "protected_term_statuses": ["Approved"],
            },
        )
        write_json(
            project / "terms.json",
            [
                {"source": "A", "target": "Alpha", "status": "Approved"},
                {"source": "B", "target": "Beta", "status": "Draft"},
            ],
        )
        source = self.root / "statuses.csv"
        source.write_text("Source,Target\nA,Alpha\n", encoding="utf-8")
        state_path = self.root / "statuses-job" / "state.json"

        result = subprocess.run(
            [
                sys.executable,
                str(IO_SCRIPT),
                "read",
                "--input",
                str(source),
                "--project",
                str(project / "profile.json"),
                "--source-col",
                "Source",
                "--target-col",
                "Target",
                "--out",
                str(state_path),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        terms = read_json(Path(read_json(state_path)["terms_path"]))
        self.assertEqual([term["protected"] for term in terms], [True, False])

    def test_protect_segments_cli_writes_protected_state_and_payload(self):
        state_path = self.root / "protect-job" / "state.json"
        write_json(
            state_path,
            {
                "segments": [
                    {"id": 0, "source": "A", "target": "Alpha", "corrected": "Edit"},
                    {"id": 1, "source": "B", "target": "Beta", "corrected": None},
                ]
            },
        )

        result = subprocess.run(
            [
                sys.executable,
                str(IO_SCRIPT),
                "protect-segments",
                "--state",
                str(state_path),
                "--protected-ids",
                "0",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        state = read_json(state_path)
        self.assertTrue(state["segments"][0]["protected"])
        self.assertIsNone(state["segments"][0]["corrected"])
        self.assertNotIn("locked", state["segments"][0])
        payload = read_json(state_path.parent / "tm_protected.json")
        self.assertEqual(payload["protected_ids"], [0])
        self.assertNotIn("locked_ids", payload)

    def test_protect_segments_rejects_candidate_file_from_another_job(self):
        job, state_path, _, payload = self.make_candidate_protection_job()
        foreign_candidates = self.root / "other-job" / "tm_candidates.json"
        write_json(foreign_candidates, payload)
        original_state = state_path.read_bytes()

        result = self.run_io(
            "protect-segments",
            "--state",
            state_path,
            "--protected-file",
            foreign_candidates,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tm_candidates_path", result.stderr)
        self.assertEqual(state_path.read_bytes(), original_state)
        self.assertFalse((job / "tm_protected.json").exists())

    def test_protect_segments_rejects_tampered_candidate_ownership(self):
        job, state_path, candidate_path, payload = self.make_candidate_protection_job()
        original_state = state_path.read_bytes()
        cases = {
            "missing-evidence": {
                **payload,
                "segments": [{**payload["segments"][0], "evidence": {}}],
            },
            "wrong-source-ref": {
                **payload,
                "segments": [
                    {
                        **payload["segments"][0],
                        "source_ref": {
                            **payload["segments"][0]["source_ref"],
                            "tu_id": "other-tu",
                        },
                    }
                ],
            },
        }
        for name, candidate_payload in cases.items():
            with self.subTest(name=name):
                write_json(candidate_path, candidate_payload)
                result = self.run_io(
                    "protect-segments",
                    "--state",
                    state_path,
                    "--protected-file",
                    candidate_path,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("candidate", result.stderr.lower())
                self.assertEqual(state_path.read_bytes(), original_state)
                self.assertFalse((job / "tm_protected.json").exists())

    def test_protect_segments_rejects_output_aliasing_state(self):
        job, state_path, _, _ = self.make_candidate_protection_job()
        original_state = state_path.read_bytes()

        result = self.run_io(
            "protect-segments",
            "--state",
            state_path,
            "--protected-ids",
            "0",
            "--out",
            state_path,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicts with input state", result.stderr)
        self.assertEqual(state_path.read_bytes(), original_state)
        self.assertFalse((job / "tm_protected.json").exists())

    def test_protect_segments_output_failure_does_not_mutate_state(self):
        _, state_path, _, _ = self.make_candidate_protection_job()
        original_state = state_path.read_bytes()
        missing_output = self.root / "missing-parent" / "decision.json"

        result = self.run_io(
            "protect-segments",
            "--state",
            state_path,
            "--protected-ids",
            "0",
            "--out",
            missing_output,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state_path.read_bytes(), original_state)
        self.assertFalse(missing_output.exists())

    def test_protect_segments_rejects_output_aliasing_candidate_input(self):
        _, state_path, candidate_path, _ = self.make_candidate_protection_job()
        original_state = state_path.read_bytes()
        original_candidates = candidate_path.read_bytes()

        result = self.run_io(
            "protect-segments",
            "--state",
            state_path,
            "--protected-file",
            candidate_path,
            "--out",
            candidate_path,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicts with input tm_candidates_path", result.stderr)
        self.assertEqual(state_path.read_bytes(), original_state)
        self.assertEqual(candidate_path.read_bytes(), original_candidates)

    def test_protect_segments_state_publish_failure_rolls_back_decision(self):
        job, state_path, _, _ = self.make_candidate_protection_job()
        original_state = state_path.read_bytes()
        original_replace = lqe_io.os.replace

        def fail_state_publish(source, destination):
            if Path(destination).resolve() == state_path.resolve():
                raise OSError("simulated state publication failure")
            return original_replace(source, destination)

        args = SimpleNamespace(
            state=str(state_path),
            protected_ids="0",
            protected_file=None,
            reason="TM_100_MATCH",
            out=None,
        )
        with mock.patch.object(lqe_io.os, "replace", fail_state_publish):
            with self.assertRaisesRegex(SystemExit, "state publication failure"):
                call_quietly(lqe_io.cmd_protect_segments, args)

        self.assertEqual(state_path.read_bytes(), original_state)
        self.assertFalse((job / "tm_protected.json").exists())

    def test_protected_segment_is_not_scored(self):
        state_path = self.root / "state.json"
        errors_path = self.root / "errors.json"
        write_json(
            state_path,
            {
                "wordcount": 10,
                "segments": [
                    {
                        "id": 0,
                        "source": "源文",
                        "target": "Target",
                        "protected": True,
                    }
                ],
            },
        )
        write_json(
            errors_path,
            [
                {
                    "id": 0,
                    "errors": [
                        {
                            "category": "Mistranslation",
                            "severity": "Major",
                            "comment": "protected",
                        }
                    ],
                    "corrected": None,
                }
            ],
        )

        result = subprocess.run(
            [
                sys.executable,
                str(CALC_SCRIPT),
                "--state",
                str(state_path),
                "--errors",
                str(errors_path),
                "--json",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["errors"], 0)

    def test_tm_candidate_file_cannot_bypass_protection_decision(self):
        state_path = self.root / "candidate-state.json"
        errors_path = self.root / "candidate-errors.json"
        candidates_path = self.root / "tm_candidates.json"
        write_json(
            state_path,
            {
                "wordcount": 10,
                "segments": [
                    {"id": 0, "source": "Source", "target": "Target"}
                ],
            },
        )
        write_json(
            errors_path,
            [
                {
                    "id": 0,
                    "errors": [
                        {
                            "category": "Grammar",
                            "severity": "Major",
                            "comment": "must still count",
                        }
                    ],
                    "corrected": None,
                }
            ],
        )
        write_json(candidates_path, {"candidate_ids": [0]})

        result = subprocess.run(
            [
                sys.executable,
                str(CALC_SCRIPT),
                "--state",
                str(state_path),
                "--errors",
                str(errors_path),
                "--protected-file",
                str(candidates_path),
                "--json",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("candidate_ids", result.stderr)


if __name__ == "__main__":
    unittest.main()
