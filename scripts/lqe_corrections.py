"""Validate local correction edits and build complete corrected translations."""

from __future__ import annotations

import copy
import re
from collections import Counter

from lqe_engine import current_target, get_review_policy
from lqe_target_form import (
    introduced_edit_boundary_particle_defects,
    introduced_target_form_defects,
)


class CheckFormatError(ValueError):
    pass


_ISSUE_FIELDS = (
    "category",
    "severity",
    "comment",
    "term_source",
    "expected_targets",
    "term_spans",
    "protected",
    "repeated",
    "resolution_status",
    "reason_codes",
    "non_authorizing_evidence",
)
_EDIT_REQUIRED_FIELDS = {"from", "to", "evidence"}
_EDIT_OPTIONAL_FIELDS = {"start", "end"}
_PROVENANCE_FIELDS = {
    "finding_origin",
    "ai_reviewed",
    "review_module",
    "reviewed_segment_id",
    "edit_origin",
    "ai_edited",
}
_PROVENANCE_ORIGINS = {"ai_module", "machine_precheck", "script_derived"}
_VARIABLE_RE = re.compile(r"\{[^{}]*\}|%(?:\d+\$)?[sd]")
_TAG_RE = re.compile(
    r"<[^<>]+>|\[[^\[\]]+\]|#(?:G|C|Y|E)(?=$|[^A-Za-z])"
)
_TERM_FIELDS = {"term_source", "expected_targets", "term_spans"}
_TERM_SPAN_GROUPS = {"source", "target"}
_TERM_SPAN_FIELDS = {"start", "end", "text"}
_RESOLUTION_STATUSES = {
    "resolved",
    "reference_allowed",
    "human_choice_required",
    "conflict",
}
_NON_AUTHORIZING_FIELDS = {
    "source_term",
    "cannot_authorize_source",
    "target",
    "reason",
}


def _canonical_resolution_fields(value: dict, *, label: str) -> dict:
    output = {}
    status = value.get("resolution_status")
    if status is not None:
        if status not in _RESOLUTION_STATUSES:
            raise CheckFormatError(f"{label}: resolution_status is invalid")
        if status == "resolved" and value["needs_confirmation"] is True:
            raise CheckFormatError(
                f"{label}: resolved issue must not need confirmation"
            )
        if status in {
            "reference_allowed",
            "human_choice_required",
            "conflict",
        }:
            if value["needs_confirmation"] is not True:
                raise CheckFormatError(
                    f"{label}: {status} issue must need confirmation"
                )
            if value.get("edit") is not None:
                raise CheckFormatError(
                    f"{label}: {status} issue must set edit to null"
                )
        output["resolution_status"] = status

    reason_codes = value.get("reason_codes")
    if reason_codes is not None:
        if (
            not isinstance(reason_codes, list)
            or not reason_codes
            or any(
                not isinstance(code, str) or not code.strip()
                for code in reason_codes
            )
            or len(reason_codes) != len(set(reason_codes))
        ):
            raise CheckFormatError(
                f"{label}: reason_codes must be a unique non-empty string array"
            )
        output["reason_codes"] = list(reason_codes)

    evidence = value.get("non_authorizing_evidence")
    if evidence is not None:
        if not isinstance(evidence, list) or not evidence:
            raise CheckFormatError(
                f"{label}: non_authorizing_evidence must be a non-empty array"
            )
        canonical = []
        for index, item in enumerate(evidence):
            item_label = f"{label}.non_authorizing_evidence[{index}]"
            if not isinstance(item, dict) or set(item) != _NON_AUTHORIZING_FIELDS:
                raise CheckFormatError(f"{item_label}: fields are invalid")
            if any(
                not isinstance(item[field], str) or not item[field].strip()
                for field in _NON_AUTHORIZING_FIELDS
            ):
                raise CheckFormatError(
                    f"{item_label}: values must be non-empty strings"
                )
            canonical.append(copy.deepcopy(item))
        output["non_authorizing_evidence"] = canonical
    return output


def _canonical_term_span_list(
    value: object,
    *,
    label: str,
    require_nonempty: bool,
) -> list[dict]:
    if not isinstance(value, list):
        raise CheckFormatError(f"{label} must be an array")
    if require_nonempty and not value:
        raise CheckFormatError(f"{label} must be non-empty")

    spans = []
    seen = set()
    previous_key = None
    previous_end = None
    for index, span in enumerate(value):
        span_label = f"{label}[{index}]"
        if not isinstance(span, dict):
            raise CheckFormatError(f"{span_label} must be an object")
        if set(span) != _TERM_SPAN_FIELDS:
            raise CheckFormatError(f"{span_label} has invalid fields")
        start = span["start"]
        end = span["end"]
        text = span["text"]
        if type(start) is not int or type(end) is not int:
            raise CheckFormatError(f"{span_label} start/end must be integers")
        if start < 0 or end <= start:
            raise CheckFormatError(
                f"{span_label} must use a non-empty 0-based half-open range"
            )
        if not isinstance(text, str) or not text:
            raise CheckFormatError(f"{span_label} text must be a non-empty string")

        key = (start, end, text)
        if key in seen:
            raise CheckFormatError(f"{label} contains a duplicate span")
        if previous_key is not None and key < previous_key:
            raise CheckFormatError(f"{label} must be sorted")
        if previous_end is not None and start < previous_end:
            raise CheckFormatError(f"{label} spans must not overlap")
        seen.add(key)
        previous_key = key
        previous_end = end
        spans.append({"start": start, "end": end, "text": text})
    return spans


def _canonical_term_spans(value: object, *, label: str) -> dict:
    if not isinstance(value, dict):
        raise CheckFormatError(f"{label}: term_spans must be an object")
    if set(value) != _TERM_SPAN_GROUPS:
        raise CheckFormatError(
            f"{label}: term_spans must contain exactly source and target"
        )
    return {
        "source": _canonical_term_span_list(
            value["source"],
            label=f"{label}.term_spans.source",
            require_nonempty=True,
        ),
        "target": _canonical_term_span_list(
            value["target"],
            label=f"{label}.term_spans.target",
            require_nonempty=False,
        ),
    }


def _canonical_issue_term_fields(value: dict, *, label: str) -> dict | None:
    present_term_fields = _TERM_FIELDS.intersection(value)
    if value.get("category") == "Terminology" and present_term_fields != _TERM_FIELDS:
        raise CheckFormatError(
            f"{label}: Terminology issue requires term_source, "
            "expected_targets, and term_spans"
        )
    if present_term_fields and present_term_fields != _TERM_FIELDS:
        raise CheckFormatError(
            f"{label}: term_source, expected_targets, and term_spans "
            "must be provided together"
        )
    if not present_term_fields:
        return None

    term_source = value["term_source"]
    expected_targets = value["expected_targets"]
    if not isinstance(term_source, str) or not term_source.strip():
        raise CheckFormatError(f"{label}: term_source must be a non-empty string")
    if (
        not isinstance(expected_targets, list)
        or not expected_targets
        or not all(
            isinstance(target, str) and target.strip()
            for target in expected_targets
        )
    ):
        raise CheckFormatError(
            f"{label}: expected_targets must be an array of non-empty strings"
        )
    return {
        "term_source": term_source,
        "expected_targets": copy.deepcopy(expected_targets),
        "term_spans": _canonical_term_spans(value["term_spans"], label=label),
    }


def validate_error_history_term_contract(
    history: object,
    *,
    segments: object,
    label: str = "error_history",
) -> None:
    if not isinstance(history, list):
        return
    if not isinstance(segments, list):
        segments = []
    segment_by_id = {
        segment.get("id"): segment
        for segment in segments
        if isinstance(segment, dict) and type(segment.get("id")) is int
    }
    for entry_index, entry in enumerate(history):
        if not isinstance(entry, dict):
            continue
        term_items = []
        results = entry.get("errors", [])
        if not isinstance(results, list):
            continue
        for result_index, result in enumerate(results):
            if not isinstance(result, dict):
                continue
            issues = result.get("errors", [])
            if not isinstance(issues, list):
                continue
            for issue_index, issue in enumerate(issues):
                if not isinstance(issue, dict):
                    continue
                if issue.get("category") != "Terminology" and not (
                    _TERM_FIELDS.intersection(issue)
                ):
                    continue
                issue_label = (
                    f"{label}[{entry_index}].errors[{result_index}]."
                    f"errors[{issue_index}]"
                )
                canonical_fields = _canonical_issue_term_fields(
                    issue, label=issue_label
                )
                segment_id = result.get("id")
                if type(segment_id) is not int:
                    raise CheckFormatError(f"{issue_label}: result id must be an integer")
                term_items.append((segment_id, canonical_fields, issue_label))

        if not term_items:
            continue
        snapshots = entry.get("review_targets")
        if not isinstance(snapshots, dict):
            raise CheckFormatError(
                f"{label}[{entry_index}].review_targets is required for term history"
            )
        if not all(
            isinstance(key, str) and isinstance(target, str)
            for key, target in snapshots.items()
        ):
            raise CheckFormatError(
                f"{label}[{entry_index}].review_targets must map string ids "
                "to string targets"
            )
        missing = [
            segment_id
            for segment_id in sorted({item[0] for item in term_items})
            if str(segment_id) not in snapshots
        ]
        if missing:
            raise CheckFormatError(
                f"{label}[{entry_index}].review_targets is missing term "
                f"segment ids {missing}"
            )
        for segment_id, canonical_fields, issue_label in term_items:
            segment = segment_by_id.get(segment_id)
            if segment is None:
                raise CheckFormatError(
                    f"{issue_label}: segment {segment_id} is missing from state"
                )
            source = segment.get("source")
            if not isinstance(source, str):
                raise CheckFormatError(
                    f"{issue_label}: segment source must be a string"
                )
            _validate_term_span_slices(
                canonical_fields,
                source,
                snapshots[str(segment_id)],
                label=issue_label,
            )


def _canonical_review_provenance(value: object, *, label: str) -> dict:
    if not isinstance(value, dict):
        raise CheckFormatError(f"{label}: review_provenance must be an object")
    if set(value) != _PROVENANCE_FIELDS:
        raise CheckFormatError(f"{label}: review_provenance has invalid fields")

    ai_reviewed = value["ai_reviewed"]
    if type(ai_reviewed) is not bool:
        raise CheckFormatError(f"{label}: ai_reviewed must be boolean")
    if type(value["ai_edited"]) is not bool:
        raise CheckFormatError(f"{label}: ai_edited must be boolean")

    finding_origin = value["finding_origin"]
    if finding_origin not in _PROVENANCE_ORIGINS:
        raise CheckFormatError(
            f"{label}: review_provenance finding_origin is invalid"
        )
    edit_origin = value["edit_origin"]
    if edit_origin is not None and edit_origin not in _PROVENANCE_ORIGINS:
        raise CheckFormatError(
            f"{label}: review_provenance edit_origin is invalid"
        )
    review_module = value["review_module"]
    if review_module is not None and (
        not isinstance(review_module, str) or not review_module.strip()
    ):
        raise CheckFormatError(
            f"{label}: review_provenance review_module is invalid"
        )
    reviewed_segment_id = value["reviewed_segment_id"]
    if reviewed_segment_id is not None and type(reviewed_segment_id) is not int:
        raise CheckFormatError(
            f"{label}: review_provenance reviewed_segment_id is invalid"
        )

    if ai_reviewed:
        if review_module is None or reviewed_segment_id is None:
            raise CheckFormatError(
                f"{label}: AI-reviewed provenance requires review_module and "
                "reviewed_segment_id"
            )
    elif review_module is not None or reviewed_segment_id is not None:
        raise CheckFormatError(
            f"{label}: unreviewed provenance cannot name a review module or segment"
        )
    if finding_origin == "ai_module" and not ai_reviewed:
        raise CheckFormatError(
            f"{label}: AI-origin finding must be AI-reviewed"
        )
    if finding_origin == "ai_module" and edit_origin == "machine_precheck":
        raise CheckFormatError(
            f"{label}: AI-origin finding cannot claim a machine edit"
        )
    if edit_origin == "ai_module" and not ai_reviewed:
        raise CheckFormatError(
            f"{label}: AI-origin edit requires AI review"
        )

    return {
        "finding_origin": finding_origin,
        "ai_reviewed": ai_reviewed,
        "ai_edited": False,
        "review_module": review_module,
        "reviewed_segment_id": reviewed_segment_id,
        "edit_origin": edit_origin,
    }


def _canonical_issue(
    value: object,
    *,
    label: str,
    allow_internal_provenance: bool = False,
    require_internal_provenance: bool = False,
    review_policy: dict | None = None,
) -> dict:
    if not isinstance(value, dict):
        raise CheckFormatError(f"{label}: issue must be an object")

    for field in ("category", "severity", "comment"):
        if field not in value or not isinstance(value[field], str):
            raise CheckFormatError(f"{label}: {field} must be a string")
    if not value["comment"].strip():
        raise CheckFormatError(f"{label}: comment must be non-empty")
    if "needs_confirmation" not in value:
        raise CheckFormatError(f"{label}: needs_confirmation is required")
    needs_confirmation = value["needs_confirmation"]
    if type(needs_confirmation) is not bool:
        raise CheckFormatError(f"{label}: needs_confirmation must be boolean")
    for field in ("protected", "repeated"):
        if field in value and type(value[field]) is not bool:
            raise CheckFormatError(f"{label}: {field} must be boolean")
    canonical_term_fields = _canonical_issue_term_fields(value, label=label)

    if "edit" in value:
        edit = value["edit"]
        if edit is not None and not isinstance(edit, dict):
            raise CheckFormatError(f"{label}: edit must be an object or null")
        edit = copy.deepcopy(edit) if edit is not None else None
    else:
        edit = None
    value_with_edit = dict(value)
    value_with_edit["edit"] = edit
    canonical_resolution_fields = _canonical_resolution_fields(
        value_with_edit,
        label=label,
    )
    policy = get_review_policy(
        {"review_policy": review_policy} if review_policy is not None else {}
    )
    if value["severity"] == "Minor" and not policy["minor_edits_allowed"]:
        if needs_confirmation is not True:
            raise CheckFormatError(
                f"{label}: Minor issue must set needs_confirmation to true"
            )
        if edit is not None:
            raise CheckFormatError(
                f"{label}: Minor issue must set edit to null"
            )

    result = {
        key: copy.deepcopy(value[key])
        for key in _ISSUE_FIELDS
        if key in value
        and key not in {
            "resolution_status",
            "reason_codes",
            "non_authorizing_evidence",
        }
    }
    if canonical_term_fields is not None:
        result.update(canonical_term_fields)
    result.update(canonical_resolution_fields)
    if "precheck_ref" in value:
        precheck_ref = value["precheck_ref"]
        if not isinstance(precheck_ref, str) or not precheck_ref.strip():
            raise CheckFormatError(f"{label}: precheck_ref must be a non-empty string")
        result["precheck_ref"] = precheck_ref
    if require_internal_provenance and "review_provenance" not in value:
        raise CheckFormatError(f"{label}: review_provenance is required")
    if allow_internal_provenance and "review_provenance" in value:
        result["review_provenance"] = _canonical_review_provenance(
            value["review_provenance"], label=label
        )
        edit_origin = result["review_provenance"]["edit_origin"]
        if (edit is None) != (edit_origin is None):
            raise CheckFormatError(
                f"{label}: edit_origin must be null exactly when edit is null"
            )
    result["needs_confirmation"] = needs_confirmation
    result["edit"] = edit
    return result


def normalize_check_entries(
    entries: object,
    *,
    label: str,
    allow_internal_provenance: bool = False,
    require_internal_provenance: bool = False,
    review_policy: dict | None = None,
) -> list[dict]:
    if require_internal_provenance and not allow_internal_provenance:
        raise CheckFormatError(
            f"{label}: requiring provenance also requires internal provenance mode"
        )
    if not isinstance(entries, list):
        raise CheckFormatError(f"{label}: check entries must be an array")

    normalized = []
    seen_ids = set()
    for index, entry in enumerate(entries):
        entry_label = f"{label}[{index}]"
        if not isinstance(entry, dict):
            raise CheckFormatError(f"{entry_label}: entry must be an object")
        if "id" not in entry or type(entry["id"]) is not int:
            raise CheckFormatError(f"{entry_label}: id must be an integer")
        segment_id = entry["id"]
        if segment_id in seen_ids:
            raise CheckFormatError(f"{label}: duplicate id {segment_id}")
        seen_ids.add(segment_id)
        if "corrected" in entry:
            raise CheckFormatError(f"{entry_label}: corrected is not allowed")
        issues = entry.get("issues")
        if not isinstance(issues, list):
            raise CheckFormatError(f"{entry_label}: issues must be an array")
        normalized.append(
            {
                "id": segment_id,
                "issues": [
                    _canonical_issue(
                        issue,
                        label=f"{entry_label}.issues[{issue_index}]",
                        allow_internal_provenance=allow_internal_provenance,
                        require_internal_provenance=require_internal_provenance,
                        review_policy=review_policy,
                    )
                    for issue_index, issue in enumerate(issues)
                ],
            }
        )
    return normalized


def _validate_evidence(value: object, *, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise CheckFormatError(f"{label}: edit evidence must be an object or null")
    if set(value) != {"type", "source", "target"}:
        raise CheckFormatError(f"{label}: edit evidence has invalid fields")
    if not all(isinstance(value.get(field), str) for field in value):
        raise CheckFormatError(f"{label}: edit evidence fields must be strings")


def _occurrence_starts(text: str, value: str) -> list[int]:
    starts = []
    start = text.find(value)
    while start >= 0:
        starts.append(start)
        start = text.find(value, start + 1)
    return starts


def _resolve_edit(value: dict, original: str, *, label: str) -> dict:
    fields = set(value)
    if not _EDIT_REQUIRED_FIELDS.issubset(fields) or not fields.issubset(
        _EDIT_REQUIRED_FIELDS | _EDIT_OPTIONAL_FIELDS
    ):
        raise CheckFormatError(f"{label}: edit has invalid fields")

    frm = value["from"]
    to = value["to"]
    if not isinstance(frm, str) or not frm or not isinstance(to, str):
        raise CheckFormatError(f"{label}: edit from/to must be non-empty/string")
    _validate_evidence(value["evidence"], label=label)

    has_start = "start" in value
    has_end = "end" in value
    if has_start != has_end:
        raise CheckFormatError(f"{label}: edit start/end must be provided together")
    if has_start:
        start = value["start"]
        end = value["end"]
        if type(start) is not int or type(end) is not int:
            raise CheckFormatError(f"{label}: edit start/end must be integers")
        if start < 0 or end < start or end > len(original) or original[start:end] != frm:
            raise CheckFormatError(f"{label}: edit start/end does not match from")
    else:
        starts = _occurrence_starts(original, frm)
        if len(starts) != 1:
            raise CheckFormatError(f"{label}: edit from must be unique without start/end")
        start = starts[0]
        end = start + len(frm)

    return {
        "from": frm,
        "to": to,
        "start": start,
        "end": end,
        "evidence": copy.deepcopy(value["evidence"]),
    }


def _overlaps(left: dict, right: dict) -> bool:
    return left["start"] < right["end"] and right["start"] < left["end"]


def _same_edit(left: dict, right: dict) -> bool:
    return all(left[key] == right[key] for key in ("start", "end", "from", "to"))


def _term_spans(original: str, term_hits: object) -> list[tuple[int, int]]:
    spans = []
    if not isinstance(term_hits, list):
        return spans
    for hit in term_hits:
        if not isinstance(hit, dict):
            continue
        matched_text = hit.get("matched_text")
        value = (
            matched_text
            if isinstance(matched_text, str) and matched_text
            else hit.get("target")
        )
        if not isinstance(value, str) or not value:
            continue
        start = original.find(value)
        while start >= 0:
            spans.append((start, start + len(value)))
            start = original.find(value, start + 1)
    return spans


def _requires_term_evidence(segment: dict, resolved: dict, original: str) -> bool:
    if segment.get("kind") == "name":
        return True
    if any(
        resolved["start"] < end and start < resolved["end"]
        for start, end in _term_spans(original, segment.get("term_hits"))
    ):
        return True
    targets = [
        hit.get("target")
        for hit in segment.get("term_hits", [])
        if isinstance(hit, dict) and isinstance(hit.get("target"), str)
    ]
    return any(target and target in resolved["to"] for target in targets)


def _has_matching_confirmed_term(
    segment: dict, resolved: dict, original: str
) -> bool:
    evidence = resolved["evidence"]
    if not isinstance(evidence, dict) or evidence.get("type") != "confirmed_term":
        return False
    if evidence.get("target") != resolved["to"]:
        return False
    term_hits = segment.get("term_hits")
    if not isinstance(term_hits, list):
        return False
    if segment.get("kind") == "name":
        if resolved["start"] != 0 or resolved["end"] != len(original):
            return False
        confirmed_hits = [
            hit
            for hit in term_hits
            if isinstance(hit, dict) and hit.get("confirmed") is True
        ]
        if len(confirmed_hits) != 1:
            return False
        hit = confirmed_hits[0]
        return (
            hit.get("source") == evidence.get("source")
            and hit.get("target") == evidence.get("target")
        )
    for hit in term_hits:
        if not isinstance(hit, dict):
            continue
        if not (
            hit.get("source") == evidence.get("source")
            and hit.get("target") == evidence.get("target")
            and hit.get("confirmed") is True
        ):
            continue
        matched_text = hit.get("matched_text")
        if matched_text is not None and matched_text != resolved["from"]:
            # The hit records the exact rendering found in the target; an edit
            # that cites this confirmed term but rewrites a different surface
            # form is misattributed and must not be auto-applied.
            continue
        return True
    return False


def _protection_signature(text: str, protected_texts: object) -> tuple:
    protected = []
    if isinstance(protected_texts, list):
        protected = [value for value in protected_texts if isinstance(value, str) and value]
    return (
        tuple(_VARIABLE_RE.findall(text)),
        tuple(_TAG_RE.findall(text)),
        text.count("\n"),
        text.count(r"\n"),
        tuple((value, text.count(value)) for value in protected),
    )


def _damages_protected_text(segment: dict, original: str, resolved: dict) -> bool:
    candidate = (
        original[: resolved["start"]]
        + resolved["to"]
        + original[resolved["end"] :]
    )
    return _protection_signature(candidate, segment.get("protected_texts")) != (
        _protection_signature(original, segment.get("protected_texts"))
    )


def _downgrade_edit(error: dict, reason_codes: list[str] | None = None) -> None:
    error["needs_confirmation"] = True
    error["edit"] = None
    if error.get("resolution_status") == "resolved":
        error.pop("resolution_status", None)
    if reason_codes:
        existing = error.get("reason_codes", [])
        error["reason_codes"] = list(dict.fromkeys([*existing, *reason_codes]))
    if "review_provenance" in error:
        error["review_provenance"]["edit_origin"] = None


def _edit_key(resolved: dict) -> tuple:
    return tuple(resolved[field] for field in ("start", "end", "from", "to"))


def _apply_edit_groups(original: str, groups: dict[tuple, dict]) -> str:
    corrected = original
    for group in sorted(
        groups.values(),
        key=lambda value: value["resolved"]["start"],
        reverse=True,
    ):
        resolved = group["resolved"]
        corrected = (
            corrected[: resolved["start"]]
            + resolved["to"]
            + corrected[resolved["end"] :]
        )
    return corrected


def _adjacent_edit_clusters(groups: dict[tuple, dict]) -> list[list[tuple]]:
    ordered = sorted(
        groups,
        key=lambda key: (groups[key]["resolved"]["start"], key),
    )
    clusters: list[list[tuple]] = []
    cluster_end = None
    for key in ordered:
        resolved = groups[key]["resolved"]
        if clusters and resolved["start"] == cluster_end:
            clusters[-1].append(key)
        else:
            clusters.append([key])
        cluster_end = resolved["end"]
    return clusters


def _cluster_particle_defects(
    original: str,
    groups: dict[tuple, dict],
    keys: list[tuple],
    target_form_policy: dict | None,
) -> list[str]:
    if target_form_policy is None:
        return []
    resolved_items = [groups[key]["resolved"] for key in keys]
    start = min(item["start"] for item in resolved_items)
    end = max(item["end"] for item in resolved_items)
    replacement = original[start:end]
    for resolved in sorted(
        resolved_items,
        key=lambda item: item["start"],
        reverse=True,
    ):
        relative_start = resolved["start"] - start
        relative_end = resolved["end"] - start
        replacement = (
            replacement[:relative_start]
            + resolved["to"]
            + replacement[relative_end:]
        )
    return introduced_edit_boundary_particle_defects(
        original,
        {
            "start": start,
            "end": end,
            "from": original[start:end],
            "to": replacement,
        },
        target_form_policy,
    )


def _gate_combined_edits(
    original: str,
    errors: list[dict],
    resolved_by_index: dict[int, dict],
    conflicts: set[int],
    target_form_policy: dict | None,
) -> tuple[dict[tuple, dict], str]:
    groups: dict[tuple, dict] = {}
    for index, resolved in resolved_by_index.items():
        if index in conflicts or errors[index]["edit"] is None:
            continue
        key = _edit_key(resolved)
        group = groups.setdefault(
            key,
            {"resolved": resolved, "indices": []},
        )
        group["indices"].append(index)

    while groups and target_form_policy is not None:
        rejected_particle_cluster = False
        for keys in _adjacent_edit_clusters(groups):
            defects = _cluster_particle_defects(
                original,
                groups,
                keys,
                target_form_policy,
            )
            if not defects:
                continue
            rejected_particle_cluster = True
            for key in keys:
                group = groups.pop(key)
                for index in group["indices"]:
                    _downgrade_edit(
                        errors[index],
                        ["TARGET_FORM_MUTATION_REJECTED", *defects],
                    )
        if rejected_particle_cluster:
            continue

        corrected = _apply_edit_groups(original, groups)
        defects = introduced_target_form_defects(
            original,
            corrected,
            target_form_policy,
        )
        if not defects:
            return groups, corrected
        defect_counts = Counter(defects)
        culprit_keys = []
        for key in groups:
            without = {k: value for k, value in groups.items() if k != key}
            remaining_counts = Counter(introduced_target_form_defects(
                original,
                _apply_edit_groups(original, without),
                target_form_policy,
            ))
            if any(
                remaining_counts.get(code, 0) < count
                for code, count in defect_counts.items()
            ):
                culprit_keys.append(key)
        if not culprit_keys:
            culprit_keys = list(groups)
        for key in culprit_keys:
            group = groups.pop(key)
            for index in group["indices"]:
                _downgrade_edit(
                    errors[index],
                    ["TARGET_FORM_MUTATION_REJECTED", *defects],
                )

    return groups, _apply_edit_groups(original, groups)


def _validate_term_span_slices(
    error: dict,
    source: str,
    target: str,
    *,
    label: str,
) -> None:
    for group, text in (("source", source), ("target", target)):
        for index, span in enumerate(error["term_spans"][group]):
            start = span["start"]
            end = span["end"]
            if end > len(text) or text[start:end] != span["text"]:
                raise CheckFormatError(
                    f"{label}.term_spans.{group}[{index}] does not match "
                    f"segment {group} text"
                )
            if group == "source" and span["text"] != error["term_source"]:
                raise CheckFormatError(
                    f"{label}.term_spans.source[{index}] text must equal "
                    "term_source"
                )


def validate_reference_target(
    segment: dict,
    target: object,
    *,
    label: str = "reference target",
    target_form_policy: dict | None = None,
) -> str:
    """Validate a report-only full-translation suggestion.

    Reference targets are intentionally less restrictive than automatic edits:
    they may rewrite the whole translation, but they may not target protected
    segments or alter placeholders, tags, line breaks, or protected text.
    """
    if not isinstance(segment, dict) or type(segment.get("id")) is not int:
        raise CheckFormatError(f"{label}: segment id must be an integer")
    if segment.get("protected") is True:
        raise CheckFormatError(
            f"{label}: segment {segment['id']} is protected"
        )
    original = current_target(segment)
    if not isinstance(original, str):
        raise CheckFormatError(
            f"{label}: segment {segment['id']} target must be a string"
        )
    if not isinstance(target, str) or not target.strip():
        raise CheckFormatError(
            f"{label}: segment {segment['id']} suggestion must be a non-empty string"
        )
    if target == original:
        raise CheckFormatError(
            f"{label}: segment {segment['id']} suggestion does not change the target"
        )
    if _protection_signature(target, segment.get("protected_texts")) != (
        _protection_signature(original, segment.get("protected_texts"))
    ):
        raise CheckFormatError(
            f"{label}: segment {segment['id']} suggestion changes protected content"
        )
    introduced = introduced_target_form_defects(
        original,
        target,
        target_form_policy,
    )
    if introduced:
        raise CheckFormatError(
            f"{label}: segment {segment['id']} suggestion introduces "
            f"target-form defect(s): {', '.join(introduced)}"
        )
    return target


def build_segment_result(
    segment: dict,
    issues: list[dict],
    *,
    allow_internal_provenance: bool = False,
    require_internal_provenance: bool = False,
    review_policy: dict | None = None,
    target_form_policy: dict | None = None,
) -> dict:
    if require_internal_provenance and not allow_internal_provenance:
        raise CheckFormatError(
            "requiring provenance also requires internal provenance mode"
        )
    if not isinstance(segment, dict) or type(segment.get("id")) is not int:
        raise CheckFormatError("segment id must be an integer")
    original = current_target(segment)
    if not isinstance(original, str):
        raise CheckFormatError(f"segment {segment['id']}: target must be a string")
    if not isinstance(issues, list):
        raise CheckFormatError(f"segment {segment['id']}: issues must be an array")

    errors = [
        _canonical_issue(
            value,
            label=f"segment {segment['id']}.issues[{index}]",
            allow_internal_provenance=allow_internal_provenance,
            require_internal_provenance=require_internal_provenance,
            review_policy=review_policy,
        )
        for index, value in enumerate(issues)
    ]
    term_errors = [
        (index, error)
        for index, error in enumerate(errors)
        if "term_spans" in error
    ]
    if term_errors:
        source = segment.get("source")
        if not isinstance(source, str):
            raise CheckFormatError(
                f"segment {segment['id']}: source must be a string for "
                "issues with term_spans"
            )
        for index, error in term_errors:
            _validate_term_span_slices(
                error,
                source,
                original,
                label=f"segment {segment['id']}.issues[{index}]",
            )
    resolved_by_index = {}
    for index, error in enumerate(errors):
        edit = error["edit"]
        if error["needs_confirmation"] and edit is not None:
            raise CheckFormatError(
                f"segment {segment['id']}.issues[{index}]: confirmation issue cannot carry edit"
            )
        if edit is None:
            continue
        resolved = _resolve_edit(
            edit, original, label=f"segment {segment['id']}.issues[{index}]"
        )
        evidence = resolved["evidence"]
        evidence_target_mismatch = (
            isinstance(evidence, dict) and evidence.get("target") != resolved["to"]
        )
        has_confirmed_term_evidence = (
            isinstance(evidence, dict) and evidence.get("type") == "confirmed_term"
        )
        requires_term_evidence = _requires_term_evidence(segment, resolved, original)
        if evidence_target_mismatch or (
            (requires_term_evidence or has_confirmed_term_evidence)
            and not _has_matching_confirmed_term(segment, resolved, original)
        ):
            _downgrade_edit(error)
            continue
        if _damages_protected_text(segment, original, resolved):
            _downgrade_edit(error)
            continue
        resolved_by_index[index] = resolved

    conflicts = set()
    resolved_items = list(resolved_by_index.items())
    for left_position, (left_index, left) in enumerate(resolved_items):
        for right_index, right in resolved_items[left_position + 1 :]:
            if _overlaps(left, right) and not _same_edit(left, right):
                conflicts.update((left_index, right_index))
    for index in conflicts:
        _downgrade_edit(errors[index])

    _, corrected = _gate_combined_edits(
        original,
        errors,
        resolved_by_index,
        conflicts,
        target_form_policy,
    )

    for index, error in enumerate(errors):
        provenance = error.get("review_provenance")
        resolved = resolved_by_index.get(index)
        if not isinstance(provenance, dict) or resolved is None:
            continue
        provenance["ai_edited"] = bool(
            provenance["ai_reviewed"]
            and provenance["edit_origin"] == "ai_module"
            and index not in conflicts
            and error["edit"] is not None
            and resolved["from"] != resolved["to"]
        )

    return {
        "id": segment["id"],
        "errors": errors,
        "corrected": corrected if corrected != original else None,
    }


def verify_results(
    segments: list[dict],
    results: object,
    label: str,
    *,
    allow_internal_provenance: bool = False,
    require_internal_provenance: bool = False,
    review_policy: dict | None = None,
    target_form_policy: dict | None = None,
) -> list[dict]:
    if not isinstance(results, list):
        raise CheckFormatError(f"{label}: results must be an array")

    required = {"id", "errors", "corrected"}
    result_ids = []
    for index, entry in enumerate(results):
        entry_label = f"{label}[{index}]"
        if not isinstance(entry, dict):
            raise CheckFormatError(f"{entry_label}: result must be an object")
        missing_fields = sorted(required - entry.keys())
        if missing_fields:
            raise CheckFormatError(
                f"{entry_label}: missing fields: {missing_fields}"
            )
        if type(entry["id"]) is not int:
            raise CheckFormatError(f"{entry_label}: id must be an integer")
        result_ids.append(entry["id"])

    segment_ids = [segment.get("id") for segment in segments]
    missing = sorted(set(segment_ids) - set(result_ids))
    extra = sorted(set(result_ids) - set(segment_ids))
    duplicate_ids = sorted(
        {result_id for result_id in result_ids if result_ids.count(result_id) > 1}
    )
    if missing or extra or duplicate_ids:
        details = [f"missing={missing}", f"extra={extra}"]
        if duplicate_ids:
            details.append(f"duplicates={duplicate_ids}")
        raise CheckFormatError(f"{label} id coverage: {' '.join(details)}")

    by_id = {entry["id"]: entry for entry in results}
    verified = []
    for segment in segments:
        entry = by_id[segment["id"]]
        rebuilt = build_segment_result(
            segment,
            entry["errors"],
            allow_internal_provenance=allow_internal_provenance,
            require_internal_provenance=require_internal_provenance,
            review_policy=review_policy,
            target_form_policy=target_form_policy,
        )
        if entry["corrected"] != rebuilt["corrected"]:
            raise CheckFormatError(
                f"{label} segment {segment['id']}: corrected mismatch"
            )
        verified.append(rebuilt)
    return verified


def build_results(
    segments: list[dict],
    check_entries: list[dict],
    *,
    allow_internal_provenance: bool = False,
    require_internal_provenance: bool = False,
    review_policy: dict | None = None,
    target_form_policy: dict | None = None,
) -> list[dict]:
    normalized = normalize_check_entries(
        check_entries,
        label="checks",
        allow_internal_provenance=allow_internal_provenance,
        require_internal_provenance=require_internal_provenance,
        review_policy=review_policy,
    )
    issues_by_id = {entry["id"]: entry["issues"] for entry in normalized}
    return [
        build_segment_result(
            segment,
            issues_by_id.get(segment.get("id"), []),
            allow_internal_provenance=allow_internal_provenance,
            require_internal_provenance=require_internal_provenance,
            review_policy=review_policy,
            target_form_policy=target_form_policy,
        )
        for segment in segments
    ]
