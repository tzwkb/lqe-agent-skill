#!/usr/bin/env python3
"""Canonical project-asset validation and manifest/sidecar contracts."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
import re
import stat
import tempfile
from typing import Mapping, Sequence

from lqe_constraints import (
    ConstraintContractError,
    validate_context_rules as validate_constraint_rules,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas" / "context"
SCHEMA_FILES = {
    "lqe.entities": SCHEMA_DIR / "entity_registry_v1.json",
    "lqe.review-examples": SCHEMA_DIR / "review_examples_v1.json",
    "lqe.context-rules": SCHEMA_DIR / "context_rules_v1.json",
    "lqe.segment-context-overrides": SCHEMA_DIR
    / "segment_context_overrides_v1.json",
    "lqe.project-source-manifest": SCHEMA_DIR
    / "project_source_manifest_v1.json",
}
SCHEMA_VERSIONS = {name: 1 for name in SCHEMA_FILES}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_IGNORED_SCHEMA_KEYS = {
    "$schema",
    "$id",
    "$defs",
    "title",
    "description",
    "default",
    "examples",
    "deprecated",
    "readOnly",
    "writeOnly",
}


class ProfileIngestError(ValueError):
    """Raised when canonical project material is invalid or unsafe to publish."""


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProfileIngestError(f"value is not canonical JSON: {exc}") from exc


def canonical_digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def source_digest(source: object) -> str:
    text = source if isinstance(source, str) else str(source or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ProfileIngestError(f"cannot read {path}: {exc}") from exc
    return digest.hexdigest()


def _object_without(value: Mapping, key: str) -> dict:
    return {name: deepcopy(item) for name, item in value.items() if name != key}


def _load_json(path: str | Path) -> object:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
        output = {}
        for key, value in pairs:
            if key in output:
                raise ProfileIngestError(f"duplicate JSON key {key!r} in {path}")
            output[key] = value
        return output

    try:
        with Path(path).open(encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=reject_duplicates)
    except ProfileIngestError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileIngestError(f"cannot load JSON {path}: {exc}") from exc


def load_schema(schema_name: str) -> dict:
    path = SCHEMA_FILES.get(schema_name)
    if path is None:
        raise ProfileIngestError(f"unknown canonical schema: {schema_name!r}")
    value = _load_json(path)
    if not isinstance(value, dict):
        raise ProfileIngestError(f"bundled schema is not an object: {path}")
    return value


def _json_pointer(root: Mapping, reference: str) -> object:
    if not reference.startswith("#/"):
        raise ProfileIngestError(f"only local schema references are supported: {reference}")
    value: object = root
    for raw in reference[2:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, Mapping) or part not in value:
            raise ProfileIngestError(f"unresolved schema reference: {reference}")
        value = value[part]
    return value


def _type_matches(value: object, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return type(value) is int
    if expected == "number":
        return type(value) in (int, float)
    if expected == "boolean":
        return type(value) is bool
    if expected == "null":
        return value is None
    raise ProfileIngestError(f"unsupported JSON Schema type: {expected!r}")


def _path(parent: str, child: object) -> str:
    return f"{parent}.{child}" if parent != "$" else f"$.{child}"


def _schema_error(path: str, message: str) -> ProfileIngestError:
    return ProfileIngestError(f"schema validation failed at {path}: {message}")


def _validate_schema_node(
    value: object,
    schema: object,
    root: Mapping,
    path: str,
) -> None:
    if schema is True:
        return
    if schema is False:
        raise _schema_error(path, "value is forbidden")
    if not isinstance(schema, Mapping):
        raise ProfileIngestError(f"invalid bundled schema node at {path}")
    if "$ref" in schema:
        target = _json_pointer(root, schema["$ref"])
        _validate_schema_node(value, target, root, path)

    supported = _IGNORED_SCHEMA_KEYS | {
        "$ref",
        "type",
        "const",
        "enum",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "minProperties",
        "maxProperties",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
    }
    unknown = sorted(set(schema) - supported)
    if unknown:
        raise ProfileIngestError(
            f"unsupported JSON Schema keywords at {path}: {', '.join(unknown)}"
        )

    if "type" in schema:
        expected = schema["type"]
        choices = expected if isinstance(expected, list) else [expected]
        if not choices or any(not isinstance(item, str) for item in choices):
            raise ProfileIngestError(f"invalid type declaration at {path}")
        if not any(_type_matches(value, item) for item in choices):
            raise _schema_error(path, f"expected type {expected!r}")
    if "const" in schema and value != schema["const"]:
        raise _schema_error(path, f"expected constant {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise _schema_error(path, f"value is not in {schema['enum']!r}")

    for keyword in ("allOf",):
        for branch in schema.get(keyword, []):
            _validate_schema_node(value, branch, root, path)
    for keyword in ("anyOf", "oneOf"):
        if keyword not in schema:
            continue
        matched = 0
        for branch in schema[keyword]:
            try:
                _validate_schema_node(value, branch, root, path)
            except ProfileIngestError:
                continue
            matched += 1
        required_matches = 1 if keyword == "oneOf" else None
        if matched == 0 or (required_matches is not None and matched != required_matches):
            raise _schema_error(path, f"does not satisfy {keyword}")
    if "not" in schema:
        try:
            _validate_schema_node(value, schema["not"], root, path)
        except ProfileIngestError:
            pass
        else:
            raise _schema_error(path, "matches forbidden schema")

    if isinstance(value, dict):
        for required in schema.get("required", []):
            if required not in value:
                raise _schema_error(path, f"missing required property {required!r}")
        if len(value) < schema.get("minProperties", 0):
            raise _schema_error(path, "has too few properties")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            raise _schema_error(path, "has too many properties")
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise ProfileIngestError(f"invalid properties declaration at {path}")
        for name, item in value.items():
            if name in properties:
                _validate_schema_node(item, properties[name], root, _path(path, name))
                continue
            extra = schema.get("additionalProperties", True)
            if extra is False:
                raise _schema_error(path, f"unknown property {name!r}")
            if isinstance(extra, Mapping):
                _validate_schema_node(item, extra, root, _path(path, name))

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise _schema_error(path, "has too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise _schema_error(path, "has too many items")
        if schema.get("uniqueItems"):
            serialized = [_canonical_bytes(item) for item in value]
            if len(serialized) != len(set(serialized)):
                raise _schema_error(path, "items are not unique")
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                _validate_schema_node(item, item_schema, root, f"{path}[{index}]")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise _schema_error(path, "string is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise _schema_error(path, "string is too long")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise _schema_error(path, f"does not match {schema['pattern']!r}")
    if type(value) in (int, float):
        if "minimum" in schema and value < schema["minimum"]:
            raise _schema_error(path, f"must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise _schema_error(path, f"must be <= {schema['maximum']}")


def validate_json_schema(value: object, schema: Mapping) -> object:
    """Validate against the strict standard-library JSON Schema subset used here."""

    if not isinstance(schema, Mapping):
        raise ProfileIngestError("schema must be an object")
    _canonical_bytes(value)
    _validate_schema_node(value, schema, schema, "$")
    return deepcopy(value)


def _unique_ids(items: Sequence[Mapping], field: str, label: str) -> set[str]:
    output = set()
    for index, item in enumerate(items):
        value = item.get(field)
        if value in output:
            raise ProfileIngestError(f"duplicate {label} {value!r} at index {index}")
        output.add(value)
    return output


def validate_entity_registry(
    value: object, *, source_ids: Sequence[str] | None = None
) -> dict:
    document = validate_json_schema(value, load_schema("lqe.entities"))
    entities = document["entities"]
    entity_ids = _unique_ids(entities, "id", "entity id")
    record_ids = set(entity_ids)
    declared_sources = set(source_ids) if source_ids is not None else None
    for entity in entities:
        for fact in entity["facts"]:
            fact_id = fact["id"]
            if fact_id in record_ids:
                raise ProfileIngestError(f"duplicate canonical record id {fact_id!r}")
            record_ids.add(fact_id)
            if declared_sources is not None and fact["provenance"]["source_id"] not in declared_sources:
                raise ProfileIngestError(
                    f"fact {fact_id!r} references undeclared source_id"
                )
    for relation in document["relations"]:
        relation_id = relation["id"]
        if relation_id in record_ids:
            raise ProfileIngestError(f"duplicate canonical record id {relation_id!r}")
        record_ids.add(relation_id)
        if relation["from"] not in entity_ids or relation["to"] not in entity_ids:
            raise ProfileIngestError(
                f"relation {relation_id!r} references an unknown entity"
            )
        if declared_sources is not None and relation["provenance"]["source_id"] not in declared_sources:
            raise ProfileIngestError(
                f"relation {relation_id!r} references undeclared source_id"
            )
    return document


def validate_review_examples(
    value: object, *, held_out_segment_keys: Sequence[str] | None = None
) -> dict:
    document = validate_json_schema(value, load_schema("lqe.review-examples"))
    _unique_ids(document["examples"], "id", "review example id")
    held_out = set(held_out_segment_keys or ())
    for example in document["examples"]:
        uses = set(example["uses"])
        if example["scope"] == "segment" and not example.get("segment_key"):
            raise ProfileIngestError(
                f"segment-scoped example {example['id']!r} lacks segment_key"
            )
        if uses == {"regression_only", "runtime_reference"}:
            raise ProfileIngestError(
                f"example {example['id']!r} cannot mix regression and runtime uses"
            )
        if (
            "runtime_reference" in uses
            and example.get("segment_key") in held_out
            and example["scope"] != "segment"
        ):
            raise ProfileIngestError(
                f"runtime example {example['id']!r} leaks a held-out segment"
            )
        if example["review_status"] == "reviewed" and not example.get("preferred_target"):
            raise ProfileIngestError(
                f"reviewed example {example['id']!r} lacks preferred_target"
            )
    return document


def validate_context_rules(value: object, *, target_lang: str | None = None) -> dict:
    document = validate_json_schema(value, load_schema("lqe.context-rules"))
    try:
        document = validate_constraint_rules(document)
    except ConstraintContractError as exc:
        raise ProfileIngestError(f"invalid context rules: {exc}") from exc
    authority_rank = set(document["authority_rank"])
    for rule in document["rules"]:
        language_rule = rule["capability"].startswith("language.")
        if language_rule and not rule.get("target_lang"):
            raise ProfileIngestError(
                f"language rule {rule['id']!r} lacks target_lang"
            )
        if language_rule and target_lang and rule["target_lang"] != target_lang:
            raise ProfileIngestError(
                f"language rule {rule['id']!r} target_lang does not match profile"
            )
        if rule["rule_status"] == "confirmed":
            issuer = rule["authority"]["issuer"]
            if issuer not in authority_rank:
                raise ProfileIngestError(
                    f"confirmed rule {rule['id']!r} has unranked authority {issuer!r}"
                )
            provenance = rule["provenance"]
            if not isinstance(provenance, dict) or not any(
                item not in (None, "", [], {}) for item in provenance.values()
            ):
                raise ProfileIngestError(
                    f"confirmed rule {rule['id']!r} lacks provenance"
                )
    return document


def _coverage_records(details: object) -> list[dict]:
    if isinstance(details, list):
        records = details
    elif isinstance(details, dict) and isinstance(details.get("records"), list):
        records = details["records"]
    else:
        raise ProfileIngestError("coverage details must be an array or {records: [...]}")
    statuses = {"converted", "normalized", "ignored", "unmapped"}
    output = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ProfileIngestError(f"coverage record {index} must be an object")
        status = record.get("status")
        if status not in statuses:
            raise ProfileIngestError(f"coverage record {index} has invalid status")
        if status == "ignored" and not isinstance(record.get("reason"), str):
            raise ProfileIngestError(f"ignored coverage record {index} lacks reason")
        if status == "ignored" and not record["reason"].strip():
            raise ProfileIngestError(f"ignored coverage record {index} lacks reason")
        output.append(deepcopy(record))
    return output


def _portable_relative_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileIngestError(f"{label} must be a non-empty relative path")
    path = value.strip()
    if "\x00" in path:
        raise ProfileIngestError(f"{label} must not contain NUL")
    if "\\" in path:
        raise ProfileIngestError(f"{label} must use portable '/' separators")
    posix = PurePosixPath(path)
    windows = PureWindowsPath(path)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise ProfileIngestError(f"{label} must be a portable relative path")
    if ".." in posix.parts or ".." in windows.parts:
        raise ProfileIngestError(f"{label} must not escape the manifest directory")
    if path in {".", "./"}:
        raise ProfileIngestError(f"{label} must identify a file")
    return path


def _bound_regular_file(path: Path, *, label: str) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ProfileIngestError(f"cannot inspect {label}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ProfileIngestError(f"{label} must be a regular non-symlink file")
    return path


def _resolve_manifest_relative_file(
    manifest_path: Path,
    relative_path: object,
    *,
    label: str,
) -> Path:
    portable = _portable_relative_path(relative_path, label)
    base = manifest_path.parent.resolve()
    candidate = manifest_path.parent / portable
    _bound_regular_file(candidate, label=label)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(base)
    except (OSError, ValueError) as exc:
        raise ProfileIngestError(
            f"{label} escapes or cannot be resolved from the manifest directory"
        ) from exc
    return candidate


def validate_project_source_manifest(
    value: object,
    *,
    coverage_details: object | None = None,
    require_coverage_details: bool = False,
) -> dict:
    document = validate_json_schema(
        value, load_schema("lqe.project-source-manifest")
    )
    actual_digest = canonical_digest(_object_without(document, "manifest_digest"))
    if document["manifest_digest"] != actual_digest:
        raise ProfileIngestError("project source manifest digest mismatch")
    source_ids = _unique_ids(document["sources"], "id", "source id")
    _unique_ids(document["generated_assets"], "asset_id", "generated asset id")
    for source in document["sources"]:
        if source["availability"] == "included" and not source.get("filename"):
            raise ProfileIngestError(
                f"included source {source['id']!r} lacks filename"
            )
    for asset in document["generated_assets"]:
        _portable_relative_path(
            asset["path"],
            f"generated asset {asset['asset_id']!r} path",
        )
        unknown = sorted(set(asset["derived_from"]) - source_ids)
        if unknown:
            raise ProfileIngestError(
                f"generated asset {asset['asset_id']!r} has undeclared sources: {unknown}"
            )
    coverage = document["coverage"]
    _portable_relative_path(coverage["details_path"], "coverage details_path")
    total = sum(coverage[name] for name in ("converted", "normalized", "ignored", "unmapped"))
    if coverage["total_nonempty"] != total:
        raise ProfileIngestError("coverage total_nonempty does not equal status counts")
    if coverage["unmapped"] != 0:
        raise ProfileIngestError("coverage contains unmapped source material")
    if require_coverage_details and coverage_details is None:
        raise ProfileIngestError("coverage details are required for publication")
    if coverage_details is not None:
        records = _coverage_records(coverage_details)
        counts = {name: 0 for name in ("converted", "normalized", "ignored", "unmapped")}
        for record in records:
            counts[record["status"]] += 1
        if len(records) != coverage["total_nonempty"] or any(
            counts[name] != coverage[name] for name in counts
        ):
            raise ProfileIngestError("coverage details do not match manifest summary")
        expected = coverage["details_sha256"]
        if canonical_digest(coverage_details) != expected:
            raise ProfileIngestError("coverage details digest mismatch")
    elif coverage["ignored"] and require_coverage_details:
        raise ProfileIngestError("ignored source material lacks reason records")
    return document


def load_project_source_manifest(path: str | Path) -> dict:
    manifest_path = Path(path)
    if not manifest_path.is_absolute():
        manifest_path = manifest_path.absolute()
    _bound_regular_file(manifest_path, label="project source manifest")
    manifest = _load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ProfileIngestError("project source manifest must be an object")
    structural = validate_project_source_manifest(manifest)
    details_path = _resolve_manifest_relative_file(
        manifest_path,
        structural["coverage"]["details_path"],
        label="project source coverage details",
    )
    details = _load_json(details_path)
    return validate_project_source_manifest(
        structural,
        coverage_details=details,
        require_coverage_details=True,
    )


def build_project_source_manifest(
    *,
    project: str,
    manifest_scope: str,
    sources: Sequence[Mapping],
    generated_assets: Sequence[Mapping],
    coverage: Mapping,
    coverage_details: object | None = None,
) -> dict:
    summary = deepcopy(dict(coverage))
    if coverage_details is not None:
        summary["details_sha256"] = canonical_digest(coverage_details)
    manifest = {
        "schema": "lqe.project-source-manifest",
        "version": 1,
        "project": project,
        "manifest_scope": manifest_scope,
        "sources": deepcopy(list(sources)),
        "generated_assets": deepcopy(list(generated_assets)),
        "coverage": summary,
    }
    manifest["manifest_digest"] = canonical_digest(manifest)
    return validate_project_source_manifest(
        manifest,
        coverage_details=coverage_details,
        require_coverage_details=True,
    )


def _declared_extension_fields(declared: Mapping) -> dict[str, set[str]]:
    output: dict[str, set[str]] = {}
    for raw_name, raw_value in declared.items():
        name = str(raw_name)
        short = name.removeprefix("context.")
        if not short or short == "core":
            continue
        if isinstance(raw_value, Mapping) and isinstance(raw_value.get("fields"), Mapping):
            fields = raw_value["fields"].keys()
        elif isinstance(raw_value, Mapping):
            fields = raw_value.keys()
        elif isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes)):
            fields = raw_value
        else:
            raise ProfileIngestError(f"declared extension {name!r} has invalid fields")
        normalized = {str(field) for field in fields}
        if "status" in normalized:
            normalized.remove("status")
        if not normalized:
            raise ProfileIngestError(f"declared extension {name!r} has no data fields")
        if short in output:
            raise ProfileIngestError(f"duplicate declared extension {short!r}")
        output[short] = normalized
    return output


def _segment_extensions(segment: Mapping) -> Mapping:
    context = segment.get("context")
    if isinstance(context, Mapping):
        extensions = context.get("extensions")
        if isinstance(extensions, Mapping):
            return extensions
    state = segment.get("segment_context")
    if isinstance(state, Mapping):
        extensions = state.get("extensions")
        if isinstance(extensions, Mapping):
            return extensions
    return {}


def _extension_entry(extensions: Mapping, name: str) -> Mapping:
    key = name if name in extensions else f"context.{name}"
    if key not in extensions:
        return {}
    value = extensions[key]
    if not isinstance(value, Mapping):
        raise ProfileIngestError(f"segment extension {name!r} must be an object")
    return value


def _nonempty(value: object) -> bool:
    return value not in (None, "", [], {})


def validate_segment_context_overrides(
    value: object,
    *,
    segments: Sequence[Mapping],
    declared_extensions: Mapping,
    source_ids: Sequence[str] | None = None,
) -> dict:
    document = validate_json_schema(
        value, load_schema("lqe.segment-context-overrides")
    )
    allowed = _declared_extension_fields(declared_extensions)
    if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes)):
        raise ProfileIngestError("segments must be an array")
    indexed = {}
    for index, segment in enumerate(segments):
        if not isinstance(segment, Mapping):
            raise ProfileIngestError(f"segment {index} must be an object")
        key = segment.get("segment_key")
        if not isinstance(key, str) or not key:
            raise ProfileIngestError(f"segment {index} lacks segment_key")
        if key in indexed:
            raise ProfileIngestError(f"input contains duplicate segment_key {key!r}")
        indexed[key] = segment
    declared_sources = set(source_ids) if source_ids is not None else None
    seen = set()
    for entry in document["entries"]:
        key = entry["segment_key"]
        if key in seen:
            raise ProfileIngestError(f"sidecar contains duplicate segment_key {key!r}")
        seen.add(key)
        if key not in indexed:
            raise ProfileIngestError(f"sidecar references unknown segment_key {key!r}")
        segment = indexed[key]
        if "source" in segment:
            actual_source_digest = source_digest(segment.get("source"))
            recorded = segment.get("source_digest")
            if recorded is not None and recorded != actual_source_digest:
                raise ProfileIngestError(f"input segment {key!r} has stale source_digest")
        else:
            actual_source_digest = segment.get("source_digest")
        if not isinstance(actual_source_digest, str) or not _HEX64.fullmatch(actual_source_digest):
            raise ProfileIngestError(f"segment {key!r} lacks a usable source digest")
        if entry["source_digest"] != actual_source_digest:
            raise ProfileIngestError(f"sidecar source_digest is stale for {key!r}")
        source_id = entry["provenance"]["source_id"]
        if declared_sources is not None and source_id not in declared_sources:
            raise ProfileIngestError(
                f"sidecar entry {key!r} references undeclared source_id {source_id!r}"
            )
        current_extensions = _segment_extensions(segment)
        for extension, patch in entry["context_patch"]["extensions"].items():
            if extension not in allowed:
                raise ProfileIngestError(f"sidecar uses undeclared extension {extension!r}")
            if "status" in patch:
                raise ProfileIngestError("sidecar cannot set extension status")
            unknown_fields = sorted(set(patch) - allowed[extension])
            if unknown_fields:
                raise ProfileIngestError(
                    f"sidecar uses undeclared fields for {extension!r}: {unknown_fields}"
                )
            current = _extension_entry(current_extensions, extension)
            status = current.get("status")
            if status in {"not_applicable", "conflict", "disabled"}:
                raise ProfileIngestError(
                    f"sidecar cannot patch extension {extension!r} with status {status!r}"
                )
            conflicts = sorted(field for field in patch if _nonempty(current.get(field)))
            if conflicts:
                raise ProfileIngestError(
                    f"input/sidecar conflict for {key!r} {extension!r}: {conflicts}"
                )
    return document


def segment_context_override_digest(value: object) -> str:
    document = validate_json_schema(
        value, load_schema("lqe.segment-context-overrides")
    )
    return canonical_digest(document)


def apply_segment_context_overrides(
    value: object,
    *,
    segments: Sequence[Mapping],
    declared_extensions: Mapping,
    source_ids: Sequence[str] | None = None,
) -> list[dict]:
    sidecar = validate_segment_context_overrides(
        value,
        segments=segments,
        declared_extensions=declared_extensions,
        source_ids=source_ids,
    )
    output = deepcopy(list(segments))
    by_key = {segment["segment_key"]: segment for segment in output}
    for entry in sidecar["entries"]:
        segment = by_key[entry["segment_key"]]
        if isinstance(segment.get("context"), dict):
            container = segment["context"].setdefault("extensions", {})
        else:
            state = segment.setdefault("segment_context", {})
            container = state.setdefault("extensions", {})
        provenance = segment.setdefault("provenance", {})
        for extension, patch in entry["context_patch"]["extensions"].items():
            storage_key = (
                f"context.{extension}"
                if f"context.{extension}" in container and extension not in container
                else extension
            )
            target = container.setdefault(storage_key, {})
            for field, field_value in patch.items():
                target[field] = deepcopy(field_value)
                provenance[f"context.extensions.{extension}.{field}"] = {
                    "method": "human_sidecar",
                    "record_id": entry["provenance"]["record_id"],
                    "source_id": entry["provenance"]["source_id"],
                    "status": "verified",
                }
    return output


def validate_canonical_asset(
    value: object,
    *,
    expected_schema: str | None = None,
    target_lang: str | None = None,
) -> dict:
    if not isinstance(value, dict):
        raise ProfileIngestError("canonical asset must be an object")
    schema_name = value.get("schema")
    if expected_schema is not None and schema_name != expected_schema:
        raise ProfileIngestError(
            f"expected schema {expected_schema!r}, received {schema_name!r}"
        )
    if schema_name not in SCHEMA_FILES:
        raise ProfileIngestError(f"unknown canonical schema: {schema_name!r}")
    if value.get("version") != SCHEMA_VERSIONS[schema_name]:
        raise ProfileIngestError(
            f"unsupported {schema_name} version: {value.get('version')!r}"
        )
    if schema_name == "lqe.entities":
        return validate_entity_registry(value)
    if schema_name == "lqe.review-examples":
        return validate_review_examples(value)
    if schema_name == "lqe.context-rules":
        return validate_context_rules(value, target_lang=target_lang)
    if schema_name == "lqe.project-source-manifest":
        return validate_project_source_manifest(value)
    if schema_name == "lqe.segment-context-overrides":
        raise ProfileIngestError(
            "segment-context overrides require segments and declared extensions"
        )
    raise ProfileIngestError(f"no validator for schema {schema_name!r}")


def write_json_atomic(path: str | Path, value: object) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _array_from_file(path: str | Path, label: str) -> list:
    value = _load_json(path)
    if not isinstance(value, list):
        raise ProfileIngestError(f"{label} must be a JSON array")
    return value


def _mapping_from_file(path: str | Path, label: str) -> dict:
    value = _load_json(path)
    if not isinstance(value, dict):
        raise ProfileIngestError(f"{label} must be a JSON object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="validate one canonical asset")
    validate.add_argument("input")
    validate.add_argument("--schema", choices=sorted(SCHEMA_FILES))
    validate.add_argument("--target-lang")

    manifest = subparsers.add_parser("manifest", help="build a bound source manifest")
    manifest.add_argument("--project", required=True)
    manifest.add_argument("--manifest-scope", choices=["internal", "public", "redacted"], required=True)
    manifest.add_argument("--sources", required=True)
    manifest.add_argument("--generated-assets", required=True)
    manifest.add_argument("--coverage", required=True)
    manifest.add_argument("--coverage-details", required=True)
    manifest.add_argument("--output", required=True)

    overrides = subparsers.add_parser(
        "validate-overrides", help="validate a sidecar against current segments"
    )
    overrides.add_argument("--input", required=True)
    overrides.add_argument("--segments", required=True)
    overrides.add_argument("--declared-extensions", required=True)
    overrides.add_argument("--source-ids")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            document = validate_canonical_asset(
                _load_json(args.input),
                expected_schema=args.schema,
                target_lang=args.target_lang,
            )
            result = {
                "status": "valid",
                "schema": document["schema"],
                "version": document["version"],
                "digest": canonical_digest(document),
            }
        elif args.command == "manifest":
            details = _load_json(args.coverage_details)
            document = build_project_source_manifest(
                project=args.project,
                manifest_scope=args.manifest_scope,
                sources=_array_from_file(args.sources, "sources"),
                generated_assets=_array_from_file(
                    args.generated_assets, "generated assets"
                ),
                coverage=_mapping_from_file(args.coverage, "coverage"),
                coverage_details=details,
            )
            write_json_atomic(args.output, document)
            result = {
                "status": "written",
                "output": str(Path(args.output)),
                "manifest_digest": document["manifest_digest"],
            }
        else:
            source_ids = None
            if args.source_ids:
                source_ids = _array_from_file(args.source_ids, "source ids")
            sidecar = validate_segment_context_overrides(
                _load_json(args.input),
                segments=_array_from_file(args.segments, "segments"),
                declared_extensions=_mapping_from_file(
                    args.declared_extensions, "declared extensions"
                ),
                source_ids=source_ids,
            )
            result = {
                "status": "valid",
                "schema": sidecar["schema"],
                "version": sidecar["version"],
                "digest": canonical_digest(sidecar),
                "entries": len(sidecar["entries"]),
            }
    except ProfileIngestError as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ProfileIngestError",
    "SCHEMA_FILES",
    "apply_segment_context_overrides",
    "build_project_source_manifest",
    "canonical_digest",
    "file_sha256",
    "load_project_source_manifest",
    "load_schema",
    "segment_context_override_digest",
    "source_digest",
    "validate_canonical_asset",
    "validate_context_rules",
    "validate_entity_registry",
    "validate_json_schema",
    "validate_project_source_manifest",
    "validate_review_examples",
    "validate_segment_context_overrides",
    "write_json_atomic",
]
