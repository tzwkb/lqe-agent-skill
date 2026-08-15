import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
IO = ROOT / "scripts" / "lqe_io.py"


class TabularMarkerContractTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.source = self.root / "input.csv"
        with self.source.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows([
                ["Source", "Target"],
                ["对话类文本", "Dialogue text"],
                ["保存", "Save"],
            ])

    def tearDown(self):
        self.tempdir.cleanup()

    def run_read(self, state, *extra):
        return subprocess.run(
            [
                sys.executable,
                str(IO),
                "read",
                "--input",
                str(self.source),
                "--source-col",
                "Source",
                "--target-col",
                "Target",
                "--source-lang",
                "zh",
                "--target-lang",
                "en",
                "--no-terminology",
                *map(str, extra),
                "--out",
                str(state),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def write_profile(self, rules):
        directory = self.root / "profile"
        directory.mkdir(exist_ok=True)
        path = directory / "profile.json"
        path.write_text(json.dumps({
            "name": "test/zh-en",
            "language_pair": "zh-en",
            "source_lang": "zh",
            "target_lang": "en",
            "wordcount_basis": "source-chars",
            "tabular": {"text_type_marker_rules": rules},
        }, ensure_ascii=False), encoding="utf-8")
        return path

    def test_marker_like_source_is_an_ordinary_segment_by_default(self):
        state_path = self.root / "default" / "state.json"
        result = self.run_read(state_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual([item["source"] for item in state["segments"]], [
            "对话类文本",
            "保存",
        ])
        self.assertEqual(state["text_type_markers"], [])

    def test_profile_declared_marker_is_skipped_and_audited(self):
        profile = self.write_profile([{
            "id": "dialogue",
            "source_equals": "对话类文本",
            "text_type_context": "dialogue",
        }])
        state_path = self.root / "declared" / "state.json"
        result = self.run_read(state_path, "--project", profile)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(len(state["segments"]), 1)
        self.assertEqual(state["segments"][0]["source"], "保存")
        self.assertEqual(state["segments"][0]["text_type_context"], "dialogue")
        self.assertEqual(state["text_type_markers"][0]["source"], "对话类文本")

    def test_invalid_marker_rules_fail_before_state_publication(self):
        profile = self.write_profile([{
            "id": "bad",
            "source_equals": "对话类文本",
            "text_type_from": "unknown",
        }])
        state_path = self.root / "invalid" / "state.json"
        result = self.run_read(state_path, "--project", profile)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("text_type_from", result.stderr)
        self.assertFalse(state_path.exists())


if __name__ == "__main__":
    unittest.main()
