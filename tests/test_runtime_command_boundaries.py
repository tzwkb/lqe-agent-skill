import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


class HistoricalRuntimeCommandBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.job = self.root / "historical-job"
        self.job.mkdir()
        self.state_path = self.job / "state.json"
        self.state_path.write_text(
            json.dumps(
                {
                    "segments": [],
                    "wordcount": 0,
                    "source_lang": "en",
                    "target_lang": "en",
                }
            ),
            encoding="utf-8",
        )
        self.errors_path = self.job / "errors.json"
        self.errors_path.write_text("[]", encoding="utf-8")
        self.precheck_path = self.job / "errors_precheck.json"
        self.precheck_path.write_text("[]", encoding="utf-8")

    def tearDown(self):
        self.tempdir.cleanup()

    def run_script(self, script, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), *map(str, args)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def assert_historical_block(self, result, command):
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertIn(command, output)
        self.assertIn("historical runtime v1 job is read-only", output)

    def test_derivation_commands_fail_at_the_runtime_boundary(self):
        unused = self.root / "unused.json"
        cases = [
            (
                "pre-check",
                "lqe_checks.py",
                (),
                None,
            ),
            (
                "calc",
                "lqe_calc.py",
                ("--state", self.state_path, "--errors", self.errors_path),
                None,
            ),
            (
                "split",
                "lqe_chunk.py",
                (
                    "split",
                    "--state",
                    self.state_path,
                    "--errors",
                    self.precheck_path,
                    "--outdir",
                    self.job / "chunks",
                ),
                None,
            ),
            (
                "merge",
                "lqe_chunk.py",
                (
                    "merge",
                    "--state",
                    self.state_path,
                    "--errors",
                    self.precheck_path,
                    "--outdir",
                    self.job / "chunks",
                    "--out",
                    self.errors_path,
                ),
                None,
            ),
            (
                "merge-checks",
                "lqe_chunk.py",
                ("merge-checks", "--job", self.job),
                None,
            ),
            (
                "reconcile",
                "lqe_chunk.py",
                ("reconcile", "--job", self.job),
                None,
            ),
            (
                "publish-module",
                "lqe_chunk.py",
                (
                    "publish-module",
                    "--job",
                    self.job,
                    "--chunk",
                    "0",
                    "--module",
                    "accuracy",
                    "--input",
                    unused,
                    "--split-fingerprint",
                    "stale",
                    "--chunk-payload-digest",
                    "stale",
                ),
                None,
            ),
            (
                "split-half",
                "lqe_chunk.py",
                ("split-half", "--job", self.job, "--chunk", "0"),
                None,
            ),
            (
                "review-prepare",
                "lqe_review.py",
                ("prepare", "--job", self.job),
                None,
            ),
            (
                "review-publish",
                "lqe_review.py",
                (
                    "publish",
                    "--job",
                    self.job,
                    "--chunk",
                    "0",
                    "--module",
                    "accuracy",
                    "--input",
                    unused,
                ),
                None,
            ),
            (
                "review-auto-publish",
                "lqe_review.py",
                ("auto-publish", "--job", self.job),
                None,
            ),
            (
                "suggestions-prepare",
                "lqe_suggestions.py",
                ("prepare", "--job", self.job),
                None,
            ),
            (
                "suggestions-publish-candidates",
                "lqe_suggestions.py",
                (
                    "publish-candidates",
                    "--job",
                    self.job,
                    "--input",
                    unused,
                ),
                None,
            ),
            (
                "suggestion-review-prepare",
                "lqe_suggestion_review.py",
                ("prepare", "--job", self.job),
                None,
            ),
            (
                "suggestion-review-publish-review",
                "lqe_suggestion_review.py",
                ("publish-review", "--job", self.job, "--input", unused),
                None,
            ),
            (
                "suggestion-review-publish-final",
                "lqe_suggestion_review.py",
                ("publish-final", "--job", self.job),
                None,
            ),
        ]
        original = self.state_path.read_bytes()
        for command, script, argv, _ in cases:
            with self.subTest(command=command):
                if script == "lqe_checks.py":
                    code = (
                        "from pathlib import Path; "
                        "from lqe_checks import run_pre_check; "
                        f"run_pre_check(Path({str(self.state_path)!r}))"
                    )
                    result = subprocess.run(
                        [sys.executable, "-c", code],
                        cwd=ROOT,
                        capture_output=True,
                        text=True,
                        check=False,
                        env={
                            **__import__("os").environ,
                            "PYTHONPATH": str(SCRIPTS),
                        },
                    )
                else:
                    result = self.run_script(script, *argv)
                self.assert_historical_block(result, command)
                self.assertEqual(self.state_path.read_bytes(), original)

    def test_validate_existing_and_stateless_legacy_tools_remain_available(self):
        validate = self.run_script(
            "lqe_suggestions.py", "validate", "--job", self.job
        )
        combined = validate.stdout + validate.stderr
        self.assertNotIn("historical runtime v1 job is read-only", combined)

        part = self.root / "part.json"
        part.write_text("[]", encoding="utf-8")
        joined = self.root / "joined.json"
        result = self.run_script(
            "lqe_chunk.py",
            "join-parts",
            "--parts",
            part,
            "--out",
            joined,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(joined.read_text(encoding="utf-8")), [])

        checkpoint = self.root / "checkpoint.jsonl"
        result = self.run_script(
            "lqe_chunk.py",
            "ckpt-append",
            "--file",
            checkpoint,
            "--entry",
            '{"id":0,"issues":[]}',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        finalized = self.root / "finalized.json"
        result = self.run_script(
            "lqe_chunk.py",
            "ckpt-finalize",
            "--jsonl",
            checkpoint,
            "--out",
            finalized,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(finalized.read_text(encoding="utf-8")),
            [{"id": 0, "issues": []}],
        )


if __name__ == "__main__":
    unittest.main()
