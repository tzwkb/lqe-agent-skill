from pathlib import Path
from typing import Literal


InputFormat = Literal["tabular", "sdlxliff"]

_SDLXLIFF_SUFFIX = ".sdlxliff"
_XLIFF_SUFFIXES = {_SDLXLIFF_SUFFIX, ".xliff", ".xlf"}
_TABULAR_SUFFIXES = {".csv", ".tsv", ".xls", ".xlsx", ".xlsm"}
_SUPPORTED_SUFFIXES = {*_XLIFF_SUFFIXES, *_TABULAR_SUFFIXES}


def _file_format(path: Path) -> InputFormat | None:
    suffix = path.suffix.casefold()
    if suffix in _XLIFF_SUFFIXES:
        return "sdlxliff"
    if suffix in _TABULAR_SUFFIXES:
        return "tabular"
    return None


def detect_input_format(path: Path, requested: str) -> InputFormat:
    path = Path(path)
    if requested not in {"auto", "tabular", "sdlxliff", "xliff"}:
        raise ValueError(
            "requested input format must be auto, tabular, sdlxliff, or "
            f"xliff: {requested!r}"
        )
    normalized_request = "sdlxliff" if requested == "xliff" else requested
    if not path.exists():
        raise ValueError(f"input path does not exist: {path}")

    if path.is_file():
        detected = _file_format(path)
        if detected is None:
            raise ValueError(f"unsupported input file suffix: {path.suffix or '<none>'}")
        if normalized_request != "auto" and normalized_request != detected:
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

    if normalized_request == "tabular":
        raise ValueError("tabular directories are not supported")
    if normalized_request == "sdlxliff":
        if not sdl_files:
            raise ValueError(f"no XLIFF files found in directory: {path}")
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
    render_xliff_writeback,
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
    "render_xliff_writeback",
    "read_xls",
    "workbook_for_corrected_export",
    "serialize_mixed",
]
