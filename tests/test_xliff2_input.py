import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXTURE = ROOT / "tests" / "fixtures" / "xliff2" / "basic.xliff"
XLIFF2_NS = "urn:oasis:names:tc:xliff:document:2.0"
X2 = "{" + XLIFF2_NS + "}"

sys.path.insert(0, str(SCRIPTS))

from lqe_inputs import detect_input_format
from lqe_inputs.sdlxliff import (
    SDLXLIFFImportError,
    SDLXLIFFOptions,
    read_sdlxliff,
    render_xliff_writeback,
)


class XLIFF2InputTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.input = self.root / "basic.xliff"
        shutil.copy2(FIXTURE, self.input)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_auto_detection_and_read(self):
        self.assertEqual(detect_input_format(self.input, "auto"), "sdlxliff")
        self.assertEqual(detect_input_format(self.input, "xliff"), "sdlxliff")
        result = read_sdlxliff(self.input, options=SDLXLIFFOptions())
        self.assertEqual(result.source_lang, "en")
        self.assertEqual(result.target_lang, "fr")
        self.assertEqual(result.manifest["input_format"], "xliff")
        self.assertEqual(result.manifest["importer"]["name"], "xliff2")
        self.assertEqual(len(result.segments), 3)
        self.assertEqual(result.segments[0]["source_plain"], "Hello !")
        self.assertEqual(
            result.segments[0]["metadata"]["sdlxliff"]["comment"],
            "Greeting shown at login.",
        )
        self.assertTrue(result.segments[1]["protected"])
        self.assertEqual(
            result.segments[1]["protected_reason"], "SOURCE_LOCKED"
        )
        self.assertEqual(result.segments[2]["target"], "")

    def test_xml_writeback_updates_and_creates_targets(self):
        result = read_sdlxliff(self.input, options=SDLXLIFFOptions())
        outputs = render_xliff_writeback(
            self.input,
            segments=result.segments,
            corrections={
                0: 'Salut <ph id="1"/> !',
                2: "Cible ajoutée",
            },
        )
        self.assertEqual([item[0] for item in outputs], ["basic.xliff"])
        root = ET.fromstring(outputs[0][1])
        segments = root.findall(f".//{X2}segment")
        first_target = segments[0].find(f"{X2}target")
        self.assertIsNotNone(first_target)
        self.assertEqual("".join(first_target.itertext()), "Salut  !")
        self.assertIsNotNone(first_target.find(f"{X2}ph"))
        self.assertEqual(
            "".join(segments[1].find(f"{X2}target").itertext()),
            "Texte verrouillé",
        )
        self.assertEqual(
            "".join(segments[2].find(f"{X2}target").itertext()),
            "Cible ajoutée",
        )
        self.assertIsNotNone(root.find(".//{https://example.com/xliff/ext}quality"))

    def test_xml_writeback_rejects_stale_source_references(self):
        result = read_sdlxliff(self.input, options=SDLXLIFFOptions())
        result.segments[0]["source_ref"]["relative_path"] = "missing.xliff"

        with self.assertRaisesRegex(
            SDLXLIFFImportError, "source paths are not in the input"
        ):
            render_xliff_writeback(
                self.input,
                segments=result.segments,
                corrections={0: "Salut"},
            )

    def test_lqe_read_accepts_xliff_alias(self):
        state_path = self.root / "job" / "state.json"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "lqe_io.py"),
                "read",
                "--input",
                str(self.input),
                "--input-format",
                "xliff",
                "--no-terminology",
                "--review-mode",
                "full",
                "--out",
                str(state_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["input_format"], "sdlxliff")
        self.assertEqual(state["xml_input_format"], "xliff")
        self.assertEqual(
            state["segments"][0]["source_provenance"]["adapter"],
            "xliff2@1",
        )

        source_before = self.input.read_bytes()
        export = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "lqe_io.py"),
                "export",
                "--state",
                str(state_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(export.returncode, 0, export.stderr)
        self.assertTrue((state_path.parent / "job_corrected.xlsx").is_file())
        corrected_xml = state_path.parent / "job_corrected.xliff"
        self.assertTrue(corrected_xml.is_file())
        self.assertEqual(self.input.read_bytes(), source_before)
        self.assertEqual(ET.parse(corrected_xml).getroot().tag, X2 + "xliff")

    def test_missing_trg_lang_requires_and_accepts_explicit_target(self):
        no_target_lang = self.root / "no-target-lang.xliff"
        no_target_lang.write_text(
            FIXTURE.read_text(encoding="utf-8").replace(' trgLang="fr"', ""),
            encoding="utf-8",
        )
        failed_state = self.root / "failed" / "state.json"
        failed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "lqe_io.py"),
                "read",
                "--input",
                str(no_target_lang),
                "--input-format",
                "xliff",
                "--no-terminology",
                "--review-mode",
                "full",
                "--out",
                str(failed_state),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("requires profile or CLI target language", failed.stderr)
        self.assertFalse(failed_state.exists())

        state_path = self.root / "explicit-target" / "state.json"
        accepted = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "lqe_io.py"),
                "read",
                "--input",
                str(no_target_lang),
                "--input-format",
                "xliff",
                "--target-lang",
                "fr",
                "--no-terminology",
                "--review-mode",
                "full",
                "--out",
                str(state_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(
            json.loads(state_path.read_text(encoding="utf-8"))["target_lang"],
            "fr",
        )


if __name__ == "__main__":
    unittest.main()
