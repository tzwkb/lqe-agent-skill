import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CONVERTER = SCRIPTS / "mastertb_to_terms.py"


def build_master_tb(path: Path, *, status_hdr="术语状态 Status",
                    extra_status_hdr=None, lowercase_variants=False,
                    include_status=True) -> None:
    """Master TB layout: a banner band, then a header row with 术语 ZHCN / TH /
    [status] / 术语类别 category, then data rows (one multisense source included).

    Detection is RULE-based (header contains 'status'/'状态'), so callers can spell
    the status header however they like to exercise drift resistance.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["ROCO Master TB — TH"])
    ws.append(["guideline banner row"])
    header = ["术语 ZHCN", "TH"]
    if include_status:
        header.append(status_hdr)
    header.append("术语类别 category")
    if extra_status_hdr:
        header.append(extra_status_hdr)
    ws.append(header)

    rows = [
        ["花衣蝶", "ผีเสื้อบุปผา", "Approved", "Creature Species"],
        ["水枝枝", "น้ำกิ่ง", "Denied", "Creature Species"],
        ["海珊瑚", "ปะการังทะเล", "New", "Creature Species"],
        ["火花", "ประกายไฟ", "Approved", "Spell"],
        ["火花", "สปาร์ก", "New", "Item"],
        ["空译词", "", "Approved", "Creature Species"],  # empty target -> dropped
    ]
    if lowercase_variants:
        rows.append(["枯木", "กระดูก", "denied", "Creature Species"])   # lowercase Denied
        rows.append(["星尘", "ผงดาว", "approved", "Spell"])            # lowercase Approved
    for r in rows:
        if not include_status:
            r = r[:2] + r[3:]  # drop the status element
        ws.append(r)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


class MasterTBToTermsStatusMappingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.work = Path(self.tempdir.name)
        self.master = self.work / "master.xlsx"
        build_master_tb(self.master)
        self.out = self.work / "terms_th.json"

    def tearDown(self):
        self.tempdir.cleanup()

    def run_converter(self, *extra, input_path=None):
        return subprocess.run(
            [sys.executable, str(CONVERTER),
             "--input", str(input_path or self.master),
             "--out", str(self.out),
             "--target-col", "TH", *map(str, extra)],
            cwd=ROOT, text=True, capture_output=True,
        )

    def load(self):
        return json.loads(self.out.read_text(encoding="utf-8"))

    # ---- fail-closed (the safety net) ----------------------------------------

    def test_fail_closed_when_status_column_present_but_no_mapping(self):
        """LQE term-confirmation contract: refuse to emit silently all-unconfirmed."""
        result = self.run_converter()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("fail-closed", result.stderr)
        self.assertIn("Distinct status values found", result.stderr)
        for v in ("Approved", "Denied", "New"):
            self.assertIn(v, result.stderr)
        self.assertFalse(self.out.exists(), "converter must not emit terms on fail-closed")

    def test_protected_only_triggers_fail_closed(self):
        """--protected-statuses alone is NOT a confirmation decision -> fail-closed."""
        result = self.run_converter("--protected-statuses", "Denied")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("fail-closed", result.stderr)
        self.assertFalse(self.out.exists())

    # ---- confirmation mapping (case-insensitive, a rule) ---------------------

    def test_mapping_sets_confirmed_and_protected(self):
        # Denied is always excluded, so map protected to a non-Denied status (New).
        result = self.run_converter(
            "--approved-statuses", "Approved,合规审核通过",
            "--protected-statuses", "New",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        terms = {t["source"]: t for t in self.load()}
        self.assertTrue(terms["花衣蝶"]["confirmed"])
        self.assertFalse(terms["花衣蝶"]["protected"])
        self.assertNotIn("水枝枝", terms)   # Denied -> excluded by default
        self.assertTrue(terms["海珊瑚"]["protected"])  # New -> protected per mapping
        self.assertFalse(terms["海珊瑚"]["confirmed"])
        self.assertNotIn("空译词", terms)

    def test_mapping_applies_per_sense_in_multisense_source(self):
        result = self.run_converter(
            "--approved-statuses", "Approved",
            "--protected-statuses", "Denied",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        spark = next(t for t in self.load() if t.get("source") == "火花")
        self.assertIn("senses", spark)
        by_tgt = {s["target"]: s for s in spark["senses"]}
        self.assertTrue(by_tgt["ประกายไฟ"]["confirmed"])   # Approved
        self.assertFalse(by_tgt["สปาร์ก"]["confirmed"])     # New

    def test_approved_star_confirms_whole_glossary(self):
        result = self.run_converter("--approved-statuses", "*")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for t in self.load():
            if "senses" in t:
                for s in t["senses"]:
                    self.assertTrue(s["confirmed"])
            else:
                self.assertTrue(t["confirmed"])

    def test_empty_approved_explicitly_unconfirmed(self):
        """Choice 2 made explicit: all unconfirmed, but converter succeeds (not fail-closed)."""
        result = self.run_converter("--approved-statuses", "")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for t in self.load():
            if "senses" in t:
                for s in t["senses"]:
                    self.assertFalse(s["confirmed"])
            else:
                self.assertFalse(t["confirmed"])

    # ---- Denied always excluded (default, case-insensitive) ------------------

    def test_denied_excluded_without_flag(self):
        """Standing rule: 'Denied' is dropped even when --exclude-statuses is NOT passed."""
        result = self.run_converter("--approved-statuses", "Approved")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("excluded by default(Denied)+--exclude-statuses: 1", result.stdout)
        terms = {t["source"]: t for t in self.load()}
        self.assertNotIn("水枝枝", terms)  # Denied -> dropped by default
        self.assertIn("花衣蝶", terms)      # Approved -> kept
        self.assertIn("海珊瑚", terms)      # New -> kept (unconfirmed, not excluded)

    def test_exclude_statuses_drops_terms_entirely(self):
        """Client-rejected (Denied) terms must be absent from the glossary."""
        result = self.run_converter(
            "--approved-statuses", "Approved",
            "--exclude-statuses", "Denied",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("excluded by default(Denied)+--exclude-statuses: 1", result.stdout)
        terms = {t["source"]: t for t in self.load()}
        self.assertNotIn("水枝枝", terms)  # Denied -> dropped
        self.assertIn("花衣蝶", terms)      # Approved -> kept
        self.assertIn("海珊瑚", terms)      # New -> kept (unconfirmed, not excluded)

    # ---- RULE-BASED robustness: no future drift can silently bypass ----------

    def test_status_col_renamed_with_suffix_still_detected(self):
        """Detection is a rule (header contains 'status'/'状态'), so a renamed
        column like '术语状态 Status(TH)' is still caught and mapped."""
        m = self.work / "renamed.xlsx"
        build_master_tb(m, status_hdr="术语状态 Status(TH)")
        result = self.run_converter("--approved-statuses", "Approved", input_path=m)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        terms = {t["source"]: t for t in self.load()}
        self.assertNotIn("水枝枝", terms)  # Denied excluded despite rename
        self.assertTrue(terms["花衣蝶"]["confirmed"])  # Approved mapped

    def test_lowercase_status_values_compared_case_insensitively(self):
        """'denied' (lowercase) is excluded by default; 'approved' (lowercase) maps
        to confirmed — case never breaks the rule."""
        m = self.work / "lower.xlsx"
        build_master_tb(m, lowercase_variants=True)
        result = self.run_converter("--approved-statuses", "Approved", input_path=m)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        terms = {t["source"]: t for t in self.load()}
        self.assertNotIn("枯木", terms)    # lowercase 'denied' excluded
        self.assertIn("星尘", terms)
        self.assertTrue(terms["星尘"]["confirmed"])  # lowercase 'approved' mapped

    def test_no_status_column_fail_closed_unless_no_status(self):
        """If no status column is detected, the converter MUST fail-closed (it must
        not silently emit all-unconfirmed). This closes the 'renamed column -> silent
        all-false' hole for good."""
        m = self.work / "nostatus.xlsx"
        build_master_tb(m, include_status=False)
        result = self.run_converter("--approved-statuses", "Approved", input_path=m)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no status column detected", result.stderr)
        self.assertFalse(self.out.exists())

    def test_no_status_column_ok_with_no_status_flag(self):
        """--no-status asserts the glossary truly has no confirmation info; then the
        converter proceeds (all unconfirmed, by explicit assertion)."""
        m = self.work / "nostatus.xlsx"
        build_master_tb(m, include_status=False)
        result = self.run_converter("--no-status", "--approved-statuses", "", input_path=m)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        terms = {t["source"]: t for t in self.load()}
        self.assertIn("花衣蝶", terms)
        self.assertFalse(terms["花衣蝶"]["confirmed"])

    def test_no_status_column_can_be_explicitly_confirmed_as_whole_glossary(self):
        """Choice 1 applies even when the workbook has no status column."""
        m = self.work / "nostatus.xlsx"
        build_master_tb(m, include_status=False)
        result = self.run_converter(
            "--no-status", "--approved-statuses", "*", input_path=m
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for term in self.load():
            senses = term.get("senses") or [term]
            self.assertTrue(all(sense["confirmed"] is True for sense in senses))
            self.assertTrue(all(sense["protected"] is False for sense in senses))

    def test_whole_glossary_confirmation_includes_backfilled_terms(self):
        m = self.work / "nostatus.xlsx"
        old = self.work / "old.json"
        build_master_tb(m, include_status=False)
        old.write_text(
            json.dumps([
                {
                    "source": "空译词",
                    "target": "คำเก่า",
                    "confirmed": False,
                    "protected": False,
                }
            ], ensure_ascii=False),
            encoding="utf-8",
        )
        result = self.run_converter(
            "--no-status", "--approved-statuses", "*", "--backfill", old,
            input_path=m,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        terms = {t["source"]: t for t in self.load()}
        self.assertTrue(terms["空译词"]["confirmed"])
        self.assertFalse(terms["空译词"]["protected"])

    def test_ambiguous_status_columns_fail_closed(self):
        """Multiple status-keyword columns -> fail-closed until disambiguated by
        --status-col (never guesses)."""
        m = self.work / "ambiguous.xlsx"
        build_master_tb(m, extra_status_hdr="审核状态 Status")
        result = self.run_converter("--approved-statuses", "Approved", input_path=m)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("multiple status-column candidates", result.stderr)
        self.assertFalse(self.out.exists())

    def test_ambiguous_status_columns_disambiguated_by_status_col(self):
        m = self.work / "ambiguous.xlsx"
        build_master_tb(m, extra_status_hdr="审核状态 Status")
        result = self.run_converter(
            "--approved-statuses", "Approved",
            "--status-col", "术语状态 Status",
            input_path=m,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        terms = {t["source"]: t for t in self.load()}
        self.assertTrue(terms["花衣蝶"]["confirmed"])


if __name__ == "__main__":
    unittest.main()
