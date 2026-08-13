import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import openpyxl


ROOT = Path(__file__).resolve().parents[1]
IO_SCRIPT = ROOT / "scripts" / "lqe_io.py"
CHUNK_SCRIPT = ROOT / "scripts" / "lqe_chunk.py"
sys.path.insert(0, str(ROOT / "scripts"))

from lqe_terms import TermContractError, canonicalize_terms


def write_json(path: Path, value: object) -> None:
    if (
        path.name == "state.json"
        and isinstance(value, dict)
        and isinstance(value.get("segments"), list)
        and "job_runtime_contract_version" not in value
    ):
        value["job_runtime_contract_version"] = 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class TerminologyReadContractTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.source = self.root / "source.csv"
        self.source.write_text("Source,Target\nA,Alpha\n", encoding="utf-8")
        self.state = self.root / "job" / "state.json"

    def tearDown(self):
        self.tempdir.cleanup()

    def run_read(self, terminology: Path, *, profile: Path | None = None):
        command = [
            sys.executable,
            str(IO_SCRIPT),
            "read",
            "--input",
            str(self.source),
            "--source-col",
            "Source",
            "--target-col",
            "Target",
            "--out",
            str(self.state),
        ]
        if profile is None:
            command.extend(("--terminology", str(terminology)))
        else:
            command.extend(("--project", str(profile)))
        return subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def assert_no_job_artifacts(self):
        for name in ("state.json", "scope.json", "terms.json"):
            self.assertFalse((self.state.parent / name).exists(), name)

    def raw_files(self):
        csv_path = self.root / "terms.csv"
        csv_path.write_text(
            "Source,Target,Term Status\nA,Alpha,Approved\nB,Beta,Denied\n",
            encoding="utf-8",
        )
        xlsx_path = self.root / "terms.xlsx"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["Source", "Target", "Term Status"])
        sheet.append(["A", "Alpha", "Approved"])
        sheet.append(["B", "Beta", "Denied"])
        workbook.save(xlsx_path)
        workbook.close()
        json_path = self.root / "terms.json"
        write_json(
            json_path,
            [
                {"source": "A", "target": "Alpha", "status": "Approved"},
                {"source": "B", "target": "Beta", "status": "Denied"},
            ],
        )
        return csv_path, xlsx_path, json_path

    def canonical_status_files(self):
        headers = ["Source", "Target", "Term Status", "confirmed", "protected"]
        rows = [
            ["A", "Alpha", "Approved", False, True],
            ["B", "Beta", "Denied", True, False],
        ]
        csv_path = self.root / "canonical-status.csv"
        csv_path.write_text(
            "Source,Target,Term Status,confirmed,protected\n"
            "A,Alpha,Approved,false,true\n"
            "B,Beta,Denied,true,false\n",
            encoding="utf-8",
        )
        xlsx_path = self.root / "canonical-status.xlsx"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
        workbook.save(xlsx_path)
        workbook.close()
        json_path = self.root / "canonical-status.json"
        write_json(
            json_path,
            [
                {
                    "source": "A",
                    "target": "Alpha",
                    "status": "Approved",
                    "confirmed": False,
                    "protected": True,
                },
                {
                    "source": "B",
                    "target": "Beta",
                    "status": "Denied",
                    "confirmed": True,
                    "protected": False,
                },
            ],
        )
        return csv_path, xlsx_path, json_path

    def test_raw_terminology_without_confirmation_mapping_fails_closed(self):
        for terminology in self.raw_files():
            with self.subTest(suffix=terminology.suffix):
                result = self.run_read(terminology)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("confirmation mapping", result.stderr)
                self.assertIn("Approved", result.stderr)
                self.assert_no_job_artifacts()

    def test_split_cannot_bypass_canonical_terminology_contract(self):
        terminology = self.root / "raw-terms.json"
        write_json(
            terminology,
            [{"source": "A", "target": "Alpha", "status": "Approved"}],
        )
        job = self.root / "split-job"
        state = job / "state.json"
        errors = job / "errors_precheck.json"
        chunks = job / "chunks"
        write_json(
            state,
            {
                "source_lang": "en",
                "target_lang": "en",
                "segments": [{"id": 0, "source": "A", "target": "Alpha"}],
            },
        )
        write_json(errors, [{"id": 0, "issues": []}])

        result = subprocess.run(
            [
                sys.executable,
                str(CHUNK_SCRIPT),
                "split",
                "--state",
                str(state),
                "--errors",
                str(errors),
                "--terms",
                str(terminology),
                "--outdir",
                str(chunks),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("confirmation mapping", result.stderr)
        self.assertFalse((chunks / "chunk_00.json").exists())
        self.assertFalse((chunks / "split_manifest.json").exists())

    def test_profile_term_status_map_sets_flags_and_excludes_denied(self):
        project = self.root / "project"
        terms = project / "terms.json"
        write_json(
            terms,
            [
                {"source": "A", "target": "Alpha", "status": "Approved"},
                {"source": "B", "target": "Beta", "status": "Locked"},
                {"source": "C", "target": "Gamma", "status": "dEnIeD"},
            ],
        )
        profile = project / "profile.json"
        write_json(
            profile,
            {
                "name": "fixture/zh-en",
                "language_pair": "zh-en",
                "source_lang": "zh",
                "target_lang": "en",
                "terminology": "terms.json",
                "term_status_map": {
                    "Approved": "confirmed",
                    "Locked": {"confirmed": False, "protected": True},
                },
            },
        )

        result = self.run_read(terms, profile=profile)

        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(Path(state["terms_path"]), self.state.parent / "terms.json")
        loaded = json.loads(Path(state["terms_path"]).read_text(encoding="utf-8"))
        self.assertEqual(
            loaded,
            [
                {
                    "source": "A",
                    "target": "Alpha",
                    "status": "Approved",
                    "confirmed": True,
                    "protected": False,
                },
                {
                    "source": "B",
                    "target": "Beta",
                    "status": "Locked",
                    "confirmed": False,
                    "protected": True,
                },
            ],
        )

    def test_canonical_json_without_mapping_preserves_explicit_flags(self):
        terminology = self.root / "canonical.json"
        write_json(
            terminology,
            [
                {
                    "source": "A",
                    "target": "Alpha",
                    "confirmed": False,
                    "protected": True,
                }
            ],
        )

        result = self.run_read(terminology)

        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        loaded = json.loads(Path(state["terms_path"]).read_text(encoding="utf-8"))
        self.assertEqual(loaded[0]["confirmed"], False)
        self.assertEqual(loaded[0]["protected"], True)

    def test_status_bearing_canonical_files_do_not_require_mapping(self):
        original_state = self.state
        try:
            for index, terminology in enumerate(self.canonical_status_files()):
                with self.subTest(suffix=terminology.suffix):
                    self.state = self.root / f"canonical-job-{index}" / "state.json"

                    result = self.run_read(terminology)

                    self.assertEqual(result.returncode, 0, result.stderr)
                    state = json.loads(self.state.read_text(encoding="utf-8"))
                    loaded = json.loads(
                        Path(state["terms_path"]).read_text(encoding="utf-8")
                    )
                    self.assertEqual(
                        loaded,
                        [
                            {
                                "source": "A",
                                "target": "Alpha",
                                "status": "Approved",
                                "confirmed": False,
                                "protected": True,
                            }
                        ],
                    )
        finally:
            self.state = original_state

    def test_protected_statuses_requires_array_of_non_empty_strings(self):
        terms = [
            {
                "source": "A",
                "target": "Alpha",
                "status": "Approved",
                "confirmed": False,
                "protected": False,
            }
        ]
        invalid_values = (
            "Approved",
            ("Approved",),
            {"Approved": True},
            [""],
            [" \u200b "],
            [1],
            [None],
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaisesRegex(TermContractError, "protected_statuses"):
                    canonicalize_terms(terms, protected_statuses=value)

        loaded = canonicalize_terms(
            terms,
            protected_statuses=[" Approved "],
        )
        self.assertTrue(loaded[0]["protected"])

    def test_profile_rejects_invalid_protected_statuses_before_publication(self):
        invalid_values = ("Approved", [1], [""])
        for index, value in enumerate(invalid_values):
            with self.subTest(value=value):
                project = self.root / f"invalid-protected-statuses-{index}"
                terms = project / "terms.json"
                write_json(
                    terms,
                    [
                        {
                            "source": "A",
                            "target": "Alpha",
                            "status": "Approved",
                            "confirmed": False,
                            "protected": False,
                        }
                    ],
                )
                profile = project / "profile.json"
                write_json(
                    profile,
                    {
                        "name": "fixture/zh-en",
                        "language_pair": "zh-en",
                        "source_lang": "zh",
                        "target_lang": "en",
                        "terminology": "terms.json",
                        "protected_term_statuses": value,
                    },
                )

                result = self.run_read(terms, profile=profile)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("protected_term_statuses", result.stderr)
                self.assert_no_job_artifacts()

    def test_protected_statuses_alone_do_not_supply_confirmation_mapping(self):
        project = self.root / "project-protected"
        terms = project / "terms.json"
        write_json(terms, [{"source": "A", "target": "Alpha", "status": "Approved"}])
        profile = project / "profile.json"
        write_json(
            profile,
            {
                "name": "fixture/zh-en",
                "language_pair": "zh-en",
                "source_lang": "zh",
                "target_lang": "en",
                "terminology": "terms.json",
                "protected_term_statuses": ["Approved"],
            },
        )

        result = self.run_read(terms, profile=profile)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("confirmation mapping", result.stderr)
        self.assert_no_job_artifacts()

    def test_term_status_map_cannot_map_denied(self):
        project = self.root / "project-denied"
        terms = project / "terms.json"
        write_json(terms, [{"source": "A", "target": "Alpha", "status": "Denied"}])
        profile = project / "profile.json"
        write_json(
            profile,
            {
                "name": "fixture/zh-en",
                "language_pair": "zh-en",
                "source_lang": "zh",
                "target_lang": "en",
                "terminology": "terms.json",
                "term_status_map": {"Denied": "confirmed"},
            },
        )

        result = self.run_read(terms, profile=profile)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Denied must not be mapped", result.stderr)
        self.assert_no_job_artifacts()


if __name__ == "__main__":
    unittest.main()
