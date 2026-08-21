from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
USER_FACING = [
    "SKILL.md",
    "README.md",
    "README_ZH.md",
    "projects/nrc/common/confirmed_rules_common.md",
    "projects/nrc/zh-en/confirmed_rules.md",
    "projects/nrc/zh-en/checks.json",
    "projects/nrc/zh-th/confirmed_rules.md",
    "projects/nrc/zh-th/checks.json",
    "projects/wwm/zh-en/confirmed_rules.md",
    "projects/wwm/zh-en/checks.json",
    "projects/wwm/zh-en/lqa_notes.md",
    "references/check_modules",
    "scripts/finalize_job.sh",
    "scripts/mastertb_prep.py",
    "scripts/term_suggest.py",
    "scripts/tm_index.py",
]
CHECK_MODULE_NAMES = (
    "common.md",
    "precheck_review.md",
    "terminology.md",
    "accuracy.md",
    "grammar.md",
    "naturalness.md",
    "proper_names.md",
    "term_audit.md",
)
ENGLISH_BANNED = (
    "lens",
    "adjudication",
    "pending_adjudication",
    "correction_status",
    "corr_candidates",
    "locked_ids",
    "lock_statuses",
    "lock-segments",
    "tm_locked",
    "[locked]",
    "ai correction",
)
CHINESE_BANNED_CODEPOINTS = (
    (0x88C1, 0x51B3),
    (0x0041, 0x0049, 0x0020, 0x4FEE, 0x6B63),
    (0x4FEE, 0x6B63, 0x72B6, 0x6001),
    (0x900F, 0x955C),
    (0x9501, 0x5B9A, 0x6BB5),
)


def _visible_files():
    for relative in USER_FACING:
        path = ROOT / relative
        if path.is_dir():
            yield from sorted(path.rglob("*"))
        else:
            yield path


class PlainLanguageTests(unittest.TestCase):
    def test_user_facing_sources_use_plain_language(self):
        for path in _visible_files():
            self.assertTrue(path.is_file(), f"missing user-facing file: {path.relative_to(ROOT)}")
            text = path.read_text(encoding="utf-8").lower()
            for phrase in ENGLISH_BANNED:
                with self.subTest(path=path.relative_to(ROOT), phrase=phrase):
                    if phrase in text:
                        self.fail(f"{path.relative_to(ROOT)} contains banned phrase: {phrase}")
            for points in CHINESE_BANNED_CODEPOINTS:
                phrase = "".join(chr(point) for point in points)
                with self.subTest(path=path.relative_to(ROOT), codepoints=points):
                    if phrase in text:
                        self.fail(
                            f"{path.relative_to(ROOT)} contains banned codepoints: {points}"
                        )

    def test_check_modules_use_issue_edit_contract(self):
        module_dir = ROOT / "references/check_modules"
        actual = tuple(sorted(path.name for path in module_dir.glob("*.md"))) if module_dir.exists() else ()
        self.assertEqual(sorted(CHECK_MODULE_NAMES), sorted(actual))
        for name in CHECK_MODULE_NAMES:
            text = (module_dir / name).read_text(encoding="utf-8")
            with self.subTest(module=name):
                self.assertIn('"issues"', text)
                self.assertIn('"edit"', text)
                self.assertIn('"needs_confirmation"', text)
                self.assertNotIn('"corrected"', text)

    def test_runtime_references_have_no_legacy_docs_paths(self):
        self.assertFalse((ROOT / "docs/check_modules").exists())
        self.assertFalse((ROOT / "docs/lenses").exists())
        self.assertFalse((ROOT / "references/lenses").exists())

    def test_sdlxliff_boundaries_are_plain_and_visible(self):
        requirements = {
            "SKILL.md": (
                "未知厂商扩展",
                "corrected XML",
                "--input-format xliff",
            ),
        }
        for path, phrases in requirements.items():
            content = (ROOT / path).read_text(encoding="utf-8")
            for phrase in phrases:
                with self.subTest(path=path, phrase=phrase):
                    self.assertIn(phrase, content)


if __name__ == "__main__":
    unittest.main()
