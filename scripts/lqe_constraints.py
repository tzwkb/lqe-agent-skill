"""Project-neutral context-rule validation and deterministic resolution.

This module owns only rule lifecycle, applicability, validity, authority,
priority, and conflict handling.  The meaning of ``expect`` is deliberately
delegated to a trusted provider supplied by the caller.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timezone
import hashlib
import json
import re
from typing import Callable, Mapping, Sequence


CONTEXT_RULES_SCHEMA = "lqe.context-rules"
CONTEXT_RULES_VERSION = 1
RULE_STATUSES = frozenset({"confirmed", "draft", "deprecated", "rejected"})

_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,255}$")
_LANG_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})*$")
_RULE_KEYS = frozenset(
    {
        "id",
        "capability",
        "provider",
        "target_lang",
        "rule_status",
        "priority",
        "authority",
        "valid_from",
        "valid_until",
        "when",
        "expect",
        "provenance",
    }
)
_POLICY_KEYS = frozenset({"schema", "version", "authority_rank", "rules"})
_MISSING = object()
_AMBIGUOUS = object()


class ConstraintContractError(ValueError):
    """Raised when context rules cannot be interpreted safely."""


def _canonical_bytes(value: object, *, label: str) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ConstraintContractError(f"{label} is not canonical JSON: {exc}") from exc


def canonical_digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value, label="constraint value")).hexdigest()


def _text(value: object, field: str, pattern: re.Pattern | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConstraintContractError(f"{field} must be a non-empty string")
    output = value.strip()
    if "\x00" in output:
        raise ConstraintContractError(f"{field} must not contain NUL")
    if pattern is not None and not pattern.fullmatch(output):
        raise ConstraintContractError(f"{field} has an invalid value: {output!r}")
    return output


def _string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ConstraintContractError(f"{field} must be a non-empty array")
    output: list[str] = []
    for item in value:
        normalized = _text(item, field)
        if normalized in output:
            raise ConstraintContractError(f"{field} must not contain duplicates")
        output.append(normalized)
    return output


def normalize_provider_ref(value: object, *, field: str = "provider") -> dict:
    if not isinstance(value, Mapping):
        raise ConstraintContractError(f"{field} must be an object")
    unknown = sorted(set(value) - {"id", "api_version"})
    if unknown:
        raise ConstraintContractError(f"{field} has unknown fields: {unknown}")
    provider_id = _text(value.get("id"), f"{field}.id", _PROVIDER_ID_RE)
    api_version = value.get("api_version")
    if type(api_version) is not int or api_version < 1:
        raise ConstraintContractError(f"{field}.api_version must be a positive integer")
    return {"id": provider_id, "api_version": api_version}


def provider_key(value: object) -> str:
    provider = normalize_provider_ref(value)
    return f"{provider['id']}@{provider['api_version']}"


def _parse_boundary(value: object, field: str, *, end: bool) -> datetime | None:
    if value is None:
        return None
    raw = _text(value, field)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            parsed_date = date.fromisoformat(raw)
            parsed = datetime.combine(parsed_date, time.max if end else time.min)
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConstraintContractError(f"{field} must be an ISO-8601 date or datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_as_of(value: datetime | date | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    parsed = _parse_boundary(value, "as_of", end=False)
    if parsed is None:
        raise ConstraintContractError("as_of must not be null")
    return parsed


def _condition_values(value: object, field: str) -> list[object]:
    values = value if isinstance(value, list) else [value]
    if not values:
        raise ConstraintContractError(f"{field} must not be empty")
    output: list[object] = []
    seen: set[bytes] = set()
    for item in values:
        if type(item) not in (str, int, bool):
            raise ConstraintContractError(
                f"{field} values must be strings, integers, or booleans"
            )
        if isinstance(item, str):
            item = _text(item, field)
        token = _canonical_bytes(item, label=field)
        if token in seen:
            raise ConstraintContractError(f"{field} has duplicate values")
        seen.add(token)
        output.append(item)
    return output


def _normalize_when(value: object, field: str) -> dict[str, list[object]]:
    if not isinstance(value, Mapping):
        raise ConstraintContractError(f"{field} must be an object")
    output: dict[str, list[object]] = {}
    for raw_name in sorted(value):
        name = _text(raw_name, f"{field} field", _FIELD_RE)
        output[name] = _condition_values(value[raw_name], f"{field}.{name}")
    return output


def _normalize_json_object(value: object, field: str, *, nonempty: bool) -> dict:
    if not isinstance(value, Mapping) or (nonempty and not value):
        suffix = " a non-empty object" if nonempty else " an object"
        raise ConstraintContractError(f"{field} must be{suffix}")
    normalized = deepcopy(dict(value))
    _canonical_bytes(normalized, label=field)
    return normalized


def _normalize_rule(value: object, index: int) -> dict:
    field = f"rules[{index}]"
    if not isinstance(value, Mapping):
        raise ConstraintContractError(f"{field} must be an object")
    unknown = sorted(set(value) - _RULE_KEYS)
    if unknown:
        raise ConstraintContractError(f"{field} has unknown fields: {unknown}")

    rule_id = _text(value.get("id"), f"{field}.id")
    capability = _text(value.get("capability"), f"{field}.capability", _CAPABILITY_RE)
    provider = normalize_provider_ref(value.get("provider"), field=f"{field}.provider")
    status = value.get("rule_status")
    if status not in RULE_STATUSES:
        raise ConstraintContractError(
            f"{field}.rule_status must be one of {sorted(RULE_STATUSES)}"
        )
    priority = value.get("priority", 0)
    if type(priority) is not int:
        raise ConstraintContractError(f"{field}.priority must be an integer")

    authority = value.get("authority")
    if not isinstance(authority, Mapping):
        raise ConstraintContractError(f"{field}.authority must be an object")
    unknown_authority = sorted(set(authority) - {"issuer"})
    if unknown_authority:
        raise ConstraintContractError(
            f"{field}.authority has unknown fields: {unknown_authority}"
        )
    issuer = _text(authority.get("issuer"), f"{field}.authority.issuer")

    target_lang = value.get("target_lang")
    if capability.startswith("language."):
        target_lang = _text(target_lang, f"{field}.target_lang", _LANG_RE)
    elif target_lang is not None:
        target_lang = _text(target_lang, f"{field}.target_lang", _LANG_RE)

    valid_from = value.get("valid_from")
    valid_until = value.get("valid_until")
    parsed_from = _parse_boundary(valid_from, f"{field}.valid_from", end=False)
    parsed_until = _parse_boundary(valid_until, f"{field}.valid_until", end=True)
    if parsed_from is not None and parsed_until is not None and parsed_from > parsed_until:
        raise ConstraintContractError(
            f"{field}.valid_from must not be later than valid_until"
        )

    output = {
        "id": rule_id,
        "capability": capability,
        "provider": provider,
        "rule_status": status,
        "priority": priority,
        "authority": {"issuer": issuer},
        "valid_from": valid_from,
        "valid_until": valid_until,
        "when": _normalize_when(value.get("when", {}), f"{field}.when"),
        "expect": _normalize_json_object(value.get("expect"), f"{field}.expect", nonempty=True),
        "provenance": _normalize_json_object(
            value.get("provenance", {}), f"{field}.provenance", nonempty=False
        ),
    }
    if target_lang is not None:
        output["target_lang"] = target_lang
    return output


def validate_context_rules(policy: object) -> dict:
    """Validate and normalize a multi-provider context-rule container."""

    if not isinstance(policy, Mapping):
        raise ConstraintContractError("context rules must be an object")
    unknown = sorted(set(policy) - _POLICY_KEYS)
    if unknown:
        raise ConstraintContractError(f"context rules have unknown fields: {unknown}")
    if policy.get("schema") != CONTEXT_RULES_SCHEMA:
        raise ConstraintContractError("unknown context-rules schema")
    if policy.get("version") != CONTEXT_RULES_VERSION:
        raise ConstraintContractError("unsupported context-rules version")

    authority_rank = _string_list(policy.get("authority_rank"), "authority_rank")
    raw_rules = policy.get("rules")
    if not isinstance(raw_rules, list):
        raise ConstraintContractError("rules must be an array")
    rules = [_normalize_rule(rule, index) for index, rule in enumerate(raw_rules)]
    ids: set[str] = set()
    for rule in rules:
        if rule["id"] in ids:
            raise ConstraintContractError(f"duplicate rule id: {rule['id']}")
        ids.add(rule["id"])
    return {
        "schema": CONTEXT_RULES_SCHEMA,
        "version": CONTEXT_RULES_VERSION,
        "authority_rank": authority_rank,
        "rules": rules,
    }


def _recursive_named_values(value: object, name: str) -> list[object]:
    found: list[object] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == name:
                found.append(child)
            found.extend(_recursive_named_values(child, name))
    elif isinstance(value, list):
        for child in value:
            found.extend(_recursive_named_values(child, name))
    return found


def _lookup_context(context: Mapping, field: str) -> object:
    if field in context:
        return context[field]
    if "." in field:
        current: object = context
        for part in field.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return _MISSING
            current = current[part]
        return current
    matches = _recursive_named_values(context, field)
    if not matches:
        return _MISSING
    unique = {_canonical_bytes(item, label=f"context.{field}") for item in matches}
    if len(unique) > 1:
        return _AMBIGUOUS
    return matches[0]


def _matches(actual: object, allowed: Sequence[object]) -> bool:
    actual_values = actual if isinstance(actual, list) else [actual]
    allowed_tokens = {_canonical_bytes(item, label="rule condition") for item in allowed}
    return any(
        _canonical_bytes(item, label="context condition") in allowed_tokens
        for item in actual_values
    )


def _applicability(rule: Mapping, context: Mapping) -> tuple[bool, str | None]:
    for field, allowed in rule["when"].items():
        actual = _lookup_context(context, field)
        if actual is _MISSING:
            return False, "missing_context_field"
        if actual is _AMBIGUOUS:
            return False, "ambiguous_context_field"
        if not _matches(actual, allowed):
            return False, "condition_not_met"
    return True, None


def _specificity(rule: Mapping) -> tuple[int, int]:
    conditions = rule["when"]
    alternatives = sum(max(0, len(values) - 1) for values in conditions.values())
    return len(conditions), -alternatives


def _active_at(rule: Mapping, as_of: datetime) -> bool:
    valid_from = _parse_boundary(rule.get("valid_from"), "valid_from", end=False)
    valid_until = _parse_boundary(rule.get("valid_until"), "valid_until", end=True)
    return (valid_from is None or as_of >= valid_from) and (
        valid_until is None or as_of <= valid_until
    )


def select_constraint_rules(
    policy: object,
    context: Mapping,
    *,
    capability: str,
    provider: object,
    target_lang: str | None = None,
    as_of: datetime | date | str | None = None,
    expect_validator: Callable[[object], dict] | None = None,
) -> dict:
    """Select the winning rule tier without interpreting target semantics."""

    normalized = validate_context_rules(policy)
    if not isinstance(context, Mapping):
        raise ConstraintContractError("context must be an object")
    capability = _text(capability, "capability", _CAPABILITY_RE)
    normalized_provider = normalize_provider_ref(provider)
    provider_identity = provider_key(normalized_provider)
    if capability.startswith("language."):
        target_lang = _text(target_lang, "target_lang", _LANG_RE)
    elif target_lang is not None:
        target_lang = _text(target_lang, "target_lang", _LANG_RE)
    moment = _normalize_as_of(as_of)
    authority_index = {
        issuer: index for index, issuer in enumerate(normalized["authority_rank"])
    }

    candidates: list[dict] = []
    ignored: dict[str, list[str]] = {}
    scoped_count = 0
    for rule in normalized["rules"]:
        if rule["capability"] != capability or provider_key(rule["provider"]) != provider_identity:
            continue
        if rule.get("target_lang") != target_lang:
            continue
        scoped_count += 1
        reason = None
        if rule["rule_status"] != "confirmed":
            reason = "not_confirmed"
        elif not _active_at(rule, moment):
            reason = "outside_validity"
        else:
            applies, reason = _applicability(rule, context)
            if applies:
                normalized_rule = deepcopy(rule)
                if expect_validator is not None:
                    try:
                        expected = expect_validator(deepcopy(rule["expect"]))
                    except (TypeError, ValueError) as exc:
                        raise ConstraintContractError(
                            f"rule {rule['id']} expect is invalid: {exc}"
                        ) from exc
                    if not isinstance(expected, Mapping) or not expected:
                        raise ConstraintContractError(
                            f"rule {rule['id']} provider returned an invalid expectation"
                        )
                    normalized_rule["expect"] = deepcopy(dict(expected))
                    _canonical_bytes(normalized_rule["expect"], label=f"rule {rule['id']} expect")
                issuer = rule["authority"]["issuer"]
                candidates.append(
                    {
                        "rule": normalized_rule,
                        "specificity": _specificity(rule),
                        "authority_rank": authority_index.get(issuer, len(authority_index)),
                        "priority": rule["priority"],
                    }
                )
                continue
        ignored.setdefault(reason or "not_applicable", []).append(rule["id"])

    if not candidates:
        reason_codes = ["no_applicable_confirmed_rule"]
        if scoped_count == 0:
            reason_codes = ["no_rule_for_scope"]
        elif "missing_context_field" in ignored or "ambiguous_context_field" in ignored:
            reason_codes = ["insufficient_context"]
        result = {
            "status": "insufficient_context",
            "capability": capability,
            "provider": normalized_provider,
            "target_lang": target_lang,
            "rule_ids": [],
            "rules": [],
            "expected": None,
            "reason_codes": reason_codes,
            "ignored": ignored,
        }
        result["resolution_digest"] = canonical_digest(
            {key: value for key, value in result.items() if key != "rules"}
        )
        return result

    best_specificity = max(item["specificity"] for item in candidates)
    candidates = [item for item in candidates if item["specificity"] == best_specificity]
    best_authority = min(item["authority_rank"] for item in candidates)
    candidates = [item for item in candidates if item["authority_rank"] == best_authority]
    best_priority = max(item["priority"] for item in candidates)
    candidates = [item for item in candidates if item["priority"] == best_priority]

    selected_rules = [item["rule"] for item in candidates]
    expectations: dict[bytes, dict] = {}
    for rule in selected_rules:
        token = _canonical_bytes(rule["expect"], label=f"rule {rule['id']} expect")
        expectations[token] = rule["expect"]
    conflict = len(expectations) > 1
    result = {
        "status": "conflict" if conflict else "resolved",
        "capability": capability,
        "provider": normalized_provider,
        "target_lang": target_lang,
        "rule_ids": [rule["id"] for rule in selected_rules],
        "rules": selected_rules,
        "expected": None if conflict else deepcopy(next(iter(expectations.values()))),
        "reason_codes": ["equal_rank_conflicting_expectations"] if conflict else [],
        "selection": {
            "specificity": {
                "condition_count": best_specificity[0],
                "alternative_penalty": -best_specificity[1],
            },
            "authority_rank": best_authority,
            "priority": best_priority,
        },
        "ignored": ignored,
    }
    digest_value = deepcopy(result)
    digest_value["rules_digest"] = canonical_digest(selected_rules)
    digest_value.pop("rules")
    result["resolution_digest"] = canonical_digest(digest_value)
    return result


def resolve_constraints(
    policy: object,
    context: Mapping,
    *,
    capability: str,
    provider: object,
    target_lang: str | None = None,
    as_of: datetime | date | str | None = None,
    expect_validator: Callable[[object], dict] | None = None,
) -> dict:
    """Public alias for deterministic generic rule resolution."""

    return select_constraint_rules(
        policy,
        context,
        capability=capability,
        provider=provider,
        target_lang=target_lang,
        as_of=as_of,
        expect_validator=expect_validator,
    )
