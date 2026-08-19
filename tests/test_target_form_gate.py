from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lqe_checks import run_pre_check
from lqe_corrections import build_segment_result, validate_reference_target
from lqe_engine import build_check_scope, build_review_policy
from lqe_project_assets import build_project_asset_snapshot
from lqe_split_contract import SplitContractError, state_revision_payload
from lqe_target_form import checks_path_for_state


PARTICLE_POLICY = {"ko_particle_edit_boundary": True}
PAIR_POLICY = {"paired_punct": True}


def local_issue(text: str, before: str, after: str) -> dict:
    start = text.index(before)
    return {
        "category": "Grammar",
        "severity": "Major",
        "comment": "target-form fixture",
        "needs_confirmation": False,
        "edit": {
            "from": before,
            "to": after,
            "start": start,
            "end": start + len(before),
            "evidence": None,
        },
    }


def segment(row: int, target: str) -> dict:
    return {
        "id": row,
        "source": "fixture",
        "target": target,
        "kind": "desc",
        "term_hits": [],
    }


class TargetFormGateTests(unittest.TestCase):
    def test_nbsp_is_normalized_as_one_maximal_horizontal_run(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            checks_path = root / "checks.json"
            state_path = root / "state.json"
            output_path = root / "precheck.json"
            checks_path.write_text(
                json.dumps({"builtin": {"forbidden_nbsp": True}}),
                encoding="utf-8",
            )
            target = "A \u00a0\t B"
            state = {
                "job_runtime_contract_version": 2,
                "source_lang": "zh",
                "target_lang": "en",
                "check_scope": build_check_scope(True, "test"),
                "review_policy": build_review_policy("full", "test"),
                "checks_path": str(checks_path),
                "segments": [{"id": 1, "source": "A B", "target": target}],
            }
            state_path.write_text(
                json.dumps(state, ensure_ascii=False),
                encoding="utf-8",
            )

            run_pre_check(state_path, output_path)

            issues = json.loads(output_path.read_text(encoding="utf-8"))[0][
                "issues"
            ]
            nbsp = [
                item
                for item in issues
                if item["comment"] == "Forbidden non-breaking space in target"
            ]
            self.assertEqual(len(nbsp), 1)
            self.assertEqual(
                nbsp[0]["edit"],
                {
                    "from": target[1:5],
                    "to": " ",
                    "start": 1,
                    "end": 5,
                    "evidence": None,
                },
            )
            self.assertFalse(
                any(item["comment"] == "Double space in target" for item in issues)
            )

    def test_complementary_punctuation_edits_are_gated_after_merge(self):
        target = "Alpha middle Omega"
        result = build_segment_result(
            segment(1, target),
            [
                local_issue(target, "Alpha", "(Alpha"),
                local_issue(target, "Omega", "Omega)"),
            ],
            target_form_policy=PAIR_POLICY,
        )

        self.assertEqual(result["corrected"], "(Alpha middle Omega)")
        self.assertTrue(
            all(not error["needs_confirmation"] for error in result["errors"])
        )

    def test_wide_local_edit_rejects_invalid_object_particle(self):
        target = "책을 정리했다."
        result = build_segment_result(
            segment(1, target),
            [local_issue(target, "책을", "사과을")],
            target_form_policy=PARTICLE_POLICY,
        )

        self.assertIsNone(result["corrected"])
        self.assertEqual(
            result["errors"][0]["reason_codes"],
            [
                "TARGET_FORM_MUTATION_REJECTED",
                "KO_PARTICLE_EDIT_BOUNDARY:을->를",
            ],
        )

    def test_local_stem_edit_rejects_invalid_object_particle(self):
        target = "상자를 옮겼다."
        result = build_segment_result(
            segment(1, target),
            [local_issue(target, "상자", "책")],
            target_form_policy=PARTICLE_POLICY,
        )

        self.assertIsNone(result["corrected"])
        self.assertIn(
            "KO_PARTICLE_EDIT_BOUNDARY:를->을",
            result["errors"][0]["reason_codes"],
        )

    def test_adjacent_cluster_rejects_invalid_object_particle_together(self):
        target = "큰 상자를 옮겼다."
        result = build_segment_result(
            segment(1, target),
            [
                local_issue(target, "큰 ", "무거운 "),
                local_issue(target, "상자", "책"),
            ],
            target_form_policy=PARTICLE_POLICY,
        )

        self.assertIsNone(result["corrected"])
        self.assertTrue(
            all(error["needs_confirmation"] for error in result["errors"])
        )
        self.assertTrue(
            all(
                "KO_PARTICLE_EDIT_BOUNDARY:를->을" in error["reason_codes"]
                for error in result["errors"]
            )
        )

    def test_adjacent_particle_edit_allows_legal_dolhwabuneul(self):
        target = "사과를 정리했다."
        result = build_segment_result(
            segment(1, target),
            [
                local_issue(target, "사과", "책"),
                local_issue(target, "를", "을"),
            ],
            target_form_policy=PARTICLE_POLICY,
        )

        self.assertEqual(result["corrected"], "책을 정리했다.")
        self.assertTrue(
            all(not error["needs_confirmation"] for error in result["errors"])
        )

    def test_full_reference_target_intentionally_does_not_scan_particle_form(self):
        original = "상자를 옮겼다."
        reference = "책를 옮겼다."

        self.assertEqual(
            validate_reference_target(
                segment(1, original),
                reference,
                target_form_policy=PARTICLE_POLICY,
            ),
            reference,
        )

    def test_v2_bound_checks_summary_or_live_digest_drift_fails_closed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            checks_path = root / "checks.json"
            checks_path.write_text("{}", encoding="utf-8")
            profile = {
                "profile_contract_version": 2,
                "language_pair": "zh-ko",
                "source_lang": "zh",
                "target_lang": "ko",
                "assets": {
                    "quality_checks": {
                        "kind": "checks",
                        "path": checks_path.name,
                        "required": True,
                        "authority": {
                            "issuer": "test",
                            "level": "authoritative",
                        },
                        "provenance": {"kind": "test_fixture"},
                        "distribution": "internal_only",
                        "availability": "included",
                    }
                },
            }
            snapshot = build_project_asset_snapshot(profile, profile_dir=root)
            state = {
                "job_runtime_contract_version": 2,
                "target_lang": "ko",
                "segments": [],
                "checks_path": str(checks_path),
                "project_asset_snapshot": snapshot,
                "project_asset_snapshot_digest": snapshot["digest"],
                "project_asset_paths": {"quality_checks": str(checks_path)},
            }
            expected_sha = snapshot["assets"]["quality_checks"]["sha256"]

            summary = state_revision_payload(state)["asset_paths"]["checks_path"]
            self.assertEqual(
                summary,
                {
                    "path": str(checks_path),
                    "status": "file",
                    "sha256": expected_sha,
                },
            )
            self.assertEqual(checks_path_for_state(state), str(checks_path))

            stale_summary = deepcopy(state)
            stale_summary["project_asset_snapshot"]["assets"][
                "quality_checks"
            ]["sha256"] = "0" * 64
            with self.assertRaisesRegex(SplitContractError, "digest mismatch"):
                state_revision_payload(stale_summary)
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                checks_path_for_state(stale_summary)

            checks_path.write_text('{"changed":true}', encoding="utf-8")
            self.assertEqual(
                state_revision_payload(state)["asset_paths"]["checks_path"],
                summary,
            )
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                checks_path_for_state(state)


if __name__ == "__main__":
    unittest.main()
