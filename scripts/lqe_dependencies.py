"""Runtime dependency checks that do not import third-party packages."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


REQUIRED_DEPENDENCIES = (
    ("httpx", "httpx>=0.28,<1"),
    ("jsonschema", "jsonschema>=4.20,<5"),
    ("openpyxl", "openpyxl>=3.1,<4"),
    ("docx", "python-docx>=1.1,<2"),
    ("regex", "regex>=2024.5"),
    ("xlrd", "xlrd>=2.0,<3"),
)


def requirements_path() -> Path:
    return Path(__file__).resolve().parents[1] / "requirements.txt"


def dependency_report() -> dict:
    dependencies = []
    for module, requirement in REQUIRED_DEPENDENCIES:
        try:
            spec = importlib.util.find_spec(module)
        except (ImportError, ModuleNotFoundError, ValueError) as exc:
            spec = None
            error = f"{type(exc).__name__}: {exc}"
        else:
            error = None
        dependencies.append(
            {
                "module": module,
                "requirement": requirement,
                "available": spec is not None,
                "origin": getattr(spec, "origin", None) if spec else None,
                "error": error,
            }
        )
    return {
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "requirements": str(requirements_path()),
        "dependencies": dependencies,
        "ok": all(item["available"] for item in dependencies),
    }


def format_dependency_error(report: dict) -> str:
    missing = [
        f"{item['module']} ({item['requirement']})"
        for item in report["dependencies"]
        if not item["available"]
    ]
    install = (
        f'"{report["python"]}" -m pip install '
        f'-r "{report["requirements"]}"'
    )
    return "\n".join(
        [
            "[lqe dependencies] dependency check failed",
            f"Python: {report['python']} ({report['python_version']})",
            "Missing: " + ", ".join(missing),
            f"Install with: {install}",
            "Packages installed in another Python interpreter are not visible to this command.",
        ]
    )


def require_runtime_dependencies() -> dict:
    report = dependency_report()
    if not report["ok"]:
        raise SystemExit(format_dependency_error(report))
    return report


def main() -> int:
    report = dependency_report()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        print(format_dependency_error(report), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
