import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import openpyxl


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
IO_SCRIPT = SCRIPTS / "lqe_io.py"
CHUNK_SCRIPT = SCRIPTS / "lqe_chunk.py"
FINALIZE_SCRIPT = SCRIPTS / "finalize_job.sh"
sys.path.insert(0, str(SCRIPTS))

from lqe_corrections import build_segment_result
from lqe_engine import build_check_scope


def write_json(path: Path, value: object) -> None:
    if (
        path.name == "state.json"
        and isinstance(value, dict)
        and isinstance(value.get("segments"), list)
        and "job_runtime_contract_version" not in value
    ):
        value["job_runtime_contract_version"] = 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def local_issue(frm: str, to: str) -> dict:
    return {
        "category": "Grammar",
        "severity": "Major",
        "comment": "Apply the verified local edit.",
        "needs_confirmation": False,
        "edit": {"from": frm, "to": to, "evidence": None},
    }


class IterationStateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def run_script(self, script: Path, *args: object):
        return subprocess.run(
            [sys.executable, str(script), *map(str, args)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def base_state(self, source: Path, target: str = "Original") -> dict:
        return {
            "input_format": "tabular",
            "input_path": str(source),
            "headers": ["Source", "Target"],
            "rows_raw": [["Source", target]],
            "source_col": "Source",
            "target_col": "Target",
            "source_lang": "en",
            "target_lang": "en",
            "wordcount": 100,
            "iteration": 0,
            "check_scope": build_check_scope(True, "test"),
            "segments": [
                {
                    "id": 0,
                    "row_index": 0,
                    "source": "Source",
                    "target": target,
                    "kind": "desc",
                    "term_hits": [],
                    "protected_texts": [],
                }
            ],
        }

    def test_correction_builder_edits_current_target(self):
        result = build_segment_result(
            {
                "id": 0,
                "source": "Source",
                "target": "Original",
                "current_target": "First pass",
            },
            [local_issue("First", "Second")],
        )

        self.assertEqual(result["corrected"], "Second pass")

    def test_precheck_and_split_use_current_target_and_detect_stale_chunks(self):
        source = self.root / "source.csv"
        source.write_text("Source,Target\nSource,Clean target.\n", encoding="utf-8")
        job = self.root / "job"
        state_path = job / "state.json"
        precheck_path = job / "errors_precheck.json"
        chunks = job / "chunks"
        state = self.base_state(source, "Clean target.")
        state["segments"][0]["current_target"] = "Bad — target."
        write_json(state_path, state)

        checked = self.run_script(
            IO_SCRIPT,
            "pre-check",
            "--state",
            state_path,
            "--out",
            precheck_path,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)
        issues = json.loads(precheck_path.read_text(encoding="utf-8"))[0]["issues"]
        self.assertTrue(any("Em dash" in item["comment"] for item in issues))

        split = self.run_script(
            CHUNK_SCRIPT,
            "split",
            "--state",
            state_path,
            "--errors",
            precheck_path,
            "--outdir",
            chunks,
        )
        self.assertEqual(split.returncode, 0, split.stderr)
        chunk = json.loads((chunks / "chunk_00.json").read_text(encoding="utf-8"))
        self.assertEqual(chunk["segments"][0]["target"], "Bad — target.")
        self.assertIn("state_fingerprint", chunk)

        for module in ("precheck_review", "accuracy", "grammar", "naturalness"):
            write_json(chunks / f"chunk_00.{module}.json", [{"id": 0, "issues": []}])
        state["segments"][0]["current_target"] = "Later target."
        write_json(state_path, state)

        stale = self.run_script(
            CHUNK_SCRIPT,
            "validate-checks",
            "--job",
            job,
        )
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("stale", stale.stderr.lower())

        rechecked = self.run_script(
            IO_SCRIPT,
            "pre-check",
            "--state",
            state_path,
            "--out",
            precheck_path,
        )
        self.assertEqual(rechecked.returncode, 0, rechecked.stderr)
        resplit = self.run_script(
            CHUNK_SCRIPT,
            "split",
            "--state",
            state_path,
            "--errors",
            precheck_path,
            "--outdir",
            chunks,
        )
        self.assertEqual(resplit.returncode, 0, resplit.stderr)
        self.assertFalse((chunks / "chunk_00.grammar.json").exists())
        self.assertTrue(
            any((chunks.parent / "chunks_archive").glob("*/chunk_00.grammar.json"))
        )
        missing = self.run_script(
            CHUNK_SCRIPT,
            "validate-checks",
            "--job",
            job,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("MISSING", missing.stderr)

    def test_apply_fixes_rejects_forged_corrected_without_mutating_state(self):
        source = self.root / "source.csv"
        source.write_text("Source,Target\nSource,Original\n", encoding="utf-8")
        state_path = self.root / "forged" / "state.json"
        errors_path = state_path.parent / "errors.json"
        state = self.base_state(source)
        write_json(state_path, state)
        write_json(
            errors_path,
            [{"id": 0, "errors": [], "corrected": "Forged translation"}],
        )
        before = state_path.read_bytes()

        result = self.run_script(
            IO_SCRIPT,
            "apply-fixes",
            "--state",
            state_path,
            "--errors",
            errors_path,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("corrected mismatch", result.stderr)
        self.assertEqual(state_path.read_bytes(), before)

    def test_apply_fixes_advances_verified_current_target_and_marks_recheck(self):
        source = self.root / "source.csv"
        source.write_text("Source,Target\nSource,Original\n", encoding="utf-8")
        state_path = self.root / "iterate" / "state.json"
        errors_path = state_path.parent / "errors.json"
        state = self.base_state(source)
        state["segments"][0]["current_target"] = "First pass"
        write_json(state_path, state)
        write_json(
            errors_path,
            [
                {
                    "id": 0,
                    "errors": [local_issue("First", "Second")],
                    "corrected": "Second pass",
                }
            ],
        )

        result = self.run_script(
            IO_SCRIPT,
            "apply-fixes",
            "--state",
            state_path,
            "--errors",
            errors_path,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        updated = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(updated["segments"][0]["current_target"], "Second pass")
        self.assertEqual(updated["iteration"], 1)
        self.assertTrue(updated["pending_recheck"])
        self.assertEqual(
            updated["error_history"][-1]["review_targets"],
            {"0": "First pass"},
        )

        checks_path = state_path.parent / "checks.json"
        next_errors = state_path.parent / "errors-next.json"
        write_json(
            checks_path,
            [{"id": 0, "issues": [local_issue("Second", "Third")]}],
        )
        built = self.run_script(
            IO_SCRIPT,
            "build-results",
            "--state",
            state_path,
            "--checks",
            checks_path,
            "--out",
            next_errors,
        )
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertEqual(
            json.loads(next_errors.read_text(encoding="utf-8"))[0]["corrected"],
            "Third pass",
        )

    def test_export_keeps_accumulated_current_target_when_overlay_has_no_edit(self):
        source = self.root / "source.csv"
        source.write_text("Source,Target\nSource,Original\n", encoding="utf-8")
        state_path = self.root / "export" / "state.json"
        errors_path = state_path.parent / "errors.json"
        state = self.base_state(source)
        state["segments"][0]["current_target"] = "Applied pass"
        write_json(state_path, state)
        write_json(errors_path, [{"id": 0, "errors": [], "corrected": None}])

        result = self.run_script(
            IO_SCRIPT,
            "export",
            "--state",
            state_path,
            "--errors",
            errors_path,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        output = state_path.parent / "export_corrected.csv"
        self.assertIn("Applied pass", output.read_text(encoding="utf-8-sig"))

    def test_final_report_keeps_accumulated_current_target_after_recheck(self):
        source = self.root / "source.csv"
        source.write_text("Source,Target\nSource,Original\n", encoding="utf-8")
        state_path = self.root / "report" / "state.json"
        errors_path = state_path.parent / "errors.json"
        state = self.base_state(source)
        state["segments"][0]["current_target"] = "Applied pass"
        state["pending_recheck"] = True
        write_json(state_path, state)
        write_json(errors_path, [{"id": 0, "errors": [], "corrected": None}])

        result = self.run_script(
            IO_SCRIPT,
            "write",
            "--state",
            state_path,
            "--errors",
            errors_path,
            "--score",
            "100",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        updated = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertFalse(updated["pending_recheck"])
        report = state_path.parent / "report_lqe.xlsx"
        workbook = openpyxl.load_workbook(report, data_only=True)
        try:
            sheet = workbook["LQE Results"]
            headers = [cell.value for cell in sheet[1]]
            original = headers.index("原译") + 1
            suggestion = headers.index("AI/建议译文") + 1
            self.assertEqual(
                sheet.cell(row=2, column=original).value,
                "Applied pass",
            )
            self.assertIsNone(sheet.cell(row=2, column=suggestion).value)
        finally:
            workbook.close()

    def test_deduplicated_members_receive_term_context_for_verification(self):
        source = self.root / "dedup-source.csv"
        source.write_text(
            "Source,Target\n世界,World\n世界,World\n", encoding="utf-8"
        )
        job = self.root / "dedup"
        state_path = job / "state.json"
        errors_path = job / "errors.json"
        state = {
            "input_format": "tabular",
            "input_path": str(source),
            "headers": ["Source", "Target"],
            "rows_raw": [["世界", "World"], ["世界", "World"]],
            "source_col": "Source",
            "target_col": "Target",
            "source_lang": "zh",
            "target_lang": "en",
            "wordcount": 2,
            "iteration": 0,
            "check_scope": build_check_scope(False, "test"),
            "terminology": [
                {
                    "source": "世界",
                    "target": "Realm",
                    "confirmed": True,
                    "protected": False,
                }
            ],
            "segments": [
                {"id": 0, "row_index": 0, "source": "世界", "target": "World"},
                {"id": 1, "row_index": 1, "source": "世界", "target": "World"},
            ],
        }
        write_json(state_path, state)
        precheck_path = job / "errors_precheck.json"
        write_json(
            precheck_path,
            [{"id": 0, "issues": []}, {"id": 1, "issues": []}],
        )
        split = self.run_script(
            CHUNK_SCRIPT,
            "split",
            "--state",
            state_path,
            "--errors",
            precheck_path,
            "--outdir",
            job / "chunks",
        )
        self.assertEqual(split.returncode, 0, split.stderr)
        term_issue = {
            "category": "Terminology",
            "severity": "Major",
            "comment": "Use the confirmed project term.",
            "term_source": "世界",
            "expected_targets": ["Realm"],
            "term_spans": {
                "source": [{"start": 0, "end": 2, "text": "世界"}],
                "target": [{"start": 0, "end": 5, "text": "World"}],
            },
            "needs_confirmation": False,
            "edit": {
                "from": "World",
                "to": "Realm",
                "evidence": {
                    "type": "confirmed_term",
                    "source": "世界",
                    "target": "Realm",
                },
            },
        }
        write_json(
            errors_path,
            [
                {"id": 0, "errors": [term_issue], "corrected": "Realm"},
                {"id": 1, "errors": [term_issue], "corrected": "Realm"},
            ],
        )

        result = self.run_script(
            IO_SCRIPT,
            "export",
            "--state",
            state_path,
            "--errors",
            errors_path,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        output = job / "dedup_corrected.csv"
        self.assertEqual(
            output.read_text(encoding="utf-8-sig").count("Realm"), 2
        )

    def test_finalize_rejects_unknown_mode_before_running_job(self):
        result = subprocess.run(
            ["bash", str(FINALIZE_SCRIPT), str(self.root / "missing"), "1", "typo"],
            cwd=ROOT,
            env={**os.environ, "PYTHON": sys.executable},
            text=True,
            capture_output=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid mode", result.stderr.lower())

    def test_finalize_marks_only_pass_and_iterate_exports_state_only(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        log = self.root / "calls.log"
        python_stub = bin_dir / "python3"
        python_stub.write_text(
            """#!/bin/sh
printf '%s\n' "$*" >> "$CALL_LOG"
case "$1" in
  -)
    script=$(cat)
    case "$script" in
      *enabled_modules*) printf 'precheck_review, accuracy, grammar, naturalness\n' ;;
      *) printf '98\n' ;;
    esac
    ;;
  *lqe_calc.py)
    printf '{"score":%s,"status":"%s","errors":1,"wordcount":1,"critical":0,"npt":0}\n' "$TEST_SCORE" "$TEST_STATUS"
    ;;
  -c)
    case "$2" in
      *score*) printf '%s\n' "$TEST_SCORE" ;;
      *status*) printf '%s\n' "$TEST_STATUS" ;;
      *applied_count*) printf '%s\n' "$TEST_APPLIED" ;;
    esac
    ;;
  *lqe_io.py)
    if [ "$2" = "apply-fixes" ]; then
      printf '{"applied_count":%s,"lifecycle":"%s"}\n' "$TEST_APPLIED" "$TEST_LIFECYCLE"
    fi
    ;;
esac
""",
            encoding="utf-8",
        )
        python_stub.chmod(0o755)

        cases = [
            ("pass", "PASS", "single", 0, True, False),
            ("single-fail", "FAIL", "single", 0, False, False),
            ("iterate-fail", "FAIL", "iterate", 1, False, True),
            ("iterate-no-edit", "FAIL", "iterate", 0, False, False),
        ]
        for name, status, mode, applied, finalized, pending in cases:
            with self.subTest(name=name):
                job = self.root / name
                (job / "chunks").mkdir(parents=True)
                write_json(job / "state.json", {})
                write_json(job / "chunks" / "chunk_00.json", {})
                if name == "single-fail":
                    (job / ".iteration_pending").touch()
                env = {
                    **os.environ,
                    "PYTHON": str(python_stub),
                    "CALL_LOG": str(log),
                    "TEST_SCORE": "100" if status == "PASS" else "90",
                    "TEST_STATUS": status,
                    "TEST_APPLIED": str(applied),
                    "TEST_LIFECYCLE": (
                        "pending_recheck" if applied else "review_required"
                    ),
                }
                result = subprocess.run(
                    ["bash", str(FINALIZE_SCRIPT), str(job), "1", mode],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((job / ".finalized").exists(), finalized)
                self.assertEqual((job / ".iteration_pending").exists(), pending)

        calls = log.read_text(encoding="utf-8").splitlines()
        iterate_exports = [
            call
            for call in calls
            if "lqe_io.py export" in call and "iterate-fail" in call
        ]
        self.assertEqual(len(iterate_exports), 1)
        self.assertNotIn("--errors", iterate_exports[0])
        no_edit_exports = [
            call
            for call in calls
            if "lqe_io.py export" in call and "iterate-no-edit" in call
        ]
        self.assertEqual(len(no_edit_exports), 1)
        self.assertIn("--errors", no_edit_exports[0])


if __name__ == "__main__":
    unittest.main()
