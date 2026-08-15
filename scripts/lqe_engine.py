"""Shared constants & language-attribute layer for lqe_io.py / lqe_checks.py / lqe_calc.py."""

# 父维度对齐 MQM-Core / ISO 5060:2024 七个一级维度：
#   Terminology, Accuracy, Linguistic Conventions, Style,
#   Locale Conventions, Audience Appropriateness, Design and Markup (+ Other)
CATEGORY_PARENT = {
    "Terminology": "Terminology", "Locale convention": "Locale Conventions", "Other": "Other",
    "Addition": "Accuracy", "Omission": "Accuracy", "Mistranslation": "Accuracy",
    "Untranslated": "Accuracy", "Punctuation": "Linguistic Conventions", "Spelling": "Linguistic Conventions",
    "Grammar": "Linguistic Conventions", "Inconsistency": "Style", "Company style": "Style",
    "Unidiomatic": "Style", "Length": "Design and Markup", "Markup": "Design and Markup",
    "Culture specific reference": "Audience Appropriateness",
    "Audience appropriateness": "Audience Appropriateness",
}
CATEGORY_ORDER = tuple(CATEGORY_PARENT)
WEIGHTS = {
    "Terminology": 1.5, "Locale convention": 1.0, "Other": 1.0,
    "Addition": 1.5, "Omission": 1.5, "Mistranslation": 1.5, "Untranslated": 1.5,
    "Punctuation": 1.0, "Spelling": 1.0, "Grammar": 1.5,
    "Inconsistency": 1.5, "Company style": 1.5, "Unidiomatic": 1.5,
    "Length": 1.0, "Markup": 1.5, "Culture specific reference": 1.5,
    "Audience appropriateness": 1.5,
}
# 默认 LISA 档严重度乘数；MQM 指数档 (Critical=25) 见 SEVERITY_POINTS_MQM（opt-in）
SEVERITY_POINTS = {"Neutral": 0, "Minor": 1, "Major": 5, "Critical": 10}
SEVERITY_POINTS_MQM = {"Neutral": 0, "Minor": 1, "Major": 5, "Critical": 25}
FORCED_SEVERITY = {
    "Terminology": "Major", "Untranslated": "Major",
    "Markup":       "Major", "Length":       "Major",
}
VALID_CATEGORIES = set(CATEGORY_ORDER)
VALID_SEVERITIES = {"Neutral", "Minor", "Major", "Critical"}

# AI 若误填"父维度名"而非子类别，归一回退到该维度的代表子类。
# 同时保留旧父类名（Fluency/Design/Verity）与新 MQM 维度名，保证向后兼容。
_PARENT_REMAP = {
    "Accuracy": "Mistranslation",
    "Fluency": "Grammar", "Linguistic Conventions": "Grammar",
    "Style": "Unidiomatic",
    "Verity": "Culture specific reference",
    "Audience Appropriateness": "Culture specific reference",
    "Design": "Markup", "Design and Markup": "Markup",
    "Locale Conventions": "Locale convention",
}


def normalize_category(cat: str) -> str:
    return _PARENT_REMAP.get(cat, cat)


def current_target(segment: dict) -> str:
    current = segment.get("current_target")
    if isinstance(current, str):
        return current
    corrected = segment.get("corrected")
    if isinstance(corrected, str):
        return corrected
    target = segment.get("target", "")
    return target if isinstance(target, str) else str(target or "")


import json
from pathlib import Path


def _legacy_scorecard_profile() -> dict:
    """In-code fallback for the original scoring standard.

    The external `scorecard_profiles/legacy/profile.json` is the source of
    truth going forward; this fallback keeps older checkouts usable if the
    profile directory is missing.
    """
    return {
        "id": "legacy",
        "name": "Current LQE Scoring",
        "threshold": 98,
        "category_parent": dict(CATEGORY_PARENT),
        "category_order": list(CATEGORY_ORDER),
        "category_weights": dict(WEIGHTS),
        "severity_points": dict(SEVERITY_POINTS),
        "severity_points_mqm": dict(SEVERITY_POINTS_MQM),
        "forced_severity": dict(FORCED_SEVERITY),
        "category_aliases": {},
    }


def _scorecard_profile_path(profile_id: str) -> Path:
    raw = Path(profile_id)
    if raw.is_dir():
        return raw / "profile.json"
    if raw.suffix == ".json" or raw.is_absolute():
        return raw
    return _SKILL_ROOT / "scorecard_profiles" / profile_id / "profile.json"


def _normalize_scorecard_profile(profile: dict, profile_id: str = "legacy") -> dict:
    out = _legacy_scorecard_profile()
    out.update(profile or {})
    out.setdefault("id", profile_id)
    out["category_parent"] = dict(out.get("category_parent") or CATEGORY_PARENT)
    out["category_order"] = list(out.get("category_order") or out["category_parent"].keys())
    out["category_weights"] = dict(out.get("category_weights") or WEIGHTS)
    out["severity_points"] = dict(out.get("severity_points") or SEVERITY_POINTS)
    out["severity_points_mqm"] = dict(out.get("severity_points_mqm") or SEVERITY_POINTS_MQM)
    out["forced_severity"] = dict(out.get("forced_severity") or FORCED_SEVERITY)
    out["category_aliases"] = dict(out.get("category_aliases") or {})
    return out


def load_scorecard_profile(profile_id: str = "legacy") -> dict:
    """Load an LQE scorecard profile by id, directory, or JSON file path."""
    profile_id = profile_id or "legacy"
    path = _scorecard_profile_path(profile_id)
    if path.exists():
        profile = read_json(path)
    elif profile_id == "legacy":
        profile = _legacy_scorecard_profile()
    else:
        raise FileNotFoundError(f"scorecard profile not found: {profile_id} ({path})")
    return _normalize_scorecard_profile(profile, profile_id)


def normalize_category_for_profile(cat: str, scorecard_profile: dict | None = None) -> str:
    normalized = normalize_category(cat)
    aliases = (scorecard_profile or {}).get("category_aliases", {})
    return aliases.get(normalized, normalized)


def scorecard_category_order(scorecard_profile: dict | None = None) -> tuple[str, ...]:
    return tuple((scorecard_profile or {}).get("category_order") or CATEGORY_ORDER)


def scorecard_category_parent(cat: str, scorecard_profile: dict | None = None) -> str:
    parents = (scorecard_profile or {}).get("category_parent") or CATEGORY_PARENT
    return parents.get(cat, "Other")


def scorecard_category_weight(cat: str, scorecard_profile: dict | None = None) -> float:
    weights = (scorecard_profile or {}).get("category_weights") or WEIGHTS
    return float(weights.get(cat, 1.0))


def scorecard_severity_points(scorecard_profile: dict | None = None, severity_scale: str = "lisa") -> dict:
    if severity_scale == "mqm":
        return dict((scorecard_profile or {}).get("severity_points_mqm") or SEVERITY_POINTS_MQM)
    return dict((scorecard_profile or {}).get("severity_points") or SEVERITY_POINTS)


def apply_severity(cat: str, sev: str, scorecard_profile: dict | None = None) -> str:
    forced = (scorecard_profile or {}).get("forced_severity") or FORCED_SEVERITY
    return forced.get(cat, sev.strip().capitalize() if sev else "Minor")


def raw_points(counts: dict, scorecard_profile: dict | None = None, severity_points: dict | None = None) -> int:
    points = severity_points or scorecard_severity_points(scorecard_profile)
    return sum(points.get(sev, 0) * n for sev, n in counts.items())


def weighted_points(cat: str, counts: dict, scorecard_profile: dict | None = None,
                    severity_points: dict | None = None) -> float:
    return scorecard_category_weight(cat, scorecard_profile) * raw_points(counts, scorecard_profile, severity_points)


STANDARD_REQUIRED_MODULES = ("terminology", "accuracy", "grammar", "naturalness")
NO_TERMINOLOGY_REQUIRED_MODULES = ("precheck_review", "accuracy", "grammar", "naturalness")
OPTIONAL_MODULES = ("proper_names",)
TERMINOLOGY_MODULES = ("terminology", "proper_names", "term_audit")
REVIEW_MODES = ("optimized", "full")


def build_review_policy(mode: str, source: str = "runtime") -> dict:
    if mode not in REVIEW_MODES:
        raise ValueError(f"unsupported review mode: {mode!r}")
    optimized = mode == "optimized"
    return {
        "mode": mode,
        "minor_edits_allowed": not optimized,
        "comment_soft_target": (
            {"min_chars": 20, "max_chars": 30} if optimized else None
        ),
        "suggestion_candidate_severities": (
            ["Critical", "Major"]
            if optimized
            else ["Neutral", "Minor", "Major", "Critical"]
        ),
        "text_type_routing_enabled": optimized,
        "source": source,
    }


def get_review_policy(state: dict) -> dict:
    if not isinstance(state, dict):
        raise ValueError("state must be an object")
    raw = state.get("review_policy")
    if raw is None:
        return build_review_policy("optimized", "legacy-default")
    if not isinstance(raw, dict):
        raise ValueError("review_policy must be an object")
    mode = raw.get("mode")
    if mode not in REVIEW_MODES:
        raise ValueError(f"unsupported review_policy mode: {mode!r}")
    resolved = build_review_policy(mode, raw.get("source") or "state")
    for key in (
        "minor_edits_allowed",
        "comment_soft_target",
        "suggestion_candidate_severities",
        "text_type_routing_enabled",
    ):
        if key in raw and raw[key] != resolved[key]:
            raise ValueError(f"review_policy {key} conflicts with mode {mode}")
    return resolved


def build_check_scope(no_terminology: bool, source: str = "runtime") -> dict:
    if no_terminology:
        return {
            "mode": "no-terminology",
            "terminology_enabled": False,
            "enabled_modules": list(NO_TERMINOLOGY_REQUIRED_MODULES),
            "disabled_modules": list(TERMINOLOGY_MODULES),
            "source": source,
        }
    return {
        "mode": "standard",
        "terminology_enabled": True,
        "enabled_modules": list(STANDARD_REQUIRED_MODULES),
        "disabled_modules": [],
        "source": source,
    }


def get_check_scope(state: dict) -> dict:
    if not isinstance(state, dict):
        raise ValueError("state must be an object")
    raw = state.get("check_scope")
    if raw is None:
        return build_check_scope(False, "legacy-default")
    if not isinstance(raw, dict):
        raise ValueError("check_scope must be an object")
    mode = raw.get("mode")
    if mode not in {"standard", "no-terminology"}:
        raise ValueError(f"unsupported check_scope mode: {mode!r}")
    resolved = build_check_scope(mode == "no-terminology", raw.get("source") or "state")
    for key in ("terminology_enabled", "enabled_modules", "disabled_modules"):
        if key in raw and raw[key] != resolved[key]:
            raise ValueError(f"check_scope {key} conflicts with mode {mode}")
    return resolved


def required_modules(state: dict) -> tuple[str, ...]:
    return tuple(get_check_scope(state)["enabled_modules"])


def optional_modules(state: dict) -> tuple[str, ...]:
    return OPTIONAL_MODULES if get_check_scope(state)["mode"] == "standard" else ()


def disabled_modules(state: dict) -> tuple[str, ...]:
    return tuple(get_check_scope(state)["disabled_modules"])


def terminology_enabled(state: dict) -> bool:
    return bool(get_check_scope(state)["terminology_enabled"])


def issue_contract_problem(
    issue: dict,
    review_policy: dict | None = None,
) -> str | None:
    if not isinstance(issue, dict):
        return "issue must be an object"
    category = issue.get("category")
    if not isinstance(category, str):
        return "category must be a string"
    if category not in VALID_CATEGORIES:
        return f"unsupported category {category!r}"
    severity = issue.get("severity")
    if not isinstance(severity, str):
        return "severity must be a string"
    if severity not in VALID_SEVERITIES:
        return f"unsupported severity {severity!r}"
    comment = issue.get("comment")
    if not isinstance(comment, str):
        return "comment must be a string"
    if not comment.strip():
        return "comment must be non-empty"
    policy = get_review_policy(
        {"review_policy": review_policy} if review_policy is not None else {}
    )
    if severity == "Minor" and not policy["minor_edits_allowed"]:
        if issue.get("needs_confirmation") is not True:
            return "Minor issue must set needs_confirmation to true"
        if issue.get("edit") is not None:
            return "Minor issue must set edit to null"
    for field in ("needs_confirmation", "protected", "repeated"):
        if field in issue and type(issue[field]) is not bool:
            return f"{field} must be boolean"
    resolution_status = issue.get("resolution_status")
    if resolution_status is not None:
        allowed = {
            "resolved",
            "reference_allowed",
            "human_choice_required",
            "conflict",
        }
        if resolution_status not in allowed:
            return f"unsupported resolution_status {resolution_status!r}"
        needs_confirmation = issue.get("needs_confirmation") is True
        edit = issue.get("edit")
        if resolution_status == "resolved" and needs_confirmation:
            return "resolved issue must not need confirmation"
        if resolution_status in {
            "reference_allowed",
            "human_choice_required",
            "conflict",
        }:
            if not needs_confirmation:
                return f"{resolution_status} issue must need confirmation"
            if edit is not None:
                return f"{resolution_status} issue must set edit to null"
    reason_codes = issue.get("reason_codes")
    if reason_codes is not None and (
        not isinstance(reason_codes, list)
        or not reason_codes
        or any(
            not isinstance(code, str) or not code.strip()
            for code in reason_codes
        )
        or len(reason_codes) != len(set(reason_codes))
    ):
        return "reason_codes must be a unique array of non-empty strings"
    non_authorizing = issue.get("non_authorizing_evidence")
    if non_authorizing is not None:
        required_fields = {
            "source_term",
            "cannot_authorize_source",
            "target",
            "reason",
        }
        if not isinstance(non_authorizing, list) or not non_authorizing:
            return "non_authorizing_evidence must be a non-empty array"
        for item in non_authorizing:
            if not isinstance(item, dict) or set(item) != required_fields:
                return "non_authorizing_evidence fields are invalid"
            if any(
                not isinstance(item[field], str) or not item[field].strip()
                for field in required_fields
            ):
                return "non_authorizing_evidence values must be non-empty strings"
    return None


def scope_issue_problem(state: dict, issue: dict) -> str | None:
    problem = issue_contract_problem(issue, get_review_policy(state))
    if problem:
        return problem
    if terminology_enabled(state):
        return None
    category = issue["category"]
    if category.strip().casefold() == "terminology":
        return "Terminology issue is disabled by check scope"
    comment = issue["comment"]
    if comment.lstrip().casefold().startswith("term review:"):
        return "TERM REVIEW evidence is disabled by check scope"
    edit = issue.get("edit")
    evidence = edit.get("evidence") if isinstance(edit, dict) else None
    evidence_type = evidence.get("type") if isinstance(evidence, dict) else None
    if (
        isinstance(evidence_type, str)
        and evidence_type.strip().casefold() == "confirmed_term"
    ):
        return "confirmed_term edit evidence is disabled by check scope"
    return None


def validate_scope_entries(
    state: dict,
    entries: list[dict],
    *,
    issues_key: str,
    label: str,
) -> None:
    if issues_key not in {"issues", "errors"}:
        raise ValueError(f"unsupported issues key: {issues_key!r}")
    for entry in entries:
        for issue in entry.get(issues_key, []):
            problem = scope_issue_problem(state, issue)
            if problem:
                raise ValueError(f"{label}: scope conflict: {problem}")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


ARTIFACT_CONTRACT_VERSION = 1
JOB_RUNTIME_CONTRACT_VERSION = 2


def requires_bound_artifacts(state: dict) -> bool:
    version = state.get("artifact_contract_version")
    if version is None:
        return False
    if version != ARTIFACT_CONTRACT_VERSION:
        raise ValueError(
            "unsupported state artifact_contract_version: "
            f"{version!r}"
        )
    return True


def job_runtime_contract_version(state: dict) -> int:
    value = state.get("job_runtime_contract_version")
    if value is None:
        return 1
    if value != JOB_RUNTIME_CONTRACT_VERSION:
        raise ValueError(
            "unsupported state job_runtime_contract_version: " f"{value!r}"
        )
    return value


def require_current_job_runtime(state: dict, command: str) -> None:
    if job_runtime_contract_version(state) != JOB_RUNTIME_CONTRACT_VERSION:
        raise ValueError(
            f"{command}: historical runtime v1 job is read-only; "
            "re-read the original input into a new job"
        )


def load_terms(state: dict) -> list[dict]:
    if not terminology_enabled(state):
        return []
    path = state.get("terms_path", "")
    if path:
        terms_path = Path(path)
        if not terms_path.is_file():
            raise ValueError(f"configured terminology file is missing: {terms_path}")
        return json.loads(terms_path.read_text(encoding="utf-8"))
    if state.get("terminology"):
        return state["terminology"]
    return []


def term_senses(entry: dict) -> list[dict]:
    """把单义 {source,target,...} / 多义 {source,senses:[...]} 两种形状统一拍平
    成候选列表，每项含 target(必有)/confirmed/protected，
    并保留 status/category/definition。
    是所有脚本读取术语候选译法的唯一入口——不允许绕过它直接读 entry["target"]，
    多义条目没有这个 key。"""
    raw_senses = entry["senses"] if "senses" in entry else [entry]
    senses = []
    for raw in raw_senses:
        sense = {
            key: raw[key]
            for key in ("target", "status", "category", "definition")
            if key in raw
        }
        sense["confirmed"] = raw.get("confirmed") is True
        sense["protected"] = raw.get("protected") is True
        senses.append(sense)
    return senses


def group_terms(terms: list[dict]) -> dict[str, list[dict]]:
    """source -> 候选列表（见 term_senses），跨条目累加。"""
    out: dict[str, list[dict]] = {}
    for t in terms:
        src = (t.get("source") or "").strip()
        if src:
            out.setdefault(src, []).extend(term_senses(t))
    return out


def load_style_guide(state: dict) -> str:
    path = state.get("sg_path", "")
    if path and Path(path).exists():
        return Path(path).read_text(encoding="utf-8")
    return state.get("style_guide", "")


import re

RE_CJK = re.compile(r'[一-鿿]')

# 目标语言属性层：target_languages/<code>/（skill 根，锚定脚本位置而非 CWD）。
# 每语言一个文件夹，固定文件名：attributes.json（语言学事实声明）+ eval_notes.md（语言级
# AI 评估关注点，存在即挂载）。属性不放检查开关；开关由 _lang_toggle_defaults 从属性推导。
# 入层标准：项目 SG 可能推翻的不是属性（em_dash/省略号/引号样式=项目取向，留 checks.json）。
# 合并顺序：内置默认 < 属性推导 < 项目 checks.json < CLI 显式参数。
_SKILL_ROOT = Path(__file__).resolve().parent.parent
_LANG_DIR = _SKILL_ROOT / "target_languages"


_LANG_ALIASES = {
    "zhcn": "zh",
    "zh-cn": "zh",
    "zh_hans": "zh",
    "zh-hans": "zh",
}


def normalize_language_tag(value: str) -> str:
    parts = str(value or "").strip().replace("_", "-").split("-")
    if not parts or not parts[0]:
        return ""
    normalized = [parts[0].lower()]
    script_seen = False
    region_seen = False
    extension_started = False
    for part in parts[1:]:
        if len(part) == 1:
            extension_started = True
            normalized.append(part)
        elif (
            not extension_started
            and not script_seen
            and not region_seen
            and len(part) == 4
            and part.isalpha()
        ):
            normalized.append(part.title())
            script_seen = True
        elif (
            not extension_started
            and not region_seen
            and len(part) == 2
            and part.isalpha()
        ):
            normalized.append(part.upper())
            region_seen = True
        else:
            normalized.append(part)
    return "-".join(normalized)


def language_tags_match(configured: str, declared: str) -> bool:
    configured_tag = normalize_language_tag(configured)
    declared_tag = normalize_language_tag(declared)
    if not configured_tag or not declared_tag:
        return False
    if configured_tag == declared_tag:
        return True
    return "-" not in configured_tag and (
        configured_tag == declared_tag.split("-", 1)[0]
    )


def _lang_code(value) -> str:
    raw = str(value or "").strip().lower()
    return _LANG_ALIASES.get(raw, raw)


def _split_language_pair(pair) -> tuple[str, str]:
    raw = _lang_code(pair)
    if "-" not in raw:
        return "", ""
    source, target = raw.rsplit("-", 1)
    return _lang_code(source), _lang_code(target)


def _source_lang(state_or_pair) -> str:
    if isinstance(state_or_pair, dict):
        return _lang_code(state_or_pair.get("source_lang", ""))
    return _split_language_pair(state_or_pair or "")[0]


def _target_lang(state_or_pair) -> str:
    if isinstance(state_or_pair, dict):
        return _lang_code(state_or_pair.get("target_lang", ""))
    return _split_language_pair(state_or_pair or "")[1]


def resolve_language_assets(lang: str) -> tuple[str, dict]:
    if not lang:
        return "", {}
    code = normalize_language_tag(lang).lower()
    code = _LANG_ALIASES.get(code, code)
    for candidate in dict.fromkeys((code, code.split("-", 1)[0])):
        path = _LANG_DIR / candidate / "attributes.json"
        if path.exists():
            return candidate, read_json(path)
    return "", {}


def _load_lang(lang: str) -> dict:
    return resolve_language_assets(lang)[1]


def _lang_toggle_defaults(attrs: dict) -> dict:
    """属性 → 内置检查适用性推导（项目 checks.json 仍可覆盖最终开关）。"""
    if not attrs:
        return {}
    d = {}
    if attrs.get("script") == "cjk":
        d["fullwidth_punct"] = False  # CJK 目标语言（ja 等）全角标点合法
        d["untranslated_cjk"] = False # CJK 目标语言本身会含 CJK 字符
    if attrs.get("sentence_terminator", ".") == "none":
        d["terminal_punct"] = False   # N5：无句号体系语言（th）
    if attrs.get("word_delim", "space") != "space":
        d["word_repeat"] = False      # N7：非空格分词语言（th，重复另有 ๆ 体系）
    if attrs.get("script", "latin") != "latin":
        d["intra_word_case"] = False  # N8：非拉丁字母语言无大小写
        d["term_case"] = False        # #7 同理
    return d
