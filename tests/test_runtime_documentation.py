import re
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_TEXT = (ROOT / "SKILL.md").read_text(encoding="utf-8")
CLI_TEXT = (ROOT / "references" / "cli.md").read_text(encoding="utf-8")


class RuntimeDocumentationTests(unittest.TestCase):
    def test_skill_relative_links_exist(self):
        links = re.findall(r"\[[^]]+\]\(([^)]+)\)", SKILL_TEXT)
        self.assertTrue(links)
        for link in links:
            if "://" in link or link.startswith("#"):
                continue
            path = link.split("#", 1)[0]
            self.assertTrue((ROOT / path).exists(), f"broken SKILL.md link: {link}")

    def test_cli_reference_covers_every_public_subcommand(self):
        expected = {
            "lqe_io.py": {
                "read", "reread", "apply-fixes", "protect-segments",
                "build-results", "write", "verify-output", "pre-check", "lookup-terms",
                "export", "ingest-corpus",
            },
            "lqe_chunk.py": {
                "split", "merge", "merge-checks", "validate-checks",
                "reconcile", "publish-module", "split-half", "join-parts",
                "ckpt-append", "ckpt-finalize",
            },
            "lqe_review.py": {
                "prepare", "publish", "publish-directory", "reuse-drafts",
                "auto-publish",
            },
            "lqe_suggestions.py": {
                "prepare", "publish", "publish-candidates", "validate",
            },
            "lqe_suggestion_review.py": {
                "prepare", "publish-review", "publish-final",
            },
            "lqe_context_overrides.py": {"gaps", "scaffold"},
            "lqe_batch.py": {"plan", "merge"},
            "tm_index.py": {"build", "tm-match"},
            "mastertb_prep.py": {"prep", "chunks", "merge", "report", "view"},
            "lqe_profile_ingest.py": {"validate", "manifest", "validate-overrides"},
        }
        for script, commands in expected.items():
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / script), "--help"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            match = re.search(r"\{([^}]+)\}", result.stdout)
            self.assertIsNotNone(match, f"cannot read subcommands from {script} --help")
            actual = set(match.group(1).split(","))
            self.assertEqual(actual, commands, f"CLI inventory drifted for {script}")
            for command in commands:
                pattern = rf"{re.escape(script)}(?:\"|`)?\s+{re.escape(command)}(?:\s|`|:|$)"
                self.assertRegex(CLI_TEXT, pattern, f"missing command: {script} {command}")

    def test_standard_workflow_uses_real_cli_names_and_required_flags(self):
        self.assertIn('lqe_io.py" pre-check', SKILL_TEXT)
        self.assertNotIn('lqe_io.py" precheck', SKILL_TEXT)
        self.assertIn('lqe_chunk.py" split --state "$JOB/state.json" --errors', SKILL_TEXT)
        self.assertNotIn("--precheck", SKILL_TEXT)
        self.assertRegex(
            SKILL_TEXT,
            r'lqe_suggestions\.py" publish-candidates --job "\$JOB" --input ',
        )
        self.assertIn('$JOB/reference_suggestions.draft.json', SKILL_TEXT)
        self.assertIn('$JOB/suggestion_context/batches', SKILL_TEXT)
        self.assertIn('lqe_suggestion_review.py" prepare --job "$JOB"', SKILL_TEXT)
        self.assertNotIn(
            'lqe_suggestion_review.py" prepare --job "$JOB" --worker-batch-size',
            SKILL_TEXT,
        )
        self.assertIn('$JOB/suggestion_review.draft.json', SKILL_TEXT)
        self.assertIn('$JOB/suggestion_review_context', SKILL_TEXT)

    def test_profile_validation_and_corpus_paths_are_production_documented(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "lqe_profile_ingest.py"),
                "validate",
                "--help",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--schema", result.stdout)
        self.assertIn("<profile.json>", CLI_TEXT)
        self.assertIn("--input-format xliff", CLI_TEXT)
        self.assertIn("--dry-run", CLI_TEXT)
        self.assertNotIn("reserved stub", CLI_TEXT)


if __name__ == "__main__":
    unittest.main()
