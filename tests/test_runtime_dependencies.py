import json
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lqe_dependencies import (  # noqa: E402
    dependency_report,
    format_dependency_error,
    requirements_path,
)


class RuntimeDependencyTests(unittest.TestCase):
    def test_dependency_report_matches_repository_requirements(self):
        report = dependency_report()
        self.assertEqual(Path(report["requirements"]), requirements_path())
        declared = {
            line.split("=", 1)[0].split("<", 1)[0].split(">", 1)[0].strip()
            for line in requirements_path().read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        expected = {item["requirement"].split(">=", 1)[0].split("<", 1)[0] for item in report["dependencies"]}
        self.assertEqual(expected, declared)

    def test_missing_dependency_message_names_interpreter_and_install_command(self):
        report = dependency_report()
        with mock.patch(
            "lqe_dependencies.dependency_report",
            return_value={
                **report,
                "ok": False,
                "dependencies": [
                    {
                        "module": "regex",
                        "requirement": "regex>=2024.5",
                        "available": False,
                        "origin": None,
                        "error": None,
                    }
                ],
            },
        ):
            from lqe_dependencies import require_runtime_dependencies

            with self.assertRaises(SystemExit) as raised:
                require_runtime_dependencies()
        message = str(raised.exception)
        self.assertIn("regex", message)
        self.assertIn(sys.executable, message)
        self.assertIn("-m pip install", message)

    def test_report_is_json_serializable(self):
        json.dumps(dependency_report(), ensure_ascii=False)
