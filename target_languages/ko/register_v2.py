"""Conservative Korean register and narration-style policy provider.

The observer recognizes only a small set of high-confidence terminal forms.
Person and tense observations also abstain on omitted subjects, multiple
clauses, and ambiguous morphology.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Mapping, Sequence


PROVIDER_ID = "ko.register"
API_VERSION = 2
TARGET_LANG = "ko"
CAPABILITY = "language.register"

POLITENESS_VALUES = frozenset({"plain", "polite", "formal_polite"})
ENDING_FAMILIES = frozenset({"hae", "haera", "haeyo", "hapsyo"})
PERSON_VALUES = frozenset({"first", "second", "third"})
TENSE_VALUES = frozenset({"past", "present"})
FAMILY_POLITENESS = {
    "hae": "plain",
    "haera": "plain",
    "haeyo": "polite",
    "hapsyo": "formal_polite",
}
_POLICY_KEYS = frozenset(
    {"politeness", "ending_families", "forbidden_families", "person", "tense"}
)

_ENDING_SUFFIXES = (
    (
        "hapsyo",
        (
            "하십시오",
            "오십시오",
            "십시오",
            "하십니까",
            "습니까",
            "합니다",
            "입니다",
            "입니까",
            "습니다",
            "옵니다",
            "니까",
            "니다",
            "시오",
        ),
    ),
    (
        "haeyo",
        (
            "하세요",
            "오세요",
            "가세요",
            "주세요",
            "해요",
            "와요",
            "가요",
            "아요",
            "어요",
            "여요",
            "예요",
            "이에요",
            "네요",
            "군요",
            "죠",
            "지요",
            "나요",
            "까요",
            "래요",
            "대요",
            "든요",
        ),
    ),
    (
        "haera",
        (
            "해라",
            "하라",
            "와라",
            "가라",
            "어라",
            "아라",
            "한다",
            "된다",
            "간다",
            "온다",
            "준다",
            "있다",
            "없다",
            "했다",
            "됐다",
            "이다",
            "아니다",
            "겠다",
            "자",
        ),
    ),
    (
        "hae",
        (
            "따라와",
            "돌아가",
            "해",
            "와",
            "가",
            "봐",
            "줘",
            "돼",
            "어",
            "아",
            "지",
            "네",
            "야",
            "래",
            "대",
        ),
    ),
)
_MARKUP_RE = re.compile(
    r"<[^>]*>|\{[^{}]*\}|\\[nrt]|%(?:\d+\$)?[sdif]|\$\{[^{}]*\}|\[[^\[\]]+\]"
)
_QUOTE_CHARS = frozenset('"\'“”‘’「」『』«»')
_OUTER_QUOTE_PAIRS = {
    ('"', '"'),
    ("'", "'"),
    ("“", "”"),
    ("‘", "’"),
    ("「", "」"),
    ("『", "』"),
    ("«", "»"),
}
_CLAUSE_SPLIT_RE = re.compile(r"[.!?。！？…;,，；\n]+")
_PERSON_PATTERNS = {
    "first": re.compile(
        r"(?<![가-힣])(?:나는|내가|저는|제가|우리는|우리가|저희는|저희가)(?![가-힣])"
    ),
    "second": re.compile(
        r"(?<![가-힣])(?:너는|네가|당신은|당신이|그대는|그대가)(?![가-힣])"
    ),
    "third": re.compile(
        r"(?<![가-힣])(?:그는|그가|그녀는|그녀가|그들은|그들이)(?![가-힣])"
    ),
}
_TENSE_SUFFIXES = (
    "하십시오",
    "오십시오",
    "십시오",
    "하십니까",
    "습니까",
    "합니다",
    "입니다",
    "입니까",
    "습니다",
    "옵니다",
    "니까",
    "니다",
    "하세요",
    "오세요",
    "가세요",
    "주세요",
    "해요",
    "와요",
    "가요",
    "아요",
    "어요",
    "여요",
    "예요",
    "이에요",
    "네요",
    "군요",
    "죠",
    "지요",
    "나요",
    "까요",
    "래요",
    "대요",
    "든요",
    "해라",
    "하라",
    "와라",
    "가라",
    "어라",
    "아라",
    "한다",
    "된다",
    "간다",
    "온다",
    "준다",
    "있다",
    "없다",
    "했다",
    "됐다",
    "이다",
    "아니다",
    "겠다",
    "자",
    "따라와",
    "돌아가",
    "해",
    "와",
    "가",
    "봐",
    "줘",
    "돼",
    "어",
    "아",
    "지",
    "네",
    "야",
    "래",
    "대",
)
_PRESENT_EXACT_ENDINGS = frozenset(
    {
        "한다",
        "된다",
        "간다",
        "온다",
        "준다",
        "있다",
        "없다",
        "이다",
        "아니다",
    }
)


class KoreanRegisterPolicyError(ValueError):
    """Raised when a Korean register expectation is invalid."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise KoreanRegisterPolicyError(f"policy is not canonical JSON: {exc}") from exc


def _values(value: object, field: str, allowed: frozenset[str]) -> list[str]:
    raw = value if isinstance(value, list) else [value]
    if not raw or raw == [None]:
        raise KoreanRegisterPolicyError(f"{field} must be a non-empty string array")
    output: list[str] = []
    for item in raw:
        if not isinstance(item, str) or item not in allowed:
            raise KoreanRegisterPolicyError(
                f"{field} values must be one of {sorted(allowed)}"
            )
        if item in output:
            raise KoreanRegisterPolicyError(f"{field} must not contain duplicates")
        output.append(item)
    return output


def validate_policy(policy: object) -> dict:
    """Validate one provider-defined ``expect`` object."""

    if not isinstance(policy, Mapping) or not policy:
        raise KoreanRegisterPolicyError("register policy must be a non-empty object")
    unknown = sorted(set(policy) - _POLICY_KEYS)
    if unknown:
        raise KoreanRegisterPolicyError(
            f"register policy has unknown fields: {unknown}"
        )
    normalized = {}
    if "politeness" in policy:
        normalized["politeness"] = _values(
            policy["politeness"], "politeness", POLITENESS_VALUES
        )
    if "ending_families" in policy:
        normalized["ending_families"] = _values(
            policy["ending_families"], "ending_families", ENDING_FAMILIES
        )
    if "forbidden_families" in policy:
        normalized["forbidden_families"] = _values(
            policy["forbidden_families"], "forbidden_families", ENDING_FAMILIES
        )
    if "person" in policy:
        normalized["person"] = _values(
            policy["person"], "person", PERSON_VALUES
        )
    if "tense" in policy:
        normalized["tense"] = _values(policy["tense"], "tense", TENSE_VALUES)
    allowed_families = set(normalized.get("ending_families", []))
    forbidden_families = set(normalized.get("forbidden_families", []))
    overlap = sorted(allowed_families & forbidden_families)
    if overlap:
        raise KoreanRegisterPolicyError(
            f"ending_families and forbidden_families overlap: {overlap}"
        )
    allowed_politeness = set(normalized.get("politeness", []))
    incompatible = sorted(
        family
        for family in allowed_families
        if allowed_politeness and FAMILY_POLITENESS[family] not in allowed_politeness
    )
    if incompatible:
        raise KoreanRegisterPolicyError(
            f"ending_families conflict with politeness: {incompatible}"
        )
    _canonical(normalized)
    return normalized


def resolve(context: Mapping, rules: Sequence[Mapping]) -> dict:
    """Normalize expectations from a rule tier selected by the core resolver."""

    if not isinstance(context, Mapping):
        raise KoreanRegisterPolicyError("context must be an object")
    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        raise KoreanRegisterPolicyError("rules must be an array")
    if not rules:
        return {
            "status": "insufficient_context",
            "expected": None,
            "reason_codes": ["no_selected_rule"],
        }
    expectations: dict[bytes, dict] = {}
    for index, rule in enumerate(rules):
        if not isinstance(rule, Mapping) or "expect" not in rule:
            raise KoreanRegisterPolicyError(f"rules[{index}] has no expect object")
        normalized = validate_policy(rule["expect"])
        expectations[_canonical(normalized)] = normalized
    if len(expectations) > 1:
        return {
            "status": "conflict",
            "expected": None,
            "reason_codes": ["selected_rules_conflict"],
        }
    return {
        "status": "resolved",
        "expected": deepcopy(next(iter(expectations.values()))),
        "reason_codes": [],
    }


def _inconclusive(*reasons: str) -> dict:
    return {
        "status": "inconclusive",
        "family": None,
        "politeness": None,
        "confidence": "low",
        "reason_codes": list(dict.fromkeys(reasons)),
    }


def _strip_outer_quote(text: str) -> tuple[str | None, str | None]:
    quote_positions = [index for index, char in enumerate(text) if char in _QUOTE_CHARS]
    if not quote_positions:
        return text, None
    if len(quote_positions) != 2:
        return None, "nested_or_multiple_quotes"
    left_index, right_index = quote_positions
    if left_index != 0 or right_index != len(text) - 1:
        return None, "embedded_quote"
    if (text[left_index], text[right_index]) not in _OUTER_QUOTE_PAIRS:
        return None, "unbalanced_quotes"
    return text[1:-1].strip(), None


def _observe_clause(clause: str) -> dict | None:
    normalized = clause.strip()
    if not normalized or not re.search(r"[가-힣]", normalized):
        return None
    for family, suffixes in _ENDING_SUFFIXES:
        for suffix in suffixes:
            if len(suffix) == 1:
                continue
            if normalized.endswith(suffix):
                return {
                    "family": family,
                    "politeness": FAMILY_POLITENESS[family],
                    "ending": suffix,
                    "clause": normalized,
                }
    return None


def _observe_person(text: str) -> str | None:
    found = {
        person
        for person, pattern in _PERSON_PATTERNS.items()
        if pattern.search(text)
    }
    return next(iter(found)) if len(found) == 1 else None


def _jongseong_index(value: str) -> int | None:
    if len(value) != 1 or not "가" <= value <= "힣":
        return None
    return (ord(value) - 0xAC00) % 28


def _observe_clause_tense(clause: str) -> str | None:
    suffix = next(
        (value for value in _TENSE_SUFFIXES if clause.endswith(value)),
        None,
    )
    if suffix is None:
        return None
    stem = clause[: -len(suffix)] if suffix else clause
    if suffix in {"했다", "됐다"}:
        return "past"
    if suffix in _PRESENT_EXACT_ENDINGS:
        return "present"
    if stem:
        last = stem[-1]
        if last == "겠":
            return None
        if _jongseong_index(last) == 20 and last not in {"있", "없"}:
            return "past"
    if " 거예요" in clause or " 예정" in clause or clause.endswith("겠다"):
        return None
    if suffix in {
        "습니다",
        "습니까",
        "합니다",
        "하십니까",
        "입니다",
        "입니까",
        "옵니다",
        "니다",
        "해요",
        "와요",
        "가요",
        "아요",
        "어요",
        "여요",
        "예요",
        "이에요",
    }:
        return "present"
    return None


def _observe_tense(clauses: Sequence[str]) -> str | None:
    values = {_observe_clause_tense(clause) for clause in clauses}
    if None in values or len(values) != 1:
        return None
    return next(iter(values))


def observe(target: str) -> dict:
    """Observe a narrow set of Korean endings, abstaining on ambiguity."""

    if not isinstance(target, str) or not target.strip():
        return _inconclusive("empty_target")
    text = target.strip()
    if _MARKUP_RE.search(text):
        return _inconclusive("markup_interference")
    text, quote_error = _strip_outer_quote(text)
    if quote_error is not None:
        return _inconclusive(quote_error)
    if not text:
        return _inconclusive("empty_target")
    clauses = [part.strip() for part in _CLAUSE_SPLIT_RE.split(text) if part.strip()]
    if not clauses or len(clauses) > 4:
        return _inconclusive("complex_utterance")
    observations = [_observe_clause(clause) for clause in clauses]
    if any(item is None for item in observations):
        return _inconclusive("unrecognized_or_mixed_syntax")
    recognized = [item for item in observations if item is not None]
    families = {item["family"] for item in recognized}
    if len(families) != 1:
        return _inconclusive("mixed_ending_families")
    family = recognized[-1]["family"]
    person = _observe_person(text) if len(clauses) == 1 else None
    tense = _observe_tense(clauses) if len(clauses) == 1 else None
    result = {
        "status": "observed",
        "family": family,
        "politeness": FAMILY_POLITENESS[family],
        "person": person,
        "tense": tense,
        "confidence": "high",
        "ending": recognized[-1]["ending"],
        "reason_codes": [],
        "evidence": {"clauses": recognized},
    }
    result["observation_digest"] = hashlib.sha256(_canonical(result)).hexdigest()
    return result


def evaluate(expected: Mapping, observed: Mapping) -> dict:
    """Compare a validated expectation with a conservative observation."""

    normalized = validate_policy(expected)
    if not isinstance(observed, Mapping):
        raise KoreanRegisterPolicyError("observed must be an object")
    if observed.get("status") != "observed" or observed.get("confidence") != "high":
        return {
            "status": "inconclusive",
            "reason_codes": list(observed.get("reason_codes", ["observation_inconclusive"])),
            "expected": normalized,
            "observed": deepcopy(dict(observed)),
        }
    family = observed.get("family")
    politeness = observed.get("politeness")
    if family not in ENDING_FAMILIES or FAMILY_POLITENESS[family] != politeness:
        return {
            "status": "inconclusive",
            "reason_codes": ["invalid_observation"],
            "expected": normalized,
            "observed": deepcopy(dict(observed)),
        }
    reasons = []
    if family in normalized.get("forbidden_families", []):
        reasons.append("forbidden_ending_family")
    if normalized.get("ending_families") and family not in normalized["ending_families"]:
        reasons.append("ending_family_mismatch")
    if normalized.get("politeness") and politeness not in normalized["politeness"]:
        reasons.append("politeness_mismatch")
    person = observed.get("person")
    tense = observed.get("tense")
    if person is not None and person not in PERSON_VALUES:
        return {
            "status": "inconclusive",
            "reason_codes": ["invalid_person_observation"],
            "expected": normalized,
            "observed": deepcopy(dict(observed)),
        }
    if tense is not None and tense not in TENSE_VALUES:
        return {
            "status": "inconclusive",
            "reason_codes": ["invalid_tense_observation"],
            "expected": normalized,
            "observed": deepcopy(dict(observed)),
        }
    if normalized.get("person") and person is not None and person not in normalized["person"]:
        reasons.append("person_mismatch")
    if normalized.get("tense") and tense is not None and tense not in normalized["tense"]:
        reasons.append("tense_mismatch")
    inconclusive = []
    if normalized.get("person") and person is None:
        inconclusive.append("person_not_observed")
    if normalized.get("tense") and tense is None:
        inconclusive.append("tense_not_observed")
    if not reasons and inconclusive:
        return {
            "status": "inconclusive",
            "reason_codes": inconclusive,
            "expected": normalized,
            "observed": deepcopy(dict(observed)),
        }
    return {
        "status": "mismatch" if reasons else "match",
        "reason_codes": reasons,
        "expected": normalized,
        "observed": deepcopy(dict(observed)),
    }
