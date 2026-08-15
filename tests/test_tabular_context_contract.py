import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
IO = ROOT / "scripts" / "lqe_io.py"


class TabularContextCLITests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def write_csv(self, name, rows):
        path = self.root / name
        with path.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(rows)
        return path

    def run_io(self, *args):
        return subprocess.run(
            [sys.executable, str(IO), *map(str, args)],
            capture_output=True,
            text=True,
            check=False,
        )

    def read_args(self, source, state):
        return (
            "read",
            "--input",
            source,
            "--source-col",
            "Source",
            "--target-col",
            "Target",
            "--key-col",
            "Key",
            "--context-col",
            "content_type=Text Class",
            "--context-note-col",
            "Context Note",
            "--target-source-digest-col",
            "Source Digest",
            "--source-lang",
            "en",
            "--target-lang",
            "en",
            "--no-terminology",
            "--out",
            state,
        )

    def test_read_publishes_bound_manifest_context_identity_and_guard(self):
        digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
        source = self.write_csv(
            "neutral.csv",
            [
                [
                    "Key",
                    "Source",
                    "Target",
                    "Text Class",
                    "Context Note",
                    "Source Digest",
                ],
                ["ui-1", "Save", "Save", "ui_button", "Primary action", digest("Save")],
                ["ui-2", "Open", "Open", "ui_label", "Menu label", "0" * 64],
            ],
        )
        state_path = self.root / "job" / "state.json"
        result = self.run_io(*self.read_args(source, state_path))
        self.assertEqual(result.returncode, 0, result.stderr)

        state = json.loads(state_path.read_text(encoding="utf-8"))
        manifest_path = Path(state["tabular_source_manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "lqe.tabular-source-manifest")
        self.assertEqual(manifest["adapter"], "csv@1")
        self.assertEqual(manifest["input_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(manifest["source_col"], "Source")
        self.assertEqual(manifest["target_col"], "Target")
        self.assertEqual(manifest["key_col"], "Key")
        self.assertEqual(manifest["segments"], 2)
        self.assertIn("context.core@1.content_type", manifest["context_columns"])

        ready, blocked = state["segments"]
        self.assertEqual((ready["segment_key"], ready["key_origin"]), ("ui-1", "input"))
        self.assertEqual(ready["input_status"], "ready")
        self.assertEqual(ready["context"]["core"]["content_type"], "ui_button")
        self.assertEqual(ready["context"]["core"]["context_note"], "Primary action")
        evidence = ready["context"]["provenance"]["context.core.content_type"]
        self.assertEqual(evidence["method"], "input_column")
        self.assertEqual(evidence["adapter"], "csv@1")
        self.assertEqual(evidence["source_file_digest"], manifest["input_sha256"])
        self.assertTrue(ready["segment_revision_digest"])
        self.assertEqual(
            set(ready["module_review_equivalence_keys"]),
            {
                "terminology",
                "precheck_review",
                "accuracy",
                "grammar",
                "naturalness",
                "suggestions",
            },
        )
        self.assertEqual(blocked["input_status"], "blocked")
        self.assertEqual(
            blocked["input_block_reasons"][0]["code"],
            "TARGET_SOURCE_VERSION_MISMATCH",
        )
        self.assertEqual(state["input_guard"]["blocked_ids"], [1])
        self.assertEqual(state["wordcount"], 2)
        self.assertEqual(state["review_wordcount"], 1)

    def test_missing_target_digest_column_warns_without_blocking(self):
        source = self.write_csv(
            "unverified.csv",
            [["Source", "Target"], ["Save", "Save"]],
        )
        state_path = self.root / "unverified" / "state.json"
        result = self.run_io(
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
            "--no-terminology",
            "--out",
            state_path,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["segments"][0]["input_status"], "ready")
        self.assertEqual(
            state["segments"][0]["input_warnings"],
            [{"code": "UNVERIFIED_TARGET_PROVENANCE"}],
        )
        self.assertEqual(state["input_guard"]["warning_ids"], [0])

    def test_duplicate_business_key_and_unknown_context_field_publish_nothing(self):
        digest = hashlib.sha256(b"Save").hexdigest()
        duplicate = self.write_csv(
            "duplicate.csv",
            [
                ["Key", "Source", "Target", "Source Digest"],
                ["same", "Save", "Save", digest],
                ["same", "Save", "Save", digest],
            ],
        )
        duplicate_job = self.root / "duplicate-job"
        result = self.run_io(
            "read",
            "--input",
            duplicate,
            "--source-col",
            "Source",
            "--target-col",
            "Target",
            "--key-col",
            "Key",
            "--target-source-digest-col",
            "Source Digest",
            "--source-lang",
            "en",
            "--target-lang",
            "en",
            "--no-terminology",
            "--out",
            duplicate_job / "state.json",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate business key", result.stderr)
        self.assertFalse((duplicate_job / "state.json").exists())
        self.assertFalse((duplicate_job / "tabular_source_manifest.json").exists())

        unknown_job = self.root / "unknown-job"
        result = self.run_io(
            "read",
            "--input",
            duplicate,
            "--source-col",
            "Source",
            "--target-col",
            "Target",
            "--context-col",
            "plot_role=Key",
            "--source-lang",
            "en",
            "--target-lang",
            "en",
            "--no-terminology",
            "--out",
            unknown_job / "state.json",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown context field", result.stderr)
        self.assertFalse((unknown_job / "state.json").exists())
        self.assertFalse((unknown_job / "tabular_source_manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
