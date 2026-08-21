from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fnmatch import fnmatchcase
import hashlib
from html import escape
from io import BytesIO
import math
from pathlib import Path
import re
from xml.etree import ElementTree as ET
from xml.parsers import expat


XLIFF_NS = "urn:oasis:names:tc:xliff:document:1.2"
XLIFF2_NS = "urn:oasis:names:tc:xliff:document:2.0"
SDL_NS = "http://sdl.com/FileTypes/SdlXliff/1.0"
XML_NS = "http://www.w3.org/XML/1998/namespace"

_X = "{" + XLIFF_NS + "}"
_X2 = "{" + XLIFF2_NS + "}"
_SDL = "{" + SDL_NS + "}"
_INLINE_NAMES = {"g", "x", "bx", "ex", "ph", "bpt", "ept", "it", "sub", "mrk"}
_NATIVE_CODE_NAMES = {"bpt", "ept", "it", "ph"}
_XLIFF2_INLINE_NAMES = {"cp", "ec", "em", "mrk", "pc", "ph", "sc", "sm"}
_XLIFF2_NATIVE_CODE_NAMES = {"cp", "ec", "em", "ph", "sc", "sm"}
_TABULAR_SUFFIXES = {".csv", ".tsv", ".xlsx", ".xlsm"}
_XLIFF_SUFFIXES = {".sdlxliff", ".xliff", ".xlf"}
_TRUE_VALUES = {"1", "true", "yes"}
_TM_POLICIES = {"candidate-only", "protect-exact-source-and-target"}
_OPTION_KEYS = {"tm_protection", "content_type_rules", "exclude_rules"}
_CONTENT_RULE_KEYS = {"id", "glob", "content_type"}
_EXCLUDE_RULE_KEYS = {"id", "field", "equals", "regex", "reason", "glob"}
_EXCLUDE_FIELDS = {
    "relative_path",
    "file_original",
    "confirmation",
    "origin",
    "locked",
    "source",
    "target",
}
_RESERVED_RULE_IDS = {"blank-both-sides"}
_MANIFEST_SCHEMA = "lqe.sdlxliff.import-manifest"
_MANIFEST_VERSION = 1


class SDLXLIFFImportError(ValueError):
    pass


@dataclass(frozen=True)
class SDLXLIFFOptions:
    tm_protection: str = "candidate-only"
    content_type_rules: tuple[dict, ...] = ()
    exclude_rules: tuple[dict, ...] = ()


@dataclass(frozen=True)
class SerializedMixedContent:
    display: str
    plain: str
    raw_xml: str
    tag_signature: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]


@dataclass
class SDLXLIFFImportResult:
    headers: list[str]
    rows_raw: list[list[str]]
    segments: list[dict]
    source_lang: str
    target_lang: str
    input_paths: list[str]
    manifest: dict
    tm_candidates: dict


def _nonempty_string(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SDLXLIFFImportError(f"{location} must be a non-empty string")
    return value


def _validate_glob(value: object, location: str) -> str:
    pattern = _nonempty_string(value, location)
    if "\x00" in pattern or "\\" in pattern or pattern.startswith("/"):
        raise SDLXLIFFImportError(f"{location} is not a valid POSIX relative glob")
    parts = pattern.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise SDLXLIFFImportError(f"{location} is not a valid POSIX relative glob")
    for part in parts:
        index = 0
        while index < len(part):
            if part[index] == "]":
                raise SDLXLIFFImportError(f"{location} contains a malformed glob")
            if part[index] != "[":
                index += 1
                continue
            end = part.find("]", index + 1)
            if end < 0 or end == index + 1 or (
                part[index + 1] in {"!", "^"} and end == index + 2
            ):
                raise SDLXLIFFImportError(f"{location} contains a malformed glob")
            index = end + 1
    return pattern


def _rule_sequence(value: object, location: str) -> Sequence:
    if not isinstance(value, (list, tuple)):
        raise SDLXLIFFImportError(f"{location} must be an array")
    return value


def _rule_mapping(value: object, location: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise SDLXLIFFImportError(f"{location} rule must be an object")
    return value


def validate_options(
    raw: object,
    *,
    cli_protect_exact_tm: bool = False,
) -> SDLXLIFFOptions:
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise SDLXLIFFImportError("SDLXLIFF options must be an object")
    if not isinstance(cli_protect_exact_tm, bool):
        raise SDLXLIFFImportError("cli_protect_exact_tm must be a boolean")
    unknown = sorted(set(raw) - _OPTION_KEYS)
    if unknown:
        raise SDLXLIFFImportError(f"unknown SDLXLIFF option: {unknown[0]}")

    policy = raw.get("tm_protection", "candidate-only")
    if not isinstance(policy, str) or policy not in _TM_POLICIES:
        raise SDLXLIFFImportError(f"unsupported tm_protection: {policy!r}")

    seen_ids: set[str] = set()
    content_rules: list[dict] = []
    for index, value in enumerate(
        _rule_sequence(raw.get("content_type_rules", ()), "content_type_rules")
    ):
        location = f"content_type_rules[{index}]"
        rule = _rule_mapping(value, location)
        unknown = sorted(set(rule) - _CONTENT_RULE_KEYS)
        missing = sorted(_CONTENT_RULE_KEYS - set(rule))
        if unknown:
            raise SDLXLIFFImportError(f"{location} has unknown key {unknown[0]!r}")
        if missing:
            raise SDLXLIFFImportError(f"{location} is missing {missing[0]!r}")
        rule_id = _nonempty_string(rule["id"], f"{location}.id")
        if rule_id in _RESERVED_RULE_IDS:
            raise SDLXLIFFImportError(f"reserved rule id {rule_id!r}")
        if rule_id in seen_ids:
            raise SDLXLIFFImportError(f"duplicate rule id {rule_id!r}")
        seen_ids.add(rule_id)
        content_rules.append(
            {
                "id": rule_id,
                "glob": _validate_glob(rule["glob"], f"{location}.glob"),
                "content_type": _nonempty_string(
                    rule["content_type"], f"{location}.content_type"
                ),
            }
        )

    exclude_rules: list[dict] = []
    for index, value in enumerate(
        _rule_sequence(raw.get("exclude_rules", ()), "exclude_rules")
    ):
        location = f"exclude_rules[{index}]"
        rule = _rule_mapping(value, location)
        unknown = sorted(set(rule) - _EXCLUDE_RULE_KEYS)
        if unknown:
            raise SDLXLIFFImportError(f"{location} has unknown key {unknown[0]!r}")
        required = {"id", "field", "reason"}
        missing = sorted(required - set(rule))
        if missing:
            raise SDLXLIFFImportError(f"{location} is missing {missing[0]!r}")
        rule_id = _nonempty_string(rule["id"], f"{location}.id")
        if rule_id in _RESERVED_RULE_IDS:
            raise SDLXLIFFImportError(f"reserved rule id {rule_id!r}")
        if rule_id in seen_ids:
            raise SDLXLIFFImportError(f"duplicate rule id {rule_id!r}")
        seen_ids.add(rule_id)
        field = _nonempty_string(rule["field"], f"{location}.field")
        if field not in _EXCLUDE_FIELDS:
            raise SDLXLIFFImportError(f"unsupported exclude field {field!r}")
        matcher_keys = [key for key in ("equals", "regex") if key in rule]
        if len(matcher_keys) != 1:
            raise SDLXLIFFImportError(
                f"{location} must contain exactly one of equals or regex"
            )
        matcher_key = matcher_keys[0]
        matcher_value = rule[matcher_key]
        if matcher_key == "equals":
            if not isinstance(matcher_value, (str, int, float, bool, type(None))):
                raise SDLXLIFFImportError(f"{location}.equals must be a scalar")
            if isinstance(matcher_value, float) and not math.isfinite(matcher_value):
                raise SDLXLIFFImportError(f"{location}.equals must be finite")
        else:
            if not isinstance(matcher_value, str):
                raise SDLXLIFFImportError(f"{location}.regex must be a string")
            try:
                re.compile(matcher_value)
            except re.error as exc:
                raise SDLXLIFFImportError(
                    f"{location}.regex is invalid: {exc}"
                ) from exc
        normalized = {
            "id": rule_id,
            "field": field,
            matcher_key: matcher_value,
            "reason": _nonempty_string(rule["reason"], f"{location}.reason"),
        }
        if "glob" in rule:
            normalized["glob"] = _validate_glob(rule["glob"], f"{location}.glob")
        exclude_rules.append(normalized)

    if cli_protect_exact_tm:
        policy = "protect-exact-source-and-target"
    return SDLXLIFFOptions(
        tm_protection=policy,
        content_type_rules=tuple(content_rules),
        exclude_rules=tuple(exclude_rules),
    )


def _glob_matches(relative_path: str, pattern: str) -> bool:
    path_parts = tuple(part for part in relative_path.split("/") if part not in {"", "."})
    pattern_parts = tuple(pattern.split("/"))

    def match(pattern_index: int, path_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        current = pattern_parts[pattern_index]
        if current == "**":
            return any(
                match(pattern_index + 1, candidate)
                for candidate in range(path_index, len(path_parts) + 1)
            )
        return (
            path_index < len(path_parts)
            and fnmatchcase(path_parts[path_index], current)
            and match(pattern_index + 1, path_index + 1)
        )

    return match(0, 0)


def match_content_type(
    relative_path: str,
    rules: tuple[dict, ...],
) -> tuple[str | None, str | None]:
    for rule in rules:
        if _glob_matches(relative_path, rule["glob"]):
            return rule["content_type"], rule["id"]
    return None, None


def _regex_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def match_exclusions(candidate: dict, rules: tuple[dict, ...]) -> list[dict]:
    matches: list[dict] = []
    for rule in rules:
        relative_path = candidate.get("relative_path")
        if "glob" in rule and (
            not isinstance(relative_path, str)
            or not _glob_matches(relative_path, rule["glob"])
        ):
            continue
        actual = candidate.get(rule["field"])
        if "equals" in rule:
            matched = actual == rule["equals"]
            operator = "equals"
            expected = rule["equals"]
        else:
            text = _regex_value(actual)
            matched = text is not None and re.search(rule["regex"], text) is not None
            operator = "regex"
            expected = rule["regex"]
        if matched:
            matches.append(
                {
                    "id": rule["id"],
                    "reason": rule["reason"],
                    "field": rule["field"],
                    "operator": operator,
                    "expected": expected,
                    "actual": actual,
                }
            )
    return matches


def _tm_evidence(metadata: dict) -> dict:
    origin = metadata.get("origin")
    percent = metadata.get("match_percent", metadata.get("percent"))
    text_match = metadata.get("text_match", metadata.get("text-match"))
    origin_is_tm = isinstance(origin, str) and origin.strip().casefold() == "tm"
    text_match_is_source_and_target = (
        isinstance(text_match, str)
        and text_match.strip().casefold() == "sourceandtarget"
    )
    percent_is_100 = False
    if not isinstance(percent, bool) and percent is not None:
        try:
            parsed = Decimal(str(percent).strip())
            percent_is_100 = parsed.is_finite() and parsed == Decimal("100")
        except (InvalidOperation, ValueError):
            pass
    return {
        "origin": origin,
        "match_percent": percent,
        "text_match": text_match,
        "origin_is_tm": origin_is_tm,
        "percent_is_100": percent_is_100,
        "text_match_is_source_and_target": text_match_is_source_and_target,
    }


def is_exact_tm(metadata: dict) -> bool:
    if not isinstance(metadata, dict):
        return False
    evidence = _tm_evidence(metadata)
    return all(
        evidence[key]
        for key in (
            "origin_is_tm",
            "percent_is_100",
            "text_match_is_source_and_target",
        )
    )


def _split_qname(value: str) -> tuple[str | None, str]:
    if value.startswith("{") and "}" in value:
        namespace, local = value[1:].split("}", 1)
        return namespace, local
    return None, value


class _QNameFormatter:
    def __init__(self, namespace_map: dict[str, str]):
        self._prefixes_by_uri: dict[str, list[str]] = {}
        for prefix, uri in namespace_map.items():
            self._prefixes_by_uri.setdefault(uri, []).append(prefix or "")
        for prefixes in self._prefixes_by_uri.values():
            prefixes.sort(key=lambda prefix: (prefix != "", prefix))
        self._assigned: dict[tuple[str, bool], str] = {}
        self._used_prefixes = set(namespace_map)
        self.declarations: dict[str, str] = {}
        self._fallback_index = 0

    def _fallback(self) -> str:
        while True:
            prefix = f"ns{self._fallback_index}"
            self._fallback_index += 1
            if prefix not in self._used_prefixes:
                self._used_prefixes.add(prefix)
                return prefix

    def _prefix(self, uri: str, *, attribute: bool) -> str:
        key = (uri, attribute)
        if key in self._assigned:
            return self._assigned[key]
        if uri == XML_NS:
            prefix = "xml"
        else:
            candidates = self._prefixes_by_uri.get(uri, [])
            if attribute:
                prefix = next((candidate for candidate in candidates if candidate), "")
            else:
                prefix = candidates[0] if candidates else ""
            if not prefix and (attribute or not candidates):
                prefix = self._fallback()
            if not attribute and uri == XLIFF_NS and "" in candidates:
                prefix = ""
        self._assigned[key] = prefix
        if prefix != "xml":
            existing = self.declarations.get(prefix)
            if existing is not None and existing != uri:
                prefix = self._fallback()
                self._assigned[key] = prefix
            self.declarations[prefix] = uri
        return prefix

    def qname(self, value: str, *, attribute: bool = False) -> str:
        uri, local = _split_qname(value)
        if uri is None:
            return local
        prefix = self._prefix(uri, attribute=attribute)
        return f"{prefix}:{local}" if prefix else local

    def scan(self, element: ET.Element) -> None:
        self.qname(element.tag)
        for name in sorted(element.attrib):
            self.qname(name, attribute=True)
        for child in element:
            self.scan(child)


def _render_element(
    element: ET.Element,
    formatter: _QNameFormatter,
    *,
    declare_root: bool,
) -> str:
    if declare_root:
        formatter.scan(element)
    name = formatter.qname(element.tag)
    pieces = ["<", name]
    if declare_root:
        for prefix, uri in sorted(
            formatter.declarations.items(), key=lambda item: (item[0] != "", item[0])
        ):
            declaration = "xmlns" if not prefix else f"xmlns:{prefix}"
            pieces.extend([" ", declaration, '="', escape(uri, quote=True), '"'])
    for attr_name, value in sorted(element.attrib.items(), key=lambda item: item[0]):
        pieces.extend(
            [
                " ",
                formatter.qname(attr_name, attribute=True),
                '="',
                escape(str(value), quote=True),
                '"',
            ]
        )
    if len(element) == 0 and not element.text:
        pieces.append("/>")
    else:
        pieces.append(">")
        if element.text:
            pieces.append(escape(element.text, quote=False))
        for child in element:
            pieces.append(_render_element(child, formatter, declare_root=False))
        pieces.extend(["</", name, ">"])
    if element.tail:
        pieces.append(escape(element.tail, quote=False))
    return "".join(pieces)


def _content_xml(element: ET.Element, namespace_map: dict[str, str], *, raw: bool) -> str:
    pieces = [escape(element.text, quote=False) if element.text else ""]
    if raw:
        for child in element:
            formatter = _QNameFormatter(namespace_map)
            pieces.append(_render_element(child, formatter, declare_root=True))
    else:
        formatter = _QNameFormatter(namespace_map)
        for child in element:
            pieces.append(_render_element(child, formatter, declare_root=False))
    return "".join(pieces)


def _tag_signature(
    element: ET.Element,
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    signature: list[tuple[str, tuple[tuple[str, str], ...]]] = []

    def visit(parent: ET.Element) -> None:
        for child in parent:
            namespace, local = _split_qname(child.tag)
            if namespace == XLIFF_NS and local in _INLINE_NAMES:
                signature.append(
                    (
                        child.tag,
                        tuple(
                            (name, str(value))
                            for name, value in sorted(
                                child.attrib.items(), key=lambda item: item[0]
                            )
                        ),
                    )
                )
            visit(child)

    visit(element)
    return tuple(signature)


def _plain_text(element: ET.Element) -> str:
    pieces: list[str] = []

    def visit(parent: ET.Element, translatable: bool) -> None:
        if translatable and parent.text:
            pieces.append(parent.text)
        for child in parent:
            namespace, local = _split_qname(child.tag)
            child_translatable = (
                True
                if namespace == XLIFF_NS and local == "sub"
                else translatable
                and not (namespace == XLIFF_NS and local in _NATIVE_CODE_NAMES)
            )
            visit(child, child_translatable)
            if translatable and child.tail:
                pieces.append(child.tail)

    visit(element, True)
    return "".join(pieces)


def serialize_mixed(
    element: ET.Element,
    namespace_map: dict[str, str],
) -> SerializedMixedContent:
    return SerializedMixedContent(
        display=_content_xml(element, namespace_map, raw=False),
        plain=_plain_text(element),
        raw_xml=_content_xml(element, namespace_map, raw=True),
        tag_signature=_tag_signature(element),
    )


def _serialize_full(element: ET.Element, namespace_map: dict[str, str]) -> str:
    formatter = _QNameFormatter(namespace_map)
    return _render_element(element, formatter, declare_root=True)


def _context(
    relative_path: str,
    *,
    file_index: int | None = None,
    tu_id: str | None = None,
    tu_index: int | None = None,
    segment_id: str | None = None,
    segment_index: int | None = None,
    element: ET.Element | None = None,
) -> str:
    parts = [relative_path]
    if file_index is not None:
        parts.append(f"file[{file_index}]")
    if tu_index is not None:
        parts.append(f"TU {tu_id!r} (index {tu_index})")
    if segment_index is not None:
        parts.append(f"segment {segment_id!r} (index {segment_index})")
    line = getattr(element, "sourceline", None) if element is not None else None
    if line is not None:
        parts.append(f"line {line}")
    return ", ".join(parts)


def _fail(message: str, context: str) -> None:
    raise SDLXLIFFImportError(f"{context}: {message}")


def _reject_dtd_and_entities(data: bytes, relative_path: str) -> None:
    parser = expat.ParserCreate()

    def reject(*_args) -> None:
        raise SDLXLIFFImportError(
            f"{relative_path}: line {parser.CurrentLineNumber}: "
            "DOCTYPE/entity declarations are not allowed"
        )

    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise SDLXLIFFImportError(
            f"{relative_path}: line {exc.lineno}, column {exc.offset}: "
            f"invalid XML: {exc}"
        ) from exc


def _parse_xml(
    data: bytes,
    relative_path: str,
) -> tuple[ET.Element, dict[str, str], list[tuple[str, str]]]:
    _reject_dtd_and_entities(data, relative_path)
    namespace_map: dict[str, str] = {}
    namespace_declarations: list[tuple[str, str]] = []
    try:
        parser = ET.iterparse(BytesIO(data), events=("start-ns", "end"))
        for event, value in parser:
            if event == "start-ns":
                prefix, uri = value
                normalized_prefix = prefix or ""
                namespace_map.setdefault(normalized_prefix, uri)
                namespace_declarations.append((normalized_prefix, uri))
        root = parser.root
    except ET.ParseError as exc:
        line, column = getattr(exc, "position", (None, None))
        location = (
            f"line {line}, column {column}" if line is not None else "unknown location"
        )
        raise SDLXLIFFImportError(
            f"{relative_path}: {location}: invalid XML: {exc}"
        ) from exc
    if root.tag != _X + "xliff" or root.get("version") != "1.2":
        _fail("expected XLIFF 1.2 root namespace and version", relative_path)
    if SDL_NS not in {uri for _, uri in namespace_declarations}:
        _fail(f"supported SDL namespace {SDL_NS!r} is required", relative_path)
    return root, namespace_map, namespace_declarations


def _parse_xliff2_xml(
    data: bytes,
    relative_path: str,
) -> tuple[ET.Element, dict[str, str], list[tuple[str, str]]]:
    _reject_dtd_and_entities(data, relative_path)
    namespace_map: dict[str, str] = {}
    namespace_declarations: list[tuple[str, str]] = []
    try:
        parser = ET.iterparse(BytesIO(data), events=("start-ns", "end"))
        for event, value in parser:
            if event == "start-ns":
                prefix, uri = value
                normalized_prefix = prefix or ""
                namespace_map.setdefault(normalized_prefix, uri)
                namespace_declarations.append((normalized_prefix, uri))
        root = parser.root
    except ET.ParseError as exc:
        line, column = getattr(exc, "position", (None, None))
        location = (
            f"line {line}, column {column}"
            if line is not None
            else "unknown location"
        )
        raise SDLXLIFFImportError(
            f"{relative_path}: {location}: invalid XML: {exc}"
        ) from exc
    if root.tag != _X2 + "xliff" or root.get("version") != "2.0":
        _fail("expected XLIFF 2.0 root namespace and version", relative_path)
    return root, namespace_map, namespace_declarations


def _document_kind(data: bytes, relative_path: str) -> str:
    _reject_dtd_and_entities(data, relative_path)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        line, column = getattr(exc, "position", (None, None))
        raise SDLXLIFFImportError(
            f"{relative_path}: line {line}, column {column}: invalid XML: {exc}"
        ) from exc
    if root.tag == _X2 + "xliff" and root.get("version") == "2.0":
        return "xliff2"
    if root.tag == _X + "xliff" and root.get("version") == "1.2":
        _parse_xml(data, relative_path)
        return "sdlxliff1"
    _fail("supported inputs are SDLXLIFF 1.2 and XLIFF 2.0", relative_path)


def _selected_files(path: Path) -> tuple[list[tuple[Path, str]], list[str]]:
    if not path.exists():
        raise SDLXLIFFImportError(f"input path does not exist: {path}")
    if path.is_file():
        return [(path, path.name)], []
    selected = sorted(
        (
            candidate
            for candidate in path.rglob("*")
            if candidate.is_file()
            and candidate.suffix.casefold() in _XLIFF_SUFFIXES
        ),
        key=lambda candidate: candidate.relative_to(path).as_posix(),
    )
    if not selected:
        raise SDLXLIFFImportError(f"no XLIFF files found in directory: {path}")
    unselected = sorted(
        candidate.relative_to(path).as_posix()
        for candidate in path.rglob("*")
        if candidate.is_file() and candidate.suffix.casefold() in _TABULAR_SUFFIXES
    )
    return [
        (candidate, candidate.relative_to(path).as_posix()) for candidate in selected
    ], unselected


def _local_attribute(element: ET.Element, name: str) -> str | None:
    if name in element.attrib:
        return element.attrib[name]
    for attr_name, value in element.attrib.items():
        namespace, local = _split_qname(attr_name)
        if namespace == SDL_NS and local == name:
            return value
    return None


def _comment_definitions(
    scope: ET.Element,
    *,
    exclude_file_descendants: bool = False,
) -> dict[str, str]:
    definitions: dict[str, str] = {}

    def collect(parent: ET.Element) -> None:
        for element in parent:
            if exclude_file_descendants and element.tag == _X + "file":
                continue
            namespace, local = _split_qname(element.tag)
            if namespace == SDL_NS and local in {"cmt-def", "comment-def"}:
                comment_id = _local_attribute(element, "id")
                text = "".join(element.itertext()).strip()
                if comment_id and text:
                    definitions[comment_id] = text
            collect(element)

    collect(scope)
    return definitions


def _segment_definitions(tu: ET.Element, context: str) -> list[ET.Element]:
    containers = [child for child in tu if child.tag == _SDL + "seg-defs"]
    if len(containers) > 1:
        _fail("multiple direct sdl:seg-defs elements", context)
    if not containers:
        return []
    container = containers[0]
    return [child for child in container if child.tag == _SDL + "seg"]


def _last_modified_by(seg_def: ET.Element | None) -> str | None:
    if seg_def is None:
        return None
    direct = _local_attribute(seg_def, "last_modified_by")
    if direct:
        return direct
    for element in seg_def.iter():
        namespace, local = _split_qname(element.tag)
        if namespace != SDL_NS or local != "value":
            continue
        if _local_attribute(element, "key") == "last_modified_by":
            value = "".join(element.itertext()).strip()
            return value or None
    return None


def _segment_comment(
    seg_def: ET.Element | None,
    definitions: dict[str, str],
    inherited: Sequence[str] = (),
) -> str | None:
    references: list[str] = []
    inline: list[str] = []
    if seg_def is not None:
        for name in ("comments", "comment", "comment-id", "cmt-id"):
            value = _local_attribute(seg_def, name)
            if value:
                references.extend(
                    part for part in re.split(r"[,;\s]+", value) if part
                )
        for element in seg_def.iter():
            if element is seg_def:
                continue
            namespace, local = _split_qname(element.tag)
            if namespace != SDL_NS or local not in {"cmt", "comment"}:
                continue
            reference = _local_attribute(element, "id") or _local_attribute(
                element, "ref"
            )
            if reference:
                references.append(reference)
            else:
                value = "".join(element.itertext()).strip()
                if value:
                    inline.append(value)
    resolved = [
        definitions[reference]
        for reference in references
        if reference in definitions
    ]
    values = list(inherited) + resolved + inline
    return "\n".join(dict.fromkeys(values)) or None


def _tu_direct_comments(
    tu: ET.Element,
    definitions: dict[str, str],
) -> list[str]:
    values: list[str] = []
    for element in tu:
        namespace, local = _split_qname(element.tag)
        if namespace != SDL_NS or local not in {"cmt", "comment"}:
            continue
        reference = _local_attribute(element, "id") or _local_attribute(
            element, "ref"
        )
        if reference:
            if reference in definitions:
                values.append(definitions[reference])
            continue
        inline = "".join(element.itertext()).strip()
        if inline:
            values.append(inline)
    return list(dict.fromkeys(values))


def _metadata(
    *,
    file_original: str | None,
    source_language: str | None,
    target_language: str | None,
    seg_def: ET.Element | None,
    comments: dict[str, str],
    source: SerializedMixedContent,
    target: SerializedMixedContent,
    extension_xml: list[str],
    extension_attributes: list[dict[str, str]],
    inherited_comments: Sequence[str],
) -> dict:
    return {
        "file_original": file_original,
        "source_language": source_language,
        "target_language": target_language,
        "confirmation": _local_attribute(seg_def, "conf") if seg_def is not None else None,
        "origin": _local_attribute(seg_def, "origin") if seg_def is not None else None,
        "match_percent": _local_attribute(seg_def, "percent") if seg_def is not None else None,
        "text_match": _local_attribute(seg_def, "text-match") if seg_def is not None else None,
        "locked": (
            (_local_attribute(seg_def, "locked") or "").casefold() in _TRUE_VALUES
            if seg_def is not None
            else False
        ),
        "last_modified_by": _last_modified_by(seg_def),
        "comment": _segment_comment(seg_def, comments, inherited_comments),
        "source_raw_xml": source.raw_xml,
        "target_raw_xml": target.raw_xml,
        "source_tag_signature": source.tag_signature,
        "target_tag_signature": target.tag_signature,
        "extension_xml": list(extension_xml),
        "extension_attributes": list(extension_attributes),
    }


def _has_boundary_descendant(element: ET.Element) -> bool:
    boundary_tags = {
        _X + "file",
        _X + "body",
        _X + "trans-unit",
        _X + "source",
        _X + "seg-source",
        _X + "target",
        _X + "mrk",
        _SDL + "seg-defs",
    }
    return any(descendant.tag in boundary_tags for descendant in element.iter())


def _validate_structural_children(
    parent: ET.Element,
    allowed_direct: set[str],
    context: str,
) -> None:
    for child in parent:
        if child.tag in allowed_direct:
            continue
        if _has_boundary_descendant(child):
            namespace, _ = _split_qname(child.tag)
            _fail(
                f"unsupported structural extension namespace {namespace or '<none>'}",
                context,
            )


def _validate_tu_structure(tu: ET.Element, context: str) -> None:
    _validate_structural_children(
        tu,
        {_X + "source", _X + "seg-source", _X + "target", _SDL + "seg-defs"},
        context,
    )


def _validate_mixed_boundaries(element: ET.Element, context: str) -> None:
    nested_structural = {
        _X + "file",
        _X + "body",
        _X + "trans-unit",
        _X + "source",
        _X + "seg-source",
        _X + "target",
        _SDL + "seg-defs",
    }
    for descendant in element.iter():
        if descendant is element:
            continue
        if (
            descendant.tag == _X + "mrk"
            and _local_attribute(descendant, "mtype") == "seg"
        ):
            _fail("nested segmentation boundary in mixed content", context)
        if descendant.tag in nested_structural:
            _fail(f"nested structural boundary {descendant.tag!r}", context)


def _iter_trans_units(body: ET.Element, context: str):
    for child in body:
        if child.tag == _X + "trans-unit":
            yield child
            continue
        if child.tag == _X + "group":
            yield from _iter_trans_units(child, context)
            continue
        if _has_boundary_descendant(child):
            namespace, _ = _split_qname(child.tag)
            _fail(
                f"unsupported structural extension namespace {namespace or '<none>'}",
                context,
            )


def _wrapped_marker(
    marker: ET.Element,
    wrappers: tuple[ET.Element, ...],
) -> ET.Element:
    if not wrappers:
        return marker
    synthetic = ET.Element(marker.tag, dict(marker.attrib))
    parent = synthetic
    for wrapper in wrappers:
        child = ET.Element(wrapper.tag, dict(wrapper.attrib))
        parent.append(child)
        parent = child
    parent.text = marker.text
    for child in marker:
        parent.append(deepcopy(child))
    return synthetic


def _segmentation_markers(container: ET.Element, context: str) -> list[ET.Element]:
    direct_markers = [
        child
        for child in container
        if child.tag == _X + "mrk" and _local_attribute(child, "mtype") == "seg"
    ]
    if (
        len(direct_markers) == 1
        and not (container.text or "").strip()
        and all(child in direct_markers or child.tag == _X + "x" for child in container)
        and all(not (child.tail or "").strip() for child in container)
    ):
        marker = direct_markers[0]
        synthetic = ET.Element(marker.tag, dict(marker.attrib))

        def append_text(value: str | None) -> None:
            if not value:
                return
            if len(synthetic):
                synthetic[-1].tail = (synthetic[-1].tail or "") + value
            else:
                synthetic.text = (synthetic.text or "") + value

        for child in container:
            if child is marker:
                append_text(marker.text)
                for nested in marker:
                    synthetic.append(deepcopy(nested))
                continue
            copied = deepcopy(child)
            copied.tail = None
            synthetic.append(copied)
        return [synthetic]

    markers: list[ET.Element] = []

    def collect(parent: ET.Element, wrappers: tuple[ET.Element, ...]) -> None:
        if parent.text and parent.text.strip():
            _fail("text outside segmentation mrk boundaries", context)
        for child in parent:
            if (
                child.tag == _X + "mrk"
                and _local_attribute(child, "mtype") == "seg"
            ):
                markers.append(_wrapped_marker(child, wrappers))
            elif child.tag == _X + "g":
                before = len(markers)
                collect(child, wrappers + (child,))
                if len(markers) == before:
                    _fail("g wrapper contains no segmentation mrk", context)
            else:
                namespace, _ = _split_qname(child.tag)
                _fail(
                    "unsupported structural extension "
                    f"{child.tag!r} ({namespace or '<none>'})",
                    context,
                )
            if child.tail and child.tail.strip():
                _fail("text outside segmentation mrk boundaries", context)

    collect(container, ())
    if not markers:
        _fail("segmented content requires top-level mrk mtype='seg'", context)
    return markers


def _markers_by_mid(markers: list[ET.Element], context: str) -> dict[str, ET.Element]:
    by_mid: dict[str, ET.Element] = {}
    for marker in markers:
        _validate_mixed_boundaries(marker, context)
        mid = _local_attribute(marker, "mid")
        if not mid:
            _fail("segmentation mrk is missing mid", context)
        if mid in by_mid:
            _fail(f"duplicate mid {mid!r}", context)
        by_mid[mid] = marker
    return by_mid


_EMPTY_MIXED = SerializedMixedContent("", "", "", ())


def _single_direct_child(
    tu: ET.Element,
    local_name: str,
    context: str,
) -> ET.Element | None:
    elements = [child for child in tu if child.tag == _X + local_name]
    if len(elements) > 1:
        _fail(f"multiple direct {local_name} elements", context)
    return elements[0] if elements else None


def _single_direct_tag(
    parent: ET.Element,
    tag: str,
    name: str,
    context: str,
) -> ET.Element | None:
    elements = [child for child in parent if child.tag == tag]
    if len(elements) > 1:
        _fail(f"multiple direct {name} elements", context)
    return elements[0] if elements else None


def _is_blank_container(element: ET.Element | None) -> bool:
    return element is None or (
        len(element) == 0 and not (element.text or "").strip()
    )


def _pair_tu(
    tu: ET.Element,
    *,
    namespace_map: dict[str, str],
    context: str,
) -> list[
    tuple[
        SerializedMixedContent,
        SerializedMixedContent,
        str | None,
        ET.Element | None,
        tuple[ET.Element, ...],
    ]
]:
    seg_source = _single_direct_child(tu, "seg-source", context)
    source_element = _single_direct_child(tu, "source", context)
    target_element = _single_direct_child(tu, "target", context)
    seg_defs = _segment_definitions(tu, context)

    if seg_source is not None:
        if (
            not seg_defs
            and source_element is not None
            and _is_blank_container(source_element)
            and _is_blank_container(seg_source)
            and _is_blank_container(target_element)
        ):
            return [
                (
                    serialize_mixed(source_element, namespace_map),
                    serialize_mixed(target_element, namespace_map)
                    if target_element is not None
                    else _EMPTY_MIXED,
                    None,
                    None,
                    tuple(
                        element
                        for element in (source_element, seg_source, target_element)
                        if element is not None
                    ),
                )
            ]
        source_markers = _segmentation_markers(seg_source, context)
        source_by_mid = _markers_by_mid(source_markers, context)
        target_by_mid: dict[str, ET.Element] = {}
        if target_element is not None and not _is_blank_container(target_element):
            target_markers = _segmentation_markers(target_element, context)
            target_by_mid = _markers_by_mid(target_markers, context)
            if set(target_by_mid) != set(source_by_mid):
                _fail(
                    "source/target mid sets do not match: "
                    f"source={sorted(source_by_mid)} target={sorted(target_by_mid)}",
                    context,
                )

        defs_by_id: dict[str, ET.Element] = {}
        for seg_def in seg_defs:
            seg_id = _local_attribute(seg_def, "id")
            if not seg_id:
                _fail("seg-def is missing id for segmented content", context)
            if seg_id in defs_by_id:
                _fail(f"duplicate seg-def id {seg_id!r}", context)
            defs_by_id[seg_id] = seg_def
        if set(defs_by_id) != set(source_by_mid):
            _fail(
                "mrk mid and seg-def id sets do not match: "
                f"mid={sorted(source_by_mid)} seg-def={sorted(defs_by_id)}",
                context,
            )

        pairs = []
        for source_marker in source_markers:
            mid = _local_attribute(source_marker, "mid")
            target_marker = target_by_mid.get(mid) if target_element is not None else None
            pairs.append(
                (
                    serialize_mixed(source_marker, namespace_map),
                    serialize_mixed(target_marker, namespace_map)
                    if target_marker is not None
                    else _EMPTY_MIXED,
                    mid,
                    defs_by_id.get(mid),
                    tuple(
                        element
                        for element in (
                            seg_source,
                            source_marker,
                            target_element,
                            target_marker,
                        )
                        if element is not None
                    ),
                )
            )
        return pairs

    if source_element is None:
        _fail("trans-unit is missing source or seg-source", context)
    _validate_mixed_boundaries(source_element, context)
    if target_element is not None:
        _validate_mixed_boundaries(target_element, context)
    if len(seg_defs) > 1:
        _fail("multiple seg-def entries have no segmentation boundaries", context)
    seg_def = seg_defs[0] if seg_defs else None
    seg_id = _local_attribute(seg_def, "id") if seg_def is not None else None
    return [
        (
            serialize_mixed(source_element, namespace_map),
            serialize_mixed(target_element, namespace_map)
            if target_element is not None
            else _EMPTY_MIXED,
            seg_id,
            seg_def,
            tuple(
                element
                for element in (source_element, target_element)
                if element is not None
            ),
        )
    ]


def _tu_extensions(
    tu: ET.Element,
    seg_def: ET.Element | None,
    namespace_map: dict[str, str],
) -> list[str]:
    values: list[str] = []
    mixed_roots = {_X + "source", _X + "seg-source", _X + "target"}

    def collect(parent: ET.Element) -> None:
        for child in parent:
            if parent is tu and child.tag in mixed_roots:
                continue
            if child.tag == _SDL + "seg":
                if child is seg_def:
                    collect(child)
                continue
            namespace, _ = _split_qname(child.tag)
            if namespace not in {XLIFF_NS, SDL_NS}:
                values.append(_serialize_full(child, namespace_map))
                continue
            collect(child)

    collect(tu)
    return values


def _tu_extension_attributes(
    tu: ET.Element,
    seg_def: ET.Element | None,
    boundary_elements: tuple[ET.Element, ...],
) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    recorded_elements: set[int] = set()
    mixed_roots = {_X + "source", _X + "seg-source", _X + "target"}

    def record(element: ET.Element) -> None:
        if id(element) in recorded_elements:
            return
        recorded_elements.add(id(element))
        for name, value in sorted(element.attrib.items()):
            namespace, _ = _split_qname(name)
            if namespace not in {None, XLIFF_NS, SDL_NS, XML_NS}:
                values.append(
                    {"element": element.tag, "name": name, "value": str(value)}
                )

    def collect(element: ET.Element) -> None:
        record(element)
        for child in element:
            if element is tu and child.tag in mixed_roots:
                record(child)
                continue
            if child.tag == _SDL + "seg" and child is not seg_def:
                continue
            collect(child)

    collect(tu)
    for element in boundary_elements:
        record(element)
    return values


def _internal_file_summary(root: ET.Element) -> dict[str, int | bool]:
    payloads = [
        "".join(element.itertext()).strip()
        for element in root.iter(_X + "internal-file")
    ]
    return {
        "present": bool(payloads),
        "size": sum(len(payload) for payload in payloads),
    }


def _serialize_xliff2_mixed(
    element: ET.Element,
    namespace_map: dict[str, str],
) -> SerializedMixedContent:
    signature: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    plain: list[str] = []

    def visit(parent: ET.Element, translatable: bool) -> None:
        if translatable and parent.text:
            plain.append(parent.text)
        for child in parent:
            namespace, local = _split_qname(child.tag)
            if namespace == XLIFF2_NS and local in _XLIFF2_INLINE_NAMES:
                signature.append(
                    (
                        child.tag,
                        tuple(
                            (name, str(value))
                            for name, value in sorted(child.attrib.items())
                        ),
                    )
                )
            child_translatable = translatable and not (
                namespace == XLIFF2_NS
                and local in _XLIFF2_NATIVE_CODE_NAMES
            )
            visit(child, child_translatable)
            if translatable and child.tail:
                plain.append(child.tail)

    visit(element, True)
    return SerializedMixedContent(
        display=_content_xml(element, namespace_map, raw=False),
        plain="".join(plain),
        raw_xml=_content_xml(element, namespace_map, raw=True),
        tag_signature=tuple(signature),
    )


def _xliff2_notes(unit: ET.Element, segment: ET.Element) -> str | None:
    values: list[str] = []
    for scope in (unit, segment):
        for notes in scope:
            if notes.tag != _X2 + "notes":
                continue
            for note in notes:
                if note.tag != _X2 + "note":
                    continue
                value = "".join(note.itertext()).strip()
                if value:
                    values.append(value)
    return "\n".join(dict.fromkeys(values)) or None


def _iter_xliff2_units(parent: ET.Element):
    for child in parent:
        if child.tag == _X2 + "unit":
            yield child
        elif child.tag == _X2 + "group":
            yield from _iter_xliff2_units(child)


def _xliff2_locked(
    file_element: ET.Element,
    unit: ET.Element,
    segment: ET.Element,
    parent_by_identity: Mapping[int, ET.Element],
) -> bool:
    scopes = [segment, unit]
    parent = parent_by_identity.get(id(unit))
    while parent is not None and parent is not file_element:
        scopes.append(parent)
        parent = parent_by_identity.get(id(parent))
    scopes.append(file_element)
    return any(
        (_local_attribute(scope, "translate") or "").casefold() == "no"
        for scope in scopes
    )


def _xliff2_extension_xml(
    unit: ET.Element,
    segment: ET.Element,
    namespace_map: dict[str, str],
) -> list[str]:
    values: list[str] = []
    for scope in (unit, segment):
        for child in scope:
            namespace, _ = _split_qname(child.tag)
            if namespace not in {XLIFF2_NS, XML_NS}:
                values.append(_serialize_full(child, namespace_map))
    return values


def _read_xliff2_selected(
    selected: list[tuple[Path, str]],
    unselected: list[str],
    options: SDLXLIFFOptions,
    input_snapshots: Mapping[str, bytes],
) -> SDLXLIFFImportResult:
    segments: list[dict] = []
    rows_raw: list[list[str]] = []
    manifest_files: list[dict] = []
    declared_languages: list[dict[str, str | None]] = []
    extension_namespaces: set[str] = set()
    content_type_matches: list[dict] = []
    excluded: list[dict] = []
    protection_evidence: list[dict] = []
    parsed_segment_count = 0
    locked_segment_count = 0

    for input_path, relative_path in selected:
        input_bytes = input_snapshots[relative_path]
        root, namespace_map, namespace_declarations = _parse_xliff2_xml(
            input_bytes, relative_path
        )
        source_language = _local_attribute(root, "srcLang")
        target_language = _local_attribute(root, "trgLang")
        declaration = {
            "source_language": source_language,
            "target_language": target_language,
        }
        declared_languages.append(declaration)
        extension_namespaces.update(
            uri
            for _, uri in namespace_declarations
            if uri not in {XLIFF2_NS, XML_NS}
        )
        file_elements = [child for child in root if child.tag == _X2 + "file"]
        if not file_elements:
            _fail("document contains no XLIFF file elements", relative_path)
        input_language_declarations: list[dict[str, str | None]] = []

        for file_index, file_element in enumerate(file_elements):
            input_language_declarations.append(dict(declaration))
            file_original = _local_attribute(file_element, "original")
            parent_by_identity = {
                id(child): parent
                for parent in file_element.iter()
                for child in parent
            }
            units = list(_iter_xliff2_units(file_element))
            if not units:
                _fail(
                    "XLIFF 2.0 file contains no unit elements",
                    _context(relative_path, file_index=file_index),
                )
            seen_business_keys: set[tuple[str, str]] = set()
            for unit_index, unit in enumerate(units):
                unit_id = _local_attribute(unit, "id")
                if not unit_id:
                    _fail(
                        "XLIFF 2.0 unit is missing id",
                        _context(relative_path, file_index=file_index),
                    )
                segment_elements = [
                    child for child in unit if child.tag == _X2 + "segment"
                ]
                for segment_index, xml_segment in enumerate(segment_elements):
                    parsed_segment_count += 1
                    segment_xml_id = _local_attribute(xml_segment, "id") or str(
                        segment_index
                    )
                    business_key = (unit_id, segment_xml_id)
                    if business_key in seen_business_keys:
                        _fail(
                            f"duplicate unit/segment business key {business_key!r}",
                            _context(
                                relative_path,
                                file_index=file_index,
                                tu_id=unit_id,
                                tu_index=unit_index,
                                segment_id=segment_xml_id,
                                segment_index=segment_index,
                            ),
                        )
                    seen_business_keys.add(business_key)
                    source_elements = [
                        child
                        for child in xml_segment
                        if child.tag == _X2 + "source"
                    ]
                    target_elements = [
                        child
                        for child in xml_segment
                        if child.tag == _X2 + "target"
                    ]
                    if len(source_elements) != 1:
                        _fail(
                            "XLIFF 2.0 segment must contain exactly one source",
                            f"{relative_path}, unit {unit_id!r}, segment {segment_xml_id!r}",
                        )
                    if len(target_elements) > 1:
                        _fail(
                            "XLIFF 2.0 segment contains multiple targets",
                            f"{relative_path}, unit {unit_id!r}, segment {segment_xml_id!r}",
                        )
                    source = _serialize_xliff2_mixed(
                        source_elements[0], namespace_map
                    )
                    target = (
                        _serialize_xliff2_mixed(target_elements[0], namespace_map)
                        if target_elements
                        else _EMPTY_MIXED
                    )
                    locked = _xliff2_locked(
                        file_element,
                        unit,
                        xml_segment,
                        parent_by_identity,
                    )
                    if locked:
                        locked_segment_count += 1
                    source_ref = {
                        "relative_path": relative_path,
                        "file_index": file_index,
                        "tu_id": unit_id,
                        "tu_index": unit_index,
                        "sdl_segment_id": segment_xml_id,
                        "segment_index": segment_index,
                        "unit_id": unit_id,
                        "segment_id": segment_xml_id,
                    }
                    metadata = {
                        "xml_format": "xliff2",
                        "xml_version": "2.0",
                        "file_original": file_original,
                        "source_language": source_language,
                        "target_language": target_language,
                        "confirmation": _local_attribute(xml_segment, "state"),
                        "origin": None,
                        "match_percent": None,
                        "text_match": None,
                        "locked": locked,
                        "last_modified_by": None,
                        "comment": _xliff2_notes(unit, xml_segment),
                        "source_raw_xml": source.raw_xml,
                        "target_raw_xml": target.raw_xml,
                        "source_tag_signature": source.tag_signature,
                        "target_tag_signature": target.tag_signature,
                        "extension_xml": _xliff2_extension_xml(
                            unit, xml_segment, namespace_map
                        ),
                        "extension_attributes": [],
                    }
                    content_type, content_type_rule_id = match_content_type(
                        relative_path, options.content_type_rules
                    )
                    rule_candidate = {
                        "relative_path": relative_path,
                        "file_original": file_original,
                        "confirmation": metadata["confirmation"],
                        "origin": None,
                        "locked": locked,
                        "source": source.display,
                        "target": target.display,
                    }
                    exclusion_matches = match_exclusions(
                        rule_candidate, options.exclude_rules
                    )
                    if not source.display.strip() and not target.display.strip():
                        exclusion_matches.append(
                            {
                                "id": "blank-both-sides",
                                "reason": "Source and target are both blank",
                                "field": "source,target",
                                "operator": "built-in",
                                "expected": "blank",
                                "actual": {
                                    "source": source.display,
                                    "target": target.display,
                                },
                            }
                        )
                    included = not exclusion_matches
                    segment_id = len(segments) if included else None
                    effective_reason = "SOURCE_LOCKED" if locked and included else None
                    evidence = {
                        "locked": {
                            "matched": locked,
                            "reason": "SOURCE_LOCKED" if locked else None,
                        },
                        "tm": {
                            "exact_match": False,
                            "candidate": False,
                            "protected_by_policy": False,
                            "conditions": _tm_evidence(metadata),
                        },
                        "effective_reason": effective_reason,
                    }
                    protection_evidence.append(
                        {
                            "segment_id": segment_id,
                            "source_ref": dict(source_ref),
                            "included": included,
                            **evidence,
                        }
                    )
                    if content_type_rule_id is not None:
                        content_type_matches.append(
                            {
                                "segment_id": segment_id,
                                "source_ref": dict(source_ref),
                                "rule_id": content_type_rule_id,
                                "content_type": content_type,
                                "included": included,
                            }
                        )
                    if exclusion_matches:
                        excluded.append(
                            {
                                "source_ref": dict(source_ref),
                                "rule_ids": [
                                    match["id"] for match in exclusion_matches
                                ],
                                "reasons": [
                                    match["reason"] for match in exclusion_matches
                                ],
                                "matches": exclusion_matches,
                                "content_type": content_type,
                                "content_type_rule_id": content_type_rule_id,
                            }
                        )
                        continue
                    segment = {
                        "id": segment_id,
                        "source_ref": source_ref,
                        "source": source.display,
                        "target": target.display,
                        "source_plain": source.plain,
                        "target_plain": target.plain,
                        "corrected": None,
                        "content_type": content_type,
                        "content_type_rule_id": content_type_rule_id,
                        "protection_evidence": evidence,
                        "metadata": {"sdlxliff": metadata},
                    }
                    if effective_reason is not None:
                        segment["protected"] = True
                        segment["protected_reason"] = effective_reason
                    segments.append(segment)
                    rows_raw.append(
                        [
                            relative_path,
                            unit_id,
                            segment_xml_id,
                            source.display,
                            target.display,
                        ]
                    )
        manifest_files.append(
            {
                "relative_path": relative_path,
                "sha256": hashlib.sha256(input_bytes).hexdigest(),
                "format": "xliff2",
                "version": "2.0",
                "namespaces": dict(sorted(namespace_map.items())),
                "namespace_declarations": [
                    {"prefix": prefix, "uri": uri}
                    for prefix, uri in namespace_declarations
                ],
                "languages": input_language_declarations,
                "internal_file": {"present": False, "size": 0},
            }
        )

    source_lang = next(
        (
            item["source_language"]
            for item in declared_languages
            if item["source_language"]
        ),
        "",
    )
    target_lang = next(
        (
            item["target_language"]
            for item in declared_languages
            if item["target_language"]
        ),
        "",
    )
    manifest = {
        "schema": "lqe.xliff.import-manifest",
        "version": 1,
        "importer": {
            "name": "xliff2",
            "schema": "lqe.xliff.import-manifest",
            "version": 1,
        },
        "input_format": "xliff",
        "files": manifest_files,
        "languages": declared_languages,
        "extension_namespaces": sorted(extension_namespaces),
        "rules": {
            "content_type": list(options.content_type_rules),
            "exclusions": list(options.exclude_rules),
        },
        "rule_matches": {
            "content_type": content_type_matches,
            "exclusions": [
                {
                    "source_ref": item["source_ref"],
                    "rule_ids": [
                        rule_id
                        for rule_id in item["rule_ids"]
                        if rule_id != "blank-both-sides"
                    ],
                }
                for item in excluded
                if any(
                    rule_id != "blank-both-sides"
                    for rule_id in item["rule_ids"]
                )
            ],
        },
        "content_type_matches": content_type_matches,
        "excluded": excluded,
        "counts": {
            "selected_files": len(selected),
            "unselected_supported_files": len(unselected),
            "parsed_segments": parsed_segment_count,
            "included_segments": len(segments),
            "excluded_segments": len(excluded),
            "content_type_matches": len(content_type_matches),
            "tm_candidates": 0,
            "locked_segments": locked_segment_count,
            "protected_segments": sum(
                1 for segment in segments if segment.get("protected")
            ),
        },
        "tm_protection": options.tm_protection,
        "protection_evidence": protection_evidence,
        "unselected_supported_files": unselected,
    }
    return SDLXLIFFImportResult(
        headers=["来源文件", "Unit ID", "Segment ID", "原文", "译文"],
        rows_raw=rows_raw,
        segments=segments,
        source_lang=source_lang,
        target_lang=target_lang,
        input_paths=[str(input_path.resolve()) for input_path, _ in selected],
        manifest=manifest,
        tm_candidates={"candidate_ids": [], "segments": []},
    )


def read_sdlxliff(
    path: Path,
    *,
    options: SDLXLIFFOptions,
) -> SDLXLIFFImportResult:
    if not isinstance(options, SDLXLIFFOptions):
        raise SDLXLIFFImportError("options must be SDLXLIFFOptions")
    options = validate_options(
        {
            "tm_protection": options.tm_protection,
            "content_type_rules": options.content_type_rules,
            "exclude_rules": options.exclude_rules,
        }
    )
    selected, unselected = _selected_files(Path(path))
    input_snapshots = {
        relative_path: input_path.read_bytes()
        for input_path, relative_path in selected
    }
    document_kinds = {
        _document_kind(input_snapshots[relative_path], relative_path)
        for _, relative_path in selected
    }
    if len(document_kinds) != 1:
        raise SDLXLIFFImportError(
            "one input job cannot mix SDLXLIFF 1.2 and XLIFF 2.0 files"
        )
    if document_kinds == {"xliff2"}:
        return _read_xliff2_selected(
            selected, unselected, options, input_snapshots
        )
    segments: list[dict] = []
    rows_raw: list[list[str]] = []
    manifest_files: list[dict] = []
    declared_languages: list[dict[str, str | None]] = []
    extension_namespaces: set[str] = set()
    content_type_matches: list[dict] = []
    excluded: list[dict] = []
    protection_evidence: list[dict] = []
    tm_candidate_ids: list[int] = []
    tm_candidate_segments: list[dict] = []
    parsed_segment_count = 0
    locked_segment_count = 0

    for input_path, relative_path in selected:
        input_bytes = input_snapshots[relative_path]
        root, namespace_map, namespace_declarations = _parse_xml(
            input_bytes, relative_path
        )
        _validate_structural_children(root, {_X + "file"}, relative_path)
        root_comments = _comment_definitions(root, exclude_file_descendants=True)
        extension_namespaces.update(
            uri
            for _, uri in namespace_declarations
            if uri not in {XLIFF_NS, SDL_NS, XML_NS}
        )
        input_language_declarations: list[dict[str, str | None]] = []
        internal_summary = _internal_file_summary(root)
        file_elements = [child for child in root if child.tag == _X + "file"]
        if not file_elements:
            _fail("document contains no XLIFF file elements", relative_path)

        for file_index, file_element in enumerate(file_elements):
            file_context = _context(relative_path, file_index=file_index)
            _validate_structural_children(file_element, {_X + "body"}, file_context)
            file_original = _local_attribute(file_element, "original")
            source_language = _local_attribute(file_element, "source-language")
            target_language = _local_attribute(file_element, "target-language")
            declaration = {
                "source_language": source_language,
                "target_language": target_language,
            }
            declared_languages.append(declaration)
            input_language_declarations.append(declaration)
            comments = {**root_comments, **_comment_definitions(file_element)}
            body = _single_direct_tag(
                file_element, _X + "body", "body", file_context
            )
            if body is None:
                _fail("XLIFF file is missing body", file_context)
            seen_business_keys: set[
                tuple[tuple[str, str | int], str | None]
            ] = set()

            for tu_index, tu in enumerate(_iter_trans_units(body, file_context)):
                tu_id = _local_attribute(tu, "id") or None
                tu_context = _context(
                    relative_path,
                    file_index=file_index,
                    tu_id=tu_id,
                    tu_index=tu_index,
                    element=tu,
                )
                _validate_tu_structure(tu, tu_context)
                inherited_comments = _tu_direct_comments(tu, comments)
                pairs = _pair_tu(tu, namespace_map=namespace_map, context=tu_context)
                for segment_index, (
                    source,
                    target,
                    sdl_segment_id,
                    seg_def,
                    boundary_elements,
                ) in enumerate(pairs):
                    parsed_segment_count += 1
                    tu_owner: tuple[str, str | int] = (
                        ("id", tu_id) if tu_id is not None else ("index", tu_index)
                    )
                    business_key = (tu_owner, sdl_segment_id)
                    if business_key in seen_business_keys:
                        segment_context = _context(
                            relative_path,
                            file_index=file_index,
                            tu_id=tu_id,
                            tu_index=tu_index,
                            segment_id=sdl_segment_id,
                            segment_index=segment_index,
                        )
                        _fail(
                            f"duplicate TU/segment business key {business_key!r}",
                            segment_context,
                        )
                    seen_business_keys.add(business_key)

                    source_ref = {
                        "relative_path": relative_path,
                        "file_index": file_index,
                        "tu_id": tu_id,
                        "tu_index": tu_index,
                        "sdl_segment_id": sdl_segment_id,
                        "segment_index": segment_index,
                    }
                    metadata = _metadata(
                        file_original=file_original,
                        source_language=source_language,
                        target_language=target_language,
                        seg_def=seg_def,
                        comments=comments,
                        source=source,
                        target=target,
                        extension_xml=_tu_extensions(tu, seg_def, namespace_map),
                        extension_attributes=_tu_extension_attributes(
                            tu, seg_def, boundary_elements
                        ),
                        inherited_comments=inherited_comments,
                    )
                    content_type, content_type_rule_id = match_content_type(
                        relative_path, options.content_type_rules
                    )
                    rule_candidate = {
                        "relative_path": relative_path,
                        "file_original": file_original,
                        "confirmation": metadata["confirmation"],
                        "origin": metadata["origin"],
                        "locked": metadata["locked"],
                        "source": source.display,
                        "target": target.display,
                    }
                    exclusion_matches = match_exclusions(
                        rule_candidate, options.exclude_rules
                    )
                    if not source.display.strip() and not target.display.strip():
                        exclusion_matches.append(
                            {
                                "id": "blank-both-sides",
                                "reason": "Source and target are both blank",
                                "field": "source,target",
                                "operator": "built-in",
                                "expected": "blank",
                                "actual": {
                                    "source": source.display,
                                    "target": target.display,
                                },
                            }
                        )

                    tm_evidence = _tm_evidence(metadata)
                    exact_tm = is_exact_tm(metadata)
                    locked = bool(metadata["locked"])
                    if locked:
                        locked_segment_count += 1
                    strict_tm = (
                        options.tm_protection
                        == "protect-exact-source-and-target"
                    )
                    included = not exclusion_matches
                    segment_id = len(segments) if included else None
                    effective_reason = None
                    if included:
                        if locked:
                            effective_reason = "SOURCE_LOCKED"
                        elif exact_tm and strict_tm:
                            effective_reason = "TM_100_MATCH"
                    evidence = {
                        "locked": {
                            "matched": locked,
                            "reason": "SOURCE_LOCKED" if locked else None,
                        },
                        "tm": {
                            "exact_match": exact_tm,
                            "candidate": exact_tm and included,
                            "protected_by_policy": exact_tm and strict_tm and included,
                            "conditions": tm_evidence,
                        },
                        "effective_reason": effective_reason,
                    }
                    protection_evidence.append(
                        {
                            "segment_id": segment_id,
                            "source_ref": dict(source_ref),
                            "included": included,
                            **evidence,
                        }
                    )
                    if content_type_rule_id is not None:
                        content_type_matches.append(
                            {
                                "segment_id": segment_id,
                                "source_ref": dict(source_ref),
                                "rule_id": content_type_rule_id,
                                "content_type": content_type,
                                "included": included,
                            }
                        )
                    if exclusion_matches:
                        excluded.append(
                            {
                                "source_ref": dict(source_ref),
                                "rule_ids": [match["id"] for match in exclusion_matches],
                                "reasons": [match["reason"] for match in exclusion_matches],
                                "matches": exclusion_matches,
                                "content_type": content_type,
                                "content_type_rule_id": content_type_rule_id,
                            }
                        )
                        continue

                    segment = {
                        "id": segment_id,
                        "source_ref": source_ref,
                        "source": source.display,
                        "target": target.display,
                        "source_plain": source.plain,
                        "target_plain": target.plain,
                        "corrected": None,
                        "content_type": content_type,
                        "content_type_rule_id": content_type_rule_id,
                        "protection_evidence": evidence,
                        "metadata": {"sdlxliff": metadata},
                    }
                    if effective_reason is not None:
                        segment["protected"] = True
                        segment["protected_reason"] = effective_reason
                    if exact_tm:
                        tm_candidate_ids.append(segment_id)
                        tm_candidate_segments.append(
                            {
                                "segment_id": segment_id,
                                "source_ref": dict(source_ref),
                                "evidence": tm_evidence,
                            }
                        )
                    segments.append(segment)
                    rows_raw.append(
                        [
                            relative_path,
                            tu_id or "",
                            sdl_segment_id or "",
                            source.display,
                            target.display,
                        ]
                    )

        manifest_files.append(
            {
                "relative_path": relative_path,
                "sha256": hashlib.sha256(input_bytes).hexdigest(),
                "namespaces": dict(sorted(namespace_map.items())),
                "namespace_declarations": [
                    {"prefix": prefix, "uri": uri}
                    for prefix, uri in namespace_declarations
                ],
                "languages": input_language_declarations,
                "internal_file": internal_summary,
            }
        )

    source_lang = next(
        (
            declaration["source_language"]
            for declaration in declared_languages
            if declaration["source_language"]
        ),
        "",
    )
    target_lang = next(
        (
            declaration["target_language"]
            for declaration in declared_languages
            if declaration["target_language"]
        ),
        "",
    )
    exclude_rule_matches = [
        {
            "source_ref": item["source_ref"],
            "rule_ids": [
                rule_id
                for rule_id in item["rule_ids"]
                if rule_id != "blank-both-sides"
            ],
        }
        for item in excluded
        if any(rule_id != "blank-both-sides" for rule_id in item["rule_ids"])
    ]
    manifest = {
        "schema": _MANIFEST_SCHEMA,
        "version": _MANIFEST_VERSION,
        "importer": {
            "name": "sdlxliff",
            "schema": _MANIFEST_SCHEMA,
            "version": _MANIFEST_VERSION,
        },
        "input_format": "sdlxliff",
        "files": manifest_files,
        "languages": declared_languages,
        "extension_namespaces": sorted(extension_namespaces),
        "rules": {
            "content_type": list(options.content_type_rules),
            "exclusions": list(options.exclude_rules),
        },
        "rule_matches": {
            "content_type": content_type_matches,
            "exclusions": exclude_rule_matches,
        },
        "content_type_matches": content_type_matches,
        "excluded": excluded,
        "counts": {
            "selected_files": len(selected),
            "unselected_supported_files": len(unselected),
            "parsed_segments": parsed_segment_count,
            "included_segments": len(segments),
            "excluded_segments": len(excluded),
            "content_type_matches": len(content_type_matches),
            "tm_candidates": len(tm_candidate_ids),
            "locked_segments": locked_segment_count,
            "protected_segments": sum(
                1 for segment in segments if segment.get("protected")
            ),
        },
        "tm_protection": options.tm_protection,
        "protection_evidence": protection_evidence,
        "unselected_supported_files": unselected,
    }
    return SDLXLIFFImportResult(
        headers=["来源文件", "TU ID", "SDL Segment ID", "原文", "译文"],
        rows_raw=rows_raw,
        segments=segments,
        source_lang=source_lang,
        target_lang=target_lang,
        input_paths=[str(input_path.resolve()) for input_path, _ in selected],
        manifest=manifest,
        tm_candidates={
            "candidate_ids": tm_candidate_ids,
            "segments": tm_candidate_segments,
        },
    )


def _register_namespaces(namespace_map: Mapping[str, str]) -> None:
    for prefix, uri in namespace_map.items():
        if prefix == "xml" or re.fullmatch(r"ns\d+", prefix or ""):
            continue
        try:
            ET.register_namespace(prefix, uri)
        except ValueError:
            continue


def _fragment_root(
    value: str,
    *,
    namespace_map: Mapping[str, str],
    default_namespace: str,
) -> ET.Element:
    declarations = [f'xmlns="{escape(default_namespace, quote=True)}"']
    for prefix, uri in sorted(namespace_map.items()):
        if not prefix or prefix == "xml" or uri == default_namespace:
            continue
        declarations.append(
            f'xmlns:{prefix}="{escape(uri, quote=True)}"'
        )
    wrapper = f"<lqe-wrapper {' '.join(declarations)}>{value}</lqe-wrapper>"
    try:
        return ET.fromstring(wrapper)
    except ET.ParseError as exc:
        if "<" not in value and ">" not in value:
            fallback = ET.Element("lqe-wrapper")
            fallback.text = value
            return fallback
        raise SDLXLIFFImportError(
            f"corrected mixed content is not well-formed XML: {exc}"
        ) from exc


def _replace_mixed_content(
    target: ET.Element,
    value: str,
    *,
    namespace_map: Mapping[str, str],
    default_namespace: str,
) -> None:
    wrapper = _fragment_root(
        value,
        namespace_map=namespace_map,
        default_namespace=default_namespace,
    )
    for child in list(target):
        target.remove(child)
    target.text = wrapper.text
    for child in list(wrapper):
        wrapper.remove(child)
        target.append(child)


def _insert_after(parent: ET.Element, anchor: ET.Element, child: ET.Element) -> None:
    parent.insert(list(parent).index(anchor) + 1, child)


def _v1_target_for_segment(
    tu: ET.Element,
    source_ref: Mapping[str, object],
) -> ET.Element:
    context = str(source_ref.get("relative_path") or "SDLXLIFF")
    seg_source = _single_direct_child(tu, "seg-source", context)
    source = _single_direct_child(tu, "source", context)
    target = _single_direct_child(tu, "target", context)
    segment_id = source_ref.get("sdl_segment_id")
    if seg_source is None:
        if target is None:
            target = ET.Element(_X + "target")
            anchor = source
            if anchor is None:
                _fail("trans-unit is missing source", context)
            _insert_after(tu, anchor, target)
        return target

    if target is None:
        target = ET.Element(_X + "target")
        _insert_after(tu, seg_source, target)
    markers = [
        item
        for item in target.iter(_X + "mrk")
        if _local_attribute(item, "mtype") == "seg"
    ]
    if not markers:
        target.text = None
        for child in list(target):
            target.remove(child)
        for source_marker in _segmentation_markers(seg_source, context):
            marker = ET.SubElement(
                target,
                _X + "mrk",
                {
                    "mtype": "seg",
                    "mid": _local_attribute(source_marker, "mid") or "",
                },
            )
            marker.text = None
        markers = list(target)
    matches = [
        marker
        for marker in markers
        if _local_attribute(marker, "mid") == segment_id
    ]
    if len(matches) != 1:
        _fail(
            f"cannot locate target mrk for segment {segment_id!r}",
            context,
        )
    return matches[0]


def _render_v1_writeback(
    root: ET.Element,
    namespace_map: Mapping[str, str],
    refs: list[tuple[Mapping[str, object], str]],
) -> None:
    files = [child for child in root if child.tag == _X + "file"]
    for source_ref, corrected in refs:
        file_index = int(source_ref.get("file_index", 0))
        if file_index < 0 or file_index >= len(files):
            _fail("file index no longer exists", str(source_ref.get("relative_path")))
        body = _single_direct_tag(
            files[file_index],
            _X + "body",
            "body",
            str(source_ref.get("relative_path")),
        )
        if body is None:
            _fail("XLIFF file is missing body", str(source_ref.get("relative_path")))
        units = list(_iter_trans_units(body, str(source_ref.get("relative_path"))))
        unit_index = int(source_ref.get("tu_index", 0))
        if unit_index < 0 or unit_index >= len(units):
            _fail("trans-unit index no longer exists", str(source_ref.get("relative_path")))
        tu = units[unit_index]
        expected_tu_id = source_ref.get("tu_id")
        if expected_tu_id is not None and _local_attribute(tu, "id") != expected_tu_id:
            _fail("trans-unit id changed after read", str(source_ref.get("relative_path")))
        target = _v1_target_for_segment(tu, source_ref)
        _replace_mixed_content(
            target,
            corrected,
            namespace_map=namespace_map,
            default_namespace=XLIFF_NS,
        )


def _render_v2_writeback(
    root: ET.Element,
    namespace_map: Mapping[str, str],
    refs: list[tuple[Mapping[str, object], str]],
) -> None:
    files = [child for child in root if child.tag == _X2 + "file"]
    for source_ref, corrected in refs:
        file_index = int(source_ref.get("file_index", 0))
        if file_index < 0 or file_index >= len(files):
            _fail("file index no longer exists", str(source_ref.get("relative_path")))
        units = list(_iter_xliff2_units(files[file_index]))
        unit_index = int(source_ref.get("tu_index", 0))
        if unit_index < 0 or unit_index >= len(units):
            _fail("unit index no longer exists", str(source_ref.get("relative_path")))
        unit = units[unit_index]
        expected_unit_id = source_ref.get("unit_id", source_ref.get("tu_id"))
        if _local_attribute(unit, "id") != expected_unit_id:
            _fail("unit id changed after read", str(source_ref.get("relative_path")))
        xml_segments = [
            child for child in unit if child.tag == _X2 + "segment"
        ]
        segment_index = int(source_ref.get("segment_index", 0))
        if segment_index < 0 or segment_index >= len(xml_segments):
            _fail("segment index no longer exists", str(source_ref.get("relative_path")))
        xml_segment = xml_segments[segment_index]
        expected_segment_id = source_ref.get(
            "segment_id", source_ref.get("sdl_segment_id")
        )
        actual_segment_id = _local_attribute(xml_segment, "id") or str(segment_index)
        if actual_segment_id != expected_segment_id:
            _fail("segment id changed after read", str(source_ref.get("relative_path")))
        sources = [
            child for child in xml_segment if child.tag == _X2 + "source"
        ]
        targets = [
            child for child in xml_segment if child.tag == _X2 + "target"
        ]
        if len(sources) != 1 or len(targets) > 1:
            _fail("segment source/target structure changed after read", str(source_ref.get("relative_path")))
        if targets:
            target = targets[0]
        else:
            target = ET.Element(_X2 + "target")
            _insert_after(xml_segment, sources[0], target)
        _replace_mixed_content(
            target,
            corrected,
            namespace_map=namespace_map,
            default_namespace=XLIFF2_NS,
        )


def render_xliff_writeback(
    path: Path,
    *,
    segments: Sequence[Mapping[str, object]],
    corrections: Mapping[int, str],
) -> list[tuple[str, bytes]]:
    selected, _ = _selected_files(Path(path))
    by_path: dict[str, list[tuple[Mapping[str, object], str]]] = {}
    known_ids = set()
    for segment in segments:
        segment_id = segment.get("id")
        if type(segment_id) is not int:
            raise SDLXLIFFImportError("segment id must be an integer")
        if segment_id in known_ids:
            raise SDLXLIFFImportError(f"duplicate segment id: {segment_id}")
        known_ids.add(segment_id)
        if segment_id not in corrections:
            continue
        corrected = corrections[segment_id]
        if not isinstance(corrected, str):
            raise SDLXLIFFImportError(
                f"correction for segment {segment_id} must be a string"
            )
        source_ref = segment.get("source_ref")
        if not isinstance(source_ref, Mapping):
            raise SDLXLIFFImportError(
                f"segment {segment_id} has no source_ref"
            )
        relative_path = source_ref.get("relative_path")
        if not isinstance(relative_path, str) or not relative_path:
            raise SDLXLIFFImportError(
                f"segment {segment_id} has invalid source_ref.relative_path"
            )
        by_path.setdefault(relative_path, []).append((source_ref, corrected))
    unknown = sorted(set(corrections) - known_ids)
    if unknown:
        raise SDLXLIFFImportError(f"unknown correction segment ids: {unknown}")
    selected_paths = {relative_path for _, relative_path in selected}
    unknown_paths = sorted(set(by_path) - selected_paths)
    if unknown_paths:
        raise SDLXLIFFImportError(
            f"correction source paths are not in the input: {unknown_paths}"
        )

    outputs: list[tuple[str, bytes]] = []
    for input_path, relative_path in selected:
        data = input_path.read_bytes()
        kind = _document_kind(data, relative_path)
        if kind == "xliff2":
            root, namespace_map, _ = _parse_xliff2_xml(data, relative_path)
            _render_v2_writeback(
                root, namespace_map, by_path.get(relative_path, [])
            )
        else:
            root, namespace_map, _ = _parse_xml(data, relative_path)
            _render_v1_writeback(
                root, namespace_map, by_path.get(relative_path, [])
            )
        _register_namespaces(namespace_map)
        outputs.append(
            (
                relative_path,
                ET.tostring(root, encoding="utf-8", xml_declaration=True),
            )
        )
    return outputs
