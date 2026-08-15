"""Project-neutral context extraction, projection, and review equivalence."""

from __future__ import annotations

from copy import deepcopy
import json
import math
import re
from typing import Mapping, Sequence

from lqe_capabilities import (
    CapabilityNegotiationError,
    ProfileContractError,
    build_descriptor_registry as _build_descriptor_registry,
    builtin_descriptor_registry as _all_builtin_descriptors,
    resolve_capability_descriptor as _resolve_capability_descriptor,
    validate_capability_descriptor as _validate_capability_descriptor,
)
from lqe_split_contract import canonical_digest


CONTEXT_CONTRACT_VERSION = 1
CORE_CAPABILITY_ID = "context.core@1"
_CONTEXT_PREFIX = "context."
_VERSION_RE = re.compile(r"@([1-9][0-9]*)\Z")
_PRECHECK_MODULES = frozenset({"terminology", "precheck_review"})
_EXTENSION_RESERVED_FIELDS = frozenset(
    {
        "capability_id",
        "context_contract_version",
        "core",
        "descriptor_digest",
        "extensions",
        "input_status",
        "missing_required",
        "provenance",
        "segment_key",
        "status",
    }
)


class ContextContractError(ValueError):
    pass


def _contract_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (ProfileContractError, CapabilityNegotiationError) as exc:
        raise ContextContractError(str(exc)) from exc


def _is_context_descriptor(capability_id: object) -> bool:
    return isinstance(capability_id, str) and capability_id.startswith(
        _CONTEXT_PREFIX
    )


def _extension_name(capability_id: str) -> str:
    if not _is_context_descriptor(capability_id):
        raise ContextContractError(f"not a context capability: {capability_id!r}")
    name = _VERSION_RE.sub("", capability_id[len(_CONTEXT_PREFIX) :])
    if not name:
        raise ContextContractError(f"invalid context capability: {capability_id!r}")
    return name


def _check_extension_names(registry: Mapping[str, dict]) -> None:
    names = {}
    for capability_id in registry:
        name = _extension_name(capability_id)
        previous = names.get(name)
        if previous is not None and previous != capability_id:
            raise ContextContractError(
                f"context capabilities share extension name {name!r}: "
                f"{previous}, {capability_id}"
            )
        names[name] = capability_id
        reserved = sorted(
            set(registry[capability_id].get("fields", {}))
            & _EXTENSION_RESERVED_FIELDS
        )
        if reserved:
            raise ContextContractError(
                f"context descriptor {capability_id} uses reserved fields: {reserved}"
            )


def validate_capability_descriptor(descriptor: object) -> dict:
    normalized = _contract_call(
        _validate_capability_descriptor, descriptor, custom=True
    )
    if not _is_context_descriptor(normalized["id"]):
        raise ContextContractError("context descriptor id must start with 'context.'")
    _check_extension_names({normalized["id"]: normalized})
    return normalized


def builtin_descriptor_registry() -> dict[str, dict]:
    registry = {
        capability_id: descriptor
        for capability_id, descriptor in _all_builtin_descriptors().items()
        if _is_context_descriptor(capability_id)
    }
    _check_extension_names(registry)
    return registry


def _profile_custom_descriptors(profile: Mapping | None) -> object:
    if not isinstance(profile, Mapping):
        return None
    canonical = profile.get("capability_descriptors")
    legacy = profile.get("context_descriptors")
    if canonical not in (None, {}, []) and legacy not in (None, {}, []):
        raise ContextContractError(
            "profile cannot define both capability_descriptors and context_descriptors"
        )
    return canonical if canonical not in (None, {}, []) else legacy


def _enabled_context_ids(capability_resolution: Mapping) -> set[str]:
    enabled = capability_resolution.get("enabled")
    if not isinstance(enabled, Mapping):
        raise ContextContractError(
            "capability_resolution.enabled must be an object"
        )
    return {
        capability_id
        for capability_id in enabled
        if _is_context_descriptor(capability_id)
    }


def descriptor_registry(
    profile: Mapping | None = None,
    *,
    capability_resolution: Mapping | None = None,
) -> dict[str, dict]:
    if profile is not None and not isinstance(profile, Mapping):
        raise ContextContractError("profile must be an object")
    registry = _contract_call(
        _build_descriptor_registry, _profile_custom_descriptors(profile)
    )
    registry = {
        capability_id: descriptor
        for capability_id, descriptor in registry.items()
        if _is_context_descriptor(capability_id)
    }
    declarations = profile.get("capabilities") if isinstance(profile, Mapping) else None
    if declarations is not None and not isinstance(declarations, Mapping):
        raise ContextContractError("profile.capabilities must be an object")
    if isinstance(declarations, Mapping):
        for capability_id, declaration in declarations.items():
            if capability_id not in registry:
                continue
            if not isinstance(declaration, Mapping):
                raise ContextContractError(
                    f"profile capability {capability_id} must be an object"
                )
            registry[capability_id] = _contract_call(
                _resolve_capability_descriptor,
                capability_id,
                declaration,
                registry,
            )
    if capability_resolution is not None:
        if not isinstance(capability_resolution, Mapping):
            raise ContextContractError("capability_resolution must be an object")
        enabled = _enabled_context_ids(capability_resolution)
        available = set(registry)
        unknown = enabled - available
        if unknown:
            raise ContextContractError(
                f"enabled context capabilities have no descriptor: {sorted(unknown)}"
            )
        registry = {
            capability_id: descriptor
            for capability_id, descriptor in registry.items()
            if capability_id in enabled
        }
        if CORE_CAPABILITY_ID not in registry:
            raise ContextContractError(
                f"enabled context registry is missing {CORE_CAPABILITY_ID}"
            )
    _check_extension_names(registry)
    return registry


def _field_index(
    registry: Mapping[str, dict],
) -> tuple[dict[str, tuple[str, str]], dict[str, list[str]], dict[str, str]]:
    qualified = {}
    unqualified: dict[str, list[str]] = {}
    aliases: dict[str, str] = {}
    for capability_id, descriptor in registry.items():
        extension = _extension_name(capability_id)
        for field_name in descriptor.get("fields", {}):
            canonical = f"{capability_id}.{field_name}"
            qualified[canonical] = (capability_id, field_name)
            unqualified.setdefault(field_name, []).append(canonical)
            if capability_id == CORE_CAPABILITY_ID:
                variants = (
                    f"context.core.{field_name}",
                    f"core.{field_name}",
                )
            else:
                variants = (
                    f"context.extensions.{extension}.{field_name}",
                    f"extensions.{extension}.{field_name}",
                    f"{extension}.{field_name}",
                )
            for variant in variants:
                previous = aliases.get(variant)
                if previous is not None and previous != canonical:
                    aliases.pop(variant, None)
                elif variant not in aliases:
                    aliases[variant] = canonical
    return qualified, unqualified, aliases


def canonical_field_ref(field_ref: str, registry: Mapping[str, dict]) -> str:
    if not isinstance(field_ref, str) or not field_ref.strip():
        raise ContextContractError("context field reference must be a non-empty string")
    field_ref = field_ref.strip()
    qualified, unqualified, aliases = _field_index(registry)
    if field_ref in qualified:
        return field_ref
    if field_ref in aliases:
        return aliases[field_ref]
    candidates = unqualified.get(field_ref, [])
    if not candidates:
        raise ContextContractError(f"unknown context field: {field_ref}")
    if len(candidates) != 1:
        raise ContextContractError(
            f"ambiguous context field {field_ref!r}; use one of {candidates}"
        )
    return candidates[0]


def parse_context_col(spec: str) -> tuple[str, str]:
    if not isinstance(spec, str) or spec.count("=") != 1:
        raise ContextContractError("--context-col must use FIELD=COLUMN exactly once")
    field_ref, column = (part.strip() for part in spec.split("=", 1))
    if not field_ref or not column:
        raise ContextContractError("--context-col field and column must be non-empty")
    return field_ref, column


def parse_context_columns(
    specs: Sequence[str] | Mapping[str, object] | None,
) -> dict[str, object]:
    if specs is None:
        return {}
    if isinstance(specs, Mapping):
        items = list(specs.items())
    else:
        if isinstance(specs, str):
            specs = [specs]
        items = [parse_context_col(spec) for spec in specs]
    output = {}
    for raw_ref, column in items:
        if not isinstance(raw_ref, str) or not raw_ref.strip():
            raise ContextContractError("context column field must be non-empty")
        field_ref = raw_ref.strip()
        if field_ref in output:
            raise ContextContractError(f"duplicate --context-col field: {field_ref}")
        if type(column) is int:
            if column < 0:
                raise ContextContractError("context column index must be non-negative")
            output[field_ref] = column
        elif isinstance(column, str) and column.strip():
            output[field_ref] = column.strip()
        else:
            raise ContextContractError("context column must be a header or index")
    return output


def _header_matches(headers: Sequence[object], candidate: str) -> list[int]:
    normalized = candidate.strip().casefold()
    return [
        index
        for index, header in enumerate(headers)
        if isinstance(header, str) and header.strip().casefold() == normalized
    ]


def _resolve_candidate(
    headers: Sequence[object],
    candidate: object,
    *,
    no_header: bool,
    label: str,
    strict: bool,
) -> int | None:
    if type(candidate) is int:
        index = candidate
    elif no_header:
        if isinstance(candidate, str) and candidate.strip().isdigit():
            index = int(candidate.strip())
        elif strict:
            raise ContextContractError(
                f"{label} must be a 0-based integer in no-header mode"
            )
        else:
            return None
    else:
        if not isinstance(candidate, str) or not candidate.strip():
            if strict:
                raise ContextContractError(f"{label} must be a non-empty header")
            return None
        matches = _header_matches(headers, candidate)
        if len(matches) > 1:
            raise ContextContractError(
                f"{label} matches duplicate headers at columns {matches}"
            )
        return matches[0] if matches else None
    if index < 0 or index >= len(headers):
        if strict:
            raise ContextContractError(f"{label} column index is out of range: {index}")
        return None
    return index


def _legacy_profile_columns(profile: Mapping | None) -> dict[str, list[object]]:
    if not isinstance(profile, Mapping):
        return {}
    schema = profile.get("context_schema")
    if schema is None:
        return {}
    if not isinstance(schema, Mapping):
        raise ContextContractError("profile.context_schema must be an object")
    raw = schema.get("columns", {})
    if not isinstance(raw, Mapping):
        raise ContextContractError("profile.context_schema.columns must be an object")
    output = {}
    for field_ref, candidates in raw.items():
        if isinstance(candidates, (str, int)) and not isinstance(candidates, bool):
            candidates = [candidates]
        if not isinstance(candidates, list) or any(
            not isinstance(candidate, (str, int)) or isinstance(candidate, bool)
            for candidate in candidates
        ):
            raise ContextContractError(
                f"profile context columns for {field_ref!r} are invalid"
            )
        output[str(field_ref)] = list(candidates)
    return output


def _profile_column_candidates(
    profile: Mapping | None, registry: Mapping[str, dict]
) -> dict[str, list[object]]:
    qualified, _, _ = _field_index(registry)
    output = {}
    for field_ref, (capability_id, field_name) in qualified.items():
        columns = registry[capability_id]["fields"][field_name].get("columns", [])
        if columns:
            output[field_ref] = list(columns)
    for raw_ref, columns in _legacy_profile_columns(profile).items():
        field_ref = canonical_field_ref(raw_ref, registry)
        output.setdefault(field_ref, []).extend(columns)
    for field_ref, columns in output.items():
        unique = []
        for column in columns:
            if column not in unique:
                unique.append(column)
        output[field_ref] = unique
    return output


def resolve_context_columns(
    headers: Sequence[object],
    registry: Mapping[str, dict] | None = None,
    *,
    cli_columns: Sequence[str] | Mapping[str, object] | None = None,
    profile: Mapping | None = None,
    no_header: bool = False,
) -> dict[str, dict]:
    if not isinstance(headers, Sequence) or isinstance(headers, (str, bytes)):
        raise ContextContractError("headers must be a sequence")
    registry = dict(registry or descriptor_registry(profile))
    cli = {}
    for raw_ref, column in parse_context_columns(cli_columns).items():
        field_ref = canonical_field_ref(raw_ref, registry)
        if field_ref in cli:
            raise ContextContractError(
                f"duplicate CLI mapping for context field: {field_ref}"
            )
        cli[field_ref] = column
    profile_candidates = _profile_column_candidates(profile, registry)
    qualified, _, _ = _field_index(registry)
    resolved = {}
    for field_ref, (capability_id, field_name) in qualified.items():
        if field_ref in cli:
            index = _resolve_candidate(
                headers,
                cli[field_ref],
                no_header=no_header,
                label=f"CLI context field {field_ref}",
                strict=True,
            )
            if index is None:
                raise ContextContractError(
                    f"CLI context column for {field_ref} was not found"
                )
            method = "cli"
        else:
            matches = []
            for candidate in profile_candidates.get(field_ref, []):
                index = _resolve_candidate(
                    headers,
                    candidate,
                    no_header=no_header,
                    label=f"profile context field {field_ref}",
                    strict=False,
                )
                if index is not None:
                    matches.append(index)
            matches = list(dict.fromkeys(matches))
            if len(matches) > 1:
                raise ContextContractError(
                    f"profile context field {field_ref} matches multiple columns: {matches}"
                )
            if matches:
                index = matches[0]
                method = "profile"
            else:
                index = _resolve_candidate(
                    headers,
                    field_name,
                    no_header=no_header,
                    label=f"safe alias for context field {field_ref}",
                    strict=False,
                )
                if index is None:
                    continue
                method = "safe_alias"
        resolved[field_ref] = {
            "capability_id": capability_id,
            "descriptor_id": capability_id,
            "extension": _extension_name(capability_id),
            "field": field_name,
            "column_index": index,
            "column": headers[index] if not no_header else index,
            "method": method,
        }

    by_index: dict[int, list[str]] = {}
    for field_ref, mapping in resolved.items():
        by_index.setdefault(mapping["column_index"], []).append(field_ref)
    collisions = {
        index: fields for index, fields in by_index.items() if len(fields) > 1
    }
    if collisions:
        raise ContextContractError(
            f"one input column maps to multiple context fields: {collisions}"
        )
    return resolved


def _nonempty(value: object) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _split_strings(value: object) -> list[object]:
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
    output = []
    for item in items:
        if _nonempty(item) and item not in output:
            output.append(item)
    return output


def _normalize_scalar(value: object, field: Mapping, label: str) -> object:
    normalizer = field["normalizer"]
    if normalizer == "trim":
        value = str(value).strip()
    elif normalizer == "lowercase":
        value = str(value).strip().casefold()
    elif normalizer == "positive_integer":
        if type(value) is bool:
            raise ContextContractError(f"{label} must be a positive integer")
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise ContextContractError(f"{label} must be a positive integer") from exc
        if value <= 0:
            raise ContextContractError(f"{label} must be a positive integer")

    field_type = field["type"]
    if field_type in {"string", "enum"}:
        value = str(value)
        if normalizer in {"trim", "lowercase", "string_list"}:
            value = value.strip()
    elif field_type == "integer":
        if type(value) is bool:
            raise ContextContractError(f"{label} must be an integer")
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise ContextContractError(f"{label} must be an integer") from exc
    elif field_type == "boolean":
        if type(value) is bool:
            pass
        elif isinstance(value, str) and value.strip().casefold() in {"true", "false"}:
            value = value.strip().casefold() == "true"
        else:
            raise ContextContractError(f"{label} must be a boolean")
    if field_type == "enum" and value not in field["values"]:
        raise ContextContractError(
            f"{label} is not an allowed enum value: {value!r}"
        )
    return value


def _normalize_value(value: object, field: Mapping, label: str) -> object:
    cardinality = field["cardinality"]
    if not _nonempty(value):
        return [] if cardinality == "many" else None
    if cardinality == "many" or field["normalizer"] == "string_list":
        values = _split_strings(value)
        normalized = [_normalize_scalar(item, field, label) for item in values]
        output = []
        for item in normalized:
            if _nonempty(item) and item not in output:
                output.append(item)
        return output
    return _normalize_scalar(value, field, label)


def _json_safe_raw(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe_raw(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _json_safe_raw(item) for key, item in value.items()}
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return str(value)


def _row_value(row: Sequence | Mapping, column: Mapping) -> object:
    index = column["column_index"]
    if isinstance(row, Mapping):
        name = column["column"]
        if name in row:
            return row[name]
        if index in row:
            return row[index]
        return None
    return row[index] if index < len(row) else None


def _provenance_ref(capability_id: str, field_name: str) -> str:
    extension = _extension_name(capability_id)
    if capability_id == CORE_CAPABILITY_ID:
        return f"context.core.{field_name}"
    return f"context.extensions.{extension}.{field_name}"


def _condition_value(
    field_ref: str, values: Mapping[str, object], registry: Mapping[str, dict]
) -> object:
    return values.get(canonical_field_ref(field_ref, registry))


def _descriptor_applies(
    capability_id: str,
    descriptor: Mapping,
    values: Mapping[str, object],
    registry: Mapping[str, dict],
) -> bool:
    if capability_id == CORE_CAPABILITY_ID:
        return True
    present_fields = descriptor.get("applies_if_present", [])
    if any(
        _nonempty(_condition_value(field_ref, values, registry))
        for field_ref in present_fields
    ):
        return True
    conditions = descriptor.get("applies_when", {})
    if conditions:
        for field_ref, accepted in conditions.items():
            actual = _condition_value(field_ref, values, registry)
            actual_values = actual if isinstance(actual, list) else [actual]
            if not any(item in accepted for item in actual_values):
                return False
        return True
    return any(
        _nonempty(values.get(f"{capability_id}.{field_name}"))
        for field_name in descriptor.get("fields", {})
    )


def required_context_fields(
    profile: Mapping | None, registry: Mapping[str, dict]
) -> set[str]:
    if not isinstance(profile, Mapping):
        return set()
    schema = profile.get("context_schema")
    if schema is None:
        return set()
    if not isinstance(schema, Mapping):
        raise ContextContractError("profile.context_schema must be an object")
    raw = schema.get("required_columns", [])
    if not isinstance(raw, list):
        raise ContextContractError("context_schema.required_columns must be an array")
    return {canonical_field_ref(str(item), registry) for item in raw}


def extract_segment_context(
    row: Sequence | Mapping,
    resolved_columns: Mapping[str, Mapping],
    registry: Mapping[str, dict] | None = None,
    *,
    profile: Mapping | None = None,
    required_fields: Sequence[str] | None = None,
    source_provenance: Mapping | None = None,
) -> dict:
    registry = dict(registry or descriptor_registry(profile))
    qualified, _, _ = _field_index(registry)
    values = {}
    provenance = {}
    for raw_ref, column in resolved_columns.items():
        field_ref = canonical_field_ref(raw_ref, registry)
        capability_id, field_name = qualified[field_ref]
        raw = _row_value(row, column)
        normalized = _normalize_value(
            raw,
            registry[capability_id]["fields"][field_name],
            field_ref,
        )
        values[field_ref] = normalized
        if _nonempty(normalized):
            evidence = deepcopy(dict(source_provenance or {}))
            evidence.update(
                {
                    "method": "input_column",
                    "column_index": column["column_index"],
                    "column": column["column"],
                    "mapping_method": column["method"],
                    "raw": _json_safe_raw(raw),
                    "normalized": deepcopy(normalized),
                    "status": "verified",
                }
            )
            provenance[_provenance_ref(capability_id, field_name)] = evidence

    applicable = {
        capability_id
        for capability_id, descriptor in registry.items()
        if _descriptor_applies(capability_id, descriptor, values, registry)
    }
    required = {
        canonical_field_ref(str(field_ref), registry)
        for field_ref in (required_fields or [])
    }
    required.update(required_context_fields(profile, registry))
    for capability_id in applicable:
        for field_name, field in registry[capability_id].get("fields", {}).items():
            if field.get("required_when_applicable") is True:
                required.add(f"{capability_id}.{field_name}")
    missing = sorted(
        field_ref for field_ref in required if not _nonempty(values.get(field_ref))
    )

    core_descriptor = registry.get(CORE_CAPABILITY_ID, {"fields": {}})
    core = {
        field_name: deepcopy(values.get(f"{CORE_CAPABILITY_ID}.{field_name}"))
        for field_name in core_descriptor.get("fields", {})
    }
    extensions = {}
    for capability_id, descriptor in registry.items():
        if capability_id == CORE_CAPABILITY_ID:
            continue
        extension = _extension_name(capability_id)
        if capability_id not in applicable:
            extensions[extension] = {"status": "not_applicable"}
            continue
        item = {
            field_name: deepcopy(values.get(f"{capability_id}.{field_name}"))
            for field_name in descriptor.get("fields", {})
            if _nonempty(values.get(f"{capability_id}.{field_name}"))
        }
        capability_missing = [
            field_ref
            for field_ref in missing
            if qualified[field_ref][0] == capability_id
        ]
        item = {
            "status": "incomplete" if capability_missing else "ready",
            **item,
        }
        extensions[extension] = item
    return {
        "context_contract_version": CONTEXT_CONTRACT_VERSION,
        "status": "context_incomplete" if missing else "ready",
        "core": core,
        "extensions": extensions,
        "provenance": provenance,
        "missing_required": [
            _provenance_ref(*qualified[field_ref]) for field_ref in missing
        ],
    }


def _context_state_from_segment(segment: Mapping) -> Mapping:
    raw = segment.get("context")
    if not isinstance(raw, Mapping):
        raw = segment.get("segment_context")
    if isinstance(raw, Mapping) and isinstance(raw.get("context"), Mapping):
        raw = raw["context"]
    return raw if isinstance(raw, Mapping) else {}


def _extension_state(
    context_state: Mapping, capability_id: str
) -> Mapping:
    extensions = context_state.get("extensions", {})
    if not isinstance(extensions, Mapping):
        return {}
    extension = _extension_name(capability_id)
    raw = extensions.get(extension)
    if not isinstance(raw, Mapping):
        raw = extensions.get(capability_id)
    return raw if isinstance(raw, Mapping) else {}


def _selected_context_fields(
    context_state: Mapping,
    modules: Sequence[str],
    registry: Mapping[str, dict],
) -> tuple[dict, set[str]]:
    raw_core = context_state.get("core", {})
    raw_core = raw_core if isinstance(raw_core, Mapping) else {}
    output = {"core": {}, "extensions": {}}
    selected_refs = set()
    for capability_id, descriptor in registry.items():
        selected = []
        module_views = descriptor.get("module_views", {})
        for module in modules:
            for field_name in module_views.get(module, []):
                if field_name not in selected:
                    selected.append(field_name)
        if not selected:
            continue
        if capability_id == CORE_CAPABILITY_ID:
            target = output["core"]
            source = raw_core
        else:
            source = _extension_state(context_state, capability_id)
            target = {}
            status = source.get("status")
            if isinstance(status, str) and status:
                target["status"] = status
        for field_name in selected:
            value = source.get(field_name)
            if _nonempty(value):
                target[field_name] = deepcopy(value)
                selected_refs.add(f"{capability_id}.{field_name}")
        if capability_id != CORE_CAPABILITY_ID and target:
            output["extensions"][_extension_name(capability_id)] = target
    return output, selected_refs


def project_context_for_module(
    context_state: Mapping,
    module: str,
    registry: Mapping[str, dict] | None = None,
    *,
    include_provenance: bool = False,
) -> dict:
    return project_context_for_modules(
        context_state,
        [module],
        registry,
        include_provenance=include_provenance,
    )


def project_context_for_modules(
    context_state: Mapping,
    modules: Sequence[str],
    registry: Mapping[str, dict] | None = None,
    *,
    include_provenance: bool = False,
) -> dict:
    if not isinstance(context_state, Mapping):
        raise ContextContractError("context state must be an object")
    if not isinstance(modules, Sequence) or isinstance(modules, (str, bytes)):
        raise ContextContractError("projection modules must be an array")
    normalized_modules = []
    for module in modules:
        if not isinstance(module, str) or not module.strip():
            raise ContextContractError(
                "projection modules must contain non-empty strings"
            )
        module = module.strip()
        if module not in normalized_modules:
            normalized_modules.append(module)
    if not normalized_modules:
        raise ContextContractError("projection modules must not be empty")
    registry = dict(registry or builtin_descriptor_registry())
    projection, selected_refs = _selected_context_fields(
        context_state, normalized_modules, registry
    )
    if include_provenance:
        raw = context_state.get("provenance", {})
        if isinstance(raw, Mapping):
            selected_paths = {
                _provenance_ref(*_field_index(registry)[0][field_ref])
                for field_ref in selected_refs
            }
            projection["provenance"] = {
                field_ref: deepcopy(evidence)
                for field_ref, evidence in raw.items()
                if field_ref in selected_paths
            }
    return projection


def project_segment_for_module(
    segment: Mapping,
    module: str,
    registry: Mapping[str, dict] | None = None,
) -> dict:
    if not isinstance(segment, Mapping):
        raise ContextContractError("segment must be an object")
    registry = dict(registry or builtin_descriptor_registry())
    context_state = _context_state_from_segment(segment)
    if not context_state:
        return {}
    projection = project_context_for_module(context_state, module, registry)
    if not projection.get("core"):
        projection.pop("core", None)
    if not projection.get("extensions"):
        projection.pop("extensions", None)
    output = {}
    if _nonempty(segment.get("segment_key")):
        output["segment_key"] = deepcopy(segment["segment_key"])
    if _nonempty(segment.get("input_status")):
        output["input_status"] = deepcopy(segment["input_status"])
    output.update(projection)
    return output


def _equivalence_projection(
    segment: Mapping, module: str, registry: Mapping[str, dict]
) -> dict:
    projection = project_segment_for_module(segment, module, registry)
    projection.pop("segment_key", None)
    projection.pop("provenance", None)
    qualified, _, _ = _field_index(registry)
    core = projection.get("core")
    if isinstance(core, dict):
        core.pop("segment_key", None)
    extensions = projection.get("extensions")
    for field_ref, (capability_id, field_name) in qualified.items():
        field = registry[capability_id]["fields"][field_name]
        if field.get("affects_review_equivalence") is True:
            continue
        if capability_id == CORE_CAPABILITY_ID:
            container = core
        elif isinstance(extensions, dict):
            container = extensions.get(_extension_name(capability_id))
        else:
            container = None
        if isinstance(container, dict):
            container.pop(field_name, None)
    if isinstance(core, dict) and not core:
        projection.pop("core", None)
    if isinstance(extensions, dict):
        projection["extensions"] = {
            extension: value for extension, value in extensions.items() if value
        }
        if not projection["extensions"]:
            projection.pop("extensions")
    return projection


def _current_target(segment: Mapping) -> str:
    for field in ("current_target", "corrected", "target"):
        value = segment.get(field)
        if isinstance(value, str):
            return value
    return str(segment.get("target") or "")


def module_review_equivalence_payload(
    segment: Mapping,
    module: str,
    registry: Mapping[str, dict] | None = None,
    *,
    context_state: Mapping | None = None,
    precheck: Sequence[Mapping] | None = None,
) -> dict:
    if not isinstance(segment, Mapping):
        raise ContextContractError("segment must be an object")
    registry = dict(registry or builtin_descriptor_registry())
    effective_segment = dict(segment)
    if context_state is not None:
        effective_segment["context"] = context_state
    input_status = segment.get("input_status", "ready")
    payload = {
        "source": segment.get("source", ""),
        "target": _current_target(segment),
        "input": {
            "status": input_status,
            "blocked": input_status == "blocked" or bool(segment.get("blocked")),
            "block_reasons": deepcopy(segment.get("input_block_reasons", [])),
            "warnings": deepcopy(segment.get("input_warnings", [])),
        },
        "protection": {
            "protected": bool(segment.get("protected")),
            "reason": segment.get("protected_reason"),
            "protected_texts": deepcopy(segment.get("protected_texts", [])),
        },
        "context_projection": _equivalence_projection(
            effective_segment, module, registry
        ),
        "resolved_constraints": deepcopy(
            segment.get("resolved_constraints", [])
        ),
    }
    legacy_context = {
        field: deepcopy(segment[field])
        for field in ("content_type", "text_type_context", "context_note")
        if _nonempty(segment.get(field))
    }
    if legacy_context:
        payload["legacy_review_fields"] = legacy_context
    if module in _PRECHECK_MODULES and precheck:
        payload["precheck"] = deepcopy(list(precheck))
    return payload


def module_review_equivalence_key(
    segment: Mapping,
    module: str,
    registry: Mapping[str, dict] | None = None,
    *,
    context_state: Mapping | None = None,
    precheck: Sequence[Mapping] | None = None,
) -> str:
    return canonical_digest(
        module_review_equivalence_payload(
            segment,
            module,
            registry,
            context_state=context_state,
            precheck=precheck,
        )
    )


__all__ = [
    "CONTEXT_CONTRACT_VERSION",
    "CORE_CAPABILITY_ID",
    "ContextContractError",
    "builtin_descriptor_registry",
    "canonical_field_ref",
    "descriptor_registry",
    "extract_segment_context",
    "module_review_equivalence_key",
    "module_review_equivalence_payload",
    "parse_context_col",
    "parse_context_columns",
    "project_context_for_module",
    "project_context_for_modules",
    "project_segment_for_module",
    "required_context_fields",
    "resolve_context_columns",
    "validate_capability_descriptor",
]
