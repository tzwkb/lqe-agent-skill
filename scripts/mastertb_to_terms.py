#!/usr/bin/env python3
"""Convert a ROCO Master TB workbook to the LQE terms_*.json format.

The master TB layout: a title/blank band on top, then a header row that
contains "术语 ZHCN" (source). The target language lives in its own column;
status/category/definition columns are detected best-effort by header text
(their position and header wording drift between master versions).

Output: list of terms — {"source","target","confirmed","protected"[,"status"]} for a source with a
single known translation, or {"source","senses":[{"target"[,"status"]
[,"category"][,"definition"],"confirmed","protected"}, ...]} when the SAME source legitimately has
more than one translation (e.g. a name reused for both a Species and a
Creature Individual, or a verb/noun pair) — a real, client-intended polysemy,
not a data error. Rows with an empty target are dropped (untranslated,
cannot serve as a term) unless --backfill recovers an old translation for
that exact gap; concepts absent from the master entirely are NOT backfilled.
`<out>.multisense.json` is always (re)written with every source that ended
up with >1 sense in this run, for a human to eyeball.

Status detection & mapping (rule-based, fail-closed by design):
- A status column is detected by a RULE: its header contains "status" or "状态"
  (case-insensitive), anywhere. No enumerative list, so renames/relocations are
  still caught. If multiple columns match, disambiguate with --status-col.
- When a status column is detected, `confirmed`/`protected` come ONLY from an
  explicit confirmation decision: `--approved-statuses` (values, or '*' for the
  whole glossary, or '' for explicit all-unconfirmed). `--protected-statuses`
  ALONE is NOT a confirmation decision. Omitting the decision makes the converter
  fail-closed (refuses to emit silently all-unconfirmed terms — LQE contract).
- If NO status column is detected, the converter fail-closed UNLESS --no-status
  is passed (asserting the glossary truly has no confirmation info). This closes
  the "renamed/relocated column -> silent all-unconfirmed" hole for good.
- Status values are compared case-insensitively (a rule). Rows whose normalized
  status is `denied` are ALWAYS dropped from the output glossary regardless of
  flags; `--exclude-statuses` drops additional status values on top of that.
"""
import argparse
import json
from collections import Counter
from pathlib import Path

from lqe_engine import read_json
from lqe_terms import (
    DENIED_STATUS,
    STATUS_HEADER_RE,
    clean_term_text,
    normalize_status,
)

import openpyxl

SRC_HDR = "术语 ZHCN"
# Status values ALWAYS dropped from the glossary, regardless of --exclude-statuses.
# Client-rejected terms (Denied) are never valid terminology and must never reach
# the LQE terminology check. Standing rule: any "Denied" value is excluded by default.
# Compared case-insensitively (so "denied"/"DENIED" are also excluded).
DEFAULT_EXCLUDE_STATUSES = {DENIED_STATUS}
# Rule-based status-column detection: a column is a status column if its header
# CONTAINS the token "status" or "状态" (case-insensitive), anywhere in the header,
# regardless of prefixes/suffixes/brackets. This is a RULE, not an enumerative list,
# so future master renames/relocations are still caught. The converter never infers
# confirmation from the ABSENCE of a detected column (see fail-closed below).
STATUS_KEYWORD_RE = STATUS_HEADER_RE
CATEGORY_HDRS = {"术语类别 category", "术语类别", "subject", "category"}
DEFINITION_HDRS = {"术语定义 definition", "definition", "note", "术语定义"}


def clean(v) -> str:
    return clean_term_text(v)


def norm_status(v: str) -> str:
    """Normalize a status value for comparison: strip ZW/space and lowercase.

    Header/value matching is done on the normalized form so status values are
    compared case-insensitively and whitespace-robustly — a RULE, not per-value
    special-casing. The original (unedited) value is still stored in the output.
    """
    return normalize_status(v)


def find_header_row(rows, src_hdr):
    for i, r in enumerate(rows):
        if any(clean(c) == src_hdr for c in r):
            return i
    raise SystemExit(f"[err] header row containing {src_hdr!r} not found")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--target-col", default="TH", help="target language header (e.g. TH/EN)")
    ap.add_argument("--source-hdr", default=SRC_HDR)
    ap.add_argument("--backfill", default=None,
                    help="old terms json: carry over translations for sources the "
                         "master left blank (待填充 gap-fill); concepts absent from "
                         "the master are NOT backfilled")
    ap.add_argument("--approved-statuses", default=None,
                    help="comma-separated status values that map to confirmed:true, "
                         "e.g. 'Approved,合规审核通过'. Use '*' to confirm the whole "
                         "emitted glossary (including --no-status and backfilled terms), "
                         "or '' (empty) to "
                         "explicitly leave all unconfirmed. "
                         "REQUIRED when the master has a status column; if omitted the "
                         "converter fail-closed exits (LQE term-confirmation contract).")
    ap.add_argument("--protected-statuses", default=None,
                    help="comma-separated status values that map to protected:true, "
                         "e.g. 'Denied'. Only user-confirmed values should be listed.")
    ap.add_argument("--exclude-statuses", default=None,
                    help="comma-separated status values to DROP entirely from the "
                         "output glossary (additional to the always-excluded set). "
                         "'Denied' is ALWAYS excluded by default and need not be listed. "
                         "Excluded rows never become terms and are not subject to "
                         "terminology checks.")
    ap.add_argument("--status-col", default=None,
                    help="explicit status-column header to use when MULTIPLE status-keyword "
                         "columns are detected (disambiguation); required in that case.")
    ap.add_argument("--no-status", action="store_true",
                    help="assert the glossary has NO confirmation info (no status column at "
                         "all). Required when no status column is detected; contradicts a "
                         "detected column (then remove --no-status / supply --approved-statuses).")
    args = ap.parse_args()

    wb = openpyxl.load_workbook(args.input, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    h = find_header_row(rows, args.source_hdr)
    hdr = [clean(c) for c in rows[h]]
    try:
        si = hdr.index(args.source_hdr)
        ti = hdr.index(args.target_col)
    except ValueError:
        raise SystemExit(f"[err] columns not found. headers={hdr}")
    # Rule-based status-column detection (NOT an enumerative list): any header
    # containing "status"/"状态" (case-insensitive, any position) is a candidate.
    status_candidates = [j for j, c in enumerate(hdr) if c and STATUS_KEYWORD_RE.search(c)]
    if args.status_col:
        try:
            sti = hdr.index(args.status_col)
        except ValueError:
            raise SystemExit(f"[err] --status-col {args.status_col!r} not found. headers={hdr}")
        status_candidates = [sti]
    else:
        if not status_candidates:
            sti = None
        else:
            after = [j for j in status_candidates if j > ti]
            sti = after[0] if after else status_candidates[0]
            if len(status_candidates) > 1:
                cols = [hdr[j] for j in status_candidates]
                raise SystemExit(
                    "[err] multiple status-column candidates detected: "
                    f"{cols}. Disambiguate with --status-col."
                )
    # category/definition headers: best-effort, anywhere (position drifts across master versions)
    ci = next((j for j, c in enumerate(hdr) if c and c.lower() in CATEGORY_HDRS), None)
    di = next((j for j, c in enumerate(hdr) if c and c.lower() in DEFINITION_HDRS), None)

    # Pre-scan distinct status values (cheap: one column) for fail-closed audit.
    status_values = set()
    if sti is not None:
        for r in rows[h + 1:]:
            sv = clean(r[sti]) if sti < len(r) else ""
            if sv:
                status_values.add(sv)

    # Once-and-for-all rule: the converter must NEVER infer confirmation from the
    # ABSENCE of a detected status column. Any uncertainty becomes an explicit
    # assertion, never a silent default.
    #   - status column detected -> caller MUST supply a confirmation decision
    #     (--approved-statuses, incl. '*' or ''; --protected-statuses alone is NOT enough)
    #   - no status column detected -> caller MUST assert --no-status (glossary has no
    #     confirmation info); otherwise fail-closed (column may have been renamed/relocated)
    if sti is not None:
        if args.no_status:
            raise SystemExit(
                "[err] a status column was detected but --no-status was given; "
                "remove --no-status or pass --approved-statuses."
            )
        if args.approved_statuses is None:
            vals = sorted(status_values)
            raise SystemExit(
                "[err] a status column was detected but no confirmation decision was supplied. "
                "Refusing to emit terms with confirmed silently defaulted to false "
                "(LQE term-confirmation contract, fail-closed). "
                "--protected-statuses alone is NOT a confirmation decision. "
                "Pass --approved-statuses '<vals>' or '*' (whole glossary) or '' (explicit "
                f"all-unconfirmed). Distinct status values found: {vals}"
            )
    else:
        if not args.no_status:
            raise SystemExit(
                "[err] no status column detected (no header matches /status|状态/i). "
                "If this glossary genuinely has no confirmation info, pass --no-status. "
                "Otherwise the status column may have been renamed or relocated."
            )

    # Build status->flag mapping from explicit channels. All comparisons are done on
    # the normalized (lowercased, whitespace-stripped) form — a rule, not per-value casing.
    approved_set = {norm_status(x) for x in (args.approved_statuses or "").split(",") if x.strip()}
    protected_set = {norm_status(x) for x in (args.protected_statuses or "").split(",") if x.strip()}
    all_approved = "*" in approved_set
    exclude_set = {norm_status(x) for x in (args.exclude_statuses or "").split(",") if x.strip()}
    exclude_set |= {norm_status(d) for d in DEFAULT_EXCLUDE_STATUSES}  # Denied always dropped

    by_src: dict[str, list[dict]] = {}
    dropped_empty = 0
    excluded = 0
    empty_src = set()  # sources present in master with a blank target (待填充)
    for r in rows[h + 1:]:
        src = clean(r[si]) if si < len(r) else ""
        if not src or src == args.source_hdr:
            continue
        tgt = clean(r[ti]) if ti < len(r) else ""
        if not tgt:
            dropped_empty += 1
            empty_src.add(src)
            continue

        raw_st = clean(r[sti]) if (sti is not None and sti < len(r)) else ""
        st = norm_status(raw_st)
        if raw_st and st in exclude_set:
            excluded += 1
            continue

        # '*' is an explicit whole-glossary confirmation decision. It also applies
        # when the workbook has no status column and the caller asserted --no-status.
        cand = {"target": tgt, "confirmed": bool(all_approved), "protected": False}
        if raw_st:
            cand["status"] = raw_st  # store original (unedited) value for readability
            cand["confirmed"] = bool(all_approved or (st in approved_set))
            cand["protected"] = bool(st in protected_set)
        cat = clean(r[ci]) if (ci is not None and ci < len(r)) else ""
        if cat:
            cand["category"] = cat
        defn = clean(r[di]) if (di is not None and di < len(r)) else ""
        if defn:
            cand["definition"] = defn

        existing = by_src.setdefault(src, [])
        dup = next((c for c in existing if c["target"] == tgt), None)
        if dup is None:
            existing.append(cand)
        else:
            for k in ("status", "category", "definition"):  # 补全：先出现的行缺、后出现的行有
                if k in cand and k not in dup:
                    dup[k] = cand[k]

    backfilled = 0
    if args.backfill:
        old = read_json(args.backfill)
        for t in old:
            s = clean(t.get("source"))
            g = clean(t.get("target"))
            # never resurrect an explicitly excluded status (compared normalized)
            ost = norm_status(t.get("status"))
            if ost and ost in exclude_set:
                excluded += 1
                continue
            # only fill master's 待填充 holes; never resurrect dropped concepts
            if s and g and s in empty_src and s not in by_src:
                item = {
                    "target": g,
                    "confirmed": bool(all_approved or t.get("confirmed") is True),
                    "protected": t.get("protected") is True,
                }
                st = clean(t.get("status"))
                if st:
                    item["status"] = st
                by_src[s] = [item]
                backfilled += 1

    terms = []
    multisense = []
    for src, cands in by_src.items():
        if len(cands) == 1:
            c = cands[0]
            item = {
                "source": src,
                "target": c["target"],
                "confirmed": c["confirmed"],
                "protected": c["protected"],
            }
            if c.get("status"):
                item["status"] = c["status"]
            terms.append(item)
        else:
            terms.append({"source": src, "senses": cands})
            multisense.append({"source": src,
                                "senses": [{"target": c["target"], "category": c.get("category", ""),
                                            "confirmed": c["confirmed"], "protected": c["protected"]}
                                           for c in cands]})

    Path(args.out).write_text(
        json.dumps(terms, ensure_ascii=False, indent=1), encoding="utf-8")

    multisense_path = Path(args.out).with_suffix("")
    multisense_path = multisense_path.with_name(multisense_path.name + ".multisense.json")
    if multisense:
        multisense_path.write_text(
            json.dumps(multisense, ensure_ascii=False, indent=1), encoding="utf-8")
    elif multisense_path.exists():
        multisense_path.unlink()  # 上一轮遗留、这轮已经没有多义分组了

    dist = Counter(c.get("status", "(none)") for cands in by_src.values() for c in cands)
    n_status = sum(v for k, v in dist.items() if k != "(none)")
    print(f"[ok] wrote {len(terms)} sources ({sum(len(v) for v in by_src.values())} total senses) -> {args.out}")
    print(f"     master-filled sources: {len(by_src) - backfilled}  backfilled (待填充 gap): {backfilled}")
    print(f"     master empty-target rows: {dropped_empty}")
    print(f"     excluded by default(Denied)+--exclude-statuses: {excluded}")
    print(f"     with status: {n_status}  dist: {dict(dist)}")
    print(f"     multi-sense sources: {len(multisense)}" +
          (f" -> {multisense_path}" if multisense else ""))


if __name__ == "__main__":
    main()
