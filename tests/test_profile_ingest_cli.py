import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lqe_profile_ingest.py"
PROFILE = ROOT / "projects" / "pop-epoch" / "en-tr" / "profile.json"


class ProfileIngestCliTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_validate_accepts_project_profile(self):
        result = self.run_cli(
            "validate", str(PROFILE), "--target-lang", "tr"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "valid")
        self.assertEqual(payload["kind"], "project_profile")
        self.assertEqual(payload["name"], "pop-epoch/en-tr")
        self.assertEqual(payload["schema"], "lqe.normalized-project-profile")
        self.assertGreaterEqual(payload["assets"], 3)

    def test_validate_rejects_profile_target_mismatch(self):
        result = self.run_cli(
            "validate", str(PROFILE), "--target-lang", "en"
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not match --target-lang", result.stderr)


if __name__ == "__main__":
    unittest.main()
