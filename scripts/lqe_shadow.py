"""Independent shadow-context artifact contract."""

from __future__ import annotations

from copy import deepcopy
from typing import Mapping, Sequence

from lqe_split_contract import canonical_digest


SHADOW_CONTEXT_SCHEMA = "lqe.shadow-context"
SHADOW_CONTEXT_VERSION = 1


class ShadowContextError(ValueError):
    pass


def _digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ShadowContextError(f"{label} must be a lowercase SHA-256 digest")
    return value


def build_shadow_context_artifact(
    state_context: Mapping,
    segments: Sequence[Mapping],
    descriptors: Mapping,
) -> dict:
    if not isinstance(state_context, Mapping):
        raise ShadowContextError("state context must be an object")
    if state_context.get("context_pipeline", {}).get("mode") != "shadow":
        raise ShadowContextError("shadow artifact requires context_pipeline.mode=shadow")
    if not isinstance(descriptors, Mapping) or not descriptors:
        raise ShadowContextError("shadow descriptors must be a non-empty object")
    if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes)):
        raise ShadowContextError("segments must be an array")

    entries = []
    ids = set()
    for segment in segments:
        if not isinstance(segment, Mapping):
            raise ShadowContextError("shadow segment must be an object")
        segment_id = segment.get("id")
        if type(segment_id) is not int or segment_id in ids:
            raise ShadowContextError("shadow segment ids must be unique integers")
        ids.add(segment_id)
        context = segment.get("shadow_context")
        if not isinstance(context, Mapping):
            raise ShadowContextError(
                f"segment {segment_id} has no shadow_context object"
            )
        entries.append({
            "id": segment_id,
            "segment_key": segment.get("segment_key"),
            "source_digest": _digest(
                segment.get("source_digest"),
                f"segment {segment_id} source_digest",
            ),
            "shadow_context": deepcopy(dict(context)),
        })
    payload = {
        "schema": SHADOW_CONTEXT_SCHEMA,
        "version": SHADOW_CONTEXT_VERSION,
        "profile_digest": _digest(
            state_context.get("profile_digest"), "profile_digest"
        ),
        "capability_resolution_digest": _digest(
            state_context.get("capability_resolution_digest"),
            "capability_resolution_digest",
        ),
        "descriptor_digest": canonical_digest(descriptors),
        "descriptors": deepcopy(dict(descriptors)),
        "segments": entries,
    }
    payload["artifact_digest"] = canonical_digest(payload)
    return validate_shadow_context_artifact(payload)


def validate_shadow_context_artifact(value: object) -> dict:
    if not isinstance(value, dict):
        raise ShadowContextError("shadow context artifact must be an object")
    required = {
        "schema",
        "version",
        "profile_digest",
        "capability_resolution_digest",
        "descriptor_digest",
        "descriptors",
        "segments",
        "artifact_digest",
    }
    if set(value) != required:
        raise ShadowContextError("shadow context artifact fields are invalid")
    if value["schema"] != SHADOW_CONTEXT_SCHEMA:
        raise ShadowContextError("unknown shadow context schema")
    if value["version"] != SHADOW_CONTEXT_VERSION:
        raise ShadowContextError("unsupported shadow context version")
    for field in (
        "profile_digest",
        "capability_resolution_digest",
        "descriptor_digest",
        "artifact_digest",
    ):
        _digest(value[field], field)
    if not isinstance(value["descriptors"], dict) or not value["descriptors"]:
        raise ShadowContextError("shadow context descriptors are invalid")
    if value["descriptor_digest"] != canonical_digest(value["descriptors"]):
        raise ShadowContextError("shadow context descriptor digest mismatch")
    if not isinstance(value["segments"], list):
        raise ShadowContextError("shadow context segments must be an array")
    ids = []
    for entry in value["segments"]:
        if not isinstance(entry, dict) or set(entry) != {
            "id",
            "segment_key",
            "source_digest",
            "shadow_context",
        }:
            raise ShadowContextError("shadow context segment fields are invalid")
        if type(entry["id"]) is not int or not isinstance(
            entry["shadow_context"], dict
        ):
            raise ShadowContextError("shadow context segment is invalid")
        _digest(entry["source_digest"], "shadow segment source_digest")
        ids.append(entry["id"])
    if len(ids) != len(set(ids)):
        raise ShadowContextError("shadow context segment ids repeat")
    unsigned = {key: deepcopy(item) for key, item in value.items() if key != "artifact_digest"}
    if value["artifact_digest"] != canonical_digest(unsigned):
        raise ShadowContextError("shadow context artifact digest mismatch")
    return deepcopy(value)
