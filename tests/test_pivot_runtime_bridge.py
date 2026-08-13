from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import openpyxl


ROOT = Path(__file__).resolve().parents[1]
IO = ROOT / "scripts" / "lqe_io.py"
sys.path.insert(0, str(ROOT / "scripts"))

from lqe_split_contract import state_fingerprint


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class PivotRuntimeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def write_book(self, name, pivot_headers, pivot_rows):
        path = self.root / name
        workbook = openpyxl.Workbook()
        main = workbook.active
        main.title = "Main"
        main.append(
            ["Key", "Source", "Target", "Content Type", "Source Digest"]
        )
        main.append(["k1", "Save", "Save", "ui_button", digest("Save")])
        main.append(["k2", "Open", "Open", "ui_label", digest("Open")])
        pivot = workbook.create_sheet("Pivot")
        pivot.append(pivot_headers)
        for row in pivot_rows:
            pivot.append(row)
        workbook.save(path)
        workbook.close()
        return path

    def run_io(self, *args):
        return subprocess.run(
            [sys.executable, str(IO), *map(str, args)],
            capture_output=True,
            text=True,
            check=False,
        )

    def read_args(self, source, state):
        return [
            "read",
            "--input",
            source,
            "--sheet",
            "Main",
            "--source-col",
            "Source",
            "--target-col",
            "Target",
            "--key-col",
            "Key",
            "--context-col",
            "content_type=Content Type",
            "--target-source-digest-col",
            "Source Digest",
            "--source-lang",
            "en",
            "--target-lang",
            "en",
            "--no-terminology",
            "--out",
            state,
        ]

    def pivot_args(self, authority="authoritative", source_column="Source Ref"):
        return [
            "--pivot-sheet",
            "Pivot",
            "--pivot-key-col",
            "Pivot Key",
            "--pivot-compare",
            f"source={source_column}",
            "--pivot-compare",
            "target=Target Ref",
            "--pivot-compare",
            "key=Key Ref",
            "--pivot-compare",
            "context.core.content_type=Type Ref",
            "--pivot-authority",
            authority,
        ]

    def load_state(self, path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def test_authoritative_join_compares_builtin_and_canonical_context_fields(self):
        source = self.write_book(
            "authoritative.xlsx",
            ["Pivot Key", "Source Ref", "Target Ref", "Key Ref", "Type Ref"],
            [
                ["k1", "Save", "Save", "k1", " ui_button "],
                ["k2", "Open old", "Open", "k2", "ui_dialog"],
            ],
        )
        state_path = self.root / "authoritative" / "state.json"
        result = self.run_io(
            *self.read_args(source, state_path),
            *self.pivot_args(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        state = self.load_state(state_path)
        ready, blocked = state["segments"]
        self.assertEqual(ready["input_status"], "ready")
        self.assertNotIn("input_warnings", ready)
        self.assertEqual(blocked["input_status"], "blocked")
        mismatch = blocked["input_block_reasons"][0]
        self.assertEqual(mismatch["code"], "SOURCE_VERSION_MISMATCH")
        self.assertEqual(
            {item["field"] for item in mismatch["mismatches"]},
            {"source", "context.core@1.content_type"},
        )
        self.assertEqual(state["input_guard"]["blocked_ids"], [1])

        guard = state["pivot_guard"]
        manifest = json.loads(
            Path(state["tabular_source_manifest_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["pivot_guard"], guard)
        self.assertEqual(guard["main_sheet"], "Main")
        self.assertEqual(guard["pivot_sheet"], "Pivot")
        self.assertEqual(guard["authority"], "authoritative")
        self.assertEqual(guard["primary_keys"], 2)
        self.assertEqual(guard["pivot_keys"], 2)
        self.assertEqual(len(guard["pivot_sheet_digest"]), 64)
        self.assertEqual(len(guard["mapping_digest"]), 64)
        self.assertEqual(
            [item["field"] for item in guard["comparisons"]],
            [
                "source",
                "target",
                "key",
                "context.core@1.content_type",
            ],
        )
        self.assertTrue(
            all(
                segment["pivot_guard_digest"] == guard["digest"]
                for segment in state["segments"]
            )
        )
        self.assertEqual(blocked["pivot_provenance"]["sheet"], "Pivot")

        original_fingerprint = state_fingerprint(state)
        tampered = deepcopy(state)
        tampered["segments"][0]["pivot_guard_digest"] = "0" * 64
        self.assertNotEqual(state_fingerprint(tampered), original_fingerprint)

    def test_diagnostic_mismatch_warns_without_blocking(self):
        source = self.write_book(
            "diagnostic.xlsx",
            ["Pivot Key", "Source Ref", "Target Ref", "Key Ref", "Type Ref"],
            [
                ["k1", "Save", "Save old", "wrong-key", "ui_button"],
                ["k2", "Open", "Open", "k2", "ui_label"],
            ],
        )
        state_path = self.root / "diagnostic" / "state.json"
        result = self.run_io(
            *self.read_args(source, state_path),
            *self.pivot_args("diagnostic"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.load_state(state_path)
        self.assertEqual(
            [segment["input_status"] for segment in state["segments"]],
            ["ready", "ready"],
        )
        self.assertEqual(
            state["segments"][0]["input_warnings"][0]["code"],
            "SOURCE_VERSION_MISMATCH",
        )
        self.assertEqual(
            {
                item["field"]
                for item in state["segments"][0]["input_warnings"][0][
                    "mismatches"
                ]
            },
            {"target", "key"},
        )
        self.assertEqual(state["input_guard"]["warning_ids"], [0])
        self.assertEqual(state["input_guard"]["blocked_ids"], [])

    def test_mapping_choice_changes_bound_guard_and_segment_revision(self):
        source = self.write_book(
            "mapping.xlsx",
            [
                "Pivot Key",
                "Source Ref",
                "Source Mirror",
                "Target Ref",
                "Key Ref",
                "Type Ref",
            ],
            [
                ["k1", "Save", "Save", "Save", "k1", "ui_button"],
                ["k2", "Open", "Open", "Open", "k2", "ui_label"],
            ],
        )
        first_path = self.root / "mapping-a" / "state.json"
        second_path = self.root / "mapping-b" / "state.json"
        first = self.run_io(
            *self.read_args(source, first_path),
            *self.pivot_args(source_column="Source Ref"),
        )
        second = self.run_io(
            *self.read_args(source, second_path),
            *self.pivot_args(source_column="Source Mirror"),
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        state_a = self.load_state(first_path)
        state_b = self.load_state(second_path)
        self.assertNotEqual(
            state_a["pivot_guard"]["mapping_digest"],
            state_b["pivot_guard"]["mapping_digest"],
        )
        self.assertNotEqual(
            state_a["pivot_guard"]["digest"], state_b["pivot_guard"]["digest"]
        )
        self.assertNotEqual(
            state_a["segments"][0]["segment_revision_digest"],
            state_b["segments"][0]["segment_revision_digest"],
        )

    def test_duplicate_or_incomplete_key_join_fails_without_publishing(self):
        duplicate = self.write_book(
            "duplicate.xlsx",
            ["Pivot Key", "Source Ref", "Target Ref", "Key Ref", "Type Ref"],
            [
                ["k1", "Save", "Save", "k1", "ui_button"],
                ["k1", "Open", "Open", "k1", "ui_label"],
            ],
        )
        duplicate_state = self.root / "duplicate" / "state.json"
        result = self.run_io(
            *self.read_args(duplicate, duplicate_state),
            *self.pivot_args(),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate pivot business key", result.stderr)
        self.assertFalse(duplicate_state.exists())
        self.assertFalse(
            (duplicate_state.parent / "tabular_source_manifest.json").exists()
        )

        incomplete = self.write_book(
            "incomplete.xlsx",
            ["Pivot Key", "Source Ref", "Target Ref", "Key Ref", "Type Ref"],
            [["k1", "Save", "Save", "k1", "ui_button"]],
        )
        incomplete_state = self.root / "incomplete" / "state.json"
        result = self.run_io(
            *self.read_args(incomplete, incomplete_state),
            *self.pivot_args(),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pivot key coverage mismatch", result.stderr)
        self.assertIn("k2", result.stderr)
        self.assertFalse(incomplete_state.exists())

    def test_partial_pivot_config_fails_and_absent_config_is_compatible(self):
        source = self.write_book(
            "compatibility.xlsx",
            ["Pivot Key", "Source Ref", "Target Ref", "Key Ref", "Type Ref"],
            [
                ["k1", "Save", "Save", "k1", "ui_button"],
                ["k2", "Open", "Open", "k2", "ui_label"],
            ],
        )
        partial_state = self.root / "partial" / "state.json"
        partial = self.run_io(
            *self.read_args(source, partial_state),
            "--pivot-sheet",
            "Pivot",
        )
        self.assertNotEqual(partial.returncode, 0)
        self.assertIn("pivot configuration requires all", partial.stderr)
        self.assertFalse(partial_state.exists())

        legacy_state = self.root / "legacy" / "state.json"
        legacy = self.run_io(*self.read_args(source, legacy_state))
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        state = self.load_state(legacy_state)
        manifest = json.loads(
            Path(state["tabular_source_manifest_path"]).read_text(encoding="utf-8")
        )
        self.assertNotIn("pivot_guard", state)
        self.assertNotIn("pivot_guard", manifest)
        self.assertTrue(
            all("pivot_guard_digest" not in segment for segment in state["segments"])
        )


class FakeCell:
    def __init__(self, value, ctype=1):
        self.value = value
        self.ctype = ctype


class FakeSheet:
    def __init__(self, rows):
        self.rows = rows
        self.nrows = len(rows)
        self.ncols = max((len(row) for row in rows), default=0)

    def cell(self, row_index, column_index):
        if column_index >= len(self.rows[row_index]):
            return FakeCell(None, 0)
        return self.rows[row_index][column_index]


class FakeBook:
    datemode = 0

    def __init__(self, sheets):
        self.sheets = sheets

    def sheet_names(self):
        return list(self.sheets)

    def sheet_by_name(self, name):
        return self.sheets[name]

    def release_resources(self):
        pass


def fake_xlrd(book):
    module = types.ModuleType("xlrd")
    module.__version__ = "2.0.2"
    module.XL_CELL_EMPTY = 0
    module.XL_CELL_TEXT = 1
    module.XL_CELL_NUMBER = 2
    module.XL_CELL_DATE = 3
    module.XL_CELL_BOOLEAN = 4
    module.XL_CELL_ERROR = 5
    module.XL_CELL_BLANK = 6
    module.open_workbook = mock.Mock(return_value=book)
    module.xldate = types.SimpleNamespace(
        xldate_as_datetime=lambda value, datemode: datetime(2026, 8, 13)
    )
    return module


class LegacyXLSPivotRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.source = self.root / "pivot.xls"
        self.source.write_bytes(b"pivot xls fixture; parsing is mocked")
        self.state_path = self.root / "job" / "state.json"

    def tearDown(self):
        self.tempdir.cleanup()

    def book(self):
        return FakeBook(
            {
                "Main": FakeSheet(
                    [
                        [
                            FakeCell("Key"),
                            FakeCell("Source"),
                            FakeCell("Target"),
                            FakeCell("Source Digest"),
                        ],
                        [
                            FakeCell("k1"),
                            FakeCell("Save"),
                            FakeCell("Save"),
                            FakeCell(digest("Save")),
                        ],
                    ]
                ),
                "Pivot": FakeSheet(
                    [
                        [FakeCell("ID"), FakeCell("Source Ref")],
                        [FakeCell("k1"), FakeCell("Save")],
                    ]
                ),
            }
        )

    def test_xls_main_and_pivot_sheets_share_the_runtime_guard(self):
        import lqe_io

        arguments = [
            "read",
            "--input",
            self.source,
            "--sheet",
            "Main",
            "--source-col",
            "Source",
            "--target-col",
            "Target",
            "--key-col",
            "Key",
            "--target-source-digest-col",
            "Source Digest",
            "--pivot-sheet",
            "Pivot",
            "--pivot-key-col",
            "ID",
            "--pivot-compare",
            "source=Source Ref",
            "--pivot-authority",
            "authoritative",
            "--source-lang",
            "en",
            "--target-lang",
            "en",
            "--no-terminology",
            "--out",
            self.state_path,
        ]
        book = self.book()
        with mock.patch.dict(sys.modules, {"xlrd": fake_xlrd(book)}), mock.patch.object(
            sys, "argv", ["lqe_io.py", *map(str, arguments)]
        ):
            lqe_io.main()

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["sheet_name"], "Main")
        self.assertEqual(state["pivot_guard"]["pivot_sheet"], "Pivot")
        self.assertEqual(state["pivot_guard"]["authority"], "authoritative")
        self.assertEqual(state["segments"][0]["input_status"], "ready")
        self.assertEqual(
            state["segments"][0]["pivot_guard_digest"],
            state["pivot_guard"]["digest"],
        )


if __name__ == "__main__":
    unittest.main()
