"""
LQE 批次编排 + 断点续跑。

子命令：
  plan     state.json → batches/batch_NN.txt（按输出体量自适应分批 + manifest.json）
  merge    evals/*.json → errors.json（断点：缺批占位，报告完成度）

设计原则：
  - 按预计输出长度分批，不按段数均分。剧情和对话通常需要更长的问题说明与局部修改。
  - 每批写独立 eval_NN.json。某批失败时只重跑该批。
  - merge 可重复运行；缺少的批次会明确列出，全部完成后再计算最终分。
"""
import argparse
import json
import re
from pathlib import Path

from lqe_corrections import CheckFormatError, build_results, normalize_check_entries
from lqe_target_form import load_target_form_policy
from lqe_engine import (
    RE_CJK as _RE_CJK,
    current_target,
    get_check_scope,
    get_review_policy,
    load_terms,
    requires_bound_artifacts,
    term_senses,
    terminology_enabled,
    validate_scope_entries,
)
from lqe_chunk import (
    _PRECHECK_REVIEW_CATEGORIES,
    _precheck_provenance_problem,
    with_precheck_refs,
)
from lqe_split_contract import generation_lock


def _est_output_chars(seg, term_hits):
    """估算该段检查结果长度：问题说明与局部修改约为源文长度三倍，再加术语命中说明。"""
    src_len = len(seg["source"])
    base = 60                      # JSON 骨架 + 空错
    correction = src_len * 3       # 可能的问题说明与局部修改
    terms = term_hits * 25         # 每条命中注记
    return base + correction + terms


def cmd_plan(args):
    job = Path(args.job)
    state = json.loads((job / "state.json").read_text(encoding="utf-8"))
    segs = state["segments"]
    scope = get_check_scope(state)
    review_policy = get_review_policy(state)
    term_enabled = terminology_enabled(state)
    terms = load_terms(state)
    tlist = []
    for term in terms:
        source = term.get("source")
        if not isinstance(source, str) or len(source) < 2:
            continue
        for sense in term_senses(term):
            target = sense.get("target")
            if not isinstance(target, str) or not target:
                continue
            tlist.append(
                {
                    "source": source,
                    "target": target,
                    "status": sense.get("status", ""),
                    "confirmed": sense.get("confirmed") is True,
                    "protected": sense.get("protected") is True,
                }
            )
    pre = {}
    pc = job / "errors_precheck.json"
    if pc.exists():
        try:
            pre_entries = normalize_check_entries(
                json.loads(pc.read_text(encoding="utf-8")),
                label=str(pc),
                review_policy=review_policy,
            )
        except (json.JSONDecodeError, CheckFormatError) as exc:
            raise SystemExit(f"[plan] invalid pre-check results: {exc}") from exc
        try:
            validate_scope_entries(
                state,
                pre_entries,
                issues_key="issues",
                label=pc.name,
            )
        except ValueError as exc:
            raise SystemExit(f"[plan] {exc}") from exc
        pre = {
            entry["id"]: with_precheck_refs(entry["id"], entry["issues"])
            for entry in pre_entries
        }

    bdir = job / "batches"
    bdir.mkdir(exist_ok=True)
    budget = args.output_budget      # 每批输出 char 上限，默认 24000（≈安全 < 64K token）

    batches, cur, cur_cost = [], [], 0
    for s in segs:
        hits = [term for term in tlist if term["source"] in s["source"]]
        cost = _est_output_chars(s, len(hits))
        if cur and cur_cost + cost > budget:
            batches.append(cur)
            cur, cur_cost = [], 0
        cur.append((s, hits))
        cur_cost += cost
    if cur:
        batches.append(cur)

    scope_lines = [
        f"# Enabled check modules: {', '.join(scope['enabled_modules'])}",
        f"# Terminology check: {'enabled' if term_enabled else 'disabled'}",
    ]
    if term_enabled:
        scope_lines.append(
            "# Terminology issues must identify the source term, expected termbase "
            "translation, and deviation."
        )
    else:
        scope_lines.append(
            "# Do not output Terminology, proper-name, TERM REVIEW, or confirmed-term "
            "(confirmed_term) evidence."
        )
        scope_lines.append(
            "# Issues in precheck-review categories must copy precheck_ref from the "
            "matching PRECHECK item; do not invent or reuse a reference."
        )
    if review_policy["mode"] == "optimized":
        review_lines = (
            "comment 不得为空，以 20–30 个字符为软目标，完整清楚优先。"
            "Minor 固定为 needs_confirmation=true、edit=null；"
            "非 Minor 的安全局部修改才可写入 edit。"
            "若输入带 CONTENT_TYPE 或文本类型 CONTEXT，只按上游分类调整检查重点，"
            "不得自行分类。"
        )
    else:
        review_lines = (
            "comment 不得为空并应简明完整。"
            "任意严重度中，安全、唯一的局部修改可写入 edit；"
            "不能安全修改时写 needs_confirmation=true、edit=null。"
            "CONTENT_TYPE 和文本类型 CONTEXT 仅作上下文，不改变检查强度或建议口径。"
        )
    _SCHEMA_HEADER = "\n".join(scope_lines) + "\n" + (
        "# 输出格式（强制）：JSON 数组，每项为 {id, issues:[...]}。"
        "每个 issue 必须含 category / severity / comment / needs_confirmation / edit；"
        + review_lines
        + "批量同类问题也要逐条填写。检查任务不得输出 corrected。"
        "\n"
        "# ────────────────────────────────────────\n"
    )
    manifest = []
    for i, batch in enumerate(batches):
        lines = []
        for s, hits in batch:
            sid, src, tgt = s["id"], s["source"], current_target(s)
            hh = []
            for term in hits:
                flags = (
                    f"confirmed={str(term['confirmed']).lower()}, "
                    f"protected={str(term['protected']).lower()}"
                )
                status = f"[status={term['status']}]" if term["status"] else ""
                hh.append(f"{term['source']}={term['target']}[{flags}]{status}")
            flags = '; '.join(
                f"{e['category']}:{e['comment'][:60]} "
                f"[precheck_ref={e['precheck_ref']}]"
                for e in pre.get(sid, [])
            )
            block = f"#{sid}\nSRC: {src}\nTGT: {tgt}"
            if s.get("content_type"):
                block += f"\nCONTENT_TYPE: {s['content_type']}"
            contexts = [
                value
                for value in (
                    s.get("text_type_context"),
                    s.get("context_note"),
                )
                if value
            ]
            if contexts:
                block += f"\nCONTEXT: {' | '.join(dict.fromkeys(contexts))}"
            if hh:
                block += f"\nTERMS: {' | '.join(hh[:8])}"
            if flags:
                block += f"\nPRECHECK: {flags}"
            lines.append(block)
        (bdir / f"batch_{i:02d}.txt").write_text(_SCHEMA_HEADER + '\n---\n'.join(lines), encoding="utf-8")
        ids = [s["id"] for s, _ in batch]
        manifest.append({"batch": i, "n": len(batch), "id_min": ids[0], "id_max": ids[-1],
                          "est_output_chars": sum(_est_output_chars(s, len(h)) for s, h in batch)})

    (job / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[plan] {len(segs)} segs → {len(batches)} batches (budget {budget} chars/batch)")
    big = [m for m in manifest if m["est_output_chars"] > budget * 1.1]
    for m in manifest:
        flag = "  ⚠large" if m in big else ""
        print(f"  batch_{m['batch']:02d}: {m['n']:>3} segs  ids {m['id_min']}-{m['id_max']}  ~{m['est_output_chars']//1000}k chars{flag}")


def _cmd_merge_locked(args, job: Path, state: dict):
    review_policy = get_review_policy(state)
    precheck_by_id = {}
    precheck_path = job / "errors_precheck.json"
    if precheck_path.exists():
        try:
            precheck_entries = normalize_check_entries(
                json.loads(precheck_path.read_text(encoding="utf-8")),
                label=str(precheck_path),
                review_policy=review_policy,
            )
        except (json.JSONDecodeError, CheckFormatError) as exc:
            raise SystemExit(f"[merge] invalid pre-check results: {exc}") from exc
        precheck_by_id = {
            entry["id"]: with_precheck_refs(entry["id"], entry["issues"])
            for entry in precheck_entries
        }
    evals = sorted((job / "evals").glob("eval_*.json"))
    merged = {}
    for f in evals:
        try:
            entries = normalize_check_entries(
                json.loads(f.read_text(encoding="utf-8")),
                label=str(f),
                review_policy=review_policy,
            )
            for r in entries:
                merged[r["id"]] = r          # 后到覆盖：子批(04a)覆盖原批(04)残留
        except (json.JSONDecodeError, CheckFormatError) as exc:
            raise SystemExit(f"[merge] invalid {f.name}: {exc}") from exc

    seg_ids = [s["id"] for s in state["segments"]]
    seg_id_set = set(seg_ids)
    missing = sorted(i for i in seg_ids if i not in merged)
    extra = sorted(i for i in merged if i not in seg_id_set)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing={missing}")
        if extra:
            details.append(f"extra={extra}")
        raise SystemExit(f"[merge] incomplete check coverage: {', '.join(details)}")
    try:
        validate_scope_entries(
            state,
            list(merged.values()),
            issues_key="issues",
            label="batch merge",
        )
    except ValueError as exc:
        raise SystemExit(f"[merge] {exc}") from exc
    if not terminology_enabled(state):
        for entry in merged.values():
            reviewed = [
                issue
                for issue in entry["issues"]
                if issue.get("category") in _PRECHECK_REVIEW_CATEGORIES
            ]
            problem = _precheck_provenance_problem(
                precheck_by_id.get(entry["id"], []), reviewed
            )
            if problem:
                raise SystemExit(
                    f"[merge] precheck provenance for id {entry['id']}: {problem}"
                )
    if requires_bound_artifacts(state):
        raise SystemExit(
            "[merge] legacy batch outputs cannot publish into a current job; "
            "use lqe_chunk.py split + lqe_review.py prepare/publish + merge"
        )
    out = build_results(
        state["segments"],
        list(merged.values()),
        review_policy=review_policy,
        target_form_policy=load_target_form_policy(state),
    )
    (job / "errors.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print(
        f"[merge] {len(evals)} eval files → "
        f"{len(seg_ids)}/{len(seg_ids)} segs covered → errors.json"
    )
    print("[merge] complete — 全段检查完成，可以计算最终分")


def cmd_merge(args):
    job = Path(args.job)
    with generation_lock(job / "chunks", exclusive=True):
        state = json.loads((job / "state.json").read_text(encoding="utf-8"))
        _cmd_merge_locked(args, job, state)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan"); p.add_argument("--job", required=True); p.add_argument("--output-budget", type=int, default=24000)
    m = sub.add_parser("merge"); m.add_argument("--job", required=True)
    args = ap.parse_args()
    {"plan": cmd_plan, "merge": cmd_merge}[args.cmd](args)


if __name__ == "__main__":
    main()
