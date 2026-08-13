import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
IO = ROOT / "scripts" / "lqe_io.py"


class LegacyJobCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.source = self.root / "source.csv"
        with self.source.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(
                [
                    ["Source", "Target"],
                    ["Save", "Save"],
                    ["Open", "Open"],
                ]
            )
        self.old_job = self.root / "old-job"
        result = self.run_io(
            "read",
            "--input",
            self.source,
            "--source-col",
            "Source",
            "--target-col",
            "Target",
            "--source-lang",
            "en",
            "--target-lang",
            "en",
            "--wordcount-basis",
            "target-words",
            "--review-mode",
            "full",
            "--no-terminology",
            "--out",
            self.old_job / "state.json",
        )
        if result.returncode != 0:
            self.fail(result.stderr or result.stdout)
        state_path = self.old_job / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.pop("job_runtime_contract_version")
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def run_io(self, *args):
        return subprocess.run(
            [sys.executable, str(IO), *map(str, args)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def assert_historical_block(self, result, command):
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertIn(command, output)
        self.assertIn("historical runtime v1 job is read-only", output)

    def test_historical_job_blocks_io_derivation_commands(self):
        state_path = self.old_job / "state.json"
        unused = self.root / "unused.json"
        cases = [
            (
                "pre-check",
                ["pre-check", "--state", state_path],
            ),
            (
                "protect-segments",
                [
                    "protect-segments",
                    "--state",
                    state_path,
                    "--protected-ids",
                    "0",
                ],
            ),
            (
                "export",
                ["export", "--state", state_path],
            ),
            (
                "build-results",
                [
                    "build-results",
                    "--state",
                    state_path,
                    "--checks",
                    unused,
                    "--out",
                    self.root / "results.json",
                ],
            ),
            (
                "apply-fixes",
                [
                    "apply-fixes",
                    "--state",
                    state_path,
                    "--errors",
                    unused,
                ],
            ),
            (
                "write",
                [
                    "write",
                    "--state",
                    state_path,
                    "--errors",
                    unused,
                    "--score",
                    "100",
                ],
            ),
        ]
        original = state_path.read_bytes()
        for command, argv in cases:
            with self.subTest(command=command):
                self.assert_historical_block(self.run_io(*argv), command)
                self.assertEqual(state_path.read_bytes(), original)

    def test_lookup_terms_remains_a_read_only_view(self):
        result = self.run_io(
            "lookup-terms", "--state", self.old_job / "state.json"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no terminology available", result.stderr)

    def test_reread_publishes_new_v2_job_without_touching_old_job(self):
        old_state = self.old_job / "state.json"
        original = old_state.read_bytes()
        new_job = self.root / "new-job"
        result = self.run_io(
            "reread",
            "--from-job",
            self.old_job,
            "--input",
            self.source,
            "--job",
            new_job,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        first_line = result.stdout.splitlines()[0]
        plan = json.loads(first_line)
        self.assertEqual(plan["schema"], "lqe.job-reread-migration-plan")
        self.assertEqual(plan["validation_status"], "pending")
        self.assertEqual(old_state.read_bytes(), original)

        new_state = json.loads(
            (new_job / "state.json").read_text(encoding="utf-8")
        )
        old_data = json.loads(original)
        self.assertEqual(new_state["job_runtime_contract_version"], 2)
        self.assertEqual(new_state["review_policy"], old_data["review_policy"])
        self.assertEqual(new_state["check_scope"], old_data["check_scope"])
        self.assertEqual(
            [
                (segment["row_index"], segment["source"], segment["target"])
                for segment in new_state["segments"]
            ],
            [
                (segment["row_index"], segment["source"], segment["target"])
                for segment in old_data["segments"]
            ],
        )
        self.assertTrue(
            new_state["tabular_source_manifest_path"].startswith(
                str(new_job.resolve())
            )
        )

    def test_reread_digest_mismatch_fails_closed_after_plan(self):
        different = self.root / "different.csv"
        with different.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(
                [["Source", "Target"], ["Changed", "Changed"]]
            )
        new_job = self.root / "digest-mismatch"
        result = self.run_io(
            "reread",
            "--from-job",
            self.old_job,
            "--input",
            different,
            "--job",
            new_job,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lqe.job-reread-migration-plan", result.stdout)
        self.assertIn("digest does not match", result.stderr)
        self.assertFalse(new_job.exists())

    def test_reread_missing_input_or_existing_target_fails_closed(self):
        missing_target = self.root / "missing-target"
        result = self.run_io(
            "reread",
            "--from-job",
            self.old_job,
            "--input",
            self.root / "missing.csv",
            "--job",
            missing_target,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lqe.job-reread-migration-plan", result.stdout)
        self.assertIn("original input is missing", result.stderr)
        self.assertFalse(missing_target.exists())

        occupied = self.root / "occupied"
        occupied.mkdir()
        marker = occupied / "existing.txt"
        marker.write_text("keep", encoding="utf-8")
        result = self.run_io(
            "reread",
            "--from-job",
            self.old_job,
            "--input",
            self.source,
            "--job",
            occupied,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("target job already exists", result.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_reread_rejects_unmappable_column_contract(self):
        state_path = self.old_job / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.pop("source_col")
        state_path.write_text(json.dumps(state), encoding="utf-8")
        new_job = self.root / "unmappable"
        result = self.run_io(
            "reread",
            "--from-job",
            self.old_job,
            "--input",
            self.source,
            "--job",
            new_job,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source_col is not reconstructable", result.stderr)
        self.assertFalse(new_job.exists())

    def test_reread_preserves_sdlxliff_digest_and_segment_coverage(self):
        source = (
            ROOT
            / "tests"
            / "fixtures"
            / "sdlxliff"
            / "multi_segment.sdlxliff"
        )
        old_job = self.root / "old-sdl"
        result = self.run_io(
            "read",
            "--input",
            source,
            "--input-format",
            "sdlxliff",
            "--wordcount-basis",
            "target-words",
            "--review-mode",
            "optimized",
            "--no-terminology",
            "--out",
            old_job / "state.json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        old_state_path = old_job / "state.json"
        old_state = json.loads(old_state_path.read_text(encoding="utf-8"))
        old_state.pop("job_runtime_contract_version")
        old_state_path.write_text(json.dumps(old_state), encoding="utf-8")

        new_job = self.root / "new-sdl"
        result = self.run_io(
            "reread",
            "--from-job",
            old_job,
            "--input",
            source,
            "--job",
            new_job,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        new_state = json.loads(
            (new_job / "state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(new_state["job_runtime_contract_version"], 2)
        self.assertEqual(
            [
                (segment["source_ref"], segment["source"], segment["target"])
                for segment in new_state["segments"]
            ],
            [
                (segment["source_ref"], segment["source"], segment["target"])
                for segment in old_state["segments"]
            ],
        )
        self.assertTrue(
            new_state["source_manifest_path"].startswith(str(new_job.resolve()))
        )


if __name__ == "__main__":
    unittest.main()
