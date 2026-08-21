import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lqe_checks import _check_issues
from lqe_corrections import CheckFormatError, build_segment_result
from lqe_engine import build_review_policy, get_review_policy
from lqe_review import build_review_packet
from lqe_split_contract import state_fingerprint


def minor_edit_issue():
    return {
        "category": "Spelling",
        "severity": "Minor",
        "comment": "The word is misspelled.",
        "needs_confirmation": False,
        "edit": {
            "from": "Wrng",
            "to": "Wrong",
            "evidence": None,
        },
    }


class ReviewModeSwitchTests(unittest.TestCase):
    def test_policy_modes_resolve_to_distinct_contracts(self):
        optimized = build_review_policy("optimized")
        full = build_review_policy("full")

        self.assertFalse(optimized["minor_edits_allowed"])
        self.assertEqual(
            optimized["comment_soft_target"],
            {"min_chars": 20, "max_chars": 30},
        )
        self.assertEqual(
            optimized["suggestion_candidate_severities"],
            ["Critical", "Major"],
        )
        self.assertTrue(optimized["text_type_routing_enabled"])
        self.assertTrue(full["minor_edits_allowed"])
        self.assertIsNone(full["comment_soft_target"])
        self.assertEqual(
            full["suggestion_candidate_severities"],
            ["Neutral", "Minor", "Major", "Critical"],
        )
        self.assertFalse(full["text_type_routing_enabled"])

    def test_missing_policy_keeps_optimized_compatibility(self):
        self.assertEqual(get_review_policy({})["mode"], "optimized")
        self.assertEqual(get_review_policy({})["source"], "legacy-default")

    def test_minor_edit_is_rejected_in_optimized_and_built_in_full(self):
        segment = {"id": 0, "target": "Wrng"}
        with self.assertRaisesRegex(CheckFormatError, "Minor issue"):
            build_segment_result(segment, [minor_edit_issue()])

        result = build_segment_result(
            segment,
            [minor_edit_issue()],
            review_policy=build_review_policy("full"),
        )
        self.assertEqual(result["corrected"], "Wrong")

    def test_precheck_minor_edit_follows_mode(self):
        raw = [{
            "category": "Spelling",
            "severity": "Minor",
            "comment": "Misspelled.",
            "edit": {"from": "Wrng", "to": "Wrong", "evidence": None},
        }]
        optimized = _check_issues(raw, build_review_policy("optimized"))[0]
        full = _check_issues(raw, build_review_policy("full"))[0]

        self.assertTrue(optimized["needs_confirmation"])
        self.assertIsNone(optimized["edit"])
        self.assertFalse(full["needs_confirmation"])
        self.assertIsNotNone(full["edit"])

    def test_policy_is_bound_to_state_fingerprint_and_review_packet(self):
        base_state = {
            "segments": [{"id": 0, "source": "S", "target": "T"}],
        }
        optimized_state = {
            **base_state,
            "review_policy": build_review_policy("optimized"),
        }
        full_state = {
            **base_state,
            "review_policy": build_review_policy("full"),
        }
        self.assertNotEqual(
            state_fingerprint(optimized_state),
            state_fingerprint(full_state),
        )

        base = {
            "chunk_id": 0,
            "iteration": 0,
            "split_fingerprint": "split",
            "payload_digest": "payload",
            "review_policy": build_review_policy("full"),
            "segments": [{"id": 0, "source": "S", "target": "T"}],
        }
        packet = build_review_packet(base, "grammar")
        self.assertEqual(packet["review_policy"]["mode"], "full")

    def test_read_persists_explicit_mode(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "input.csv"
            state_path = root / "job" / "state.json"
            source.write_text("Source,Target\nHello,Wrng\n", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "lqe_io.py"),
                    "read",
                    "--input",
                    str(source),
                    "--source-col",
                    "Source",
                    "--target-col",
                    "Target",
                    "--review-mode",
                    "full",
                    "--out",
                    str(state_path),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["review_policy"]["mode"], "full")
            self.assertEqual(state["suggestion_guard_version"], 2)

    def test_skill_requires_agent_to_ask_before_new_job(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("必须先询问一次并等待回答", skill)
        self.assertIn("不得根据项目、文件、历史任务或成本偏好", skill)
        self.assertIn("--review-mode \"<optimized|full>\"", skill)
        self.assertIn("state.review_policy", skill)


if __name__ == "__main__":
    unittest.main()
