import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FINALIZE = ROOT / "scripts" / "finalize_job.sh"


class FinalizeSuggestionRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.job = self.root / "job"
        (self.job / "chunks").mkdir(parents=True)
        self._write_json(
            self.job / "state.json",
            {"job_runtime_contract_version": 2},
        )
        self._write_json(self.job / "chunks" / "chunk_00.json", {})
        self.log = self.root / "python-calls.log"
        self.python = self.root / "python3"
        self.python.write_text(
            """#!/bin/sh
printf '%s\n' "$*" >> "$CALL_LOG"
case "$1" in
  -)
    script=$(cat)
    case "$script" in
      *enabled_modules*) printf 'terminology, accuracy, grammar, naturalness\n' ;;
      *job_runtime_contract_version*) printf '2\n' ;;
      *reviewed_ids*) printf '%s\n' "$REVIEW_COUNT" ;;
      *) printf '0\n' ;;
    esac
    ;;
  *lqe_calc.py)
    printf '{"score":100,"status":"PASS","errors":0,"wordcount":1,"critical":0,"npt":0}\n'
    ;;
  -c)
    case "$2" in
      *score*) printf '100\n' ;;
      *status*) printf 'PASS\n' ;;
      *) printf '1\n' ;;
    esac
    ;;
esac
""",
            encoding="utf-8",
        )
        self.python.chmod(0o755)

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def _write_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def run_finalize(self, *, review_count):
        env = dict(os.environ)
        env.update(
            {
                "CALL_LOG": str(self.log),
                "PYTHON": str(self.python),
                "REVIEW_COUNT": str(review_count),
            }
        )
        return subprocess.run(
            ["bash", str(FINALIZE), str(self.job), "1", "single"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env=env,
        )

    def calls(self):
        return self.log.read_text(encoding="utf-8").splitlines()

    def test_deterministic_only_candidates_publish_without_review_artifact(self):
        self._write_json(self.job / "reference_suggestions.candidates.json", {})

        result = self.run_finalize(review_count=0)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("SUGGESTION-REVIEW-PENDING", result.stdout)
        self.assertTrue((self.job / ".finalized").is_file())
        calls = self.calls()
        self.assertTrue(
            any("lqe_suggestion_review.py prepare" in call for call in calls), calls
        )
        self.assertTrue(
            any("lqe_suggestion_review.py publish-final" in call for call in calls),
            calls,
        )
        self.assertTrue(
            any("lqe_suggestions.py validate" in call for call in calls), calls
        )

    def test_independent_candidates_without_review_remain_pending(self):
        self._write_json(self.job / "reference_suggestions.candidates.json", {})

        result = self.run_finalize(review_count=1)

        self.assertEqual(result.returncode, 10, result.stderr)
        self.assertIn("SUGGESTION-REVIEW-PENDING", result.stdout)
        self.assertFalse((self.job / ".finalized").exists())
        calls = self.calls()
        self.assertTrue(
            any("lqe_suggestion_review.py prepare" in call for call in calls), calls
        )
        self.assertFalse(
            any("lqe_suggestion_review.py publish-final" in call for call in calls),
            calls,
        )
        self.assertFalse(any("lqe_io.py write" in call for call in calls), calls)

    def test_deterministic_only_draft_continues_after_candidate_publish(self):
        self._write_json(self.job / "reference_suggestions.draft.json", {})

        result = self.run_finalize(review_count=0)

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        candidate_index = next(
            index
            for index, call in enumerate(calls)
            if "lqe_suggestions.py publish-candidates" in call
        )
        final_index = next(
            index
            for index, call in enumerate(calls)
            if "lqe_suggestion_review.py publish-final" in call
        )
        self.assertLess(candidate_index, final_index)
        self.assertTrue(
            any("lqe_suggestions.py validate" in call for call in calls), calls
        )
        self.assertTrue((self.job / ".finalized").is_file())


if __name__ == "__main__":
    unittest.main()
