from collections import defaultdict
from copy import deepcopy
import hashlib
import json
import math

from lqe_engine import (
    apply_severity,
    current_target,
    load_scorecard_profile,
    normalize_category_for_profile,
    scorecard_category_weight,
    scorecard_severity_points,
)


DEFAULT_SCORING_POLICY = {
    "threshold": 98.0,
    "scorecard_profile": "legacy",
    "severity_scale": "lisa",
    "critical_gate": False,
    "repeat_dedup": True,
}


def _review_wordcount_digest(state: dict, blocked: set[int]) -> str:
    payload = json.dumps(
        {
            "wordcount": state.get("wordcount", 0),
            "review_wordcount": state.get("review_wordcount"),
            "blocked_ids": sorted(blocked),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
_POLICY_KEYS = set(DEFAULT_SCORING_POLICY)


def scoring_policy_overrides(args: object) -> dict:
    return {
        key: getattr(args, key, None)
        for key in DEFAULT_SCORING_POLICY
    }


def resolve_scoring_policy(
    state: dict | None,
    overrides: dict | None = None,
) -> dict:
    if state is None:
        state = {}
    if not isinstance(state, dict):
        raise ValueError("state must be an object")
    raw = state.get("scoring_policy")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("state.scoring_policy must be an object")
    unknown = set(raw) - _POLICY_KEYS
    if unknown:
        raise ValueError(f"state.scoring_policy has unknown keys: {sorted(unknown)}")
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        raise ValueError("scoring policy overrides must be an object")
    unknown_overrides = set(overrides) - _POLICY_KEYS
    if unknown_overrides:
        raise ValueError(f"unknown scoring policy overrides: {sorted(unknown_overrides)}")

    def choose(key, fallback):
        value = overrides.get(key)
        if value is not None:
            return value
        if key in raw:
            return raw[key]
        return fallback

    profile_id = choose("scorecard_profile", DEFAULT_SCORING_POLICY["scorecard_profile"])
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("scoring_policy.scorecard_profile must be a non-empty string")
    profile_id = profile_id.strip()
    profile = load_scorecard_profile(profile_id)

    legacy_threshold = state.get("threshold")
    if legacy_threshold is None:
        legacy_threshold = profile.get("threshold", DEFAULT_SCORING_POLICY["threshold"])
    threshold = choose("threshold", legacy_threshold)
    if type(threshold) not in (int, float):
        raise ValueError("scoring_policy.threshold must be numeric")
    threshold = float(threshold)
    if not math.isfinite(threshold) or not 0 <= threshold <= 100:
        raise ValueError("scoring_policy.threshold must be between 0 and 100")

    severity_scale = choose(
        "severity_scale", DEFAULT_SCORING_POLICY["severity_scale"]
    )
    if severity_scale not in {"lisa", "mqm"}:
        raise ValueError("scoring_policy.severity_scale must be 'lisa' or 'mqm'")

    critical_gate = choose(
        "critical_gate", DEFAULT_SCORING_POLICY["critical_gate"]
    )
    repeat_dedup = choose(
        "repeat_dedup", DEFAULT_SCORING_POLICY["repeat_dedup"]
    )
    if type(critical_gate) is not bool:
        raise ValueError("scoring_policy.critical_gate must be boolean")
    if type(repeat_dedup) is not bool:
        raise ValueError("scoring_policy.repeat_dedup must be boolean")

    return {
        "threshold": threshold,
        "scorecard_profile": profile_id,
        "severity_scale": severity_scale,
        "critical_gate": critical_gate,
        "repeat_dedup": repeat_dedup,
    }


def score_totals(
    total_weighted: float,
    wordcount: float,
    critical_count: int,
    policy: dict,
) -> dict:
    if type(total_weighted) not in (int, float) or not math.isfinite(
        total_weighted
    ) or total_weighted < 0:
        raise ValueError("total weighted penalty must be a finite non-negative number")
    if type(wordcount) not in (int, float) or not math.isfinite(
        wordcount
    ) or wordcount < 0:
        raise ValueError("state.wordcount must be a finite non-negative number")
    if type(critical_count) is not int or critical_count < 0:
        raise ValueError("critical count must be a non-negative integer")

    gate_fail = policy["critical_gate"] and critical_count > 0
    if wordcount == 0:
        score = 0.0 if total_weighted > 0 else 100.0
        npt = 0.0
        status = "FAIL" if gate_fail or total_weighted > 0 else "PASS"
    else:
        score = max((1 - total_weighted / wordcount) * 100, 0)
        npt = total_weighted * 1000 / wordcount
        status = (
            "FAIL" if gate_fail or score < policy["threshold"] else "PASS"
        )
    return {
        "score": round(score, 2),
        "status": status,
        "wordcount": wordcount,
        "critical": critical_count,
        "npt": round(npt, 2),
        "critical_gate": gate_fail,
    }


def score_errors(
    state: dict,
    errors: list,
    policy: dict,
    *,
    protected_ids: set[int] | None = None,
) -> dict:
    annotated = deepcopy(errors)
    for entry in annotated:
        for error in entry.get("errors", []):
            error.pop("repeated", None)

    scorecard_profile = load_scorecard_profile(policy["scorecard_profile"])
    severity_points = scorecard_severity_points(
        scorecard_profile, policy["severity_scale"]
    )
    protected = set(protected_ids or ())
    protected.update(
        segment["id"]
        for segment in state.get("segments", [])
        if segment.get("protected")
    )
    blocked = {
        segment["id"]
        for segment in state.get("segments", [])
        if segment.get("input_status") == "blocked"
    }
    protected.update(blocked)
    segment_map = {
        segment["id"]: (
            str(segment.get("source", "")).strip(),
            current_target(segment).strip(),
        )
        for segment in state.get("segments", [])
    }
    category_raw = defaultdict(float)
    category_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    repeated_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    distribution = defaultdict(int)
    seen: dict[tuple, int] = {}
    total_errors = 0
    critical_count = 0
    repeated_count = 0

    for entry in annotated:
        segment_id = entry.get("id")
        if segment_id in protected or any(
            error.get("protected") is True
            for error in entry.get("errors", [])
            if isinstance(error, dict)
        ):
            continue
        for error in entry.get("errors", []):
            raw_category = error.get("category", "Other")
            category = normalize_category_for_profile(
                raw_category, scorecard_profile
            )
            severity = apply_severity(
                category, str(error.get("severity", "Minor")), scorecard_profile
            )
            if policy["repeat_dedup"] and segment_id in segment_map:
                key = segment_map[segment_id] + (category, severity)
                if seen.setdefault(key, segment_id) != segment_id:
                    error["repeated"] = True
                    repeated_count += 1
                    repeated_counts[category][severity] += 1
                    continue
            category_raw[category] += severity_points.get(severity, 0)
            category_counts[category][severity] += 1
            distribution[f"{raw_category} [{severity}]"] += 1
            total_errors += 1
            if severity == "Critical":
                critical_count += 1

    review_wordcount = state.get("review_wordcount")
    review_digest = state.get("review_wordcount_basis_digest")
    if (
        review_wordcount is not None
        and isinstance(review_digest, str)
        and review_digest == _review_wordcount_digest(state, blocked)
    ):
        wordcount = review_wordcount
    elif blocked:
        wordcount = review_wordcount if review_wordcount is not None else 0
    else:
        wordcount = state.get("wordcount", 0)
    total_weighted = sum(
        scorecard_category_weight(category, scorecard_profile) * raw
        for category, raw in category_raw.items()
    )
    if blocked and wordcount == 0:
        totals = {
            "score": None,
            "status": "REVIEW_NOT_RUN",
            "review_status": "INPUT_BLOCKED",
            "wordcount": 0,
            "critical": 0,
            "npt": None,
            "critical_gate": False,
        }
    else:
        totals = score_totals(total_weighted, wordcount, critical_count, policy)
    output = {
        **totals,
        "errors": total_errors,
        "repeated": repeated_count,
    }
    if (
        "review_wordcount" in state
        or blocked
        or state.get("job_runtime_contract_version") == 2
    ):
        output.update(
            {
                "blocked_segments": len(blocked),
                "total_wordcount": state.get("wordcount", wordcount),
            }
        )
    return {
        "output": output,
        "annotated_errors": annotated,
        "annotations_changed": annotated != errors,
        "distribution": dict(distribution),
        "total_weighted": total_weighted,
        "category_counts": {
            category: dict(counts)
            for category, counts in category_counts.items()
        },
        "repeated_counts": {
            category: dict(counts)
            for category, counts in repeated_counts.items()
        },
    }
