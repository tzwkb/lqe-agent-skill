"""Trusted target-language policy provider registry and dispatcher.

Profiles and project assets may reference a provider identifier, but never a
module path.  Only the fixed repository-owned mapping below is importable.
"""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Mapping, Sequence

from lqe_constraints import (
    canonical_digest,
    resolve_constraints,
    validate_context_rules,
)


_ROOT = Path(__file__).resolve().parents[1]
_TRUSTED_PROVIDER_SPECS = {
    "ko.register@1": {
        "id": "ko.register",
        "api_version": 1,
        "target_lang": "ko",
        "capability": "language.register",
        "module_path": _ROOT / "target_languages" / "ko" / "register.py",
    }
}
_REQUIRED_PROVIDER_CALLS = ("validate_policy", "resolve", "observe", "evaluate")


class LanguagePolicyError(ValueError):
    """Base error for trusted language-policy dispatch."""


class ProviderNotFoundError(LanguagePolicyError):
    """Raised when a provider is not in the repository-owned registry."""


class ProviderTargetMismatchError(LanguagePolicyError):
    """Raised when a trusted provider is requested for another target language."""


def _provider_key(provider: object) -> str:
    if isinstance(provider, str):
        value = provider.strip()
        if not value or value.count("@") != 1:
            raise LanguagePolicyError("provider must use id@api_version")
        provider_id, raw_version = value.rsplit("@", 1)
        if not raw_version.isdigit() or int(raw_version) < 1:
            raise LanguagePolicyError("provider api_version must be a positive integer")
        key = f"{provider_id}@{int(raw_version)}"
    elif isinstance(provider, Mapping):
        unknown = sorted(set(provider) - {"id", "api_version"})
        if unknown:
            raise LanguagePolicyError(
                f"provider reference has unknown fields: {unknown}"
            )
        provider_id = provider.get("id")
        api_version = provider.get("api_version")
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise LanguagePolicyError("provider.id must be a non-empty string")
        if type(api_version) is not int or api_version < 1:
            raise LanguagePolicyError(
                "provider.api_version must be a positive integer"
            )
        key = f"{provider_id.strip()}@{api_version}"
    else:
        raise LanguagePolicyError("provider must be an id@version string or object")
    if key not in _TRUSTED_PROVIDER_SPECS:
        raise ProviderNotFoundError(f"untrusted or unavailable provider: {key}")
    return key


def _module_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _public_descriptor(key: str) -> dict:
    spec = _TRUSTED_PROVIDER_SPECS[key]
    path = spec["module_path"]
    if not path.is_file() or path.is_symlink():
        raise ProviderNotFoundError(f"trusted provider module is unavailable: {key}")
    return {
        "id": spec["id"],
        "api_version": spec["api_version"],
        "target_lang": spec["target_lang"],
        "capability": spec["capability"],
        "module_sha256": _module_digest(path),
    }


def trusted_provider_registry(
    *, target_lang: str | None = None, capability: str | None = None
) -> dict[str, dict]:
    """Return descriptors without importing provider modules."""

    output = {}
    for key, spec in _TRUSTED_PROVIDER_SPECS.items():
        if target_lang is not None and spec["target_lang"] != target_lang:
            continue
        if capability is not None and spec["capability"] != capability:
            continue
        output[key] = _public_descriptor(key)
    return output


def provider_for(target_lang: str, capability: str) -> dict | None:
    matches = trusted_provider_registry(
        target_lang=target_lang, capability=capability
    )
    if not matches:
        return None
    if len(matches) > 1:
        raise LanguagePolicyError(
            f"multiple trusted providers for {target_lang}/{capability}"
        )
    return deepcopy(next(iter(matches.values())))


@lru_cache(maxsize=None)
def _load_trusted_module(key: str) -> ModuleType:
    spec = _TRUSTED_PROVIDER_SPECS[key]
    path: Path = spec["module_path"]
    trusted_root = (_ROOT / "target_languages").resolve()
    resolved_path = path.resolve()
    if (
        path.is_symlink()
        or not path.is_file()
        or trusted_root not in resolved_path.parents
    ):
        raise ProviderNotFoundError(f"trusted provider module is unavailable: {key}")
    module_spec = importlib.util.spec_from_file_location(
        f"_lqe_language_provider_{spec['id'].replace('.', '_')}_v{spec['api_version']}",
        path,
    )
    if module_spec is None or module_spec.loader is None:
        raise ProviderNotFoundError(f"cannot load trusted provider: {key}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    metadata = {
        "PROVIDER_ID": spec["id"],
        "API_VERSION": spec["api_version"],
        "TARGET_LANG": spec["target_lang"],
        "CAPABILITY": spec["capability"],
    }
    for name, expected_value in metadata.items():
        if getattr(module, name, None) != expected_value:
            raise LanguagePolicyError(
                f"trusted provider {key} has invalid metadata field {name}"
            )
    missing = [name for name in _REQUIRED_PROVIDER_CALLS if not callable(getattr(module, name, None))]
    if missing:
        raise LanguagePolicyError(
            f"trusted provider {key} is missing interface calls: {missing}"
        )
    return module


def load_provider(provider: object, target_lang: str) -> ModuleType:
    """Load a trusted provider after enforcing its target-language binding."""

    key = _provider_key(provider)
    descriptor = _TRUSTED_PROVIDER_SPECS[key]
    if descriptor["target_lang"] != target_lang:
        raise ProviderTargetMismatchError(
            f"provider {key} targets {descriptor['target_lang']}, not {target_lang}"
        )
    return _load_trusted_module(key)


def validate_policy(provider: object, target_lang: str, policy: object) -> dict:
    module = load_provider(provider, target_lang)
    try:
        normalized = module.validate_policy(deepcopy(policy))
    except (TypeError, ValueError) as exc:
        raise LanguagePolicyError(str(exc)) from exc
    if not isinstance(normalized, Mapping) or not normalized:
        raise LanguagePolicyError("provider returned an invalid normalized policy")
    return deepcopy(dict(normalized))


def resolve(
    provider: object,
    target_lang: str,
    context: Mapping,
    rules: Sequence[Mapping],
) -> dict:
    module = load_provider(provider, target_lang)
    try:
        result = module.resolve(deepcopy(dict(context)), deepcopy(list(rules)))
    except (TypeError, ValueError) as exc:
        raise LanguagePolicyError(str(exc)) from exc
    if not isinstance(result, Mapping) or result.get("status") not in {
        "resolved",
        "conflict",
        "insufficient_context",
    }:
        raise LanguagePolicyError("provider returned an invalid resolution")
    return deepcopy(dict(result))


def observe(provider: object, target_lang: str, target: str) -> dict:
    module = load_provider(provider, target_lang)
    try:
        result = module.observe(target)
    except (TypeError, ValueError) as exc:
        raise LanguagePolicyError(str(exc)) from exc
    if not isinstance(result, Mapping) or result.get("status") not in {
        "observed",
        "inconclusive",
    }:
        raise LanguagePolicyError("provider returned an invalid observation")
    return deepcopy(dict(result))


def evaluate(
    provider: object,
    target_lang: str,
    expected: Mapping,
    observed: Mapping,
) -> dict:
    module = load_provider(provider, target_lang)
    try:
        result = module.evaluate(deepcopy(dict(expected)), deepcopy(dict(observed)))
    except (TypeError, ValueError) as exc:
        raise LanguagePolicyError(str(exc)) from exc
    if not isinstance(result, Mapping) or result.get("status") not in {
        "match",
        "mismatch",
        "inconclusive",
    }:
        raise LanguagePolicyError("provider returned an invalid evaluation")
    return deepcopy(dict(result))


def _descriptor_for(provider: object, target_lang: str) -> tuple[str, dict, ModuleType]:
    key = _provider_key(provider)
    module = load_provider(provider, target_lang)
    descriptor = _public_descriptor(key)
    return key, descriptor, module


def resolve_language_policy(
    context_rules: object,
    context: Mapping,
    *,
    provider: object,
    target_lang: str,
    as_of: object = None,
) -> dict:
    """Run generic precedence first, then provider-specific resolution."""

    _, descriptor, module = _descriptor_for(provider, target_lang)
    provider_ref = {
        "id": descriptor["id"],
        "api_version": descriptor["api_version"],
    }
    try:
        normalized_rules = validate_context_rules(context_rules)
        for rule in normalized_rules["rules"]:
            if (
                rule["capability"] == descriptor["capability"]
                and rule["provider"] == provider_ref
                and rule.get("target_lang") == target_lang
            ):
                module.validate_policy(deepcopy(rule["expect"]))
        generic = resolve_constraints(
            normalized_rules,
            context,
            capability=descriptor["capability"],
            provider=provider_ref,
            target_lang=target_lang,
            as_of=as_of,
            expect_validator=module.validate_policy,
        )
    except (TypeError, ValueError) as exc:
        raise LanguagePolicyError(str(exc)) from exc

    provider_result = None
    if generic["status"] == "resolved":
        provider_result = resolve(
            provider_ref, target_lang, context, generic["rules"]
        )
    status = generic["status"]
    expected = generic["expected"]
    reason_codes = list(generic["reason_codes"])
    if provider_result is not None:
        status = provider_result["status"]
        expected = provider_result.get("expected")
        reason_codes.extend(provider_result.get("reason_codes", []))

    result = {
        "kind": descriptor["capability"],
        "provider": {
            "id": descriptor["id"],
            "api_version": descriptor["api_version"],
            "target_lang": descriptor["target_lang"],
            "module_sha256": descriptor["module_sha256"],
        },
        "status": status,
        "rule_ids": generic["rule_ids"],
        "expected": expected,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "selection": generic.get("selection"),
        "generic_resolution_digest": generic["resolution_digest"],
    }
    result["resolution_digest"] = canonical_digest(result)
    return result


def evaluate_language_policy(
    context_rules: object,
    context: Mapping,
    target: str,
    *,
    provider: object,
    target_lang: str,
    as_of: object = None,
) -> dict:
    constraint = resolve_language_policy(
        context_rules,
        context,
        provider=provider,
        target_lang=target_lang,
        as_of=as_of,
    )
    if constraint["status"] != "resolved":
        result = {
            "status": "inconclusive",
            "reason_codes": ["constraint_not_resolved"],
            "constraint": constraint,
            "observation": None,
            "evaluation": None,
        }
    else:
        observation = observe(provider, target_lang, target)
        evaluation = evaluate(
            provider, target_lang, constraint["expected"], observation
        )
        result = {
            "status": evaluation["status"],
            "reason_codes": evaluation.get("reason_codes", []),
            "constraint": constraint,
            "observation": observation,
            "evaluation": evaluation,
        }
    digest_value = deepcopy(result)
    digest_value["evaluation_digest"] = canonical_digest(result)
    return digest_value


def evaluate_resolved_constraint(constraint: object, target: str) -> dict:
    """Re-evaluate one bound language-policy constraint against a new target."""

    if not isinstance(constraint, Mapping):
        raise LanguagePolicyError("resolved constraint must be an object")
    if not isinstance(target, str):
        raise LanguagePolicyError("candidate target must be a string")
    resolution_digest = constraint.get("resolution_digest")
    if not isinstance(resolution_digest, str) or len(resolution_digest) != 64:
        raise LanguagePolicyError("resolved constraint has no valid resolution_digest")
    digest_basis = deepcopy(dict(constraint))
    digest_basis.pop("resolution_digest", None)
    digest_basis.pop("runtime_evaluation", None)
    if canonical_digest(digest_basis) != resolution_digest:
        raise LanguagePolicyError("resolved constraint digest is invalid")

    provider = constraint.get("provider")
    if not isinstance(provider, Mapping):
        raise LanguagePolicyError("resolved constraint has no provider binding")
    required_provider_fields = {
        "id",
        "api_version",
        "target_lang",
        "module_sha256",
    }
    if set(provider) != required_provider_fields:
        raise LanguagePolicyError(
            "resolved constraint provider binding has invalid fields"
        )
    provider_ref = {
        "id": provider["id"],
        "api_version": provider["api_version"],
    }
    key = _provider_key(provider_ref)
    trusted = _public_descriptor(key)
    if dict(provider) != {
        "id": trusted["id"],
        "api_version": trusted["api_version"],
        "target_lang": trusted["target_lang"],
        "module_sha256": trusted["module_sha256"],
    }:
        raise LanguagePolicyError(
            "resolved constraint provider binding is stale or untrusted"
        )
    if constraint.get("kind") != trusted["capability"]:
        raise LanguagePolicyError(
            "resolved constraint capability does not match its provider"
        )

    status = constraint.get("status")
    if status not in {"resolved", "conflict", "insufficient_context"}:
        raise LanguagePolicyError("resolved constraint status is invalid")
    if status != "resolved":
        result = {
            "status": "conflict" if status == "conflict" else "inconclusive",
            "reason_codes": [
                "constraint_conflict"
                if status == "conflict"
                else "constraint_not_resolved"
            ],
            "constraint_resolution_digest": resolution_digest,
            "observation": None,
            "evaluation": None,
        }
    else:
        expected = constraint.get("expected")
        if not isinstance(expected, Mapping) or not expected:
            raise LanguagePolicyError(
                "resolved constraint expected policy must be a non-empty object"
            )
        target_lang = trusted["target_lang"]
        observation = observe(provider_ref, target_lang, target)
        evaluation = evaluate(provider_ref, target_lang, expected, observation)
        result = {
            "status": evaluation["status"],
            "reason_codes": deepcopy(evaluation.get("reason_codes", [])),
            "constraint_resolution_digest": resolution_digest,
            "observation": observation,
            "evaluation": evaluation,
        }
    result["evaluation_digest"] = canonical_digest(result)
    return result
