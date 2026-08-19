"""
Deterministic pre-check engine (extracted from lqe_io.py).

run_pre_check(state_path, out_path) — 21+ builtin checks + project custom rules.
Toggle keys & merge order: builtin defaults < language-attribute derivation
< project checks.json < CLI. See SKILL.md for the language-attribute contract.
"""
import re
import sys
import json
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from lqe_corrections import CheckFormatError, validate_error_history_term_contract
from lqe_engine import (
    read_json, load_terms as _load_terms, group_terms as _group_terms,
    RE_CJK as _RE_CJK, _target_lang, _load_lang, _lang_toggle_defaults,
    current_target, get_review_policy, require_current_job_runtime,
    terminology_enabled, validate_scope_entries,
)
from lqe_paths import (
    publish_replacement_transaction,
    state_reference_paths,
    validate_artifact_paths,
    write_json_atomic,
)
from lqe_target_form import (
    checks_path_for_state,
    korean_single_quote_count,
)

_RE_DASH     = re.compile(r'—')
_RE_NUM      = re.compile(r'(?<!\d)(\d{4,})(?!\d)')
_RE_HEXCOLOR = re.compile(r'#[0-9A-Fa-f]{3,8}')  # hex 色值 #c15100/#292929 — 数值/Locale 检查前屏蔽，否则其数字段被误报
_RE_COLOR    = {c: re.compile(rf'#{c}[^#]*?#E') for c in 'GCY'}  # count-only, content translatable
_RE_VARS     = [re.compile(r'\{[^}]*\}'), re.compile(r'%[sd]')]   # exact match
# R1: 位置占位符顺序（无索引 %s/%d 顺序敏感；命名/带索引占位符允许重排）
# 注：颜色标签的开闭配对不做独立计数——`#` 在部分项目兼作叙述标记（#Enter/#Camera），
# `#E/#C/#G/#Y` 会误匹配英文词首；颜色标签数量异常由下方整对 `#X...#E` 源译比对负责。
_RE_POS_PH   = re.compile(r'%(?![0-9]+\$)[sd]')
# R6: 数值一致性（提取阿拉伯数字 token，归一去千位分隔符）
_RE_NUMTOK   = re.compile(r'\d[\d,]*(?:\.\d+)?')
# R3 回退条件：长度比对前剥离标记，避免标签稀释 CJK 占比或增加表面长度
_RE_MARKUP   = re.compile(r'<[^>]*>|\{[^}]*\}|%[sd]')
# R5: 译文中不应出现的全角/CJK 标点与全角空格（适用 EN/TH 等非 CJK 目标语言；
# CJK 目标语言如 ja 在语言层关闭 fullwidth_punct）
_FORBIDDEN_FW = '，。！？；：、（）【】《》「」『』“”‘’　'
_FULLWIDTH_REPLACEMENTS = {
    '，': ',', '。': '.', '！': '!', '？': '?', '；': ';', '：': ':',
    '（': '(', '）': ')', '【': '[', '】': ']', '《': '<', '》': '>',
    '「': '"', '」': '"', '『': '"', '』': '"', '“': '"', '”': '"',
    '‘': "'", '’': "'", '　': ' ',
}

# ── N5-N9 / #3 / #7 / #10（PM 批准 2026-06-12）────────────────────────────────
# N5: 句尾终止标点（sentence_terminator=none 的语言由属性推导关闭）
_SRC_TERMINAL = '。！？…!?.'
_TGT_TERMINAL = '.!?…'
_RE_TAIL_TRIM = re.compile(r'(\\n|\s|["\'」』）)】＞>]+)+$')
# N6: 中文数字+量词强模式（PM：仅带量词触发，含「一」；泰语数词本期纳入）
_CN_DIG  = {'〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
_CN_UNIT = {'十': 10, '百': 100, '千': 1000}
_CN_BIG  = {'万': 10000, '亿': 100000000}
_CN_NUMCHARS = '〇一二两三四五六七八九十百千万亿'
_CN_CLASSIFIERS = ('次|个|名|位|只|层|级|阶|天|日|年|月|周|秒|分钟|小时|章|节|卷|倍|件|枚|颗|点|张|条|把|块|回合|回|局|场|轮|波|瓶|组|份|步|人|匹|头|发|道|座|项|种|句|字|页|关')
_RE_CN_NUM = re.compile(rf'(第)?([{_CN_NUMCHARS}]+)(?:{_CN_CLASSIFIERS})')
_EN_ONES = {0: 'zero', 1: 'one', 2: 'two', 3: 'three', 4: 'four', 5: 'five', 6: 'six', 7: 'seven',
            8: 'eight', 9: 'nine', 10: 'ten', 11: 'eleven', 12: 'twelve', 13: 'thirteen', 14: 'fourteen',
            15: 'fifteen', 16: 'sixteen', 17: 'seventeen', 18: 'eighteen', 19: 'nineteen', 20: 'twenty'}
_EN_TENS = {20: 'twenty', 30: 'thirty', 40: 'forty', 50: 'fifty', 60: 'sixty', 70: 'seventy', 80: 'eighty', 90: 'ninety'}
_EN_BIG  = {100: 'hundred', 1000: 'thousand', 10000: 'ten thousand', 1000000: 'million'}
_EN_ORD  = {1: 'first', 2: 'second', 3: 'third', 4: 'fourth', 5: 'fifth', 6: 'sixth', 7: 'seventh',
            8: 'eighth', 9: 'ninth', 10: 'tenth'}
_TH_WORDS = {1: 'หนึ่ง', 2: 'สอง', 3: 'สาม', 4: 'สี่', 5: 'ห้า', 6: 'หก', 7: 'เจ็ด', 8: 'แปด', 9: 'เก้า',
             10: 'สิบ', 20: 'ยี่สิบ', 100: 'ร้อย', 1000: 'พัน', 10000: 'หมื่น', 100000: 'แสน', 1000000: 'ล้าน'}
_TH_DIGIT = {ord(c): str(i) for i, c in enumerate('๐๑๒๓๔๕๖๗๘๙')}
# N7: 单词连续重复（word_delim≠space 的语言由属性推导关闭）
_RE_WORD_REPEAT = re.compile(r'\b([A-Za-z]+)\s+\1\b', re.IGNORECASE)
_REPEAT_WHITELIST = {'had', 'that', 'so', 'no', 'very', 'really', 'many', 'ha', 'heh', 'hee', 'yo', 'ho', 'la'}
# N8: 词内大小写转折（script≠latin 由属性推导关闭）
_RE_MIXED_CASE = re.compile(r'\b[A-Za-z]*[a-z][A-Z][A-Za-z]*\b')
_RE_CASE_EXEMPT = re.compile(r'^(Mc|Mac)[A-Z]|^[a-z]{1,2}[A-Z][a-z]+$')  # McDonald / iPhone / eSports
# N9: 半角成对标点（全角/弯引号已由 R5「出现即报」拦截）
_N9_PAIRS = [('(', ')'), ('[', ']')]
# #3: 通用标签补充检查（{} 归 variables、#G..#E 归 color_tags，不重复）
_TAG_PATTERNS = [('angle', re.compile(r'<[^<>]+>')), ('square', re.compile(r'\[[^\[\]]+\]'))]
# #10: 省略号样式（文件内不混用；…… 计入 unicode 风格）
_RE_ELLIPSIS_DOTS = re.compile(r'\.{3,}')


# N1: 拼音残留——专名大写头 + ≥2 音节完整切分 + 强特征（zh/x/q[^u] 开头或 iang/uang/iong 韵），
# 或撇号分隔双音节（Ping'an 型）。ch/sh 不作强特征（英文词重叠面大），弱信号交检查模块复核。
_PY_INITIALS = ('zh', 'ch', 'sh', 'b', 'p', 'm', 'f', 'd', 't', 'n', 'l', 'g', 'k', 'h',
                'j', 'q', 'x', 'r', 'z', 'c', 's', 'y', 'w', '')
_PY_FINALS = ('iang', 'uang', 'iong', 'ang', 'eng', 'ong', 'ian', 'uan', 'iao', 'uai',
              'ing', 'ie', 'iu', 'ia', 'ua', 'uo', 'ui', 'un', 'ue', 'er', 'ai', 'ei',
              'ao', 'ou', 'an', 'en', 'a', 'o', 'e', 'i', 'u')
_PY_STRONG = re.compile(r'^(?:zh|x|q(?!u))|(?:iang|uang|iong)$')
_PY_ENGLISH = {'taxi', 'taxis'}


def _pinyin_split(word: str):
    """整词能否切分为合法拼音音节序列；不能返回 None。"""
    w = word.lower()
    n = len(w)
    memo: dict = {}

    def rec(i):
        if i == n:
            return []
        if i in memo:
            return memo[i]
        for ini in _PY_INITIALS:
            if ini and not w.startswith(ini, i):
                continue
            j = i + len(ini)
            for fin in _PY_FINALS:
                if w.startswith(fin, j):
                    rest = rec(j + len(fin))
                    if rest is not None:
                        memo[i] = [w[i:j + len(fin)]] + rest
                        return memo[i]
        memo[i] = None
        return None

    return rec(0)


def _cn_parse(s: str):
    """中文数字串 → int；解析失败返回 None（保守不报）。"""
    if not s:
        return None
    total, section, num = 0, 0, 0
    for ch in s:
        if ch in _CN_DIG:
            num = _CN_DIG[ch]
        elif ch in _CN_UNIT:
            section += (num or 1) * _CN_UNIT[ch]
            num = 0
        elif ch in _CN_BIG:
            total = (total + section + num) * _CN_BIG[ch]
            section, num = 0, 0
        else:
            return None
    return total + section + num


def _num_in_target(n: int, tgt: str, numerals: list) -> bool:
    t = tgt.lower().translate(_TH_DIGIT)
    if str(n) in {m.group(0).replace(',', '') for m in _RE_NUMTOK.finditer(t)}:
        return True
    words = set()
    if n in _EN_ONES: words.add(_EN_ONES[n])
    if n in _EN_TENS: words.add(_EN_TENS[n])
    if n in _EN_BIG:  words.add(_EN_BIG[n])
    if n in _EN_ORD:  words.add(_EN_ORD[n])
    if n == 1: words.add('once')
    if n == 2: words.update(('twice', 'double'))
    if 20 < n < 100 and n % 10 and n // 10 * 10 in _EN_TENS and n % 10 in _EN_ONES:
        words.update((f"{_EN_TENS[n // 10 * 10]}-{_EN_ONES[n % 10]}",
                      f"{_EN_TENS[n // 10 * 10]} {_EN_ONES[n % 10]}"))
    words.add(f"{n}th"); words.update((f"{n}st", f"{n}nd", f"{n}rd"))
    if 'thai' in (numerals or []) and n in _TH_WORDS:
        words.add(_TH_WORDS[n].lower())
    return any(w in t for w in words)


def _count_mismatch(pat, src: str, tgt: str):
    sc, tc = len(pat.findall(src)), len(pat.findall(tgt))
    return (sc, tc) if sc != tc else None


def _norm_nums(text: str):
    return Counter(m.group(0).replace(',', '') for m in _RE_NUMTOK.finditer(text))


def _load_checks(state: dict, lang_attrs: dict):
    toggles, custom = {}, []

    def _absorb(cfg: dict, label: str):
        toggles.update(cfg.get("builtin", {}))
        for c in cfg.get("custom", []):
            try:
                custom.append((re.compile(c["pattern"]), c))
            except (re.error, KeyError) as e:
                print(f"[warn] bad custom check {c.get('id', '?')} in {label}: {e}", file=sys.stderr)

    derived = _lang_toggle_defaults(lang_attrs)
    if derived:
        toggles.update(derived)
        print(f"[pre-check] language attrs derived: {derived}")

    p = checks_path_for_state(state)
    if p and Path(p).exists():
        _absorb(read_json(p), "project checks.json")  # 项目层后合并，覆盖语言层同名开关
        print(f"[pre-check] checks profile: {len(toggles)} toggles, {len(custom)} custom rules")
    return toggles, custom


def _fmt_sense(s: dict) -> str:
    parts = [f"'{s['target']}'"]
    if s.get("category"):
        parts.append(f"({s['category']})")
    if s.get("status"):
        parts.append(f"[TB:{s['status']}]")
    return "".join(parts)


def _local_edit(frm: str, to: str, start: int, end: int) -> dict:
    return {
        "from": frm,
        "to": to,
        "start": start,
        "end": end,
        "evidence": None,
    }


def _exact_term_spans(text: str, term: str) -> list[dict]:
    spans = []
    start = text.find(term)
    while start >= 0:
        end = start + len(term)
        spans.append({"start": start, "end": end, "text": term})
        start = text.find(term, end)
    return spans


def _check_issues(errors: list[dict], review_policy: dict | None = None) -> list[dict]:
    issues = []
    policy = get_review_policy(
        {"review_policy": review_policy} if review_policy is not None else {}
    )
    for error in errors:
        issue = dict(error)
        edit = issue.get("edit")
        if issue.get("severity") == "Minor" and not policy["minor_edits_allowed"]:
            issue["needs_confirmation"] = True
            issue["edit"] = None
        else:
            issue["needs_confirmation"] = edit is None
            issue["edit"] = edit
        issues.append(issue)
    return issues


def _publish_precheck_results(
    out: Path,
    results: list[dict],
    *,
    state_path: Path,
    state: dict,
    clear_term_history: bool,
) -> None:
    if not clear_term_history:
        write_json_atomic(out, results)
        return

    cleaned_state = dict(state)
    cleaned_state["error_history"] = []
    staged_paths = []
    try:
        replacements = []
        for destination, value in (
            (out, results),
            (state_path, cleaned_state),
        ):
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=destination.parent,
                prefix=f".{destination.name}.precheck.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                staged = Path(handle.name)
                json.dump(value, handle, ensure_ascii=False, indent=2)
            staged_paths.append(staged)
            replacements.append((staged, destination))
        publish_replacement_transaction(replacements)
    finally:
        for staged in staged_paths:
            staged.unlink(missing_ok=True)


def run_pre_check(state_path: Path, out_path: Path | None = None):
    state_path = Path(state_path)
    state = read_json(state_path)
    require_current_job_runtime(state, "pre-check")
    out = Path(out_path) if out_path is not None else state_path.parent / "errors_precheck.json"
    validate_artifact_paths(
        {"pre-check results": out},
        {"state": state_path, **state_reference_paths(state)},
        context="pre-check",
    )
    segments = state["segments"]
    blocked_ids = {
        segment["id"]
        for segment in segments
        if segment.get("input_status") == "blocked"
    }
    review_policy = get_review_policy(state)
    clear_term_history = False
    term_history_problem = None
    try:
        validate_error_history_term_contract(
            state.get("error_history", []),
            segments=segments,
            label="state.error_history",
        )
    except CheckFormatError as exc:
        clear_term_history = True
        term_history_problem = str(exc)

    terms = _load_terms(state)
    term_map: dict[str, list[dict]] = {}
    for src, senses in _group_terms(terms).items():
        valid = [
            {**s, "_target_lower": s["target"].strip().lower()}
            for s in senses
            if s.get("target")
        ]
        if valid and len(src) >= (2 if _RE_CJK.search(src) else 3):
            term_map[src] = valid

    lang_attrs = _load_lang(_target_lang(state))
    toggles, custom = _load_checks(state, lang_attrs)
    if not terminology_enabled(state):
        toggles.update({"terminology": False, "term_case": False})
    on = lambda key: toggles.get(key, True)
    opt_in = lambda key: toggles.get(key, False) is True

    numerals = lang_attrs.get("numerals", [])
    target_terminal = lang_attrs.get("sentence_terminator") or _TGT_TERMINAL
    if target_terminal == "none":
        target_terminal = ""
    # N8 豁免：术语表译法中出现过的词形（官方 CamelCase 名不报）
    term_tokens = {w for senses in term_map.values() for s in senses
                   for w in re.findall(r'[A-Za-z]+', s["target"])}
    # 术语扫描首字符分桶：每段只扫源文出现过首字符的词条（段数×全表 → 段数×命中桶）
    term_first: dict = defaultdict(list)
    for ts in term_map:
        term_first[ts[0]].append(ts)

    # 单遍预扫：#10 省略号样式（文件内混用→少数派段报）+ N2 同文件一致性
    uni_ids, dots_ids = set(), set()
    src_first, src_variants = {}, defaultdict(set)
    tgt_first, tgt_sources = {}, defaultdict(set)
    for seg in segments:
        if seg["id"] in blocked_ids:
            continue
        t_ = current_target(seg)
        if '…' in t_:
            uni_ids.add(seg["id"])
        if _RE_ELLIPSIS_DOTS.search(t_):
            dots_ids.add(seg["id"])
        s_, t_ = seg["source"].strip(), t_.strip()
        if not s_ or not t_:
            continue
        src_first.setdefault(s_, seg["id"])
        src_variants[s_].add(t_)
        tgt_first.setdefault(t_, seg["id"])
        tgt_sources[t_].add(s_)
    ellipsis_minority = set()
    if uni_ids and dots_ids:
        ellipsis_minority = uni_ids if len(uni_ids) <= len(dots_ids) else dots_ids
    div_src = {s for s, ts in src_variants.items() if len(ts) > 1}
    conv_tgt = {t for t, ss in tgt_sources.items() if len(ss) > 1 and all(len(x) >= 20 for x in ss)}

    results = []
    total = 0

    for seg in segments:
        if seg["id"] in blocked_ids:
            results.append({"id": seg["id"], "issues": []})
            continue
        src = seg["source"]
        tgt = current_target(seg)
        errs = []

        tgt_has_cjk = bool(_RE_CJK.search(tgt))
        src_cjk     = len(_RE_CJK.findall(src))

        # R7: 空译文（仅报一条，跳过其余检查避免堆叠噪音）
        if on("empty_target") and src.strip() and not tgt.strip():
            results.append({"id": seg["id"], "issues": _check_issues([
                {"category": "Untranslated", "severity": "Major",
                 "comment": "Target is empty"}], review_policy)})
            total += 1
            continue

        if on("untranslated_cjk") and tgt_has_cjk and src.strip():
            errs.append({"category": "Untranslated", "severity": "Major",
                         "comment": "Target contains Chinese characters"})

        for match in (_RE_DASH.finditer(tgt) if on("em_dash") else ()):
            start, end = match.start(), match.end()
            while start > 0 and tgt[start - 1] in " \t":
                start -= 1
            while end < len(tgt) and tgt[end] in " \t":
                end += 1
            errs.append({
                "category": "Punctuation",
                "severity": "Minor",
                "comment": "Em dash '—' found; use ' - '",
                "edit": _local_edit(tgt[start:end], " - ", start, end),
            })

        for c, pat in (_RE_COLOR.items() if on("color_tags") else ()):
            mm = _count_mismatch(pat, src, tgt)
            if mm:
                errs.append({"category": "Markup", "severity": "Major",
                             "comment": f"#{c}...#E count: source={mm[0]}, target={mm[1]}"})

        for pat in (_RE_VARS if on("variables") else ()):
            s_hits, t_hits = set(pat.findall(src)), set(pat.findall(tgt))
            for m in s_hits - t_hits:
                errs.append({"category": "Markup", "severity": "Major",
                             "comment": f"Missing variable: {m!r}"})
            for m in t_hits - s_hits:
                errs.append({"category": "Markup", "severity": "Major",
                             "comment": f"Extra variable: {m!r}"})

        src_nl, tgt_nl = src.count(r'\n'), tgt.count(r'\n')
        if on("newline_count") and src_nl != tgt_nl:
            errs.append({"category": "Markup", "severity": "Major",
                         "comment": f"\\n count: source={src_nl}, target={tgt_nl}"})

        max_len = seg.get("max_len") if on("length") else None
        if max_len:
            # R3: 有真实 UI 字段宽度上限 → 硬截断检查（优先于 1.5× 启发式）
            if len(tgt) > max_len:
                errs.append({"category": "Length", "severity": "Major",
                             "comment": f"Target {len(tgt)} chars exceeds max-length {max_len}"})
        elif on("length"):
            src_plain = _RE_MARKUP.sub('', src)
            tgt_plain = _RE_MARKUP.sub('', tgt)
            if len(_RE_CJK.findall(src_plain)) <= len(src_plain) * 0.3:
                src_len = len(src_plain.replace(" ", ""))
                tgt_len = len(tgt_plain.replace(" ", ""))
                if src_len > 0 and tgt_len > src_len * 1.5:
                    errs.append({"category": "Length", "severity": "Major",
                                 "comment": f"Target {tgt_len} chars > 1.5× source {src_len} (markup stripped)"})

        # 屏蔽 hex 色值（#c15100/#292929），其数字段不参与数值/千分位检查
        src_nohex = _RE_HEXCOLOR.sub(' ', src)
        tgt_nohex = _RE_HEXCOLOR.sub(' ', tgt)

        for m in (_RE_NUM.finditer(tgt_nohex) if on("locale_numbers") else ()):
            num = int(m.group(1))
            if not (1900 <= num <= 2099):
                errs.append({"category": "Locale convention", "severity": "Minor",
                             "comment": f"{m.group(1)} → {num:,} (thousands separator)"})
                break

        tgt_lower = tgt.lower()
        if on("terminology"):
            hit_srcs = [ts for ch in set(src) for ts in term_first.get(ch, ()) if ts in src]
            for term_src in hit_srcs:
                senses = term_map[term_src]
                # 复合术语优先：更长词条命中且其任一候选译法已在译文中 → 跳过被包含的子词条
                covered = any(other != term_src and term_src in other
                              and any(s["_target_lower"] in tgt_lower
                                      for s in term_map[other])
                              for other in hit_srcs)
                if covered:
                    continue
                matched = next(
                    (s for s in senses if s["_target_lower"] in tgt_lower),
                    None,
                )
                if matched is None:
                    cands = " or ".join(_fmt_sense(s) for s in senses)
                    note = " [PROTECTED]" if all(s.get("protected") for s in senses) else ""
                    errs.append({
                        "category": "Terminology",
                        "severity": "Major",
                        "comment": f"'{term_src}' → expected {cands}{note}",
                        "term_source": term_src,
                        "expected_targets": [s["target"] for s in senses],
                        "term_spans": {
                            "source": _exact_term_spans(src, term_src),
                            "target": [],
                        },
                    })
                else:
                    errs.append({"category": "Other", "severity": "Neutral",
                                 "comment": f"TERM REVIEW: source term '{term_src}' matched; "
                                            f"candidate {_fmt_sense(matched)} appears in target. "
                                            "Verify context and substring/overlap false matches."})
                if matched is not None and on("term_case"):
                    # #7: 全大写缩写词条精确大小写（PM 2026-06-12：仅查缩写、判严重）
                    for acro in re.findall(r'\b[A-Z]{2,}\b', matched["target"]):
                        if acro.lower() in tgt_lower and acro not in tgt:
                            errs.append({"category": "Company style", "severity": "Major",
                                         "comment": f"Acronym case: expected '{acro}' ('{term_src}' → '{matched['target']}')"})
                            break

        # R1: 无索引位置占位符 %s/%d 顺序（数量相同但顺序错位 → 参数错位）
        src_pos, tgt_pos = _RE_POS_PH.findall(src), _RE_POS_PH.findall(tgt)
        if on("pos_placeholder") and sorted(src_pos) == sorted(tgt_pos) and src_pos != tgt_pos:
            errs.append({"category": "Markup", "severity": "Major",
                         "comment": f"Positional placeholder order changed: {src_pos} → {tgt_pos}"})

        # R6: 数值一致性（仅当源含阿拉伯数字；漏译/改值是游戏 Critical 级隐患）
        if on("numbers_consistency") and _RE_NUMTOK.search(src_nohex):
            missing = _norm_nums(src_nohex) - _norm_nums(tgt_nohex)
            if missing:
                miss = ", ".join(sorted(missing.elements()))
                errs.append({"category": "Mistranslation", "severity": "Major",
                             "comment": f"Source number(s) missing/changed in target: {miss}"})

        # R5: 空白规范化 + EN 译文全角标点
        if on("whitespace") and tgt.strip() and tgt != tgt.strip():
            errs.append({
                "category": "Punctuation",
                "severity": "Minor",
                "comment": "Leading/trailing whitespace in target",
            })
        nbsp_runs = []
        if opt_in("forbidden_nbsp"):
            for match in re.finditer("[ \t\u00a0\u202f]+", tgt):
                if "\u00a0" not in match.group(0) and "\u202f" not in match.group(0):
                    continue
                nbsp_runs.append((match.start(), match.end()))
                replacement = (
                    ""
                    if match.start() == 0 or match.end() == len(tgt)
                    else " "
                )
                errs.append({
                    "category": "Punctuation",
                    "severity": "Minor",
                    "comment": "Forbidden non-breaking space in target",
                    "edit": _local_edit(
                        match.group(0), replacement, match.start(), match.end()
                    ),
                })
        for match in (
            re.finditer(r"(?<=\S) {2,}(?=\S)", tgt)
            if on("whitespace")
            else ()
        ):
            start, end = match.start(), match.end()
            if any(start < run_end and run_start < end for run_start, run_end in nbsp_runs):
                continue
            errs.append({
                "category": "Punctuation",
                "severity": "Minor",
                "comment": "Double space in target",
                "edit": _local_edit(tgt[start:end], " ", start, end),
            })
        fw = sorted({ch for ch in tgt if ch in _FORBIDDEN_FW}) if on("fullwidth_punct") else []
        for char in fw:
            replacement = _FULLWIDTH_REPLACEMENTS.get(char)
            for match in re.finditer(re.escape(char), tgt):
                error = {
                    "category": "Punctuation",
                    "severity": "Minor",
                    "comment": f"Full-width punctuation in target: {char}",
                }
                if replacement is not None:
                    error["edit"] = _local_edit(
                        char, replacement, match.start(), match.end()
                    )
                errs.append(error)

        # N5: 句尾终止标点对齐（sentence_terminator=none 的语言已由属性推导关闭）
        if on("terminal_punct") and src.strip() and tgt.strip():
            s_tail = _RE_TAIL_TRIM.sub('', src.strip())
            t_tail = _RE_TAIL_TRIM.sub('', tgt.strip())
            if s_tail and t_tail:
                s_term, t_term = s_tail[-1] in _SRC_TERMINAL, t_tail[-1] in target_terminal
                if s_term and not t_term:
                    errs.append({"category": "Punctuation", "severity": "Minor",
                                 "comment": f"Source ends with terminal punctuation '{s_tail[-1]}'; target does not"})
                elif not s_term and t_tail[-1] == '.':
                    errs.append({"category": "Punctuation", "severity": "Minor",
                                 "comment": "Target adds terminal '.' absent in source"})

        # N6: 中文数字+量词强模式 → 译侧须有对应数值/数词（含泰文数字与数词）
        if on("cn_numbers"):
            for m in _RE_CN_NUM.finditer(src):
                n = _cn_parse(m.group(2))
                if n is not None and not _num_in_target(n, tgt, numerals):
                    errs.append({"category": "Mistranslation", "severity": "Major",
                                 "comment": f"Chinese numeral '{m.group(0)}' ({n}) has no counterpart in target"})
                    break

        # N7: 单词连续重复（白名单豁免合法重复；非空格分词语言已关闭）
        if on("word_repeat"):
            for m in _RE_WORD_REPEAT.finditer(tgt):
                if m.group(1).lower() not in _REPEAT_WHITELIST:
                    errs.append({"category": "Grammar", "severity": "Minor",
                                 "comment": f"Repeated word: '{m.group(0)}'"})
                    break

        # N8/N1 共用：剥离标签变量后的译文
        plain_tgt = _RE_MARKUP.sub('', tgt) if (on("intra_word_case") or on("pinyin_residue")) else tgt

        # N8: 词内大小写转折（豁免 TB 词形 / Mc/Mac / iPhone 型 / PvP 型 / 标签变量内容）
        if on("intra_word_case"):
            bad = sorted({w for w in _RE_MIXED_CASE.findall(plain_tgt)
                          if w not in term_tokens and not _RE_CASE_EXEMPT.match(w)
                          and not (len(w) <= 4 and w[0].isupper() and w[-1].isupper())})
            if bad:
                errs.append({"category": "Spelling", "severity": "Minor",
                             "comment": f"Mixed case within word: {', '.join(bad[:3])}"})

        # N9: 半角成对标点（源侧配对完整而译侧不完整才报；' 豁免撇号；{} 归 variables）
        if on("paired_punct"):
            for o, cl in _N9_PAIRS:
                if src.count(o) == src.count(cl) and tgt.count(o) != tgt.count(cl):
                    errs.append({"category": "Punctuation", "severity": "Minor",
                                 "comment": f"Unbalanced '{o}{cl}' in target ({tgt.count(o)} open / {tgt.count(cl)} close)"})
            if src.count('"') % 2 == 0 and tgt.count('"') % 2 == 1:
                errs.append({"category": "Punctuation", "severity": "Minor",
                             "comment": "Odd number of straight double quotes in target"})
        if (
            opt_in("ko_single_quote_balance")
            and korean_single_quote_count(tgt) % 2
        ):
            errs.append({
                "category": "Punctuation",
                "severity": "Minor",
                "comment": "Odd number of Korean straight single quotes in target",
            })

        # #3: 通用标签模式集源译对账（项目特殊格式另用 custom count_match 精配）
        if on("tag_count"):
            for name, pat in _TAG_PATTERNS:
                mm = _count_mismatch(pat, src, tgt)
                if mm:
                    errs.append({"category": "Markup", "severity": "Major",
                                 "comment": f"{name}-bracket tag count: source={mm[0]}, target={mm[1]}"})

        # #10: 省略号样式与文件主流不一致（PM：一个项目只用一种，不混用）
        if on("ellipsis_mix") and seg["id"] in ellipsis_minority:
            errs.append({"category": "Inconsistency", "severity": "Minor",
                         "comment": "Ellipsis style differs from file majority ('…' vs '...'); one style per project"})

        # N1: 拼音残留（Critical；术语表词形和白名单豁免，检查模块重点复核）
        if on("pinyin_residue"):
            for w in re.findall(r"\b[A-Za-z]+(?:'[A-Za-z]+)?\b", plain_tgt):
                if not w[0].isupper() or w.lower() in _PY_ENGLISH or w in term_tokens:
                    continue
                if "'" in w:
                    a, _, b = w.partition("'")
                    if len(w) >= 5 and _pinyin_split(a) and _pinyin_split(b):
                        errs.append({"category": "Mistranslation", "severity": "Critical",
                                     "comment": f"Possible pinyin residue: '{w}'"})
                        break
                elif len(w) >= 4:
                    syl = _pinyin_split(w)
                    if syl and len(syl) >= 2 and any(_PY_STRONG.search(x) for x in syl):
                        errs.append({"category": "Mistranslation", "severity": "Critical",
                                     "comment": f"Possible pinyin residue: '{w}'"})
                        break

        # N2: 同源异译 / 异源同译（组内首段为基准不报，后续段报）
        if on("intra_consistency"):
            s_, t_ = src.strip(), tgt.strip()
            if s_ in div_src and src_first.get(s_) != seg["id"]:
                errs.append({"category": "Inconsistency", "severity": "Minor",
                             "comment": f"Same source translated differently elsewhere (first at seg {src_first[s_]})"})
            elif t_ in conv_tgt and tgt_first.get(t_) != seg["id"]:
                errs.append({"category": "Inconsistency", "severity": "Minor",
                             "comment": f"Same translation reused for different sources, all ≥20 chars (first at seg {tgt_first[t_]})"})

        for pat, c in custom:
            if c.get("type") == "count_match":
                mm = _count_mismatch(pat, src, tgt)
                if mm:
                    errs.append({"category": c.get("category", "Markup"),
                                 "severity": c.get("severity", "Major"),
                                 "comment": f"{c.get('comment', c.get('id', 'custom check'))} [source={mm[0]}, target={mm[1]}]"})
                continue
            where = c.get("where", "target")
            hay = src if where == "source" else (src + "\n" + tgt if where == "both" else tgt)
            m = pat.search(hay)
            if m:
                errs.append({"category": c.get("category", "Company style"),
                             "severity": c.get("severity", "Minor"),
                             "comment": f"{c.get('comment', c.get('id', 'custom check'))} [match: {m.group(0)[:30]}]"})

        total += len(errs)
        results.append({
            "id": seg["id"],
            "issues": _check_issues(errs, review_policy),
        })

    try:
        validate_scope_entries(
            state, results, issues_key="issues", label="pre-check results"
        )
    except ValueError as exc:
        raise SystemExit(f"[pre-check] {exc}") from exc

    _publish_precheck_results(
        out,
        results,
        state_path=state_path,
        state=state,
        clear_term_history=clear_term_history,
    )
    if term_history_problem is not None:
        print(
            "[pre-check] cleared incompatible terminology error_history: "
            f"{term_history_problem}",
            file=sys.stderr,
        )

    dist = Counter(e["category"] for r in results for e in r["issues"])
    flagged = sum(1 for r in results if r["issues"])
    print(f"[pre-check] {total} issues / {flagged} segments → {out}")
    for cat, n in dist.most_common():
        print(f"  {n:>4}x  {cat}")
