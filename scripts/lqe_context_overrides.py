#!/usr/bin/env python3
"""Verified job-level context overrides and context-gap scaffolding."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path
import unicodedata
from typing import Mapping, Sequence

from lqe_context import ContextContractError, canonical_field_ref
from lqe_paths import file_sha256, write_json_atomic
from lqe_profile_ingest import (
    ProfileIngestError,
    canonical_digest,
    validate_json_schema,
)


ROOT = Path(__file__).resolve().parents[1]
OVERRIDE_SCHEMA_PATH = ROOT / "schemas/context/job_context_overrides_v1.json"
GAP_SCHEMA_PATH = ROOT / "schemas/context/context_gap_report_v1.json"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PLACEHOLDER = re.compile(r"^(?:todo|tbd|unknown|待补|未提供)(?:\b|[:：])", re.I)
_TARGET_FIELDS = (
    "context.core@1.content_type",
    "context.dialogue@1.speaker_id",
    "context.dialogue@1.addressee_ids",
    "context.dialogue@1.scene_id",
    "context.dialogue@1.relationship_stage",
    "context.dialogue@1.scene_tone",
)
_ALIAS_FIELDS = {
    "context.dialogue@1.speaker_id",
    "context.dialogue@1.addressee_ids",
}


class ContextOverrideError(ValueError):
    pass


def _strict_json(path: str | Path) -> object:
    source = Path(path)

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
        output = {}
        for key, value in pairs:
            if key in output:
                raise ContextOverrideError(
                    f"duplicate JSON key {key!r} in {source}"
                )
            output[key] = value
        return output

    def reject_constant(value: str):
        raise ContextOverrideError(
            f"non-finite JSON number {value!r} in {source}"
        )

    try:
        return json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContextOverrideError(f"cannot read context overrides {source}: {exc}") from exc


def _schema(path: Path) -> dict:
    value = _strict_json(path)
    if not isinstance(value, dict):
        raise ContextOverrideError(f"bundled schema is not an object: {path}")
    return value


def _validate_schema(value: object, path: Path) -> dict:
    try:
        validated = validate_json_schema(value, _schema(path))
    except ProfileIngestError as exc:
        raise ContextOverrideError(str(exc)) from exc
    if not isinstance(validated, dict):
        raise ContextOverrideError("context document must be an object")
    return validated


def _nonempty(value: object) -> bool:
    return value not in (None, "", [], {})


def _source_digest(source: object) -> str:
    text = source if isinstance(source, str) else str(source or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def segment_set_digest(segments: Sequence[Mapping]) -> str:
    if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes)):
        raise ContextOverrideError("segments must be an array")
    records = []
    seen = set()
    for index, segment in enumerate(segments):
        if not isinstance(segment, Mapping):
            raise ContextOverrideError(f"segment {index} must be an object")
        key = segment.get("segment_key")
        if not isinstance(key, str) or not key:
            raise ContextOverrideError(f"segment {index} lacks segment_key")
        if key in seen:
            raise ContextOverrideError(f"input contains duplicate segment_key {key!r}")
        seen.add(key)
        digest = segment.get("source_digest")
        actual = _source_digest(segment.get("source", ""))
        if digest != actual or not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            raise ContextOverrideError(f"segment {key!r} has stale source_digest")
        records.append({"segment_key": key, "source_digest": digest})
    return canonical_digest(sorted(records, key=lambda item: item["segment_key"]))


def _descriptor_digest(registry: Mapping[str, Mapping]) -> str:
    if not isinstance(registry, Mapping):
        raise ContextOverrideError("context descriptor registry must be an object")
    return canonical_digest(registry)


def _field_binding(
    field_ref: str, registry: Mapping[str, Mapping]
) -> tuple[str, str, str, Mapping]:
    try:
        canonical = canonical_field_ref(field_ref, registry)
    except ContextContractError as exc:
        raise ContextOverrideError(str(exc)) from exc
    for capability_id, descriptor in registry.items():
        prefix = f"{capability_id}."
        if canonical.startswith(prefix):
            field_name = canonical[len(prefix) :]
            field = descriptor.get("fields", {}).get(field_name)
            if not isinstance(field, Mapping):
                break
            extension = capability_id.removeprefix("context.").split("@", 1)[0]
            return canonical, capability_id, extension, field
    raise ContextOverrideError(f"context field lacks a descriptor: {canonical!r}")


def _split_many(value: object) -> list[object]:
    if not _nonempty(value):
        return []
    if isinstance(value, (list, tuple)):
        items = list(value)
    elif isinstance(value, str):
        stripped = value.strip()
        parsed = None
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
        items = parsed if isinstance(parsed, list) else re.split(r"[,，;；|]", stripped)
    else:
        items = [value]
    return [item for item in items if _nonempty(item)]


def _normalize_scalar(value: object, field: Mapping, label: str) -> object:
    normalizer = field.get("normalizer")
    field_type = field.get("type")
    if normalizer == "positive_integer" or field_type == "integer":
        if type(value) is bool:
            raise ContextOverrideError(f"{label} must be an integer")
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise ContextOverrideError(f"{label} must be an integer") from exc
        if normalizer == "positive_integer" and value <= 0:
            raise ContextOverrideError(f"{label} must be a positive integer")
    elif field_type == "boolean":
        if type(value) is not bool:
            raise ContextOverrideError(f"{label} must be a boolean")
    else:
        value = str(value)
        if normalizer in {"trim", "string_list", "lowercase"}:
            value = value.strip()
        if normalizer == "lowercase":
            value = value.casefold()
    if field_type == "enum" and value not in field.get("values", []):
        raise ContextOverrideError(f"{label} is not an allowed enum value: {value!r}")
    return value


def _normalize_value(value: object, field: Mapping, label: str) -> object:
    if not _nonempty(value):
        raise ContextOverrideError(f"{label} is blank; remove it or provide a verified value")
    if field.get("cardinality") == "many" or field.get("normalizer") == "string_list":
        output = []
        for item in _split_many(value):
            normalized = _normalize_scalar(item, field, label)
            if _nonempty(normalized) and normalized not in output:
                output.append(normalized)
        if not output:
            raise ContextOverrideError(f"{label} is blank")
        return output
    normalized = _normalize_scalar(value, field, label)
    if not _nonempty(normalized):
        raise ContextOverrideError(f"{label} is blank")
    return normalized


def _context_value(context: Mapping, capability_id: str, extension: str, field: str) -> object:
    if capability_id == "context.core@1":
        core = context.get("core")
        return core.get(field) if isinstance(core, Mapping) else None
    extensions = context.get("extensions")
    if not isinstance(extensions, Mapping):
        return None
    value = extensions.get(extension, extensions.get(f"context.{extension}"))
    return value.get(field) if isinstance(value, Mapping) else None


def _put_context_value(
    context: dict,
    capability_id: str,
    extension: str,
    field: str,
    value: object,
) -> None:
    if capability_id == "context.core@1":
        context.setdefault("core", {})[field] = deepcopy(value)
        return
    extensions = context.setdefault("extensions", {})
    storage_key = (
        f"context.{extension}"
        if f"context.{extension}" in extensions and extension not in extensions
        else extension
    )
    extensions.setdefault(storage_key, {})[field] = deepcopy(value)


def _provenance_ref(capability_id: str, extension: str, field: str) -> str:
    if capability_id == "context.core@1":
        return f"context.core.{field}"
    return f"context.extensions.{extension}.{field}"


def _condition_state(
    descriptor: Mapping,
    context: Mapping,
    registry: Mapping[str, Mapping],
) -> tuple[str, list[str]]:
    present_fields = descriptor.get("applies_if_present", [])
    for raw_ref in present_fields:
        canonical, capability_id, extension, _ = _field_binding(
            str(raw_ref), registry
        )
        field = canonical[len(capability_id) + 1 :]
        if _nonempty(_context_value(context, capability_id, extension, field)):
            return "applicable", []
    conditions = descriptor.get("applies_when", {})
    if not isinstance(conditions, Mapping) or not conditions:
        return "unconditional", []
    missing = []
    for raw_ref, accepted in conditions.items():
        canonical, capability_id, extension, _ = _field_binding(str(raw_ref), registry)
        field = canonical[len(capability_id) + 1 :]
        actual = _context_value(context, capability_id, extension, field)
        if not _nonempty(actual):
            missing.append(canonical)
            continue
        values = actual if isinstance(actual, list) else [actual]
        if not any(item in accepted for item in values):
            return "not_applicable", []
    return ("unknown", missing) if missing else ("applicable", [])


def _refresh_context(
    segment: dict,
    registry: Mapping[str, Mapping],
    *,
    touched_capabilities: set[str],
    core_changed: bool,
) -> None:
    context = segment.get("context")
    if not isinstance(context, dict):
        raise ContextOverrideError(
            f"segment {segment.get('segment_key')!r} has no formal context object"
        )
    missing_required = []
    incomplete_touched = []
    extensions = context.setdefault("extensions", {})
    for capability_id, descriptor in registry.items():
        if capability_id == "context.core@1":
            continue
        extension = capability_id.removeprefix("context.").split("@", 1)[0]
        storage_key = (
            f"context.{extension}"
            if f"context.{extension}" in extensions and extension not in extensions
            else extension
        )
        value = extensions.setdefault(storage_key, {})
        if not isinstance(value, dict):
            raise ContextOverrideError(f"segment extension {extension!r} must be an object")
        fields = descriptor.get("fields", {})
        has_values = any(_nonempty(value.get(field)) for field in fields)
        condition, condition_missing = _condition_state(descriptor, context, registry)
        if condition == "not_applicable":
            if has_values:
                raise ContextOverrideError(
                    f"segment {segment['segment_key']!r} has values for non-applicable "
                    f"capability {capability_id}"
                )
            value.clear()
            value["status"] = "not_applicable"
            continue
        applicable = condition in {"applicable", "unconditional"} and has_values
        if condition == "applicable":
            applicable = True
        capability_missing = []
        if condition == "unknown" and has_values:
            capability_missing.extend(condition_missing)
            applicable = True
        if applicable:
            for field_name, field in fields.items():
                if field.get("required_when_applicable") is True and not _nonempty(
                    value.get(field_name)
                ):
                    capability_missing.append(
                        f"context.extensions.{extension}.{field_name}"
                    )
        if capability_missing:
            value["status"] = "incomplete"
            missing_required.extend(capability_missing)
            if capability_id in touched_capabilities or core_changed:
                incomplete_touched.extend(capability_missing)
        elif applicable:
            value["status"] = "ready"
        else:
            value.clear()
            value["status"] = "not_applicable"
    if incomplete_touched:
        raise ContextOverrideError(
            f"context override leaves required fields missing for "
            f"{segment['segment_key']!r}: {sorted(set(incomplete_touched))}"
        )
    context["missing_required"] = sorted(set(missing_required))
    context["status"] = "context_incomplete" if missing_required else "ready"
    segment["context_provenance"] = deepcopy(context.setdefault("provenance", {}))
    segment["context_status"] = context["status"]
    segment["context_missing_required"] = list(context["missing_required"])


def validate_job_context_overrides(
    value: object,
    *,
    segments: Sequence[Mapping],
    registry: Mapping[str, Mapping],
) -> tuple[dict, dict[str, dict[str, object]]]:
    document = _validate_schema(value, OVERRIDE_SCHEMA_PATH)
    authority = document["authority_source"]
    for field in ("source_id", "issuer"):
        if _PLACEHOLDER.match(authority[field].strip()):
            raise ContextOverrideError(f"authority_source.{field} is still a placeholder")
    if authority["kind"] == "authorized_system":
        reference = authority.get("authorization_ref")
        if not isinstance(reference, str) or not reference.strip():
            raise ContextOverrideError(
                "authorized_system context source requires authorization_ref"
            )
        if _PLACEHOLDER.match(reference.strip()):
            raise ContextOverrideError(
                "authority_source.authorization_ref is still a placeholder"
            )
    actual_set_digest = segment_set_digest(segments)
    if document["segment_set_digest"] != actual_set_digest:
        raise ContextOverrideError("context override segment_set_digest is stale")
    if not document["entries"]:
        raise ContextOverrideError("context overrides contain no entries")
    by_key = {segment["segment_key"]: segment for segment in segments}
    seen = set()
    seen_records = set()
    plan: dict[str, dict[str, object]] = {}
    for entry in document["entries"]:
        key = entry["segment_key"]
        if key in seen:
            raise ContextOverrideError(f"duplicate context override segment_key {key!r}")
        seen.add(key)
        if key not in by_key:
            raise ContextOverrideError(f"unknown context override segment_key {key!r}")
        segment = by_key[key]
        if entry["source_digest"] != segment["source_digest"]:
            raise ContextOverrideError(f"context override source_digest is stale for {key!r}")
        if entry["verification_status"] != "verified":
            raise ContextOverrideError(
                f"context override for {key!r} is not verified"
            )
        record_id = entry["provenance"]["record_id"].strip()
        if _PLACEHOLDER.match(record_id):
            raise ContextOverrideError(
                f"context override record_id for {key!r} is still a placeholder"
            )
        if record_id in seen_records:
            raise ContextOverrideError(
                f"duplicate context override record_id {record_id!r}"
            )
        seen_records.add(record_id)
        normalized_patch = {}
        canonical_seen = set()
        context = segment.get("context")
        if not isinstance(context, Mapping):
            raise ContextOverrideError(f"segment {key!r} has no formal context")
        expected_context = {}
        expected_seen = set()
        for raw_ref, raw_value in entry.get("expected_context", {}).items():
            canonical, _, _, field = _field_binding(raw_ref, registry)
            if canonical in expected_seen:
                raise ContextOverrideError(
                    f"context override for {key!r} repeats expected field "
                    f"{canonical!r}"
                )
            expected_seen.add(canonical)
            expected_context[canonical] = _normalize_value(
                raw_value,
                field,
                f"context override {key!r} expected {canonical}",
            )
        for raw_ref, raw_value in entry["context_patch"].items():
            canonical, capability_id, extension, field = _field_binding(
                raw_ref, registry
            )
            if canonical in canonical_seen:
                raise ContextOverrideError(
                    f"context override for {key!r} repeats field {canonical!r}"
                )
            canonical_seen.add(canonical)
            normalized = _normalize_value(
                raw_value, field, f"context override {key!r} {canonical}"
            )
            current = _context_value(
                context,
                capability_id,
                extension,
                canonical[len(capability_id) + 1 :],
            )
            if capability_id != "context.core@1":
                extensions = context.get("extensions")
                extension_value = (
                    extensions.get(
                        extension, extensions.get(f"context.{extension}")
                    )
                    if isinstance(extensions, Mapping)
                    else None
                )
                status = (
                    extension_value.get("status")
                    if isinstance(extension_value, Mapping)
                    else None
                )
                if status in {"conflict", "disabled"}:
                    raise ContextOverrideError(
                        f"context override cannot patch {capability_id} with "
                        f"existing status {status!r} for {key!r}"
                    )
            if _nonempty(current):
                if canonical not in expected_context:
                    relation = (
                        "duplicates" if current == normalized else "conflicts with"
                    )
                    raise ContextOverrideError(
                        f"context override for {key!r} {relation} existing field "
                        f"{canonical!r}; expected_context is required"
                    )
                if current != expected_context[canonical]:
                    raise ContextOverrideError(
                        f"context override expected value is stale for {key!r} "
                        f"{canonical!r}"
                    )
            elif canonical in expected_context:
                raise ContextOverrideError(
                    f"context override expected a current value for {key!r} "
                    f"{canonical!r}, but the field is blank"
                )
            field_name = canonical[len(capability_id) + 1 :]
            provenance_ref = _provenance_ref(
                capability_id, extension, field_name
            )
            existing_provenance = context.get("provenance")
            existing_provenance = (
                deepcopy(existing_provenance.get(provenance_ref))
                if isinstance(existing_provenance, Mapping)
                else None
            )
            normalized_patch[canonical] = {
                "value": normalized,
                "replaced_value": deepcopy(current) if _nonempty(current) else None,
                "replaced_provenance": existing_provenance,
            }
        unused_expected = sorted(set(expected_context) - canonical_seen)
        if unused_expected:
            raise ContextOverrideError(
                f"context override for {key!r} has expected_context fields "
                f"without patches: {unused_expected}"
            )
        plan[key] = normalized_patch
    return document, plan


def apply_job_context_overrides(
    value: object,
    *,
    segments: Sequence[Mapping],
    registry: Mapping[str, Mapping],
    capability_resolution_digest: str | None,
    source_file_sha256: str | None = None,
) -> dict:
    document, plan = validate_job_context_overrides(
        value, segments=segments, registry=registry
    )
    output = deepcopy(list(segments))
    by_key = {segment["segment_key"]: segment for segment in output}
    authority = document["authority_source"]
    entries = {entry["segment_key"]: entry for entry in document["entries"]}
    patched_fields = 0
    replaced_fields = 0
    for key, patch in plan.items():
        segment = by_key[key]
        context = segment["context"]
        touched_capabilities = set()
        core_changed = False
        for field_ref, patch_item in patch.items():
            field_value = patch_item["value"]
            canonical, capability_id, extension, _ = _field_binding(field_ref, registry)
            field_name = canonical[len(capability_id) + 1 :]
            _put_context_value(
                context, capability_id, extension, field_name, field_value
            )
            touched_capabilities.add(capability_id)
            core_changed = core_changed or capability_id == "context.core@1"
            provenance_ref = _provenance_ref(capability_id, extension, field_name)
            context.setdefault("provenance", {})[provenance_ref] = {
                "method": (
                    "human_sidecar"
                    if authority["kind"] == "human"
                    else "authorized_sidecar"
                ),
                "record_id": entries[key]["provenance"]["record_id"],
                "source_id": authority["source_id"],
                "issuer": authority["issuer"],
                "status": "verified",
                **(
                    {"replaced_value": deepcopy(patch_item["replaced_value"])}
                    if patch_item["replaced_value"] is not None
                    else {}
                ),
                **(
                    {
                        "replaced_provenance": deepcopy(
                            patch_item["replaced_provenance"]
                        )
                    }
                    if patch_item["replaced_provenance"] is not None
                    else {}
                ),
            }
            if canonical == "context.core@1.content_type":
                segment["content_type"] = field_value
            patched_fields += 1
            if patch_item["replaced_value"] is not None:
                replaced_fields += 1
        _refresh_context(
            segment,
            registry,
            touched_capabilities=touched_capabilities,
            core_changed=core_changed,
        )
    document_digest = canonical_digest(document)
    descriptor_digest = _descriptor_digest(registry)
    fingerprint = canonical_digest(
        {
            "document_digest": document_digest,
            "segment_set_digest": document["segment_set_digest"],
            "descriptor_digest": descriptor_digest,
            "capability_resolution_digest": capability_resolution_digest,
        }
    )
    audit = {
        "schema": "lqe.context-overrides-binding",
        "version": 1,
        "document_digest": document_digest,
        "source_file_sha256": source_file_sha256,
        "fingerprint": fingerprint,
        "segment_set_digest": document["segment_set_digest"],
        "descriptor_digest": descriptor_digest,
        "capability_resolution_digest": capability_resolution_digest,
        "entries": len(document["entries"]),
        "patched_fields": patched_fields,
        "replaced_fields": replaced_fields,
        "authority_source": deepcopy(authority),
    }
    return {"document": document, "segments": output, "audit": audit}


def load_and_apply_job_context_overrides(
    path: str | Path,
    *,
    segments: Sequence[Mapping],
    registry: Mapping[str, Mapping],
    capability_resolution_digest: str | None,
) -> dict:
    source = Path(path)
    raw_digest = file_sha256(source)
    value = _strict_json(source)
    result = apply_job_context_overrides(
        value,
        segments=segments,
        registry=registry,
        capability_resolution_digest=capability_resolution_digest,
        source_file_sha256=raw_digest,
    )
    result["source_path"] = str(source.resolve())
    return result


def _state_registry(state: Mapping) -> dict[str, Mapping]:
    output = {}
    for field in ("resolved_context_descriptors", "shadow_context_descriptors"):
        registry = state.get(field)
        if registry is None:
            continue
        if not isinstance(registry, Mapping):
            raise ContextOverrideError(f"state.{field} must be an object")
        for capability_id, descriptor in registry.items():
            if capability_id in output and output[capability_id] != descriptor:
                raise ContextOverrideError(
                    f"formal and shadow descriptors disagree for {capability_id}"
                )
            output[capability_id] = deepcopy(descriptor)
    if "context.core@1" not in output:
        raise ContextOverrideError("state has no context.core@1 descriptor")
    return output


def _merged_context(segment: Mapping) -> dict:
    formal = segment.get("context")
    shadow = segment.get("shadow_context")
    context = deepcopy(formal) if isinstance(formal, Mapping) else {}
    if not isinstance(shadow, Mapping):
        return context
    core = context.setdefault("core", {})
    for field, value in (shadow.get("core") or {}).items():
        if field != "status" and not _nonempty(core.get(field)) and _nonempty(value):
            core[field] = deepcopy(value)
    extensions = context.setdefault("extensions", {})
    for extension, shadow_value in (shadow.get("extensions") or {}).items():
        if not isinstance(shadow_value, Mapping):
            continue
        target = extensions.setdefault(extension, {})
        if not isinstance(target, dict):
            continue
        for field, value in shadow_value.items():
            if field != "status" and not _nonempty(target.get(field)) and _nonempty(value):
                target[field] = deepcopy(value)
    return context


def _alias_key(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def _entity_aliases(state: Mapping) -> tuple[set[str], dict[str, set[str]]]:
    snapshot = state.get("project_asset_snapshot")
    paths = state.get("project_asset_paths")
    if not isinstance(snapshot, Mapping) or not isinstance(paths, Mapping):
        return set(), {}
    resolution = state.get("capability_resolution")
    enabled = resolution.get("enabled") if isinstance(resolution, Mapping) else None
    filter_assets = isinstance(enabled, Mapping)
    allowed_assets = {
        item.get("asset")
        for capability_id, item in (enabled or {}).items()
        if isinstance(capability_id, str)
        and capability_id.startswith("assets.entity_registry")
        and isinstance(item, Mapping)
        and item.get("effect") in {"enforce", "shadow"}
        and isinstance(item.get("asset"), str)
    }
    entity_ids = set()
    aliases: dict[str, set[str]] = {}
    for asset_id, item in (snapshot.get("assets") or {}).items():
        if not isinstance(item, Mapping) or item.get("kind") != "entity_registry":
            continue
        if filter_assets and asset_id not in allowed_assets:
            continue
        path = paths.get(asset_id)
        if item.get("status") != "present" or not isinstance(path, str):
            continue
        source = Path(path)
        try:
            if file_sha256(source) != item.get("sha256"):
                raise ContextOverrideError(
                    f"entity registry digest mismatch: {asset_id}"
                )
            document = _strict_json(source)
        except OSError as exc:
            raise ContextOverrideError(
                f"cannot read entity registry {asset_id}: {exc}"
            ) from exc
        if not isinstance(document, Mapping):
            raise ContextOverrideError(f"entity registry {asset_id} must be an object")
        for entity in document.get("entities", []):
            if not isinstance(entity, Mapping):
                continue
            entity_id = entity.get("id")
            if not isinstance(entity_id, str) or not entity_id:
                continue
            entity_ids.add(entity_id)
            aliases.setdefault(_alias_key(entity_id), set()).add(entity_id)
            names = entity.get("names")
            if isinstance(names, Mapping):
                for values in names.values():
                    if isinstance(values, list):
                        for name in values:
                            if _nonempty(name):
                                aliases.setdefault(_alias_key(name), set()).add(entity_id)
    return entity_ids, aliases


def _alias_status(
    value: object,
    *,
    entity_ids: set[str],
    aliases: Mapping[str, set[str]],
) -> tuple[str, list[str]]:
    values = value if isinstance(value, list) else [value]
    candidates = set()
    unresolved = False
    ambiguous = False
    for raw in values:
        if raw in entity_ids:
            candidates.add(raw)
            continue
        matches = set(aliases.get(_alias_key(raw), set()))
        candidates.update(matches)
        if len(matches) == 0:
            unresolved = True
        elif len(matches) > 1:
            ambiguous = True
    if ambiguous:
        return "ambiguous_alias", sorted(candidates)
    if unresolved:
        return "unresolved_alias", sorted(candidates)
    return "provided", sorted(candidates)


def build_context_gap_report(state: Mapping) -> dict:
    if not isinstance(state, Mapping):
        raise ContextOverrideError("state must be an object")
    if state.get("job_runtime_contract_version") != 2:
        raise ContextOverrideError(
            "context gaps require job_runtime_contract_version 2; reread the old job"
        )
    segments = state.get("segments")
    if not isinstance(segments, list):
        raise ContextOverrideError("state.segments must be an array")
    registry = _state_registry(state)
    available_fields = []
    for field_ref in _TARGET_FIELDS:
        try:
            canonical, _, _, _ = _field_binding(field_ref, registry)
        except ContextOverrideError:
            continue
        available_fields.append(canonical)
    entity_ids, aliases = _entity_aliases(state)
    rows = []
    counts = {
        "not_provided": 0,
        "unresolved_alias": 0,
        "ambiguous_alias": 0,
        "not_applicable": 0,
        "provided": 0,
    }
    for segment in segments:
        context = _merged_context(segment)
        fields = []
        explicit = segment.get("context_gap_statuses")
        explicit = explicit if isinstance(explicit, Mapping) else {}
        for field_ref in available_fields:
            canonical, capability_id, extension, _ = _field_binding(
                field_ref, registry
            )
            field_name = canonical[len(capability_id) + 1 :]
            value = _context_value(context, capability_id, extension, field_name)
            candidates: list[str] = []
            explicit_status = explicit.get(canonical)
            if explicit_status in {
                "not_provided",
                "unresolved_alias",
                "ambiguous_alias",
                "not_applicable",
                "provided",
            }:
                status = explicit_status
            elif capability_id != "context.core@1":
                condition, _ = _condition_state(
                    registry[capability_id], context, registry
                )
                if condition == "not_applicable":
                    status = "not_applicable"
                elif not _nonempty(value):
                    status = "not_provided"
                elif canonical in _ALIAS_FIELDS:
                    status, candidates = _alias_status(
                        value, entity_ids=entity_ids, aliases=aliases
                    )
                else:
                    status = "provided"
            elif not _nonempty(value):
                status = "not_provided"
            else:
                status = "provided"
            counts[status] += 1
            fields.append(
                {
                    "field_ref": canonical,
                    "status": status,
                    "value": deepcopy(value),
                    "candidate_ids": candidates,
                }
            )
        rows.append(
            {
                "segment_key": segment["segment_key"],
                "source_digest": segment["source_digest"],
                "fields": fields,
            }
        )
    report = {
        "schema": "lqe.context-gap-report",
        "version": 1,
        "job_runtime_contract_version": 2,
        "segment_set_digest": segment_set_digest(segments),
        "descriptor_digest": _descriptor_digest(registry),
        "summary": {
            "segments": len(rows),
            "fields": sum(len(row["fields"]) for row in rows),
            **counts,
        },
        "segments": rows,
    }
    report["report_digest"] = canonical_digest(report)
    _validate_schema(report, GAP_SCHEMA_PATH)
    return report


def build_context_override_scaffold(
    state: Mapping,
    *,
    source_id: str = "TODO: authorized context source",
    issuer: str = "TODO: human reviewer",
) -> dict:
    report = build_context_gap_report(state)
    by_key = {
        segment["segment_key"]: segment for segment in state.get("segments", [])
    }
    entries = []
    for row in report["segments"]:
        gaps = [
            field
            for field in row["fields"]
            if field["status"]
            in {"not_provided", "unresolved_alias", "ambiguous_alias"}
        ]
        if not gaps:
            continue
        segment = by_key[row["segment_key"]]
        entry = {
            "segment_key": row["segment_key"],
            "source_digest": row["source_digest"],
            "source_preview": str(segment.get("source", ""))[:160],
            "verification_status": "pending",
            "context_patch": {field["field_ref"]: None for field in gaps},
            "provenance": {
                "record_id": f"TODO: context record for {row['segment_key']}"
            },
        }
        expected_context = {
            field["field_ref"]: deepcopy(field["value"])
            for field in gaps
            if field["status"] in {"unresolved_alias", "ambiguous_alias"}
            and _nonempty(field["value"])
        }
        if expected_context:
            entry["expected_context"] = expected_context
        entries.append(entry)
    scaffold = {
        "schema": "lqe.job-context-overrides",
        "version": 1,
        "segment_set_digest": report["segment_set_digest"],
        "authority_source": {
            "source_id": source_id,
            "kind": "human",
            "issuer": issuer,
        },
        "entries": entries,
    }
    _validate_schema(scaffold, OVERRIDE_SCHEMA_PATH)
    return scaffold


def _load_state(path: str | Path) -> dict:
    source = Path(path)
    value = _strict_json(source)
    if not isinstance(value, dict):
        raise ContextOverrideError("state must be a JSON object")
    asset_paths = value.get("project_asset_paths")
    if isinstance(asset_paths, dict):
        for asset_id, raw_path in list(asset_paths.items()):
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            candidate = Path(raw_path)
            if candidate.is_absolute() or candidate.is_file():
                continue
            job_relative = source.parent / candidate
            if job_relative.is_file():
                asset_paths[asset_id] = str(job_relative)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    gaps = subparsers.add_parser("gaps", help="write an auditable context-gap report")
    gaps.add_argument("--state", required=True)
    gaps.add_argument("--out", required=True)
    scaffold = subparsers.add_parser(
        "scaffold", help="write a pending human-fill context override template"
    )
    scaffold.add_argument("--state", required=True)
    scaffold.add_argument("--out", required=True)
    scaffold.add_argument(
        "--source-id", default="TODO: authorized context source", dest="source_id"
    )
    scaffold.add_argument(
        "--issuer", default="TODO: human reviewer"
    )
    args = parser.parse_args()
    try:
        state = _load_state(args.state)
        if args.command == "gaps":
            output = build_context_gap_report(state)
        else:
            output = build_context_override_scaffold(
                state, source_id=args.source_id, issuer=args.issuer
            )
        write_json_atomic(Path(args.out), output)
    except (ContextOverrideError, OSError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(
        json.dumps(
            {
                "status": "written",
                "output": str(Path(args.out)),
                "schema": output["schema"],
                "entries": len(output.get("entries", output.get("segments", []))),
                "digest": output.get("report_digest", canonical_digest(output)),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ContextOverrideError",
    "apply_job_context_overrides",
    "build_context_gap_report",
    "build_context_override_scaffold",
    "load_and_apply_job_context_overrides",
    "segment_set_digest",
    "validate_job_context_overrides",
]
