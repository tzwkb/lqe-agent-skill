import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CHUNK_SCRIPT = SCRIPTS / "lqe_chunk.py"
sys.path.insert(0, str(SCRIPTS))

from lqe_chunk import resolve_split_size
from lqe_engine import build_check_scope


class ContextRichSplitDefaultTests(unittest.TestCase):
    def test_default_depends_only_on_formal_context_mode(self):
        self.assertEqual(
            resolve_split_size({"context_pipeline": {"mode": "enforce"}}, None),
            5,
        )
        for state in (
            {},
            {"context_pipeline": {"mode": "off"}},
            {"context_pipeline": {"mode": "shadow"}},
        ):
            with self.subTest(state=state):
                self.assertEqual(resolve_split_size(state, None), 100)

    def test_explicit_size_always_wins(self):
        for mode in ("off", "shadow", "enforce"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    resolve_split_size(
                        {"context_pipeline": {"mode": mode}},
                        17,
                    ),
                    17,
                )

    def _split(self, mode: str, explicit_size: int | None = None) -> dict:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        job = Path(tempdir.name)
        segments = [
            {
                "id": index,
                "source": f"源文{index}",
                "target": f"Target {index}",
                "protected_texts": [],
            }
            for index in range(6)
        ]
        state = {
            "job_runtime_contract_version": 2,
            "iteration": 0,
            "source_lang": "zh",
            "target_lang": "en",
            "check_scope": build_check_scope(True, "test"),
            "context_pipeline": {"mode": mode},
            "segments": segments,
        }
        state_path = job / "state.json"
        precheck_path = job / "errors_precheck.json"
        state_path.write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
        precheck_path.write_text(
            json.dumps(
                [{"id": segment["id"], "issues": []} for segment in segments]
            ),
            encoding="utf-8",
        )
        command = [
            sys.executable,
            str(CHUNK_SCRIPT),
            "split",
            "--state",
            str(state_path),
            "--errors",
            str(precheck_path),
            "--outdir",
            str(job / "chunks"),
        ]
        if explicit_size is not None:
            command.extend(["--size", str(explicit_size)])
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(
            (job / "chunks" / "split_manifest.json").read_text(
                encoding="utf-8"
            )
        )

    def test_enforce_cli_uses_five_segment_default(self):
        manifest = self._split("enforce")
        self.assertEqual(manifest["revision"]["size"], 5)
        self.assertEqual(manifest["chunks"], 2)

    def test_off_and_shadow_cli_keep_legacy_default(self):
        for mode in ("off", "shadow"):
            with self.subTest(mode=mode):
                manifest = self._split(mode)
                self.assertEqual(manifest["revision"]["size"], 100)
                self.assertEqual(manifest["chunks"], 1)

    def test_explicit_cli_size_is_fingerprinted(self):
        manifest = self._split("enforce", explicit_size=3)
        self.assertEqual(manifest["revision"]["size"], 3)
        self.assertEqual(manifest["chunks"], 2)


if __name__ == "__main__":
    unittest.main()
