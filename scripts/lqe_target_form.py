"""Deterministic target-form checks shared by precheck and correction gates."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from lqe_project_assets import validate_project_asset_snapshot


_HORIZONTAL_EDGE_RE = re.compile(r"^[ \t\u00a0\u202f]+|[ \t\u00a0\u202f]+$")
_DOUBLE_SPACE_RE = re.compile(r"(?<=\S) {2,}(?=\S)")
_KO_BOUNDARY_PARTICLES = ("을", "를")
_KO_EDIT_TRAILING_PARTICLE_RE = re.compile(
    r"([가-힣]+?)(을|를)$"
)
_KO_EDIT_DETACHED_PARTICLE_RE = re.compile(
    r"([가-힣]+)[ \t\u00a0\u202f.,!?…:;\"'()\[\]{}<>]+(을|를)$"
)
_PAIR_CHARS = (("(", ")"), ("[", "]"))
_QUOTE_SKIP = frozenset(" \t\r\n.,!?…:;()[]{}<>")
_PARTICLE_DETACHERS = frozenset(" \t\u00a0\u202f.,!?…:;\"'()[]{}<>")


def _has_v2_asset_contract(state: dict) -> bool:
    return any(
        key in state
        for key in (
            "project_asset_snapshot",
            "project_asset_snapshot_digest",
            "project_asset_paths",
        )
    )


def checks_path_for_state(state: object) -> str:
    if not isinstance(state, dict):
        return ""
    if (
        state.get("job_runtime_contract_version") == 2
        and _has_v2_asset_contract(state)
    ):
        snapshot = validate_project_asset_snapshot(
            state.get("project_asset_snapshot")
        )
        if state.get("project_asset_snapshot_digest") != snapshot["digest"]:
            raise ValueError("v2 project asset snapshot digest is stale")
        assets = snapshot["assets"]
        present = [
            (asset_id, entry)
            for asset_id, entry in (assets.items() if isinstance(assets, dict) else [])
            if isinstance(entry, dict)
            and entry.get("kind") == "checks"
            and entry.get("status") == "present"
        ]
        if not present:
            if state.get("checks_path"):
                raise ValueError(
                    "v2 state has a live checks path without a present checks asset"
                )
            return ""
        if len(present) != 1:
            raise ValueError("multiple present checks assets are not supported")
        asset_id, entry = present[0]
        live_path = state.get("checks_path")
        if not isinstance(live_path, str) or not live_path.strip():
            raise ValueError(
                "v2 state has a present checks asset but no live checks path"
            )
        asset_paths = state.get("project_asset_paths")
        bound = asset_paths.get(asset_id) if isinstance(asset_paths, dict) else None
        if not isinstance(bound, str) or not bound:
            raise ValueError("present checks asset has no job-bound runtime path")
        path = Path(bound)
        if not path.is_file():
            raise ValueError("job-bound checks asset is missing")
        expected = entry.get("sha256")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if not isinstance(expected, str) or digest != expected:
            raise ValueError("job-bound checks asset digest mismatch")
        return bound

    asset_paths = state.get("project_asset_paths")
    if isinstance(asset_paths, dict):
        bound = asset_paths.get("checks")
        if isinstance(bound, str) and bound:
            return bound
    fallback = state.get("checks_path")
    return fallback if isinstance(fallback, str) else ""


def load_target_form_policy(state: object) -> dict | None:
    """Load an opt-in mutation policy from the job-bound checks snapshot."""
    checks_path = checks_path_for_state(state)
    if not checks_path:
        return None
    target_lang = (
        str(state.get("target_lang") or "").casefold().replace("_", "-")
        if isinstance(state, dict)
        else ""
    )
    path = Path(checks_path)
    if not path.is_file():
        return None
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("project checks document must be an object")
    builtin = document.get("builtin", {})
    if not isinstance(builtin, dict):
        raise ValueError("project checks builtin must be an object")
    if builtin.get("target_form_gate") is not True:
        return None

    custom_patterns = []
    custom = document.get("custom", [])
    if not isinstance(custom, list):
        raise ValueError("project checks custom must be an array")
    for item in custom:
        if not isinstance(item, dict) or item.get("mutation_gate") is not True:
            continue
        if item.get("where", "target") != "target":
            continue
        check_id = item.get("id")
        pattern = item.get("pattern")
        if not isinstance(check_id, str) or not check_id or not isinstance(pattern, str):
            raise ValueError("mutation-gated custom checks require id and pattern")
        re.compile(pattern)
        custom_patterns.append({"id": check_id, "pattern": pattern})

    return {
        "target_lang": target_lang,
        "whitespace": builtin.get("whitespace", True) is not False,
        "forbidden_nbsp": builtin.get("forbidden_nbsp") is True,
        "paired_punct": builtin.get("paired_punct", True) is not False,
        "ko_single_quote_balance": (
            target_lang.split("-", 1)[0] == "ko"
            and builtin.get("ko_single_quote_balance") is True
        ),
        "ko_particle_edit_boundary": (
            target_lang.split("-", 1)[0] == "ko"
            and builtin.get("ko_particle_edit_boundary") is True
        ),
        "custom_forbidden_patterns": custom_patterns,
    }


def _is_hangul_syllable(value: str) -> bool:
    return len(value) == 1 and "가" <= value <= "힣"


def _jongseong_index(value: str) -> int | None:
    if not _is_hangul_syllable(value):
        return None
    return (ord(value) - 0xAC00) % 28


def expected_korean_particle(stem_last: str, particle: str) -> str | None:
    jongseong = _jongseong_index(stem_last)
    if jongseong is None:
        return None
    if particle in _KO_BOUNDARY_PARTICLES:
        return "을" if jongseong else "를"
    return None


def _nearest_non_quote_skip(text: str, start: int, step: int) -> str:
    index = start
    while 0 <= index < len(text) and text[index] in _QUOTE_SKIP:
        index += step
    return text[index] if 0 <= index < len(text) else ""


def korean_single_quote_count(text: str) -> int:
    count = 0
    for index, char in enumerate(text):
        if char != "'":
            continue
        immediate_left = text[index - 1] if index else ""
        immediate_right = text[index + 1] if index + 1 < len(text) else ""
        if immediate_left.isascii() and immediate_left.isalpha():
            continue
        if immediate_right.isascii() and immediate_right.isalpha():
            continue
        left = _nearest_non_quote_skip(text, index - 1, -1)
        right = _nearest_non_quote_skip(text, index + 1, 1)
        if _is_hangul_syllable(left) or _is_hangul_syllable(right):
            count += 1
    return count


def _unmatched_pair_count(text: str, opening: str, closing: str) -> int:
    depth = 0
    unmatched = 0
    for char in text:
        if char == opening:
            depth += 1
        elif char == closing:
            if depth:
                depth -= 1
            else:
                unmatched += 1
    return unmatched + depth


def target_form_defects(text: str, policy: object) -> Counter[str]:
    defects: Counter[str] = Counter()
    if not isinstance(policy, dict):
        return defects

    if policy.get("whitespace"):
        edge_matches = list(_HORIZONTAL_EDGE_RE.finditer(text))
        for match in edge_matches:
            edge = "LEADING" if match.start() == 0 else "TRAILING"
            defects[f"{edge}_HORIZONTAL_WHITESPACE"] += len(match.group(0))
        defects["DOUBLE_ASCII_SPACE"] += len(_DOUBLE_SPACE_RE.findall(text))
    if policy.get("forbidden_nbsp"):
        defects["FORBIDDEN_NBSP"] += text.count("\u00a0") + text.count("\u202f")

    if policy.get("paired_punct"):
        for opening, closing in _PAIR_CHARS:
            count = _unmatched_pair_count(text, opening, closing)
            if count:
                defects[f"UNBALANCED_PAIR:{opening}{closing}"] += count
        if text.count('"') % 2:
            defects["UNBALANCED_STRAIGHT_DOUBLE_QUOTE"] += 1
    if policy.get("ko_single_quote_balance"):
        if korean_single_quote_count(text) % 2:
            defects["UNBALANCED_KO_SINGLE_QUOTE"] += 1
    for item in policy.get("custom_forbidden_patterns", []):
        pattern = re.compile(item["pattern"])
        defects[f"PROJECT_FORBIDDEN_PATTERN:{item['id']}"] += sum(
            1 for _ in pattern.finditer(text)
        )
    return +defects


def introduced_target_form_defects(
    original: str,
    candidate: str,
    policy: object,
) -> list[str]:
    before = target_form_defects(original, policy)
    after = target_form_defects(candidate, policy)
    introduced = []
    for code, count in after.items():
        if count > before.get(code, 0):
            introduced.extend([code] * (count - before.get(code, 0)))
    return sorted(introduced)


def _narrow_edit_to_changed_core(
    original: str,
    start: int,
    end: int,
    replacement: str,
) -> tuple[int, int, str]:
    original_fragment = original[start:end]
    prefix = 0
    prefix_limit = min(len(original_fragment), len(replacement))
    while (
        prefix < prefix_limit
        and original_fragment[prefix] == replacement[prefix]
    ):
        prefix += 1

    suffix = 0
    suffix_limit = min(
        len(original_fragment) - prefix,
        len(replacement) - prefix,
    )
    while (
        suffix < suffix_limit
        and original_fragment[-1 - suffix] == replacement[-1 - suffix]
    ):
        suffix += 1

    narrowed_end = end - suffix
    replacement_end = len(replacement) - suffix if suffix else len(replacement)
    return (
        start + prefix,
        narrowed_end,
        replacement[prefix:replacement_end],
    )


def introduced_edit_boundary_particle_defects(
    original: str,
    resolved_edit: object,
    policy: object,
) -> list[str]:
    if not isinstance(policy, dict) or not policy.get("ko_particle_edit_boundary"):
        return []
    if not isinstance(resolved_edit, dict):
        return []
    start = resolved_edit.get("start")
    end = resolved_edit.get("end")
    replacement = resolved_edit.get("to")
    if type(start) is not int or type(end) is not int or not isinstance(replacement, str):
        return []
    if not 0 <= start <= end <= len(original):
        return []
    start, end, replacement = _narrow_edit_to_changed_core(
        original,
        start,
        end,
        replacement,
    )
    suffix = original[end:]
    particle = next(
        (value for value in _KO_BOUNDARY_PARTICLES if suffix.startswith(value)),
        None,
    )
    if particle is not None and end > 0:
        old_last = original[end - 1]
        if replacement:
            new_last = replacement[-1]
        elif start > 0:
            new_last = original[start - 1]
        else:
            new_last = ""
        old_expected = expected_korean_particle(old_last, particle)
        new_expected = expected_korean_particle(new_last, particle)
        if old_expected == particle:
            if new_last in _PARTICLE_DETACHERS:
                return [f"KO_PARTICLE_EDIT_BOUNDARY:{particle}->detached"]
            if new_expected not in {None, particle}:
                return [f"KO_PARTICLE_EDIT_BOUNDARY:{particle}->{new_expected}"]

    trailing_chars = " \t\r\n.,!?…:;\"'()[]{}<>"
    original_fragment = original[start:end].rstrip(trailing_chars)
    replacement_fragment = replacement.rstrip(trailing_chars)
    if (
        original_fragment in _KO_BOUNDARY_PARTICLES
        and replacement_fragment in _KO_BOUNDARY_PARTICLES
        and start > 0
    ):
        stem_last = original[start - 1]
        old_expected = expected_korean_particle(stem_last, original_fragment)
        new_expected = expected_korean_particle(stem_last, replacement_fragment)
        if (
            old_expected == original_fragment
            and new_expected not in {None, replacement_fragment}
        ):
            return [
                "KO_PARTICLE_EDIT_BOUNDARY:"
                f"{replacement_fragment}->{new_expected}"
            ]

    old_match = _KO_EDIT_TRAILING_PARTICLE_RE.search(original_fragment)
    new_match = _KO_EDIT_TRAILING_PARTICLE_RE.search(replacement_fragment)
    if old_match is None:
        return []
    old_stem, old_particle = old_match.groups()
    old_expected = expected_korean_particle(old_stem[-1], old_particle)
    if old_expected != old_particle:
        return []
    detached_match = _KO_EDIT_DETACHED_PARTICLE_RE.search(replacement_fragment)
    if detached_match is not None:
        return [
            f"KO_PARTICLE_EDIT_BOUNDARY:{detached_match.group(2)}->detached"
        ]
    if new_match is None:
        return []
    new_stem, new_particle = new_match.groups()
    new_expected = expected_korean_particle(new_stem[-1], new_particle)
    if new_expected not in {None, new_particle}:
        return [
            f"KO_PARTICLE_EDIT_BOUNDARY:{new_particle}->{new_expected}"
        ]
    return []
