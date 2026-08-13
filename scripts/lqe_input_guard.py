"""Deterministic segment identity and source-version input guards."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import unicodedata


class InputGuardError(ValueError):
    pass


def canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def source_digest(source: object) -> str:
    text = source if isinstance(source, str) else str(source or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_segment_identity(
    *,
    business_key: object,
    input_digest: str,
    container: str,
    row_index: int,
) -> dict:
    if not isinstance(input_digest, str) or len(input_digest) != 64:
        raise InputGuardError("input digest must be a SHA-256 hex string")
    if not isinstance(container, str) or not container:
        raise InputGuardError("segment container must be a non-empty string")
    if type(row_index) is not int or row_index < 0:
        raise InputGuardError("segment row index must be non-negative")
    if business_key is not None and str(business_key).strip():
        return {
            "segment_key": str(business_key).strip(),
            "key_origin": "input",
        }
    generated = canonical_digest(
        {
            "input_digest": input_digest,
            "container": container,
            "row_index": row_index,
        }
    )
    return {"segment_key": f"generated:{generated}", "key_origin": "generated"}


def ensure_unique_business_keys(segments: list[dict]) -> None:
    seen: dict[str, int] = {}
    for segment in segments:
        if segment.get("key_origin") != "input":
            continue
        key = segment.get("segment_key")
        if not isinstance(key, str) or not key:
            raise InputGuardError("input segment key must be a non-empty string")
        previous = seen.setdefault(key, segment["id"])
        if previous != segment["id"]:
            raise InputGuardError(
                f"duplicate business key {key!r}: segment {previous} and {segment['id']}"
            )


def normalize_compare_value(value: object, normalizer: str = "text") -> object:
    if normalizer not in {"text", "trim", "nfc", "casefold", "integer"}:
        raise InputGuardError(f"unsupported comparison normalizer: {normalizer!r}")
    if value is None:
        return None
    if normalizer == "integer":
        if isinstance(value, bool):
            raise InputGuardError("boolean is not a valid integer comparison value")
        try:
            return int(str(value).strip())
        except ValueError as exc:
            raise InputGuardError(f"cannot normalize integer value {value!r}") from exc
    text = str(value)
    if normalizer in {"trim", "nfc", "casefold"}:
        text = text.strip()
    if normalizer in {"nfc", "casefold"}:
        text = unicodedata.normalize("NFC", text)
    if normalizer == "casefold":
        text = text.casefold()
    return text


def _block(segment: dict, reason: dict) -> None:
    segment["input_status"] = "blocked"
    reasons = segment.setdefault("input_block_reasons", [])
    if reason not in reasons:
        reasons.append(reason)


def apply_target_source_digest_guard(
    segment: dict,
    recorded_digest: object,
) -> None:
    actual = source_digest(segment.get("source", ""))
    segment["source_digest"] = actual
    if recorded_digest is None or not str(recorded_digest).strip():
        segment.setdefault("input_warnings", []).append(
            {"code": "UNVERIFIED_TARGET_PROVENANCE"}
        )
        return
    recorded = str(recorded_digest).strip().lower()
    if recorded != actual:
        _block(
            segment,
            {
                "code": "TARGET_SOURCE_VERSION_MISMATCH",
                "expected_source_digest": recorded,
                "actual_source_digest": actual,
            },
        )


def apply_pivot_comparison(
    segment: dict,
    *,
    primary_values: dict,
    pivot_values: dict,
    comparisons: list[dict],
    authority: str,
) -> None:
    if authority not in {"authoritative", "diagnostic"}:
        raise InputGuardError(f"unsupported pivot authority: {authority!r}")
    mismatches = []
    for rule in comparisons:
        if not isinstance(rule, dict):
            raise InputGuardError("pivot comparison must be an object")
        field = rule.get("field")
        if not isinstance(field, str) or not field:
            raise InputGuardError("pivot comparison field is required")
        normalizer = rule.get("normalizer", "text")
        primary = normalize_compare_value(primary_values.get(field), normalizer)
        pivot = normalize_compare_value(pivot_values.get(field), normalizer)
        if primary != pivot:
            mismatches.append(
                {
                    "field": field,
                    "normalizer": normalizer,
                    "primary": primary,
                    "pivot": pivot,
                }
            )
    if not mismatches:
        return
    reason = {"code": "SOURCE_VERSION_MISMATCH", "mismatches": mismatches}
    if authority == "authoritative":
        _block(segment, reason)
    else:
        segment.setdefault("input_warnings", []).append(reason)


def input_guard_summary(segments: list[dict]) -> dict:
    blocked = [segment["id"] for segment in segments if segment.get("input_status") == "blocked"]
    warned = [segment["id"] for segment in segments if segment.get("input_warnings")]
    payload = {
        "schema": "lqe.input-guard-summary",
        "version": 1,
        "segments": len(segments),
        "blocked_ids": blocked,
        "warning_ids": warned,
    }
    payload["digest"] = canonical_digest(payload)
    return payload


def copy_guard_fields(segment: dict) -> dict:
    return {
        key: deepcopy(segment[key])
        for key in (
            "segment_key",
            "key_origin",
            "source_digest",
            "input_status",
            "input_block_reasons",
            "input_warnings",
        )
        if key in segment
    }
