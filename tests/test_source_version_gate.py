from copy import deepcopy
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lqe_input_guard import (
    InputGuardError,
    apply_pivot_comparison,
    apply_target_source_digest_guard,
    build_segment_identity,
    canonical_digest,
    copy_guard_fields,
    ensure_unique_business_keys,
    input_guard_summary,
    source_digest,
)


class SourceVersionGateTests(unittest.TestCase):
    def segment(self, segment_id=1, source="Cafe\N{COMBINING ACUTE ACCENT}"):
        return {
            "id": segment_id,
            "source": source,
            "target": "Café",
            "input_status": "ready",
        }

    def test_business_key_wins_and_generated_identity_is_reproducible(self):
        digest = "a" * 64
        declared = build_segment_identity(
            business_key=" row-001 ",
            input_digest=digest,
            container="Sheet1",
            row_index=3,
        )
        self.assertEqual(
            declared, {"segment_key": "row-001", "key_origin": "input"}
        )
        generated = build_segment_identity(
            business_key=None,
            input_digest=digest,
            container="Sheet1",
            row_index=3,
        )
        repeated = build_segment_identity(
            business_key="",
            input_digest=digest,
            container="Sheet1",
            row_index=3,
        )
        changed = build_segment_identity(
            business_key=None,
            input_digest=digest,
            container="Sheet1",
            row_index=4,
        )
        self.assertEqual(generated, repeated)
        self.assertEqual(generated["key_origin"], "generated")
        self.assertTrue(generated["segment_key"].startswith("generated:"))
        self.assertNotEqual(generated["segment_key"], changed["segment_key"])

    def test_duplicate_input_business_keys_fail_but_generated_keys_are_independent(self):
        segments = [
            {"id": 1, "segment_key": "same", "key_origin": "input"},
            {"id": 2, "segment_key": "same", "key_origin": "input"},
        ]
        with self.assertRaisesRegex(InputGuardError, "duplicate business key"):
            ensure_unique_business_keys(segments)

        generated = deepcopy(segments)
        generated[1]["key_origin"] = "generated"
        ensure_unique_business_keys(generated)

    def test_matching_target_source_digest_stays_ready(self):
        segment = self.segment(source="Source v1")
        expected = source_digest("Source v1")
        apply_target_source_digest_guard(segment, expected.upper())
        self.assertEqual(segment["input_status"], "ready")
        self.assertEqual(segment["source_digest"], expected)
        self.assertNotIn("input_block_reasons", segment)
        self.assertNotIn("input_warnings", segment)

    def test_stale_target_source_digest_blocks_with_both_digests(self):
        segment = self.segment(source="Source v2")
        recorded = source_digest("Source v1")
        actual = source_digest("Source v2")
        apply_target_source_digest_guard(segment, recorded)
        self.assertEqual(segment["input_status"], "blocked")
        self.assertEqual(
            segment["input_block_reasons"],
            [
                {
                    "code": "TARGET_SOURCE_VERSION_MISMATCH",
                    "expected_source_digest": recorded,
                    "actual_source_digest": actual,
                }
            ],
        )

    def test_missing_target_digest_warns_without_claiming_verification(self):
        segment = self.segment(source="Source")
        apply_target_source_digest_guard(segment, None)
        self.assertEqual(segment["input_status"], "ready")
        self.assertEqual(
            segment["input_warnings"],
            [{"code": "UNVERIFIED_TARGET_PROVENANCE"}],
        )

    def test_authoritative_pivot_mismatch_blocks_and_diagnostic_only_warns(self):
        comparisons = [{"field": "revision", "normalizer": "integer"}]
        authoritative = self.segment(1)
        apply_pivot_comparison(
            authoritative,
            primary_values={"revision": "2"},
            pivot_values={"revision": "1"},
            comparisons=comparisons,
            authority="authoritative",
        )
        self.assertEqual(authoritative["input_status"], "blocked")
        self.assertEqual(
            authoritative["input_block_reasons"][0]["code"],
            "SOURCE_VERSION_MISMATCH",
        )

        diagnostic = self.segment(2)
        apply_pivot_comparison(
            diagnostic,
            primary_values={"revision": "2"},
            pivot_values={"revision": "1"},
            comparisons=comparisons,
            authority="diagnostic",
        )
        self.assertEqual(diagnostic["input_status"], "ready")
        self.assertEqual(
            diagnostic["input_warnings"][0]["code"],
            "SOURCE_VERSION_MISMATCH",
        )

    def test_explicit_normalizers_avoid_false_version_mismatches(self):
        segment = self.segment()
        apply_pivot_comparison(
            segment,
            primary_values={"title": " CAFÉ "},
            pivot_values={"title": "Cafe\N{COMBINING ACUTE ACCENT}"},
            comparisons=[{"field": "title", "normalizer": "casefold"}],
            authority="authoritative",
        )
        self.assertEqual(segment["input_status"], "ready")
        self.assertNotIn("input_block_reasons", segment)

    def test_guard_summary_and_projection_are_content_bound(self):
        ready = self.segment(1, "A")
        apply_target_source_digest_guard(ready, None)
        blocked = self.segment(2, "B")
        apply_target_source_digest_guard(blocked, source_digest("old B"))
        summary = input_guard_summary([ready, blocked])
        self.assertEqual(summary["blocked_ids"], [2])
        self.assertEqual(summary["warning_ids"], [1])
        self.assertEqual(
            summary["digest"],
            canonical_digest({key: value for key, value in summary.items() if key != "digest"}),
        )
        projected = copy_guard_fields(blocked)
        self.assertNotIn("source", projected)
        self.assertEqual(projected["input_status"], "blocked")


class SourceVersionCLIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.source = self.root / "source.csv"
        with self.source.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(
                [
                    ["Key", "Source", "Target", "Source Digest"],
                    ["ready", "Save", "Save", source_digest("Save")],
                    ["blocked", "Open", "Open", source_digest("old Open")],
                ]
            )
        self.state_path = self.root / "job" / "state.json"

    def tearDown(self):
        self.tempdir.cleanup()

    def run_io(self, *args):
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "lqe_io.py"),
                *map(str, args),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def read(self):
        result = self.run_io(
            "read",
            "--input",
            self.source,
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
            self.state_path,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_blocked_segment_cannot_be_changed_by_corrected_export(self):
        state = self.read()
        state["segments"][0]["corrected"] = "Save now"
        state["segments"][1]["corrected"] = "MUST NOT EXPORT"
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

        result = self.run_io("export", "--state", self.state_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.state_path.parent / "job_corrected.csv"
        with output.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[1][2], "Save now")
        self.assertEqual(rows[2][2], "Open")

    def test_export_rejects_source_file_drift_before_output(self):
        self.read()
        with self.source.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        result = self.run_io("export", "--state", self.state_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source input changed after read", result.stderr)
        self.assertFalse((self.state_path.parent / "job_corrected.csv").exists())


if __name__ == "__main__":
    unittest.main()
