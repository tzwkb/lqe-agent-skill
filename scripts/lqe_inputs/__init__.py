from pathlib import Path
from typing import Literal


InputFormat = Literal["tabular", "sdlxliff"]

_SDLXLIFF_SUFFIX = ".sdlxliff"
_TABULAR_SUFFIXES = {".csv", ".tsv", ".xls", ".xlsx", ".xlsm"}
_SUPPORTED_SUFFIXES = {_SDLXLIFF_SUFFIX, *_TABULAR_SUFFIXES}


def _file_format(path: Path) -> InputFormat | None:
    suffix = path.suffix.casefold()
    if suffix == _SDLXLIFF_SUFFIX:
        return "sdlxliff"
    if suffix in _TABULAR_SUFFIXES:
        return "tabular"
    return None


def detect_input_format(path: Path, requested: str) -> InputFormat:
    path = Path(path)
    if requested not in {"auto", "tabular", "sdlxliff"}:
        raise ValueError(
            f"requested input format must be auto, tabular, or sdlxliff: {requested!r}"
        )
    if not path.exists():
        raise ValueError(f"input path does not exist: {path}")

    if path.is_file():
        detected = _file_format(path)
        if detected is None:
            raise ValueError(f"unsupported input file suffix: {path.suffix or '<none>'}")
        if requested != "auto" and requested != detected:
            raise ValueError(
                f"requested format {requested!r} does not match {path.name!r}"
            )
        return detected

    supported = sorted(
        (
            candidate
            for candidate in path.rglob("*")
            if candidate.is_file()
            and candidate.suffix.casefold() in _SUPPORTED_SUFFIXES
        ),
        key=lambda candidate: candidate.relative_to(path).as_posix(),
    )
    sdl_files = [candidate for candidate in supported if _file_format(candidate) == "sdlxliff"]
    tabular_files = [candidate for candidate in supported if _file_format(candidate) == "tabular"]

    if requested == "tabular":
        raise ValueError("tabular directories are not supported")
    if requested == "sdlxliff":
        if not sdl_files:
            raise ValueError(f"no SDLXLIFF files found in directory: {path}")
        return "sdlxliff"
    if not supported:
        raise ValueError(f"no supported input files found in directory: {path}")
    if sdl_files and tabular_files:
        raise ValueError(f"mixed supported input formats in directory: {path}")
    if sdl_files:
        return "sdlxliff"
    raise ValueError("tabular directories are not supported")


from .sdlxliff import (
    SDLXLIFFImportError,
    SDLXLIFFImportResult,
    SDLXLIFFOptions,
    SerializedMixedContent,
    read_sdlxliff,
    serialize_mixed,
)
from .xls import (
    XLSImportError,
    XLSImportResult,
    read_xls,
    workbook_for_corrected_export,
)


__all__ = [
    "SDLXLIFFImportError",
    "SDLXLIFFImportResult",
    "SDLXLIFFOptions",
    "SerializedMixedContent",
    "XLSImportError",
    "XLSImportResult",
    "detect_input_format",
    "read_sdlxliff",
    "read_xls",
    "workbook_for_corrected_export",
    "serialize_mixed",
]
