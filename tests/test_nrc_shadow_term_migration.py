import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "projects" / "nrc" / "common" / "sources" / "build_shadow_assets.py"


def load_generator():
    spec = importlib.util.spec_from_file_location("nrc_shadow_assets_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NRCShadowTermMigrationTests(unittest.TestCase):
    def setUp(self):
        self.module = load_generator()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.nrc = Path(self.temp_dir.name) / "nrc"
        (self.nrc / "zh-en").mkdir(parents=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_terms(self, terms):
        (self.nrc / "zh-en" / "terms_en.json").write_text(
            json.dumps(terms, ensure_ascii=False), encoding="utf-8"
        )

    def test_preserves_explicit_flags_and_multisense_metadata(self):
        self.write_terms(
            [
                {"source": "旧词", "target": "Legacy", "status": "Approved"},
                {
                    "source": "新词",
                    "target": "Current",
                    "confirmed": True,
                    "protected": False,
                },
                {
                    "source": "多义词",
                    "senses": [
                        {
                            "target": "Sense A",
                            "confirmed": True,
                            "protected": False,
                            "category": "Species",
                        },
                        {
                            "target": "Sense B",
                            "confirmed": True,
                            "protected": False,
                            "definition": "Named individual",
                        },
                    ],
                },
                {"source": "拒绝词", "target": "Rejected", "status": "Denied"},
            ]
        )
        with mock.patch.object(self.module, "NRC", self.nrc):
            output, coverage = self.module.build_en_terms()

        self.assertEqual(len(output), 3)
        self.assertEqual(output[0]["confirmed"], True)
        self.assertEqual(output[0]["protected"], False)
        self.assertNotIn("status", output[1])
        self.assertEqual(output[2]["senses"][0]["category"], "Species")
        self.assertEqual(
            output[2]["senses"][1]["definition"], "Named individual"
        )
        self.assertEqual(coverage[-1]["status"], "ignored")

    def test_explicit_confirmation_overrides_legacy_status(self):
        self.write_terms(
            [
                {
                    "source": "冲突词",
                    "target": "Conflict",
                    "status": "New",
                    "confirmed": True,
                    "protected": False,
                }
            ]
        )
        with mock.patch.object(self.module, "NRC", self.nrc):
            output, _ = self.module.build_en_terms()

        self.assertTrue(output[0]["confirmed"])
        self.assertEqual(output[0]["status"], "New")


if __name__ == "__main__":
    unittest.main()
