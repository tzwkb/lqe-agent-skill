"""Profile v1/v2 normalization and capability negotiation.

This module is deliberately independent from ``lqe_io`` so the read command can
adopt it inside its existing staging transaction.  It never scans project
directories and never enables a capability merely because a matching file is
present.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Iterable, Mapping

from lqe_project_assets import ProjectAssetError, normalize_asset_registry


NORMALIZED_PROFILE_SCHEMA = "lqe.normalized-project-profile"
NORMALIZED_PROFILE_VERSION = 1
CAPABILITY_RESOLUTION_SCHEMA = "lqe.capability-resolution"
CAPABILITY_RESOLUTION_VERSION = 1
JOB_RUNTIME_CONTRACT_VERSION = 2

PIPELINE_MODES = frozenset({"off", "shadow", "enforce"})
FOUNDATION_CAPABILITIES = frozenset(
    {"context.core@1", "source_provenance@1"}
)

SAFE_FIELD_TYPES = frozenset({"string", "integer", "boolean", "enum"})
SAFE_CARDINALITIES = frozenset({"one", "many"})
SAFE_NORMALIZERS = frozenset(
    {"identity", "trim", "lowercase", "positive_integer", "string_list"}
)
MODULE_NAMES = frozenset(
    {
        "terminology",
        "precheck_review",
        "accuracy",
        "grammar",
        "naturalness",
        "proper_names",
        "term_audit",
        "suggestions",
    }
)
_MODULE_CONTEXT_VIEW_KEYS = frozenset(
    {
        "capabilities",
        "dimensions",
        "constraint_kinds",
        "include_constraints",
        "neighbors",
        "limits",
    }
)
_MODULE_CONTEXT_NEIGHBOR_KEYS = frozenset(
    {"before", "after", "include_target", "boundary_mode"}
)
_MODULE_CONTEXT_LIMIT_KEYS = frozenset(
    {"max_facts_per_entity", "max_relations", "max_runtime_examples"}
)

_CAPABILITY_ID_RE = re.compile(
    r"^[a-z][a-z0-9_.-]*(?:/[a-z][a-z0-9_.-]*)*@[1-9][0-9]*$"
)
_FIELD_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_FORBIDDEN_DECLARATIVE_KEYS = frozenset(
    {
        "python",
        "python_path",
        "callable",
        "function",
        "code",
        "script",
        "command",
        "exec",
        "import",
        "template",
        "prompt",
        "instructions",
        "implementation",
    }
)
_DESCRIPTOR_KEYS = frozenset(
    {
        "schema",
        "version",
        "id",
        "applies_when",
        "applies_if_present",
        "fields",
        "module_views",
        "comparison_rules",
        "window_rules",
    }
)
_FIELD_KEYS = frozenset(
    {
        "type",
        "cardinality",
        "values",
        "columns",
        "required_when_applicable",
        "normalizer",
        "affects_review_equivalence",
    }
)
_CAPABILITY_DECLARATION_KEYS = frozenset(
    {"required", "config", "asset", "provider"}
)


class ProfileContractError(ValueError):
    """Raised when a profile cannot be normalized safely."""


class CapabilityNegotiationError(ValueError):
    """Raised when a required capability cannot be resolved."""


def _canonical_bytes(value: object, *, context: str) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProfileContractError(f"{context} is not canonical JSON: {exc}") from exc


def canonical_digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value, context="profile value")).hexdigest()


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileContractError(f"{field} must be a non-empty string")
    if "\x00" in value:
        raise ProfileContractError(f"{field} must not contain NUL")
    return value.strip()


def _check_no_executable_keys(value: object, *, context: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ProfileContractError(f"{context} keys must be strings")
            if key.casefold() in _FORBIDDEN_DECLARATIVE_KEYS:
                raise ProfileContractError(
                    f"{context} contains forbidden executable field {key!r}"
                )
            _check_no_executable_keys(child, context=f"{context}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _check_no_executable_keys(child, context=f"{context}[{index}]")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ProfileContractError(f"{context} contains unsupported value type")


def _validate_string_list(value: object, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ProfileContractError(f"{field} must be a non-empty array")
    output = []
    for item in value:
        text = _nonempty_string(item, field)
        if text in output:
            raise ProfileContractError(f"{field} must not contain duplicates")
        output.append(text)
    return output


def _validate_applies_when(value: object, field: str) -> dict:
    if not isinstance(value, dict):
        raise ProfileContractError(f"{field} must be an object")
    output = {}
    for key in sorted(value):
        if not isinstance(key, str) or not _FIELD_NAME_RE.fullmatch(key):
            raise ProfileContractError(f"{field} contains an invalid field name")
        raw = value[key]
        items = raw if isinstance(raw, list) else [raw]
        if not items:
            raise ProfileContractError(f"{field}.{key} must not be empty")
        normalized = []
        for item in items:
            if not isinstance(item, (str, int, bool)) or isinstance(item, float):
                raise ProfileContractError(
                    f"{field}.{key} values must be strings, integers, or booleans"
                )
            if isinstance(item, str):
                item = _nonempty_string(item, f"{field}.{key}")
            if item in normalized:
                raise ProfileContractError(f"{field}.{key} has duplicate values")
            normalized.append(item)
        output[key] = normalized
    return output


def _validate_applies_if_present(value: object, field: str) -> list[str]:
    values = _validate_string_list(value, field)
    for item in values:
        if not _FIELD_NAME_RE.fullmatch(item):
            raise ProfileContractError(
                f"{field} contains an invalid field reference: {item!r}"
            )
    return values


def _validate_window_rules(value: object, field: str) -> dict:
    if not isinstance(value, dict):
        raise ProfileContractError(f"{field} must be an object")
    unknown = sorted(set(value) - {"before", "after"})
    if unknown:
        raise ProfileContractError(f"{field} has unknown fields: {unknown}")
    output = {}
    for key in ("before", "after"):
        raw = value.get(key, 0)
        if type(raw) is not int or raw < 0 or raw > 20:
            raise ProfileContractError(f"{field}.{key} must be an integer from 0 to 20")
        output[key] = raw
    return output


def validate_capability_descriptor(descriptor: object, *, custom: bool = False) -> dict:
    """Validate a safe declarative context descriptor.

    Custom descriptors cannot contain executable hooks.  The same structural
    validator is used for bundled descriptors so consumers receive one shape.
    """

    if not isinstance(descriptor, dict):
        raise ProfileContractError("capability descriptor must be an object")
    unknown = sorted(set(descriptor) - _DESCRIPTOR_KEYS)
    if unknown:
        raise ProfileContractError(
            f"capability descriptor has unknown fields: {', '.join(unknown)}"
        )
    _check_no_executable_keys(descriptor, context="capability descriptor")
    if descriptor.get("schema") != "lqe.context-capability-descriptor":
        raise ProfileContractError("unknown capability descriptor schema")
    if descriptor.get("version") != 1:
        raise ProfileContractError("unsupported capability descriptor version")
    capability_id = _nonempty_string(descriptor.get("id"), "descriptor.id")
    if not _CAPABILITY_ID_RE.fullmatch(capability_id):
        raise ProfileContractError(f"invalid capability id: {capability_id!r}")

    raw_fields = descriptor.get("fields", {})
    if not isinstance(raw_fields, dict):
        raise ProfileContractError("descriptor.fields must be an object")
    fields = {}
    for field_name in sorted(raw_fields):
        if not isinstance(field_name, str) or not _FIELD_NAME_RE.fullmatch(field_name):
            raise ProfileContractError(f"invalid descriptor field: {field_name!r}")
        raw = raw_fields[field_name]
        if not isinstance(raw, dict):
            raise ProfileContractError(f"descriptor field {field_name} must be an object")
        unknown_field_keys = sorted(set(raw) - _FIELD_KEYS)
        if unknown_field_keys:
            raise ProfileContractError(
                f"descriptor field {field_name} has unknown fields: {unknown_field_keys}"
            )
        field_type = raw.get("type")
        if field_type not in SAFE_FIELD_TYPES:
            raise ProfileContractError(
                f"descriptor field {field_name}.type must be one of {sorted(SAFE_FIELD_TYPES)}"
            )
        cardinality = raw.get("cardinality", "one")
        if cardinality not in SAFE_CARDINALITIES:
            raise ProfileContractError(
                f"descriptor field {field_name}.cardinality is invalid"
            )
        normalizer = raw.get("normalizer", "identity")
        if normalizer not in SAFE_NORMALIZERS:
            raise ProfileContractError(
                f"descriptor field {field_name}.normalizer is not allowed"
            )
        if field_type == "enum":
            values = _validate_string_list(
                raw.get("values"), f"descriptor field {field_name}.values"
            )
        elif "values" in raw:
            raise ProfileContractError(
                f"descriptor field {field_name}.values is only valid for enum"
            )
        else:
            values = None
        columns = raw.get("columns", [])
        if not isinstance(columns, list):
            raise ProfileContractError(
                f"descriptor field {field_name}.columns must be an array"
            )
        normalized_columns = []
        for column in columns:
            if not isinstance(column, (str, int)) or isinstance(column, bool):
                raise ProfileContractError(
                    f"descriptor field {field_name}.columns values are invalid"
                )
            if isinstance(column, str):
                column = _nonempty_string(
                    column, f"descriptor field {field_name}.columns"
                )
            elif column < 0:
                raise ProfileContractError(
                    f"descriptor field {field_name}.columns indices must be non-negative"
                )
            if column in normalized_columns:
                raise ProfileContractError(
                    f"descriptor field {field_name}.columns has duplicates"
                )
            normalized_columns.append(column)
        required = raw.get("required_when_applicable", False)
        affects = raw.get("affects_review_equivalence", False)
        if type(required) is not bool or type(affects) is not bool:
            raise ProfileContractError(
                f"descriptor field {field_name} boolean flags are invalid"
            )
        item = {
            "type": field_type,
            "cardinality": cardinality,
            "columns": normalized_columns,
            "required_when_applicable": required,
            "normalizer": normalizer,
            "affects_review_equivalence": affects,
        }
        if values is not None:
            item["values"] = values
        fields[field_name] = item

    raw_views = descriptor.get("module_views", {})
    if not isinstance(raw_views, dict):
        raise ProfileContractError("descriptor.module_views must be an object")
    views = {}
    for module in sorted(raw_views):
        if module not in MODULE_NAMES:
            raise ProfileContractError(f"unknown descriptor module view: {module}")
        names = _validate_string_list(
            raw_views[module], f"descriptor.module_views.{module}", allow_empty=True
        )
        unknown_names = sorted(set(names) - set(fields))
        if unknown_names:
            raise ProfileContractError(
                f"descriptor.module_views.{module} references unknown fields: {unknown_names}"
            )
        views[module] = names

    output = {
        "schema": "lqe.context-capability-descriptor",
        "version": 1,
        "id": capability_id,
        "fields": fields,
        "module_views": views,
    }
    if "applies_when" in descriptor:
        output["applies_when"] = _validate_applies_when(
            descriptor["applies_when"], "descriptor.applies_when"
        )
    if "applies_if_present" in descriptor:
        output["applies_if_present"] = _validate_applies_if_present(
            descriptor["applies_if_present"],
            "descriptor.applies_if_present",
        )
    if "comparison_rules" in descriptor:
        rules = descriptor["comparison_rules"]
        if not isinstance(rules, dict):
            raise ProfileContractError("descriptor.comparison_rules must be an object")
        _check_no_executable_keys(rules, context="descriptor.comparison_rules")
        _canonical_bytes(rules, context="descriptor.comparison_rules")
        output["comparison_rules"] = deepcopy(rules)
    if "window_rules" in descriptor:
        output["window_rules"] = _validate_window_rules(
            descriptor["window_rules"], "descriptor.window_rules"
        )
    if custom and capability_id in _BUILTIN_DESCRIPTORS:
        raise ProfileContractError(
            f"custom descriptor cannot replace built-in {capability_id}"
        )
    return output


def _field(
    field_type: str = "string",
    *,
    cardinality: str = "one",
    normalizer: str = "trim",
    affects: bool = True,
    required: bool = False,
) -> dict:
    return {
        "type": field_type,
        "cardinality": cardinality,
        "columns": [],
        "required_when_applicable": required,
        "normalizer": normalizer,
        "affects_review_equivalence": affects,
    }


def _descriptor(
    capability_id: str,
    fields: dict,
    views: dict,
    *,
    applies_if_present: list[str] | None = None,
) -> dict:
    output = {
        "schema": "lqe.context-capability-descriptor",
        "version": 1,
        "id": capability_id,
        "fields": fields,
        "module_views": views,
    }
    if applies_if_present is not None:
        output["applies_if_present"] = applies_if_present
    return output


_BUILTIN_DESCRIPTORS_RAW = {
    "context.core@1": _descriptor(
        "context.core@1",
        {
            "content_type": _field(),
            "context_note": _field(affects=True),
            "group_id": _field(),
        },
        {
            module: ["content_type", "context_note", "group_id"]
            for module in ("terminology", "accuracy", "grammar", "naturalness", "suggestions")
        },
    ),
    "context.dialogue@1": _descriptor(
        "context.dialogue@1",
        {
            "speaker_id": _field(required=True),
            "addressee_ids": _field(cardinality="many", normalizer="string_list"),
            "scene_id": _field(),
            "relationship_stage": _field(),
            "scene_tone": _field(),
        },
        {
            "accuracy": [
                "speaker_id",
                "addressee_ids",
                "scene_id",
                "relationship_stage",
                "scene_tone",
            ],
            "naturalness": [
                "speaker_id",
                "addressee_ids",
                "scene_id",
                "relationship_stage",
                "scene_tone",
            ],
            "suggestions": [
                "speaker_id",
                "addressee_ids",
                "scene_id",
                "relationship_stage",
                "scene_tone",
            ],
        },
        applies_if_present=["speaker_id"],
    ),
    "context.ui@1": _descriptor(
        "context.ui@1",
        {
            "screen_id": _field(),
            "component_type": _field(),
            "platform": _field(),
            "char_limit": _field("integer", normalizer="positive_integer"),
            "interaction_state": _field(),
        },
        {
            "accuracy": ["screen_id", "component_type", "interaction_state"],
            "grammar": ["char_limit"],
            "naturalness": ["component_type", "platform", "char_limit"],
            "suggestions": [
                "screen_id",
                "component_type",
                "platform",
                "char_limit",
                "interaction_state",
            ],
        },
    ),
    "context.marketing@1": _descriptor(
        "context.marketing@1",
        {
            "campaign_id": _field(),
            "channel": _field(),
            "market": _field(),
            "audience": _field(),
            "cta_type": _field(),
            "brand_tone": _field(),
        },
        {
            "accuracy": ["audience", "cta_type"],
            "naturalness": ["channel", "market", "audience", "cta_type", "brand_tone"],
            "suggestions": [
                "campaign_id",
                "channel",
                "market",
                "audience",
                "cta_type",
                "brand_tone",
            ],
        },
    ),
    "assets.review_examples@1": _descriptor(
        "assets.review_examples@1", {}, {}
    ),
    "assets.entity_registry@1": _descriptor(
        "assets.entity_registry@1", {}, {}
    ),
    "language_policy.register@1": _descriptor(
        "language_policy.register@1", {}, {}
    ),
    "source_provenance@1": _descriptor("source_provenance@1", {}, {}),
    "source.pivot_guard@1": _descriptor("source.pivot_guard@1", {}, {}),
}

_BUILTIN_DESCRIPTORS = {
    capability_id: validate_capability_descriptor(descriptor)
    for capability_id, descriptor in _BUILTIN_DESCRIPTORS_RAW.items()
}

_BUILTIN_META = {
    capability_id: {
        "pipeline_controlled": capability_id not in FOUNDATION_CAPABILITIES,
        "provider_required": capability_id.startswith("language_policy."),
    }
    for capability_id in _BUILTIN_DESCRIPTORS
}


def builtin_descriptor_registry() -> dict[str, dict]:
    return deepcopy(_BUILTIN_DESCRIPTORS)


def _normalize_custom_descriptors(value: object) -> dict[str, dict]:
    if value in (None, {}):
        return {}
    if isinstance(value, list):
        raw_items = []
        for descriptor in value:
            if not isinstance(descriptor, dict):
                raise ProfileContractError("custom descriptors must be objects")
            raw_items.append((descriptor.get("id"), descriptor))
    elif isinstance(value, dict):
        raw_items = list(value.items())
    else:
        raise ProfileContractError("capability_descriptors must be an object or array")

    output = {}
    for declared_id, raw in raw_items:
        descriptor = validate_capability_descriptor(raw, custom=True)
        if declared_id is not None and declared_id != descriptor["id"]:
            raise ProfileContractError(
                f"custom descriptor key {declared_id!r} does not match descriptor id"
            )
        if descriptor["id"] in output:
            raise ProfileContractError(
                f"duplicate custom descriptor {descriptor['id']!r}"
            )
        output[descriptor["id"]] = descriptor
    return output


def build_descriptor_registry(custom_descriptors: object = None) -> dict[str, dict]:
    registry = builtin_descriptor_registry()
    registry.update(_normalize_custom_descriptors(custom_descriptors))
    return registry


def _validate_capability_config(
    capability_id: str,
    config: object,
    descriptor: Mapping[str, object] | None,
) -> dict:
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise ProfileContractError(
            f"capability {capability_id}.config must be an object"
        )
    _check_no_executable_keys(config, context=f"capability {capability_id}.config")
    allowed = {
        "columns",
        "applies_when",
        "applies_if_present",
        "window_rules",
        "budget",
    }
    if capability_id == "context.core@1":
        allowed.add("identity")
    unknown = sorted(set(config) - allowed)
    if unknown:
        raise ProfileContractError(
            f"capability {capability_id}.config has unsupported fields: {unknown}"
        )
    output = {}
    if "columns" in config:
        columns = config["columns"]
        if not isinstance(columns, dict):
            raise ProfileContractError(
                f"capability {capability_id}.config.columns must be an object"
            )
        known_fields = set(descriptor.get("fields", {})) if descriptor else set()
        normalized_columns = {}
        for field_name in sorted(columns):
            if field_name not in known_fields:
                raise ProfileContractError(
                    f"capability {capability_id} config references unknown field {field_name!r}"
                )
            values = columns[field_name]
            if not isinstance(values, list) or not values:
                raise ProfileContractError(
                    f"capability {capability_id}.config.columns.{field_name} must be non-empty"
                )
            normalized = []
            for value in values:
                if not isinstance(value, (str, int)) or isinstance(value, bool):
                    raise ProfileContractError(
                        f"capability {capability_id}.config.columns.{field_name} is invalid"
                    )
                if isinstance(value, str):
                    value = _nonempty_string(
                        value,
                        f"capability {capability_id}.config.columns.{field_name}",
                    )
                elif value < 0:
                    raise ProfileContractError("column indices must be non-negative")
                if value in normalized:
                    raise ProfileContractError("column aliases must not repeat")
                normalized.append(value)
            normalized_columns[field_name] = normalized
        output["columns"] = normalized_columns
    if "applies_when" in config:
        output["applies_when"] = _validate_applies_when(
            config["applies_when"],
            f"capability {capability_id}.config.applies_when",
        )
    if "applies_if_present" in config:
        output["applies_if_present"] = _validate_applies_if_present(
            config["applies_if_present"],
            f"capability {capability_id}.config.applies_if_present",
        )
    if "window_rules" in config:
        output["window_rules"] = _validate_window_rules(
            config["window_rules"],
            f"capability {capability_id}.config.window_rules",
        )
    if "identity" in config:
        identity = config["identity"]
        if not isinstance(identity, dict):
            raise ProfileContractError(
                "capability context.core@1.config.identity must be an object"
            )
        _check_no_executable_keys(identity, context="context.core identity")
        _canonical_bytes(identity, context="context.core identity")
        output["identity"] = deepcopy(identity)
    if "budget" in config:
        budget = config["budget"]
        if not isinstance(budget, dict):
            raise ProfileContractError(
                f"capability {capability_id}.config.budget must be an object"
            )
        normalized_budget = {}
        for key, value in budget.items():
            if not isinstance(key, str) or not _FIELD_NAME_RE.fullmatch(key):
                raise ProfileContractError("capability budget key is invalid")
            if type(value) is not int or value < 0:
                raise ProfileContractError("capability budget values must be non-negative integers")
            normalized_budget[key] = value
        output["budget"] = normalized_budget
    return output


def _normalize_provider_reference(value: object, field: str) -> dict:
    if isinstance(value, str):
        text = _nonempty_string(value, field)
        if "@" not in text:
            raise ProfileContractError(f"{field} must include @api_version")
        provider_id, raw_version = text.rsplit("@", 1)
        try:
            api_version = int(raw_version)
        except ValueError as exc:
            raise ProfileContractError(f"{field} has invalid api version") from exc
        value = {"id": provider_id, "api_version": api_version}
    if not isinstance(value, dict):
        raise ProfileContractError(f"{field} must be a string or object")
    if set(value) != {"id", "api_version"}:
        raise ProfileContractError(f"{field} must contain exactly id and api_version")
    provider_id = _nonempty_string(value.get("id"), f"{field}.id")
    api_version = value.get("api_version")
    if type(api_version) is not int or api_version <= 0:
        raise ProfileContractError(f"{field}.api_version must be a positive integer")
    return {"id": provider_id, "api_version": api_version}


def _normalize_capability_declarations(
    raw: object,
    descriptors: Mapping[str, dict],
    *,
    legacy: bool,
) -> dict[str, dict]:
    if legacy:
        return {
            capability_id: {
                "required": True,
                "config": {},
                "declaration_origin": "legacy_adapter",
            }
            for capability_id in sorted(FOUNDATION_CAPABILITIES)
        }
    if not isinstance(raw, dict):
        raise ProfileContractError("v2 profile capabilities must be an object")
    output = {}
    for capability_id in sorted(raw):
        if not isinstance(capability_id, str) or not _CAPABILITY_ID_RE.fullmatch(capability_id):
            raise ProfileContractError(f"invalid capability id: {capability_id!r}")
        declaration = raw[capability_id]
        if not isinstance(declaration, dict):
            raise ProfileContractError(f"capability {capability_id} must be an object")
        unknown = sorted(set(declaration) - _CAPABILITY_DECLARATION_KEYS)
        if unknown:
            raise ProfileContractError(
                f"capability {capability_id} has unknown fields: {unknown}"
            )
        required = declaration.get("required")
        if type(required) is not bool:
            raise ProfileContractError(
                f"capability {capability_id}.required must be boolean"
            )
        descriptor = descriptors.get(capability_id)
        config = _validate_capability_config(
            capability_id, declaration.get("config", {}), descriptor
        )
        item = {"required": required, "config": config}
        if "asset" in declaration:
            item["asset"] = _nonempty_string(
                declaration["asset"], f"capability {capability_id}.asset"
            )
        if "provider" in declaration:
            item["provider"] = _normalize_provider_reference(
                declaration["provider"], f"capability {capability_id}.provider"
            )
        output[capability_id] = item
    for foundation in FOUNDATION_CAPABILITIES:
        item = output.get(foundation)
        if item is None or item["required"] is not True:
            raise ProfileContractError(
                f"v2 profile must declare required capability {foundation}"
            )
    return output


def _validate_scoring_policy(value: object) -> dict:
    if not isinstance(value, dict):
        raise ProfileContractError("v2 profile scoring_policy must be an object")
    required = {
        "threshold",
        "scorecard_profile",
        "severity_scale",
        "critical_gate",
        "repeat_dedup",
    }
    missing = sorted(required - set(value))
    if missing:
        raise ProfileContractError(f"v2 scoring_policy is missing fields: {missing}")
    threshold = value.get("threshold")
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise ProfileContractError("scoring_policy.threshold must be numeric")
    if not 0 <= threshold <= 100:
        raise ProfileContractError("scoring_policy.threshold must be from 0 to 100")
    for key in ("scorecard_profile", "severity_scale"):
        _nonempty_string(value.get(key), f"scoring_policy.{key}")
    for key in ("critical_gate", "repeat_dedup"):
        if type(value.get(key)) is not bool:
            raise ProfileContractError(f"scoring_policy.{key} must be boolean")
    _canonical_bytes(value, context="scoring_policy")
    return deepcopy(value)


def _validate_language_pair(source_lang: str, target_lang: str, language_pair: str) -> None:
    normalized_pair = language_pair.strip().casefold().replace("_", "-")
    normalized_source = source_lang.strip().casefold().replace("_", "-")
    normalized_target = target_lang.strip().casefold().replace("_", "-")
    if normalized_pair != f"{normalized_source}-{normalized_target}":
        raise ProfileContractError(
            "language_pair must equal source_lang-target_lang"
        )


def _normalize_tabular_adapter(value: object) -> dict:
    if value is None:
        return {"text_type_marker_rules": []}
    if not isinstance(value, dict) or set(value) != {"text_type_marker_rules"}:
        raise ProfileContractError(
            "profile.tabular must contain exactly text_type_marker_rules"
        )
    raw_rules = value["text_type_marker_rules"]
    if not isinstance(raw_rules, list):
        raise ProfileContractError(
            "profile.tabular.text_type_marker_rules must be an array"
        )
    output = []
    ids = set()
    source_values = set()
    for index, raw_rule in enumerate(raw_rules):
        field = f"profile.tabular.text_type_marker_rules[{index}]"
        if not isinstance(raw_rule, dict):
            raise ProfileContractError(f"{field} must be an object")
        allowed = {"id", "source_equals", "text_type_context", "text_type_from"}
        unknown = sorted(set(raw_rule) - allowed)
        if unknown:
            raise ProfileContractError(f"{field} has unknown fields: {unknown}")
        rule_id = _nonempty_string(raw_rule.get("id"), f"{field}.id")
        source_equals = _nonempty_string(
            raw_rule.get("source_equals"), f"{field}.source_equals"
        )
        if rule_id in ids:
            raise ProfileContractError(
                "profile.tabular text type marker rule ids must be unique"
            )
        if source_equals in source_values:
            raise ProfileContractError(
                "profile.tabular text type marker source_equals values must be unique"
            )
        ids.add(rule_id)
        source_values.add(source_equals)
        has_literal = "text_type_context" in raw_rule
        has_source = "text_type_from" in raw_rule
        if has_literal == has_source:
            raise ProfileContractError(
                f"{field} must define exactly one of text_type_context or text_type_from"
            )
        normalized = {"id": rule_id, "source_equals": source_equals}
        if has_literal:
            normalized["text_type_context"] = _nonempty_string(
                raw_rule["text_type_context"], f"{field}.text_type_context"
            )
        else:
            value_from = raw_rule["text_type_from"]
            if value_from not in {"source", "target", "content_type"}:
                raise ProfileContractError(
                    f"{field}.text_type_from must be source, target, or content_type"
                )
            normalized["text_type_from"] = value_from
        output.append(normalized)
    return {"text_type_marker_rules": output}


def _normalize_module_context_views(
    value: object,
    capabilities: Mapping[str, object],
) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProfileContractError("profile.module_context_views must be an object")
    output = {}
    declared_capabilities = set(capabilities)
    for module in sorted(value):
        if module not in MODULE_NAMES:
            raise ProfileContractError(
                f"profile.module_context_views has unknown module {module!r}"
            )
        raw_view = value[module]
        if not isinstance(raw_view, dict):
            raise ProfileContractError(
                f"profile.module_context_views.{module} must be an object"
            )
        unknown = sorted(set(raw_view) - _MODULE_CONTEXT_VIEW_KEYS)
        if unknown:
            raise ProfileContractError(
                f"profile.module_context_views.{module} has unknown fields: {unknown}"
            )
        view = {}
        if "capabilities" in raw_view:
            selected = _validate_string_list(
                raw_view["capabilities"],
                f"profile.module_context_views.{module}.capabilities",
            )
            invalid_ids = sorted(
                item for item in selected if not _CAPABILITY_ID_RE.fullmatch(item)
            )
            if invalid_ids:
                raise ProfileContractError(
                    f"profile.module_context_views.{module}.capabilities has invalid ids: "
                    f"{invalid_ids}"
                )
            undeclared = sorted(set(selected) - declared_capabilities)
            if undeclared:
                raise ProfileContractError(
                    f"profile.module_context_views.{module}.capabilities references "
                    f"undeclared capabilities: {undeclared}"
                )
            if "context.core@1" not in selected:
                raise ProfileContractError(
                    f"profile.module_context_views.{module}.capabilities must include "
                    "context.core@1"
                )
            view["capabilities"] = sorted(selected)
        for field in ("dimensions", "constraint_kinds"):
            if field in raw_view:
                view[field] = sorted(
                    _validate_string_list(
                        raw_view[field],
                        f"profile.module_context_views.{module}.{field}",
                        allow_empty=True,
                    )
                )
        if "include_constraints" in raw_view:
            include_constraints = raw_view["include_constraints"]
            if type(include_constraints) is not bool:
                raise ProfileContractError(
                    f"profile.module_context_views.{module}.include_constraints "
                    "must be boolean"
                )
            view["include_constraints"] = include_constraints
        else:
            include_constraints = True
        selected_capabilities = set(view.get("capabilities", []))
        language_policies = sorted(
            capability_id
            for capability_id in selected_capabilities
            if capability_id.startswith("language_policy.")
        )
        if (
            language_policies
            and include_constraints
            and not view.get("constraint_kinds")
        ):
            raise ProfileContractError(
                f"profile.module_context_views.{module} enables language policy "
                f"capabilities {language_policies} but does not declare "
                "constraint_kinds"
            )
        if "neighbors" in raw_view:
            raw_neighbors = raw_view["neighbors"]
            if not isinstance(raw_neighbors, dict):
                raise ProfileContractError(
                    f"profile.module_context_views.{module}.neighbors must be an object"
                )
            unknown_neighbors = sorted(
                set(raw_neighbors) - _MODULE_CONTEXT_NEIGHBOR_KEYS
            )
            if unknown_neighbors:
                raise ProfileContractError(
                    f"profile.module_context_views.{module}.neighbors has unknown "
                    f"fields: {unknown_neighbors}"
                )
            neighbors = {}
            for field in ("before", "after"):
                if field not in raw_neighbors:
                    continue
                raw_count = raw_neighbors[field]
                if type(raw_count) is not int or raw_count < 0:
                    raise ProfileContractError(
                        f"profile.module_context_views.{module}.neighbors.{field} "
                        "must be a non-negative integer"
                    )
                neighbors[field] = raw_count
            if "include_target" in raw_neighbors:
                include_target = raw_neighbors["include_target"]
                if type(include_target) is not bool:
                    raise ProfileContractError(
                        f"profile.module_context_views.{module}.neighbors.include_target "
                        "must be boolean"
                    )
                neighbors["include_target"] = include_target
            if "boundary_mode" in raw_neighbors:
                boundary_mode = raw_neighbors["boundary_mode"]
                if boundary_mode not in {"same_if_present", "strict"}:
                    raise ProfileContractError(
                        f"profile.module_context_views.{module}.neighbors.boundary_mode "
                        "must be same_if_present or strict"
                    )
                neighbors["boundary_mode"] = boundary_mode
            view["neighbors"] = neighbors
        if "limits" in raw_view:
            raw_limits = raw_view["limits"]
            if not isinstance(raw_limits, dict):
                raise ProfileContractError(
                    f"profile.module_context_views.{module}.limits must be an object"
                )
            unknown_limits = sorted(set(raw_limits) - _MODULE_CONTEXT_LIMIT_KEYS)
            if unknown_limits:
                raise ProfileContractError(
                    f"profile.module_context_views.{module}.limits has unknown fields: "
                    f"{unknown_limits}"
                )
            limits = {}
            for field in sorted(raw_limits):
                raw_limit = raw_limits[field]
                if type(raw_limit) is not int or raw_limit < 0:
                    raise ProfileContractError(
                        f"profile.module_context_views.{module}.limits.{field} "
                        "must be a non-negative integer"
                    )
                limits[field] = raw_limit
            view["limits"] = limits
        output[module] = view
    return output


def normalize_profile(profile: Mapping[str, object]) -> dict:
    """Normalize a legacy or v2 profile into one deterministic runtime shape."""

    if not isinstance(profile, Mapping):
        raise ProfileContractError("project profile must be an object")
    raw = deepcopy(dict(profile))
    raw_version = raw.get("profile_contract_version")
    if raw_version is None:
        version = 1
    elif type(raw_version) is int and raw_version in {1, 2}:
        version = raw_version
    else:
        raise ProfileContractError("unsupported profile_contract_version")
    legacy = version == 1

    language_pair = _nonempty_string(raw.get("language_pair"), "language_pair")
    source_lang = _nonempty_string(raw.get("source_lang"), "source_lang")
    target_lang = _nonempty_string(raw.get("target_lang"), "target_lang")
    if not legacy:
        _validate_language_pair(source_lang, target_lang, language_pair)
    if not legacy:
        _nonempty_string(raw.get("name"), "name")
        wordcount_basis = _nonempty_string(raw.get("wordcount_basis"), "wordcount_basis")
        if wordcount_basis not in {"source-chars", "target-words"}:
            raise ProfileContractError("v2 wordcount_basis is unsupported")
        raw["scoring_policy"] = _validate_scoring_policy(raw.get("scoring_policy"))

    raw_custom_descriptors = raw.get("capability_descriptors", {})
    custom_descriptors = _normalize_custom_descriptors(raw_custom_descriptors)
    descriptors = build_descriptor_registry(custom_descriptors)
    try:
        assets = normalize_asset_registry(raw, legacy=legacy)
    except ProjectAssetError as exc:
        raise ProfileContractError(str(exc)) from exc
    if not legacy:
        asset_kinds = {entry["kind"] for entry in assets.values()}
        missing_core_assets = sorted({"checks", "confirmed_rules"} - asset_kinds)
        if missing_core_assets:
            raise ProfileContractError(
                f"v2 profile must declare asset kinds: {missing_core_assets}"
            )

    if legacy:
        mode = "off"
    else:
        pipeline = raw.get("context_pipeline")
        if not isinstance(pipeline, dict) or set(pipeline) != {"mode"}:
            raise ProfileContractError(
                "v2 context_pipeline must contain exactly mode"
            )
        mode = pipeline.get("mode")
        if mode not in PIPELINE_MODES:
            raise ProfileContractError(
                f"context_pipeline.mode must be one of {sorted(PIPELINE_MODES)}"
            )

    capabilities = _normalize_capability_declarations(
        raw.get("capabilities"), descriptors, legacy=legacy
    )
    module_context_views = _normalize_module_context_views(
        raw.get("module_context_views"), capabilities
    )
    unsigned_profile = {
        key: value
        for key, value in dict(profile).items()
        if not (isinstance(key, str) and key.startswith("_"))
    }
    _canonical_bytes(unsigned_profile, context="project profile")

    output = deepcopy(raw)
    output.update(
        {
            "normalized_profile_schema": NORMALIZED_PROFILE_SCHEMA,
            "normalized_profile_version": NORMALIZED_PROFILE_VERSION,
            "profile_contract_version": version,
            "legacy_adapter": legacy,
            "context_pipeline": {"mode": mode},
            "assets": assets,
            "capabilities": capabilities,
            "capability_descriptors": custom_descriptors,
            "module_context_views": module_context_views,
            "source_profile_digest": canonical_digest(unsigned_profile),
        }
    )
    output["tabular"] = _normalize_tabular_adapter(raw.get("tabular"))
    return output


def resolve_capability_descriptor(
    capability_id: str,
    declaration: Mapping[str, object],
    registry: Mapping[str, dict] | None = None,
) -> dict:
    registry = registry or _BUILTIN_DESCRIPTORS
    descriptor = registry.get(capability_id)
    if descriptor is None:
        raise CapabilityNegotiationError(
            f"capability descriptor is unavailable: {capability_id}"
        )
    output = deepcopy(descriptor)
    config = declaration.get("config", {})
    if "columns" in config:
        for field_name, columns in config["columns"].items():
            output["fields"][field_name]["columns"] = deepcopy(columns)
    for key in ("applies_when", "window_rules"):
        if key in config:
            output[key] = deepcopy(config[key])
    if "applies_if_present" in config:
        output["applies_if_present"] = list(
            dict.fromkeys(
                [
                    *output.get("applies_if_present", []),
                    *config["applies_if_present"],
                ]
            )
        )
    # identity controls runtime business keys; it is not descriptor metadata.
    if "budget" in config:
        output["runtime_budget"] = deepcopy(config["budget"])
    return output


def _provider_registry(provider_registry: object) -> dict[tuple[str, int], dict]:
    if provider_registry is None:
        return {}
    if not isinstance(provider_registry, Mapping):
        raise ProfileContractError("provider_registry must be an object")
    output = {}
    for key, raw in provider_registry.items():
        if not isinstance(raw, dict):
            raise ProfileContractError(f"provider registry entry {key!r} must be an object")
        provider_id = raw.get("id")
        api_version = raw.get("api_version")
        if provider_id is None and isinstance(key, str) and "@" in key:
            provider_id, version_text = key.rsplit("@", 1)
            try:
                api_version = int(version_text)
            except ValueError as exc:
                raise ProfileContractError(f"invalid provider registry key {key!r}") from exc
        provider_id = _nonempty_string(provider_id, "provider.id")
        if type(api_version) is not int or api_version <= 0:
            raise ProfileContractError("provider.api_version must be positive")
        target_lang = _nonempty_string(raw.get("target_lang"), "provider.target_lang")
        descriptor = deepcopy(raw)
        descriptor.update(
            {"id": provider_id, "api_version": api_version, "target_lang": target_lang}
        )
        _canonical_bytes(descriptor, context="provider descriptor")
        identity = (provider_id, api_version)
        if identity in output:
            raise ProfileContractError(f"duplicate provider {provider_id}@{api_version}")
        output[identity] = descriptor
    return output


def _asset_entry(asset_statuses: object, asset_id: str) -> dict | None:
    if not isinstance(asset_statuses, Mapping):
        return None
    raw = asset_statuses.get(asset_id)
    if isinstance(raw, str):
        return {"status": raw}
    return deepcopy(raw) if isinstance(raw, dict) else None


def _supported_capabilities(
    registry: Mapping[str, dict], runtime_capabilities: object
) -> set[str]:
    if runtime_capabilities is None:
        return set(registry)
    if isinstance(runtime_capabilities, Mapping):
        raw = runtime_capabilities.keys()
    elif isinstance(runtime_capabilities, (set, frozenset, list, tuple)):
        raw = runtime_capabilities
    else:
        raise ProfileContractError(
            "runtime_capabilities must be an object or array of ids"
        )
    output = set()
    for item in raw:
        capability_id = _nonempty_string(item, "runtime capability id")
        output.add(capability_id)
    return output


def _required_failure(capability_id: str, reason: str) -> None:
    raise CapabilityNegotiationError(
        f"required capability {capability_id!r} cannot be enabled: {reason}"
    )


def resolve_capabilities(
    profile: Mapping[str, object],
    *,
    asset_statuses: Mapping[str, object] | None = None,
    runtime_capabilities: Iterable[str] | Mapping[str, object] | None = None,
    provider_registry: Mapping[str, object] | None = None,
    job_runtime_contract_version: int = JOB_RUNTIME_CONTRACT_VERSION,
) -> dict:
    """Resolve declared capabilities against runtime, assets, and providers."""

    normalized = (
        deepcopy(dict(profile))
        if profile.get("normalized_profile_schema") == NORMALIZED_PROFILE_SCHEMA
        else normalize_profile(profile)
    )
    validate_normalized_profile(normalized)
    if normalized.get("normalized_profile_version") != NORMALIZED_PROFILE_VERSION:
        raise ProfileContractError("unsupported normalized profile version")
    if type(job_runtime_contract_version) is not int or job_runtime_contract_version <= 0:
        raise ProfileContractError("job_runtime_contract_version must be positive")

    custom = normalized.get("capability_descriptors", {})
    registry = build_descriptor_registry(custom)
    supported = _supported_capabilities(registry, runtime_capabilities)
    supported.update(custom)
    providers = _provider_registry(provider_registry)
    declarations = normalized["capabilities"]
    mode = normalized["context_pipeline"]["mode"]
    enabled = {}
    disabled = {}
    warnings = []

    for capability_id in sorted(declarations):
        declaration = declarations[capability_id]
        required = declaration["required"]
        descriptor = registry.get(capability_id)
        if descriptor is None or capability_id not in supported:
            if required:
                _required_failure(capability_id, "unsupported_capability")
            disabled[capability_id] = {"reason": "unsupported_capability"}
            warnings.append(f"optional capability {capability_id} is unsupported")
            continue

        meta = _BUILTIN_META.get(
            capability_id,
            {"pipeline_controlled": True, "provider_required": False},
        )
        if mode == "off" and meta["pipeline_controlled"]:
            disabled[capability_id] = {"reason": "pipeline_off"}
            continue

        asset_id = declaration.get("asset")
        asset_snapshot = None
        if asset_id is not None:
            if asset_id not in normalized["assets"]:
                if required:
                    _required_failure(capability_id, "asset_not_declared")
                disabled[capability_id] = {
                    "reason": "asset_not_declared",
                    "asset": asset_id,
                }
                warnings.append(
                    f"optional capability {capability_id} references undeclared asset {asset_id}"
                )
                continue
            asset_snapshot = _asset_entry(asset_statuses, asset_id)
            status = asset_snapshot.get("status") if asset_snapshot else "uninspected"
            if status != "present":
                reason = "asset_missing" if status in {"missing", "external"} else "asset_uninspected"
                if required:
                    _required_failure(capability_id, reason)
                disabled[capability_id] = {
                    "reason": reason,
                    "asset": asset_id,
                    "asset_status": status,
                }
                warnings.append(
                    f"optional capability {capability_id} asset {asset_id} is {status}"
                )
                continue

        provider_reference = declaration.get("provider")
        provider = None
        if meta["provider_required"] and provider_reference is None:
            if required:
                _required_failure(capability_id, "provider_not_configured")
            disabled[capability_id] = {"reason": "provider_not_configured"}
            warnings.append(f"optional capability {capability_id} has no provider")
            continue
        if provider_reference is not None:
            identity = (
                provider_reference["id"],
                provider_reference["api_version"],
            )
            provider = providers.get(identity)
            if provider is None:
                if required:
                    _required_failure(capability_id, "provider_missing")
                disabled[capability_id] = {
                    "reason": "provider_missing",
                    "provider": f"{identity[0]}@{identity[1]}",
                }
                warnings.append(
                    f"optional capability {capability_id} provider is missing"
                )
                continue
            if provider["target_lang"].casefold() != str(
                normalized["target_lang"]
            ).casefold():
                if required:
                    _required_failure(capability_id, "provider_target_mismatch")
                disabled[capability_id] = {
                    "reason": "provider_target_mismatch",
                    "provider": f"{identity[0]}@{identity[1]}",
                }
                warnings.append(
                    f"optional capability {capability_id} provider target mismatches profile"
                )
                continue

        resolved_descriptor = resolve_capability_descriptor(
            capability_id, declaration, registry
        )
        item = {
            "required": required,
            "effect": (
                "foundation"
                if capability_id in FOUNDATION_CAPABILITIES
                else mode
            ),
            "descriptor_digest": canonical_digest(resolved_descriptor),
            "config_digest": canonical_digest(declaration.get("config", {})),
        }
        if asset_id is not None:
            item["asset"] = asset_id
            item["asset_digest"] = asset_snapshot.get("sha256")
        if provider is not None:
            item["provider"] = {
                "id": provider["id"],
                "api_version": provider["api_version"],
                "target_lang": provider["target_lang"],
                "descriptor_digest": canonical_digest(provider),
            }
        enabled[capability_id] = item

    for capability_id in sorted(_BUILTIN_DESCRIPTORS):
        if capability_id not in declarations:
            disabled[capability_id] = {"reason": "not_declared"}

    payload = {
        "schema": CAPABILITY_RESOLUTION_SCHEMA,
        "version": CAPABILITY_RESOLUTION_VERSION,
        "profile_contract_version": normalized["profile_contract_version"],
        "job_runtime_contract_version": job_runtime_contract_version,
        "context_pipeline_mode": mode,
        "source_profile_digest": normalized["source_profile_digest"],
        "enabled": enabled,
        "disabled": disabled,
        "warnings": warnings,
    }
    payload["digest"] = canonical_digest(payload)
    return payload


def validate_capability_resolution(value: object) -> dict:
    if not isinstance(value, dict):
        raise CapabilityNegotiationError("capability resolution must be an object")
    if value.get("schema") != CAPABILITY_RESOLUTION_SCHEMA:
        raise CapabilityNegotiationError("unknown capability resolution schema")
    if value.get("version") != CAPABILITY_RESOLUTION_VERSION:
        raise CapabilityNegotiationError("unsupported capability resolution version")
    for field in ("enabled", "disabled"):
        if not isinstance(value.get(field), dict):
            raise CapabilityNegotiationError(
                f"capability resolution {field} must be an object"
            )
    if not isinstance(value.get("warnings"), list):
        raise CapabilityNegotiationError(
            "capability resolution warnings must be an array"
        )
    if value.get("context_pipeline_mode") not in PIPELINE_MODES:
        raise CapabilityNegotiationError("capability resolution mode is invalid")
    digest = value.get("digest")
    if not isinstance(digest, str) or not digest:
        raise CapabilityNegotiationError("capability resolution digest is missing")
    unsigned = {key: deepcopy(item) for key, item in value.items() if key != "digest"}
    if digest != canonical_digest(unsigned):
        raise CapabilityNegotiationError("capability resolution digest mismatch")
    return deepcopy(value)


def validate_normalized_profile(value: object) -> dict:
    if not isinstance(value, dict):
        raise ProfileContractError("normalized profile must be an object")
    if value.get("normalized_profile_schema") != NORMALIZED_PROFILE_SCHEMA:
        raise ProfileContractError("unknown normalized profile schema")
    if value.get("normalized_profile_version") != NORMALIZED_PROFILE_VERSION:
        raise ProfileContractError("unsupported normalized profile version")
    if value.get("profile_contract_version") not in {1, 2}:
        raise ProfileContractError("normalized profile contract version is invalid")
    language_pair = _nonempty_string(value.get("language_pair"), "language_pair")
    source_lang = _nonempty_string(value.get("source_lang"), "source_lang")
    target_lang = _nonempty_string(value.get("target_lang"), "target_lang")
    if value.get("profile_contract_version") == 2:
        _validate_language_pair(source_lang, target_lang, language_pair)
    pipeline = value.get("context_pipeline")
    if not isinstance(pipeline, dict) or pipeline.get("mode") not in PIPELINE_MODES:
        raise ProfileContractError("normalized profile context pipeline is invalid")
    if not isinstance(value.get("assets"), dict):
        raise ProfileContractError("normalized profile assets must be an object")
    if not isinstance(value.get("capabilities"), dict):
        raise ProfileContractError("normalized profile capabilities must be an object")
    normalized_views = _normalize_module_context_views(
        value.get("module_context_views", {}), value["capabilities"]
    )
    if value.get("module_context_views", {}) != normalized_views:
        raise ProfileContractError(
            "normalized profile module_context_views is not canonical"
        )
    source_digest = value.get("source_profile_digest")
    if (
        not isinstance(source_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", source_digest)
    ):
        raise ProfileContractError("normalized profile source digest is invalid")
    return deepcopy(value)
