#!/usr/bin/env python3
"""把任务拆分给各检查模块，再合并问题与安全局部修改。

split: state.json + errors.json(pre-check) + terms.json -> chunks/chunk_NN.json
       each segment carries {id, source, target, precheck[], term_hits[], term_near[]}
merge-checks: check-module files -> chunk_NN.out.json ({id, issues} only)
merge: chunks/chunk_NN.out.json -> errors.json ({id, errors, corrected})
       validates every state id is covered; missing ids fall back to pre-check.
"""
import argparse
import copy
import hashlib
import json
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


from lqe_corrections import (
    CheckFormatError,
    build_segment_result,
    normalize_check_entries,
)
from lqe_engine import (
    current_target,
    disabled_modules,
    get_check_scope,
    get_review_policy,
    load_terms,
    optional_modules,
    read_json as load,
    require_current_job_runtime,
    requires_bound_artifacts,
    required_modules,
    scope_issue_problem,
    terminology_enabled,
    validate_scope_entries,
)
from lqe_paths import (
    publish_replacement_transaction,
    state_reference_paths,
    validate_artifact_paths,
    write_json_atomic,
)
from lqe_result_contract import build_result_contract, result_contract_path
from lqe_target_form import load_target_form_policy
from lqe_split_contract import (
    SplitContractError,
    add_chunk_payload_digest,
    build_split_manifest,
    build_split_revision,
    canonical_digest,
    generation_lock,
    make_path_reference,
    publish_generation,
    resolve_path_reference,
    state_fingerprint,
    validate_chunk_payload,
    validate_generation_payloads,
    validate_live_manifest,
)
from lqe_terms import load_canonical_terminology
from term_suggest import build_index as _tn_build, suggest as _tn_suggest

try:
    from lqe_context import (
        descriptor_registry,
        module_review_equivalence_key,
        project_segment_for_module,
    )
except ImportError:  # context module is optional during legacy bootstrap
    descriptor_registry = None
    module_review_equivalence_key = None
    project_segment_for_module = None

try:
    from lqe_capabilities import canonical_digest as _capability_digest
    from lqe_capabilities import validate_capability_descriptor
except ImportError:  # legacy bootstrap
    _capability_digest = None
    validate_capability_descriptor = None


LEGACY_DEFAULT_SPLIT_SIZE = 100
ENFORCE_DEFAULT_SPLIT_SIZE = 5


def resolve_split_size(state: dict, requested_size: int | None) -> int:
    if requested_size is not None:
        return requested_size
    pipeline = state.get("context_pipeline")
    mode = pipeline.get("mode") if isinstance(pipeline, dict) else "off"
    if mode == "enforce":
        return ENFORCE_DEFAULT_SPLIT_SIZE
    return LEGACY_DEFAULT_SPLIT_SIZE


def _state_context_registry(state: dict):
    if descriptor_registry is None:
        return None
    descriptors = state.get("resolved_context_descriptors")
    if not isinstance(descriptors, dict):
        return None
    if validate_capability_descriptor is not None:
        resolution = state.get("capability_resolution", {})
        enabled = resolution.get("enabled", {}) if isinstance(resolution, dict) else {}
        expected_context_ids = {
            capability_id
            for capability_id, item in enabled.items()
            if capability_id.startswith("context.")
            and item.get("effect") in {"foundation", "enforce"}
        }
        if expected_context_ids and set(descriptors) != expected_context_ids:
            raise SplitContractError(
                "resolved context descriptor coverage differs from capability resolution"
            )
        for capability_id, descriptor in descriptors.items():
            if descriptor.get("id") != capability_id:
                raise SplitContractError(
                    f"resolved context descriptor id mismatch: {capability_id}"
                )
            normalized = validate_capability_descriptor(
                descriptor,
                custom=capability_id not in {
                    "context.core@1",
                    "context.dialogue@1",
                    "context.ui@1",
                    "context.marketing@1",
                },
            )
            if normalized != descriptor:
                raise SplitContractError(
                    f"resolved context descriptor is not canonical: {capability_id}"
                )
            expected = enabled.get(capability_id, {}).get("descriptor_digest")
            if expected is not None and _capability_digest(descriptor) != expected:
                raise SplitContractError(
                    f"resolved context descriptor digest mismatch: {capability_id}"
                )
    return descriptors


def _group_chunk_terms(terms):
    grouped = {}
    for entry in terms:
        source = (entry.get("source") or "").strip()
        if not source:
            continue
        raw_senses = entry.get("senses") if "senses" in entry else [entry]
        if not isinstance(raw_senses, list):
            continue
        for raw in raw_senses:
            if not isinstance(raw, dict) or "target" not in raw:
                continue
            sense = {
                key: value
                for key, value in raw.items()
                if key not in {"source", "senses"}
            }
            sense["confirmed"] = raw.get("confirmed") is True
            sense["protected"] = raw.get("protected") is True
            grouped.setdefault(source, []).append(sense)
    return grouped


def _term_hits(src_txt, titems, cap=15):
    """Longest-match, coverage-filtered term hits. Keep a TB term only if it has
    an occurrence NOT fully inside a longer term's occurrence — so 优优 inside
    绒光优优 is dropped (the longer term already covers it), but a separate
    standalone 优优 elsewhere in the segment is still kept. Each retained term
    is flattened to one hit per sense for deterministic correction checks."""
    occ = []  # (start, end, src, senses)
    for ts, senses in titems:               # titems is sorted longest-first
        i = src_txt.find(ts)
        while i >= 0:
            occ.append((i, i + len(ts), ts, senses))
            i = src_txt.find(ts, i + 1)
    occ.sort(key=lambda o: -(o[1] - o[0]))  # longest span first
    accepted = []                           # spans claimed by longer terms
    kept = {}                               # src -> senses (one entry per term)
    for s, e, ts, senses in occ:
        if any(a <= s and e <= b for a, b in accepted):
            continue                        # covered by a longer term -> drop
        accepted.append((s, e))
        if ts not in kept:
            kept[ts] = senses
    hits = []
    for source, senses in list(kept.items())[:cap]:
        for sense in senses:
            hit = {"source": source, **sense}
            hit["confirmed"] = sense.get("confirmed") is True
            hit["protected"] = sense.get("protected") is True
            hits.append(hit)
    return hits


def _seg_kind(src):
    """标记内容类型，供检查模块确定适用范围。

    name 是短的单一名称；desc 是句子、复合词或含标记和占位符的内容。
    不确定时归为 desc，以免漏掉复合名称中的含义问题。
    """
    if len(src) > 6:
        return "desc"
    if any(p in src for p in "，。！？、；：,.!?;:「」“”\"'"):
        return "desc"
    if any(m in src for m in ("<", "{", "#", "%", "\\", " ")):
        return "desc"
    return "name"


def _precheck_issue_ref(segment_id: int, index: int, issue: dict) -> str:
    canonical = {
        key: value for key, value in issue.items() if key != "precheck_ref"
    }
    payload = json.dumps(
        {"id": segment_id, "index": index, "issue": canonical},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"precheck:{segment_id}:{hashlib.sha256(payload).hexdigest()[:16]}"


def with_precheck_refs(segment_id: int, issues: list[dict]) -> list[dict]:
    output = []
    for index, issue in enumerate(issues):
        item = copy.deepcopy(issue)
        item["precheck_ref"] = _precheck_issue_ref(segment_id, index, item)
        output.append(item)
    return output


def _state_fingerprint(state: dict) -> str:
    return state_fingerprint(state)


def _split_fingerprint(
    state: dict,
    precheck: list[dict],
    terms: list[dict],
    *,
    size: int,
    char_budget: int,
) -> str:
    return build_split_revision(
        state,
        precheck,
        terms,
        get_check_scope(state),
        size=size,
        char_budget=char_budget,
    )["split_fingerprint"]


def _path_is_within(path: Path, directory: Path) -> bool:
    resolved_path = Path(path).resolve()
    resolved_directory = Path(directory).resolve()
    return (
        resolved_path == resolved_directory
        or resolved_directory in resolved_path.parents
    )


def _chunk_revision_problem(
    state: dict, base: dict, manifest: dict | None = None
) -> str | None:
    try:
        validate_chunk_payload(manifest, base)
    except SplitContractError as exc:
        return str(exc)
    expected_state = _state_fingerprint(state)
    if manifest["state_fingerprint"] != expected_state:
        return "stale split manifest; live state changed after split"
    return None


def _manifest_input_path(
    manifest: dict,
    key: str,
    job_root: Path,
) -> Path | None:
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict):
        raise SplitContractError("split manifest inputs are required")
    reference = inputs.get(key)
    if reference is None:
        return None
    return resolve_path_reference(reference, job_root)


def _live_terms_for_manifest(
    state: dict,
    manifest: dict,
    job_root: Path,
) -> list[dict]:
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict):
        raise SplitContractError("split manifest inputs are required")
    mode = inputs.get("terms_mode")
    if mode == "state":
        return load_terms(state)
    if mode != "override":
        raise SplitContractError("split manifest terms_mode is invalid")
    terms_path = _manifest_input_path(manifest, "terms", job_root)
    if terms_path is None or not terms_path.is_file():
        raise SplitContractError(
            f"split terminology override is missing: {terms_path}"
        )
    try:
        return load_canonical_terminology(terms_path)
    except ValueError as exc:
        raise SplitContractError(str(exc)) from exc


def _load_verified_generation_unlocked(
    state_path: Path,
    precheck_path: Path,
    outdir: Path,
    *,
    state: dict | None = None,
) -> tuple[dict, list[dict], dict, list[dict], list[dict]]:
    """Load one generation only after verifying all live inputs and payloads.

    Returns ``(manifest, chunks, dedup_map, precheck, terms)``.
    """
    state_path = Path(state_path)
    precheck_path = Path(precheck_path)
    outdir = Path(outdir)
    manifest_path = outdir / "split_manifest.json"
    if not manifest_path.is_file():
        raise SplitContractError(f"split manifest is required: {manifest_path}")
    manifest = load(manifest_path)
    state = state if state is not None else load(state_path)
    precheck = normalize_check_entries(
        load(precheck_path),
        label=precheck_path.name,
        review_policy=get_review_policy(state),
    )
    validate_scope_entries(
        state,
        precheck,
        issues_key="issues",
        label=precheck_path.name,
    )
    terms = _live_terms_for_manifest(state, manifest, outdir.parent)
    scope = get_check_scope(state)
    validate_live_manifest(manifest, state, precheck, terms, scope)

    recorded_state = _manifest_input_path(manifest, "state", outdir.parent)
    recorded_precheck = _manifest_input_path(
        manifest, "precheck", outdir.parent
    )
    if recorded_state is None or recorded_state.resolve() != state_path.resolve():
        raise SplitContractError(
            "split manifest state input differs from the live state path"
        )
    if (
        recorded_precheck is None
        or recorded_precheck.resolve() != precheck_path.resolve()
    ):
        raise SplitContractError(
            "split manifest precheck input differs from the live precheck path"
        )

    expected_names = set(manifest["chunk_digests"])
    actual_paths = {
        path.name: path
        for path in outdir.iterdir()
        if path.is_file() and re.fullmatch(r"chunk_\d+\.json", path.name)
    }
    if set(actual_paths) != expected_names:
        raise SplitContractError(
            "chunk file coverage differs from split manifest: "
            f"missing={sorted(expected_names - set(actual_paths))} "
            f"extra={sorted(set(actual_paths) - expected_names)}"
        )
    chunks = [load(actual_paths[name]) for name in sorted(expected_names)]
    dedup_path = outdir / "dedup_map.json"
    if not dedup_path.is_file():
        raise SplitContractError(f"dedup_map.json is required: {dedup_path}")
    dedup_map = load(dedup_path)
    validate_generation_payloads(manifest, chunks, dedup_map, state)
    return manifest, chunks, dedup_map, precheck, terms


def load_verified_generation(
    state_path: Path,
    precheck_path: Path,
    outdir: Path,
    *,
    state: dict | None = None,
) -> tuple[dict, list[dict], dict, list[dict], list[dict]]:
    with generation_lock(Path(outdir), exclusive=False):
        return _load_verified_generation_unlocked(
            state_path,
            precheck_path,
            outdir,
            state=state,
        )


def _state_verification_segments(state: dict) -> list[dict]:
    segments = copy.deepcopy(state.get("segments"))
    if not isinstance(segments, list):
        raise SplitContractError("state.segments must be an array")
    segment_ids = [
        segment.get("id") if isinstance(segment, dict) else None
        for segment in segments
    ]
    if any(type(segment_id) is not int for segment_id in segment_ids):
        raise SplitContractError("state segment id must be an integer")
    if len(segment_ids) != len(set(segment_ids)):
        raise SplitContractError("state segment ids must be unique")
    grouped = _group_chunk_terms(load_terms(state))
    term_items = [
        (source, senses)
        for source, senses in grouped.items()
        if len(source) >= 2
    ]
    term_items.sort(key=lambda item: -len(item[0]))
    for segment in segments:
        source = segment.get("source", "")
        if not isinstance(source, str):
            raise SplitContractError(
                f"state segment {segment['id']} source must be a string"
            )
        if "kind" not in segment:
            segment["kind"] = _seg_kind(source)
        elif segment["kind"] not in {"name", "desc"}:
            raise SplitContractError(
                f"state segment {segment['id']} kind must be name or desc"
            )
        if "term_hits" not in segment:
            segment["term_hits"] = _term_hits(source, term_items)
        elif not isinstance(segment["term_hits"], list):
            raise SplitContractError(
                f"state segment {segment['id']} term_hits must be an array"
            )
        if "protected_texts" not in segment:
            segment["protected_texts"] = []
        elif not isinstance(segment["protected_texts"], list):
            raise SplitContractError(
                f"state segment {segment['id']} protected_texts must be an array"
            )
    return segments


def _enrich_verification_segments(
    state: dict,
    chunks: list[dict],
    dedup_map: dict,
) -> list[dict]:
    segments = _state_verification_segments(state)
    by_id = {segment["id"]: segment for segment in segments}
    contexts = {}
    for chunk in chunks:
        chunk_registry = chunk.get("resolved_context_descriptors")
        state_registry = state.get("resolved_context_descriptors")
        if isinstance(state_registry, dict) and chunk_registry != state_registry:
            raise SplitContractError(
                "chunk resolved context descriptors differ from state"
            )
        for context in chunk["segments"]:
            contexts[context["id"]] = context
    for raw_representative, members in dedup_map.items():
        representative = int(raw_representative)
        context = contexts.get(representative)
        if context is None:
            raise SplitContractError(
                f"chunk context missing representative {representative}"
            )
        for segment_id in members:
            contexts.setdefault(segment_id, context)

    for segment_id, segment in by_id.items():
        context = contexts.get(segment_id)
        if context is None:
            raise SplitContractError(
                f"chunk context missing state segment {segment_id}"
            )
        if context.get("source") != segment.get("source") or context.get(
            "target"
        ) != current_target(segment):
            raise SplitContractError(
                f"chunk context: segment {segment_id} differs from state"
            )
        for key in ("kind", "term_hits", "term_near", "protected_texts"):
            if key in context:
                segment[key] = copy.deepcopy(context[key])
    return segments


@contextmanager
def verification_generation_lease(
    state_path: Path,
    *,
    exclusive: bool,
    precheck_path: Path | None = None,
    chunks_dir: Path | None = None,
    require_generation: bool | None = None,
):
    """Hold one generation stable from live verification through publication."""
    state_path = Path(state_path)
    chunks_dir = Path(chunks_dir or state_path.parent / "chunks")
    precheck_path = Path(
        precheck_path or state_path.parent / "errors_precheck.json"
    )
    with generation_lock(chunks_dir, exclusive=exclusive):
        state = load(state_path)
        required = (
            requires_bound_artifacts(state)
            if require_generation is None
            else require_generation
        )
        if type(required) is not bool:
            raise SplitContractError("require_generation must be boolean")
        if not chunks_dir.is_dir() or not any(chunks_dir.iterdir()):
            if required:
                raise SplitContractError(
                    f"verified chunk generation is required: {chunks_dir}"
                )
            segments = _state_verification_segments(state)
            expected_state_fingerprint = _state_fingerprint(state)

            def revalidate():
                live_state = load(state_path)
                if _state_fingerprint(live_state) != expected_state_fingerprint:
                    raise SplitContractError(
                        "live state changed during artifact publication"
                    )

            yield state, segments, None, revalidate
            return

        manifest, chunks, dedup_map, _, _ = _load_verified_generation_unlocked(
            state_path,
            precheck_path,
            chunks_dir,
            state=state,
        )
        segments = _enrich_verification_segments(state, chunks, dedup_map)
        expected_manifest_digest = manifest["manifest_digest"]

        def revalidate():
            live_state = load(state_path)
            current, _, _, _, _ = _load_verified_generation_unlocked(
                state_path,
                precheck_path,
                chunks_dir,
                state=live_state,
            )
            if current["manifest_digest"] != expected_manifest_digest:
                raise SplitContractError(
                    "chunk generation changed during artifact publication"
                )

        yield state, segments, manifest, revalidate


def load_verification_segments(
    state_path: Path,
    *,
    state: dict | None = None,
    precheck_path: Path | None = None,
    chunks_dir: Path | None = None,
    require_generation: bool | None = None,
) -> list[dict]:
    """Return live state segments enriched by a verified split generation."""
    state_path = Path(state_path)
    state = state if state is not None else load(state_path)
    if require_generation is None:
        require_generation = requires_bound_artifacts(state)
    elif type(require_generation) is not bool:
        raise SplitContractError("require_generation must be boolean")
    segments = _state_verification_segments(state)

    chunks_dir = Path(chunks_dir or state_path.parent / "chunks")
    with generation_lock(chunks_dir, exclusive=False):
        if not chunks_dir.is_dir() or not any(chunks_dir.iterdir()):
            if require_generation:
                raise SplitContractError(
                    f"verified chunk generation is required: {chunks_dir}"
                )
            return segments
        precheck_path = Path(
            precheck_path or state_path.parent / "errors_precheck.json"
        )
        _, chunks, dedup_map, _, _ = _load_verified_generation_unlocked(
            state_path,
            precheck_path,
            chunks_dir,
            state=state,
        )

    return _enrich_verification_segments(state, chunks, dedup_map)


def cmd_split(a):
    state = load(a.state)
    require_current_job_runtime(state, "split")
    review_policy = get_review_policy(state)
    pre = normalize_check_entries(
        load(a.errors),
        label=Path(a.errors).name,
        review_policy=review_policy,
    )
    validate_scope_entries(
        state, pre, issues_key="issues", label=Path(a.errors).name
    )
    if a.terms and not terminology_enabled(state):
        raise SystemExit("[split] scope conflict: --terms is disabled by check scope")
    if a.terms:
        terms_path = Path(a.terms)
        if not terms_path.is_file():
            raise SystemExit(f"[split] terminology file not found: {terms_path}")
        try:
            terms = load_canonical_terminology(terms_path)
        except ValueError as exc:
            raise SystemExit(f"[split] {exc}") from exc
    else:
        terms = load_terms(state)
    segs = state["segments"]
    context_registry = _state_context_registry(state)
    size = resolve_split_size(state, a.size)
    budget = getattr(a, "char_budget", 0) or 0
    scope = get_check_scope(state)
    revision = build_split_revision(
        state,
        pre,
        terms,
        scope,
        size=size,
        char_budget=budget,
    )
    split_fingerprint = revision["split_fingerprint"]
    outdir = Path(a.outdir)
    protected_inputs = [Path(a.state), Path(a.errors)]
    if a.terms:
        protected_inputs.append(Path(a.terms))
    elif state.get("terms_path"):
        protected_inputs.append(Path(state["terms_path"]))
    conflicts = [
        path for path in protected_inputs if _path_is_within(path, outdir)
    ]
    if conflicts:
        raise SystemExit(
            "[split] output directory contains protected input(s): "
            + ", ".join(str(path) for path in conflicts)
        )
    if outdir.is_dir():
        try:
            current_manifest, _, _, _, _ = load_verified_generation(
                Path(a.state),
                Path(a.errors),
                outdir,
                state=state,
            )
        except Exception:
            current_manifest = None
        if (
            current_manifest is not None
            and current_manifest["split_fingerprint"] == split_fingerprint
        ):
            print(
                f"[split] existing generation is current; preserving module "
                f"outputs in {outdir}"
            )
            return
    pre_by_id = {e["id"]: e["issues"] for e in pre}
    precheck_by_id = {
        segment_id: with_precheck_refs(segment_id, issues)
        for segment_id, issues in pre_by_id.items()
    }
    grouped = _group_chunk_terms(terms)
    titems = [(src, senses) for src, senses in grouped.items() if len(src) >= 2]
    titems.sort(key=lambda x: -len(x[0]))

    # near-term suggester (TF-IDF over TB)：精确匹配漏的"差一两字"变体名 → term_near 参考
    # 多义词条取第一个候选译法作代表值（term_near 只是参考线索，不需要区分语义）
    tn_pairs = [(src, senses[0]["target"]) for src, senses in grouped.items() if senses]
    tn_idx = _tn_build([p[0] for p in tn_pairs], [p[1] for p in tn_pairs]) if tn_pairs else None

    # Only segments with the same effective review projection may share results.
    groups = {}
    for seg in segs:
        if module_review_equivalence_key is not None:
            equivalence = tuple(
                module_review_equivalence_key(
                    seg,
                    module,
                    context_registry,
                    precheck=precheck_by_id.get(seg["id"], []),
                )
                for module in required_modules(state)
            )
        else:
            equivalence = (
                seg.get("source", ""),
                current_target(seg),
                bool(seg.get("protected")),
                seg.get("input_status", "ready"),
                seg.get("content_type"),
                seg.get("text_type_context"),
                seg.get("context_note"),
                json.dumps(
                    precheck_by_id.get(seg["id"], []),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        groups.setdefault(equivalence, []).append(seg)
    reps, dedup_map = [], {}
    for gsegs in groups.values():          # dict 保序：组按首次出现序
        rep = min(gsegs, key=lambda s: s["id"])
        reps.append(rep)                    # 存 seg 本身，省一张 id→seg 表
        dedup_map[rep["id"]] = [s["id"] for s in gsegs]

    dedup_payload = {str(k): v for k, v in dedup_map.items()}

    # 将代表段分块。--char-budget>0 时按源文和译文总字符数切分，--size 仍是段数上限；
    # 否则按固定段数切分。较小的块可限制单次检查量、缩小失败影响并细化续跑。
    # 断点按 chunk_NN.<module>.json 跳过已完成块。
    if budget > 0:
        parts, cur, curv = [], [], 0
        for rep in reps:
            w = len(rep.get("source", "")) + len(current_target(rep))
            if cur and (curv + w > budget or len(cur) >= size):
                parts.append(cur)
                cur, curv = [], 0
            cur.append(rep)
            curv += w
        if cur:
            parts.append(cur)
    else:
        parts = [reps[i:i + size] for i in range(0, len(reps), size)]
    nchunks = len(parts)
    vols = []
    chunk_payloads = []
    for ci, part in enumerate(parts):
        rows = []
        for seg in part:
            src_txt = seg.get("source", "")
            hits = _term_hits(src_txt, titems)
            near = _tn_suggest(tn_idx, src_txt, exclude={h["source"] for h in hits}) \
                if tn_idx else []
            row_payload = {
                "id": seg["id"],
                "segment_key": seg.get("segment_key"),
                "source": src_txt,
                "target": current_target(seg),
                "input_status": seg.get("input_status", "ready"),
                "input_block_reasons": seg.get("input_block_reasons", []),
                "input_warnings": seg.get("input_warnings", []),
                "content_type": seg.get("content_type"),
                "text_type_context": seg.get("text_type_context"),
                "context_note": seg.get("context_note"),
                "source_ref": seg.get("source_ref"),
                "protected": bool(seg.get("protected")),
                "protected_reason": seg.get("protected_reason"),
                "kind": _seg_kind(src_txt),
                "precheck": precheck_by_id.get(seg["id"], []),
                "term_hits": hits,
                "term_near": near,
                "protected_texts": seg.get("protected_texts", []),
                "context": copy.deepcopy(seg.get("context")),
                "resolved_constraints": copy.deepcopy(
                    seg.get("resolved_constraints", [])
                ),
                "segment_revision_digest": seg.get("segment_revision_digest"),
                "module_review_equivalence_keys": {
                    module: (
                        module_review_equivalence_key(
                            seg,
                            module,
                            context_registry,
                            precheck=precheck_by_id.get(seg["id"], []),
                        )
                        if module_review_equivalence_key is not None
                        else None
                    )
                    for module in required_modules(state)
                },
            }
            rows.append(row_payload)
        vols.append(sum(len(r["source"]) + len(r["target"]) for r in rows))
        chunk_payloads.append(
            add_chunk_payload_digest({
                "chunk_id": ci,
                "iteration": state.get("iteration", 0),
                "state_fingerprint": revision["state_fingerprint"],
                "split_fingerprint": split_fingerprint,
                "review_policy": review_policy,
                "resolved_context_descriptors": copy.deepcopy(
                    context_registry or {}
                ),
                "segments": rows,
            })
        )

    terms_path = Path(a.terms) if a.terms else None
    if terms_path is None and state.get("terms_path"):
        terms_path = Path(state["terms_path"])
    input_references = {
        "state": make_path_reference(Path(a.state), outdir.parent),
        "precheck": make_path_reference(Path(a.errors), outdir.parent),
        "terms": (
            make_path_reference(terms_path, outdir.parent)
            if terms_path is not None
            else None
        ),
        "terms_mode": "override" if a.terms else "state",
    }
    manifest = build_split_manifest(
        revision,
        chunks=chunk_payloads,
        dedup_map=dedup_payload,
        input_references=input_references,
    )
    outdir.parent.mkdir(parents=True, exist_ok=True)
    old_iteration = "legacy"
    old_fingerprint = "unknown"
    old_manifest_path = outdir / "split_manifest.json"
    if old_manifest_path.is_file():
        try:
            old_manifest = load(old_manifest_path)
            old_iteration = old_manifest.get("iteration", old_iteration)
            old_fingerprint = str(
                old_manifest.get("split_fingerprint") or old_fingerprint
            )[:8]
        except Exception:
            pass
    archive_label = f"iter_{old_iteration}_{old_fingerprint}"
    with tempfile.TemporaryDirectory(
        dir=outdir.parent,
        prefix=f".{outdir.name}.generation.",
    ) as staging_name:
        staging = Path(staging_name)
        write_json_atomic(staging / "dedup_map.json", dedup_payload)
        for chunk in chunk_payloads:
            write_json_atomic(staging / f"chunk_{chunk['chunk_id']:02d}.json", chunk)
        write_json_atomic(staging / "split_manifest.json", manifest)

        def validate_current_inputs():
            live_state = load(a.state)
            live_pre = normalize_check_entries(
                load(a.errors),
                label=Path(a.errors).name,
                review_policy=get_review_policy(live_state),
            )
            validate_scope_entries(
                live_state,
                live_pre,
                issues_key="issues",
                label=Path(a.errors).name,
            )
            if a.terms:
                live_terms = load_canonical_terminology(Path(a.terms))
            else:
                live_terms = load_terms(live_state)
            validate_live_manifest(
                manifest,
                live_state,
                live_pre,
                live_terms,
                get_check_scope(live_state),
            )

        archived = publish_generation(
            staging,
            outdir,
            archive_label=archive_label,
            pre_publish=validate_current_inputs,
        )
    if archived is not None:
        print(f"[split] archived stale generation → {archived}")
    dup = len(segs) - len(reps)
    mode = f"char-budget {budget} (cap {size})" if budget > 0 else f"size {size}"
    print(f"[split] {len(segs)} segments -> {len(reps)} unique (deduped {dup}) "
          f"-> {nchunks} chunks ({mode}) in {outdir}")
    print(f"[split] seg-counts: {[len(p) for p in parts]}")
    print(f"[split] src+tgt chars/chunk: {vols}")


def _cmd_merge_unlocked(a, state: dict, outdir: Path):
    state_segments = state["segments"]
    state_by_id = {segment["id"]: segment for segment in state_segments}
    ids = [segment["id"] for segment in state_segments]
    protected_ids = {
        segment["id"]
        for segment in state_segments
        if segment.get("protected") or segment.get("input_status") == "blocked"
    }
    output_path = Path(a.out)
    bound_results = requires_bound_artifacts(state)
    contract_path = result_contract_path(output_path)
    try:
        validate_artifact_paths(
            {
                "merged results": output_path,
                **(
                    {"merged result contract": contract_path}
                    if bound_results
                    else {}
                ),
            },
            {
                "state": Path(a.state),
                "precheck": Path(a.errors),
                **state_reference_paths(state),
                **{
                    f"chunks/{path.name}": path
                    for path in outdir.iterdir()
                    if path.is_file()
                },
            },
            context="merge",
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(f"[merge] {exc}") from exc
    try:
        manifest, chunks, dedup_map, pre, _ = _load_verified_generation_unlocked(
            Path(a.state),
            Path(a.errors),
            outdir,
            state=state,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(f"[merge] {exc}") from exc
    pre_by_id = {entry["id"]: entry["issues"] for entry in pre}
    chunk_contexts = {}
    chunk_by_id = {}
    for base in chunks:
        chunk_by_id[base["chunk_id"]] = base
        for segment in base["segments"]:
            chunk_contexts[segment["id"]] = segment

    trusted_outputs = {}
    if bound_results:
        job = Path(a.state).parent
        expected_outdir = job / "chunks"
        if outdir.resolve() != expected_outdir.resolve():
            raise SystemExit(
                "[merge] current jobs require the bound job/chunks directory"
            )
        checked, problems = build_trusted_module_outputs(job, state)
        if problems:
            raise SystemExit(
                "[merge] bound module validation failed: "
                + "; ".join(problems[:50])
            )
        for chunk_id, (_, _, output) in checked.items():
            trusted_outputs[chunk_id] = output

    merged = {}
    files = []
    coverage_problems = []
    expected_output_names = {
        f"chunk_{chunk_id:02d}.out.json" for chunk_id in chunk_by_id
    }
    actual_output_paths = {
        path.name: path
        for path in outdir.iterdir()
        if path.is_file() and re.fullmatch(r"chunk_\d+\.out\.json", path.name)
    }
    extra_outputs = sorted(set(actual_output_paths) - expected_output_names)
    if extra_outputs:
        coverage_problems.append(f"unexpected chunk outputs {extra_outputs}")
    for chunk_id, base in sorted(chunk_by_id.items()):
        name = f"chunk_{chunk_id:02d}.out.json"
        f = actual_output_paths.get(name)
        if f is None:
            coverage_problems.append(f"{name}: MISSING")
            continue
        files.append(f)
        entries = load_module_output(
            f,
            base,
            "merged",
            state,
            allow_internal_provenance=bound_results,
            require_internal_provenance=bound_results,
        )
        if bound_results:
            trusted_entries = trusted_outputs.get(chunk_id)
            if entries != trusted_entries:
                coverage_problems.append(
                    f"{name}: content differs from current bound module outputs"
                )
            entries = trusted_entries or []
        validate_scope_entries(
            state, entries, issues_key="issues", label=f.name
        )
        expected_ids = {segment["id"] for segment in base["segments"]}
        actual_ids = {entry["id"] for entry in entries}
        missing_ids = sorted(expected_ids - actual_ids)
        extra_ids = sorted(actual_ids - expected_ids)
        if missing_ids or extra_ids:
            coverage_problems.append(
                f"{name}: id coverage missing={missing_ids} extra={extra_ids}"
            )
        for entry in entries:
            merged[entry["id"]] = copy.deepcopy(entry["issues"])
    if coverage_problems:
        raise SystemExit("[merge] " + "; ".join(coverage_problems))

    representative_by_id = {}
    for rep_str, group in dedup_map.items():
        rep = int(rep_str)
        if rep in merged:
            for segment_id in group:
                merged[segment_id] = copy.deepcopy(merged[rep])
                representative_by_id[segment_id] = rep

    reinstated = 0
    for i in ids:
        if i in protected_ids:
            continue
        if i not in merged:
            continue
        have = {issue.get("category") for issue in merged[i]}
        for issue in pre_by_id.get(i, []):
            if (
                issue.get("category") in _DETERMINISTIC_PRECHECK
                and issue.get("category") not in have
            ):
                restored = (
                    _mark_machine_precheck(issue)
                    if bound_results
                    else copy.deepcopy(issue)
                )
                merged[i].append(restored)
                reinstated += 1

    missing = [i for i in ids if i not in merged and i not in protected_ids]
    unexpected = sorted(set(merged) - set(ids))
    if missing or unexpected:
        raise SystemExit(
            f"[merge] result coverage missing={missing} unexpected={unexpected}"
        )
    out = []
    for i in ids:
        representative = representative_by_id.get(i, i)
        context = chunk_contexts.get(representative, {})
        original = state_by_id[i]
        protected_texts = original.get(
            "protected_texts", context.get("protected_texts", [])
        )
        segment = {
            "id": i,
            "source": original.get("source", ""),
            "target": current_target(original),
            "kind": context.get("kind", _seg_kind(original.get("source", ""))),
            "term_hits": context.get("term_hits", []),
            "protected_texts": protected_texts,
        }
        if i in protected_ids:
            issues = []
        elif i in merged:
            issues = merged[i]
        else:
            issues = copy.deepcopy(pre_by_id.get(i, []))
            if bound_results:
                issues = [_mark_machine_precheck(issue) for issue in issues]
        out.append(
            build_segment_result(
                segment,
                issues,
                allow_internal_provenance=bound_results,
                require_internal_provenance=bound_results,
                review_policy=get_review_policy(state),
                target_form_policy=load_target_form_policy(state),
            )
        )

    validate_scope_entries(state, out, issues_key="errors", label=Path(a.out).name)
    if bound_results:
        with tempfile.TemporaryDirectory(
            dir=output_path.parent,
            prefix=f".{output_path.name}.merge.",
        ) as staging_name:
            staging = Path(staging_name)
            staged_output = staging / output_path.name
            staged_contract = staging / contract_path.name
            write_json_atomic(staged_output, out)
            write_json_atomic(
                staged_contract,
                build_result_contract(manifest, out),
            )
            publish_replacement_transaction(
                [
                    (staged_output, output_path),
                    (staged_contract, contract_path),
                ]
            )
    else:
        write_json_atomic(output_path, out)
    cov = sum(1 for i in ids if i in merged)
    print(f"[merge] {len(files)} chunk outputs -> {a.out}")
    if reinstated:
        print(f"[merge] restored {reinstated} required machine pre-check issues")
    print(f"[merge] covered {cov}/{len(ids)} ids after copying duplicate results; MISSING {len(missing)}: {missing[:20]}")


def cmd_merge(a):
    outdir = Path(a.outdir)
    with generation_lock(outdir, exclusive=False):
        state = load(a.state)
        require_current_job_runtime(state, "merge")
        _cmd_merge_unlocked(a, state, outdir)


_ACCURACY_OWNED = {"Mistranslation", "Omission", "Addition", "Untranslated"}
_DETERMINISTIC_PRECHECK = {"Untranslated"}
_PRECHECK_REVIEW_CATEGORIES = {
    "Markup",
    "Length",
    "Locale convention",
    "Company style",
    "Inconsistency",
    "Other",
}
_MODULE_ALLOWED_CATEGORIES = {
    "terminology": _PRECHECK_REVIEW_CATEGORIES
    | {"Terminology"},
    "precheck_review": _PRECHECK_REVIEW_CATEGORIES,
    "accuracy": {"Mistranslation", "Omission", "Addition", "Untranslated"},
    "grammar": {"Grammar", "Spelling", "Punctuation"},
    "naturalness": {
        "Audience appropriateness",
        "Culture specific reference",
        "Unidiomatic",
    },
    "proper_names": {"Mistranslation", "Culture specific reference"},
}


def _chunk_idxs(outdir: Path):
    return sorted(int(re.fullmatch(r"chunk_(\d+)", p.stem).group(1))
                  for p in outdir.glob("chunk_*.json")
                  if re.fullmatch(r"chunk_(\d+)", p.stem))


def _normalize_module_output(
    arr,
    path,
    *,
    allow_internal_provenance: bool = False,
    require_internal_provenance: bool = False,
    review_policy: dict | None = None,
):
    return normalize_check_entries(
        arr,
        label=Path(path).name,
        allow_internal_provenance=allow_internal_provenance,
        require_internal_provenance=require_internal_provenance,
        review_policy=review_policy,
    )


MODULE_OUTPUT_CONTRACT_VERSION = 2
MODULE_RECEIPT_SCHEMA = "lqe.module-publication-receipt"
MODULE_RECEIPT_VERSION = 1


def module_receipt_path(module_path: Path) -> Path:
    path = Path(module_path)
    return path.with_name(f"{path.stem}.receipt.json")


def _normalize_review_provenance(value: object) -> dict:
    required_fields = {
        "review_packet_digest",
        "selected_evidence_index_digest",
        "worker_receipt",
    }
    if not isinstance(value, dict):
        raise CheckFormatError("module receipt review provenance is invalid")
    actual_fields = set(value)
    if actual_fields not in (
        required_fields,
        required_fields | {"migration_receipt"},
    ):
        raise CheckFormatError("module receipt review provenance is invalid")
    for field in ("review_packet_digest", "selected_evidence_index_digest"):
        digest = value.get(field)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise CheckFormatError(f"module receipt {field} is invalid")
    worker = value.get("worker_receipt")
    if worker is not None:
        if not isinstance(worker, dict) or set(worker) != {"worker_id", "run_id"}:
            raise CheckFormatError("module receipt worker_receipt is invalid")
        for field in ("worker_id", "run_id"):
            text = worker.get(field)
            if (
                not isinstance(text, str)
                or not text
                or text != text.strip()
                or "\x00" in text
            ):
                raise CheckFormatError(
                    f"module receipt worker_receipt.{field} is invalid"
                )
    migration = value.get("migration_receipt")
    if migration is not None:
        if (
            not isinstance(migration, dict)
            or migration.get("schema")
            != "lqe.compact-draft-migration-receipt"
            or migration.get("version") != 1
            or not isinstance(migration.get("receipt_digest"), str)
            or re.fullmatch(r"[0-9a-f]{64}", migration["receipt_digest"])
            is None
        ):
            raise CheckFormatError("module receipt migration_receipt is invalid")
        migration_payload = copy.deepcopy(migration)
        migration_digest = migration_payload.pop("receipt_digest")
        if canonical_digest(migration_payload) != migration_digest:
            raise CheckFormatError(
                "module receipt migration_receipt digest mismatch"
            )
    return copy.deepcopy(value)


def build_module_receipt(
    payload: dict,
    manifest: dict,
    destination: Path,
    *,
    review_provenance: dict | None = None,
) -> dict:
    receipt = {
        "schema": MODULE_RECEIPT_SCHEMA,
        "version": MODULE_RECEIPT_VERSION,
        "module_output": Path(destination).name,
        "manifest_digest": manifest["manifest_digest"],
        "split_fingerprint": payload["split_fingerprint"],
        "chunk_payload_digest": payload["chunk_payload_digest"],
        "module": payload["module"],
        "chunk_id": payload["chunk_id"],
        "module_output_digest": canonical_digest(payload),
    }
    if review_provenance is not None:
        receipt["review_provenance"] = _normalize_review_provenance(
            review_provenance
        )
    receipt["receipt_digest"] = canonical_digest(receipt)
    return receipt


def validate_module_receipt(
    receipt_path: Path,
    payload: dict,
    manifest: dict,
    destination: Path,
) -> None:
    try:
        actual = load(receipt_path)
    except FileNotFoundError as exc:
        raise CheckFormatError(
            f"{receipt_path.name}: publication receipt is missing"
        ) from exc
    if not isinstance(actual, dict):
        raise CheckFormatError(
            f"{receipt_path.name}: publication receipt is invalid"
        )
    review_provenance = actual.get("review_provenance")
    expected = build_module_receipt(
        payload,
        manifest,
        destination,
        review_provenance=review_provenance,
    )
    if actual != expected:
        raise CheckFormatError(
            f"{receipt_path.name}: publication receipt mismatch"
        )


def build_module_output(
    base: dict,
    module: str,
    entries: object,
    *,
    label: str,
    allow_internal_provenance: bool = False,
    require_internal_provenance: bool = False,
    review_policy: dict | None = None,
) -> dict:
    if review_policy is None and isinstance(base, dict):
        review_policy = base.get("review_policy")
    normalized = _normalize_module_output(
        entries,
        label,
        allow_internal_provenance=allow_internal_provenance,
        require_internal_provenance=require_internal_provenance,
        review_policy=review_policy,
    )
    return {
        "contract_version": MODULE_OUTPUT_CONTRACT_VERSION,
        "module": module,
        "chunk_id": base["chunk_id"],
        "split_fingerprint": base["split_fingerprint"],
        "chunk_payload_digest": base["payload_digest"],
        "entries_digest": canonical_digest(normalized),
        "entries": normalized,
    }


def load_module_output(
    path: Path,
    base: dict,
    module: str,
    state: dict,
    *,
    allow_internal_provenance: bool = False,
    require_internal_provenance: bool = False,
    publication_manifest: dict | None = None,
) -> list[dict]:
    payload = load(path)
    if isinstance(payload, list):
        if requires_bound_artifacts(state):
            raise CheckFormatError(
                f"{path.name}: bound module output envelope is required"
            )
        return _normalize_module_output(
            payload,
            path,
            allow_internal_provenance=allow_internal_provenance,
            require_internal_provenance=require_internal_provenance,
            review_policy=get_review_policy(state),
        )
    if not isinstance(payload, dict):
        raise CheckFormatError(
            f"{path.name}: module output must be a bound object"
        )
    expected_fields = {
        "contract_version",
        "module",
        "chunk_id",
        "split_fingerprint",
        "chunk_payload_digest",
        "entries_digest",
        "entries",
    }
    if set(payload) != expected_fields:
        raise CheckFormatError(
            f"{path.name}: module output envelope fields are invalid"
        )
    expected = {
        "contract_version": MODULE_OUTPUT_CONTRACT_VERSION,
        "module": module,
        "chunk_id": base["chunk_id"],
        "split_fingerprint": base["split_fingerprint"],
        "chunk_payload_digest": base["payload_digest"],
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise CheckFormatError(
                f"{path.name}: stale module output ({field} mismatch)"
            )
    normalized = _normalize_module_output(
        payload["entries"],
        path,
        allow_internal_provenance=allow_internal_provenance,
        require_internal_provenance=require_internal_provenance,
        review_policy=get_review_policy(state),
    )
    if payload["entries_digest"] != canonical_digest(normalized):
        raise CheckFormatError(
            f"{path.name}: module entries digest mismatch"
        )
    if publication_manifest is not None:
        validate_module_receipt(
            module_receipt_path(path),
            payload,
            publication_manifest,
            path,
        )
    return normalized


def _module_issue_problem(state: dict, module: str, issue: dict) -> str | None:
    problem = scope_issue_problem(state, issue)
    if problem:
        return problem
    category = issue.get("category")
    allowed = _MODULE_ALLOWED_CATEGORIES.get(module)
    if allowed is not None and category not in allowed:
        return f"{module} cannot own category {category!r}"
    if (
        state.get("job_runtime_contract_version") == 2
        and module == "terminology"
        and issue.get("needs_confirmation") is True
        and issue.get("resolution_status") not in {
            "reference_allowed",
            "human_choice_required",
            "conflict",
        }
    ):
        return (
            "terminology findings that need confirmation must declare whether "
            "a reference is allowed, a human choice is required, or evidence conflicts"
        )
    return None


def _precheck_provenance_problem(
    original_issues: list[dict], reviewed_issues: list[dict]
) -> str | None:
    used: set[int] = set()
    for reviewed in reviewed_issues:
        reviewed_ref = reviewed.get("precheck_ref")
        if not isinstance(reviewed_ref, str) or not reviewed_ref:
            return "precheck provenance requires precheck_ref"
        match = None
        for index, original in enumerate(original_issues):
            if index in used:
                continue
            if original.get("precheck_ref") != reviewed_ref:
                continue
            if original.get("category") != reviewed.get("category"):
                continue
            reviewed_edit = reviewed.get("edit")
            if reviewed_edit is not None and reviewed_edit != original.get("edit"):
                continue
            match = index
            break
        if match is None:
            return (
                "precheck provenance mismatch for category "
                f"{reviewed.get('category')!r}"
            )
        original = original_issues[match]
        if original.get("category") == "Terminology":
            if reviewed.get("term_source") != original.get("term_source"):
                return "reviewed Terminology issue must inherit term_source"
            if reviewed.get("expected_targets") != original.get("expected_targets"):
                return "reviewed Terminology issue must inherit expected_targets"
            original_spans = original.get("term_spans")
            reviewed_spans = reviewed.get("term_spans")
            if not isinstance(original_spans, dict) or not isinstance(
                reviewed_spans, dict
            ):
                return "reviewed Terminology issue requires term_spans"
            if reviewed_spans.get("source") != original_spans.get("source"):
                return (
                    "reviewed Terminology issue must inherit machine source spans"
                )
        used.add(match)
    return None


def _check_modules_unlocked(
    job: Path,
    state: dict,
) -> tuple[dict, list[str], tuple[str, ...], tuple[str, ...]]:
    outdir = job / "chunks"
    required = required_modules(state)
    optional = optional_modules(state)
    disabled = disabled_modules(state)
    chunks = {}
    problems = []
    try:
        manifest, base_chunks, _, _, _ = _load_verified_generation_unlocked(
            job / "state.json",
            job / "errors_precheck.json",
            outdir,
            state=state,
        )
    except Exception as error:
        return chunks, [f"split contract: {error}"], required, optional
    base_by_id = {base["chunk_id"]: base for base in base_chunks}
    idxs = sorted(base_by_id)

    for ci in idxs:
        base_path = outdir / f"chunk_{ci:02d}.json"
        try:
            base = base_by_id[ci]
            revision_problem = _chunk_revision_problem(state, base, manifest)
            if revision_problem:
                problems.append(f"{base_path.name}: {revision_problem}")
            ids = [segment["id"] for segment in base["segments"]]
            precheck_by_id = {
                segment["id"]: (
                    segment.get("precheck")
                    if isinstance(segment.get("precheck"), list)
                    else []
                )
                for segment in base["segments"]
            }
        except Exception as error:
            problems.append(f"{base_path.name}: invalid chunk ({error})")
            continue

        expected = set(ids)
        module_entries = {}
        for module in disabled:
            path = outdir / f"chunk_{ci:02d}.{module}.json"
            if path.exists():
                problems.append(
                    f"{path.name}: scope conflict: module {module!r} is disabled"
                )
        for module in required + optional:
            path = outdir / f"chunk_{ci:02d}.{module}.json"
            if not path.exists():
                if module in required:
                    problems.append(f"{path.name}: MISSING")
                continue
            try:
                entries = load_module_output(
                    path,
                    base,
                    module,
                    state,
                    publication_manifest=(
                        manifest if requires_bound_artifacts(state) else None
                    ),
                )
            except Exception as error:
                problems.append(str(error))
                continue

            actual = {entry["id"] for entry in entries}
            missing = expected - actual
            unexpected = actual - expected
            if module in required and missing:
                problems.append(
                    f"{path.name}: missing ids {sorted(missing)[:10]}"
                )
            if unexpected:
                problems.append(
                    f"{path.name}: unexpected ids {sorted(unexpected)[:10]}"
                )
            for entry in entries:
                if module in {"precheck_review", "terminology"}:
                    reviewed_issues = (
                        entry["issues"]
                        if module == "precheck_review"
                        else [
                            issue
                            for issue in entry["issues"]
                            if issue.get("precheck_ref") is not None
                        ]
                    )
                    provenance_problem = _precheck_provenance_problem(
                        precheck_by_id.get(entry["id"], []),
                        reviewed_issues,
                    )
                    if provenance_problem:
                        problems.append(
                            f"{path.name}: {provenance_problem} "
                            f"for id {entry['id']}"
                        )
                for issue in entry["issues"]:
                    problem = _module_issue_problem(state, module, issue)
                    if problem:
                        problems.append(f"{path.name}: scope conflict: {problem}")
            module_entries[module] = entries
        chunks[ci] = (base, ids, module_entries)
    return chunks, problems, required, optional


def _check_modules(
    job: Path,
) -> tuple[dict, list[str], tuple[str, ...], tuple[str, ...]]:
    outdir = job / "chunks"
    with generation_lock(outdir, exclusive=False):
        state = load(job / "state.json")
        return _check_modules_unlocked(job, state)


def _exit_check_problems(command: str, problems: list[str]):
    if not problems:
        return
    print(f"[{command}] FAIL ({len(problems)} problems):", file=sys.stderr)
    for problem in problems[:50]:
        print(f"  {problem}", file=sys.stderr)
    sys.exit(4)


def _issue_key(issue):
    return (
        issue.get("category"),
        issue.get("severity"),
        issue.get("comment"),
        issue.get("term_source"),
        json.dumps(issue.get("expected_targets"), ensure_ascii=False),
        json.dumps(
            issue.get("term_spans"),
            ensure_ascii=False,
            sort_keys=True,
        ),
        json.dumps(issue.get("edit"), ensure_ascii=False, sort_keys=True),
        issue.get("resolution_status"),
        json.dumps(issue.get("reason_codes"), ensure_ascii=False),
        json.dumps(
            issue.get("non_authorizing_evidence"),
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


def _mark_ai_reviewed(
    issue: dict,
    module: str,
    segment_id: int,
    precheck_issues: list[dict],
) -> dict:
    reviewed = copy.deepcopy(issue)
    precheck_ref = issue.get("precheck_ref")
    original = next(
        (
            candidate
            for candidate in precheck_issues
            if candidate.get("precheck_ref") == precheck_ref
            and candidate.get("category") == issue.get("category")
        ),
        None,
    )
    finding_origin = (
        "machine_precheck" if original is not None else "ai_module"
    )
    if original is not None:
        for field in ("term_source", "expected_targets"):
            if field in original:
                reviewed[field] = copy.deepcopy(original[field])
        original_spans = original.get("term_spans")
        if isinstance(original_spans, dict):
            reviewed_spans = reviewed.get("term_spans")
            reviewed_target_spans = (
                reviewed_spans.get("target")
                if isinstance(reviewed_spans, dict)
                else original_spans.get("target", [])
            )
            reviewed["term_spans"] = {
                "source": copy.deepcopy(original_spans.get("source", [])),
                "target": copy.deepcopy(reviewed_target_spans),
            }
    edit = issue.get("edit")
    if edit is None:
        edit_origin = None
    elif original is not None and edit == original.get("edit"):
        edit_origin = "machine_precheck"
    else:
        edit_origin = "ai_module"
    reviewed["review_provenance"] = {
        "finding_origin": finding_origin,
        "ai_reviewed": True,
        "ai_edited": False,
        "review_module": module,
        "reviewed_segment_id": segment_id,
        "edit_origin": edit_origin,
    }
    return reviewed


def _mark_machine_precheck(issue: dict) -> dict:
    restored = copy.deepcopy(issue)
    restored["review_provenance"] = {
        "finding_origin": "machine_precheck",
        "ai_reviewed": False,
        "ai_edited": False,
        "review_module": None,
        "reviewed_segment_id": None,
        "edit_origin": (
            "machine_precheck" if issue.get("edit") is not None else None
        ),
    }
    return restored


def _trusted_chunk_output(
    state: dict,
    base: dict,
    ids: list[int],
    module_entries: dict[str, list[dict]],
    required: tuple[str, ...],
    optional: tuple[str, ...],
) -> list[dict]:
    precheck_by_id = {
        segment["id"]: (
            segment.get("precheck")
            if isinstance(segment.get("precheck"), list)
            else []
        )
        for segment in base["segments"]
    }
    entries_by_module = {
        module: {entry["id"]: entry["issues"] for entry in entries}
        for module, entries in module_entries.items()
    }
    output = []
    for segment_id in ids:
        seen = set()
        issues = []
        for module in required + optional:
            for issue in entries_by_module.get(module, {}).get(segment_id, []):
                category = issue.get("category")
                if category in _ACCURACY_OWNED and module != "accuracy":
                    continue
                key = _issue_key(issue)
                if key in seen:
                    continue
                seen.add(key)
                issues.append(
                    _mark_ai_reviewed(
                        issue,
                        module,
                        segment_id,
                        precheck_by_id.get(segment_id, []),
                    )
                    if requires_bound_artifacts(state)
                    else copy.deepcopy(issue)
                )
        output.append({"id": segment_id, "issues": issues})
    return output


def build_trusted_module_outputs(
    job: Path,
    state: dict,
) -> tuple[dict[int, tuple[dict, list[int], list[dict]]], list[str]]:
    chunks, problems, required, optional = _check_modules_unlocked(job, state)
    if problems:
        return {}, problems
    return {
        chunk_id: (
            base,
            ids,
            _trusted_chunk_output(
                state,
                base,
                ids,
                module_entries,
                required,
                optional,
            ),
        )
        for chunk_id, (base, ids, module_entries) in chunks.items()
    }, []


def _cmd_merge_checks_unlocked(a, state: dict):
    job = Path(a.job)
    outdir = job / "chunks"
    chunks, problems, required, optional = _check_modules_unlocked(job, state)
    _exit_check_problems("merge-checks", problems)

    total = 0
    with tempfile.TemporaryDirectory(
        dir=outdir,
        prefix=".merge-checks.",
    ) as staging_name:
        staging = Path(staging_name)
        replacements = []
        for ci, (base, ids, module_entries) in chunks.items():
            output = _trusted_chunk_output(
                state,
                base,
                ids,
                module_entries,
                required,
                optional,
            )
            destination = outdir / f"chunk_{ci:02d}.out.json"
            staged = staging / destination.name
            payload = (
                build_module_output(
                    base,
                    "merged",
                    output,
                    label=destination.name,
                    allow_internal_provenance=True,
                    require_internal_provenance=True,
                )
                if requires_bound_artifacts(state)
                else output
            )
            write_json_atomic(staged, payload)
            replacements.append((staged, destination))
            total += len(output)
        publish_replacement_transaction(replacements)
    print(f"[merge-checks] {len(chunks)} chunks / {total} segments merged")


def cmd_merge_checks(a):
    job = Path(a.job)
    with generation_lock(job / "chunks", exclusive=False):
        state = load(job / "state.json")
        require_current_job_runtime(state, "merge-checks")
        _cmd_merge_checks_unlocked(a, state)


def cmd_validate_checks(a):
    chunks, problems, _, _ = _check_modules(Path(a.job))
    _exit_check_problems("validate-checks", problems)
    print(f"[validate-checks] OK: {len(chunks)} chunks")


def _cmd_reconcile_unlocked(a, state: dict):
    job = Path(a.job)
    outdir = job / "chunks"
    chunks, problems, _, _ = _check_modules_unlocked(job, state)
    _exit_check_problems("reconcile", problems)
    dropped = []
    reconciled = []
    for ci, (base, _, module_entries) in chunks.items():
        accuracy_issues = set()
        for entry in module_entries.get("accuracy", []):
            for issue in entry["issues"]:
                if issue.get("category") in _ACCURACY_OWNED:
                    accuracy_issues.add((entry["id"], _issue_key(issue)))

        output_path = outdir / f"chunk_{ci:02d}.out.json"
        if not output_path.exists():
            continue
        output = load_module_output(
            output_path,
            base,
            "merged",
            state,
            allow_internal_provenance=requires_bound_artifacts(state),
            require_internal_provenance=requires_bound_artifacts(state),
        )
        for entry in output:
            kept = []
            for issue in entry["issues"]:
                if (
                    issue.get("category") in _ACCURACY_OWNED
                    and (entry["id"], _issue_key(issue)) not in accuracy_issues
                ):
                    dropped.append({"chunk": ci, "id": entry["id"], **issue})
                else:
                    kept.append(issue)
            entry["issues"] = kept
        payload = (
            build_module_output(
                base,
                "merged",
                output,
                label=output_path.name,
                allow_internal_provenance=True,
                require_internal_provenance=True,
            )
            if requires_bound_artifacts(state)
            else output
        )
        reconciled.append((output_path, payload))

    archive = job / "reconcile_dropped.json"
    with tempfile.TemporaryDirectory(
        dir=job,
        prefix=".reconcile.",
    ) as staging_name:
        staging = Path(staging_name)
        replacements = []
        for destination, output in reconciled:
            staged = staging / destination.name
            write_json_atomic(staged, output)
            replacements.append((staged, destination))
        staged_archive = staging / archive.name
        write_json_atomic(staged_archive, dropped)
        replacements.append((staged_archive, archive))
        publish_replacement_transaction(replacements)
    print(f"[reconcile] dropped {len(dropped)} unconfirmed accuracy issues")


def cmd_reconcile(a):
    job = Path(a.job)
    with generation_lock(job / "chunks", exclusive=False):
        state = load(job / "state.json")
        require_current_job_runtime(state, "reconcile")
        _cmd_reconcile_unlocked(a, state)


def cmd_publish_module(a):
    job = Path(a.job)
    outdir = job / "chunks"
    raw_path = Path(a.input)
    with generation_lock(outdir, exclusive=True):
        state = load(job / "state.json")
        require_current_job_runtime(state, "publish-module")
        manifest, bases, _, _, _ = _load_verified_generation_unlocked(
            job / "state.json",
            job / "errors_precheck.json",
            outdir,
            state=state,
        )
        allowed = required_modules(state) + optional_modules(state)
        if a.module not in allowed:
            raise SystemExit(
                f"[publish-module] module {a.module!r} is not enabled"
            )
        if (
            a.module in required_modules(state)
            and not getattr(a, "_compact_review_validated", False)
        ):
            raise SystemExit(
                "[publish-module] required AI modules must be published through "
                "lqe_review.py publish with a current worker-context packet"
            )
        base = next(
            (item for item in bases if item["chunk_id"] == a.chunk),
            None,
        )
        if base is None:
            raise SystemExit(
                f"[publish-module] chunk {a.chunk} is not in the live generation"
            )
        if manifest["split_fingerprint"] != a.split_fingerprint:
            raise SystemExit(
                "[publish-module] stale task: split fingerprint mismatch"
            )
        if base["payload_digest"] != a.chunk_payload_digest:
            raise SystemExit(
                "[publish-module] stale task: chunk payload digest mismatch"
            )
        try:
            entries = _normalize_module_output(
                load(raw_path),
                raw_path,
                review_policy=get_review_policy(state),
            )
        except (OSError, ValueError) as exc:
            raise SystemExit(f"[publish-module] {exc}") from exc
        expected_ids = {segment["id"] for segment in base["segments"]}
        actual_ids = {entry["id"] for entry in entries}
        if actual_ids != expected_ids:
            raise SystemExit(
                "[publish-module] id coverage differs from chunk: "
                f"missing={sorted(expected_ids - actual_ids)} "
                f"extra={sorted(actual_ids - expected_ids)}"
            )
        precheck_by_id = {
            segment["id"]: (
                segment.get("precheck")
                if isinstance(segment.get("precheck"), list)
                else []
            )
            for segment in base["segments"]
        }
        for entry in entries:
            if a.module in {"precheck_review", "terminology"}:
                reviewed_issues = (
                    entry["issues"]
                    if a.module == "precheck_review"
                    else [
                        issue
                        for issue in entry["issues"]
                        if issue.get("precheck_ref") is not None
                    ]
                )
                problem = _precheck_provenance_problem(
                    precheck_by_id.get(entry["id"], []),
                    reviewed_issues,
                )
                if problem:
                    raise SystemExit(
                        f"[publish-module] {problem} for id {entry['id']}"
                    )
            for issue in entry["issues"]:
                problem = _module_issue_problem(state, a.module, issue)
                if problem:
                    raise SystemExit(f"[publish-module] {problem}")
        destination = outdir / f"chunk_{a.chunk:02d}.{a.module}.json"
        receipt_path = module_receipt_path(destination)
        try:
            validate_artifact_paths(
                {
                    "module output": destination,
                    "module publication receipt": receipt_path,
                },
                {
                    "module draft": raw_path,
                    "state": job / "state.json",
                    "precheck": job / "errors_precheck.json",
                    **state_reference_paths(state),
                },
                context="publish-module",
            )
            payload = build_module_output(
                base,
                a.module,
                entries,
                label=destination.name,
            )
            receipt = build_module_receipt(
                payload,
                manifest,
                destination,
            )
            with tempfile.TemporaryDirectory(
                dir=outdir,
                prefix=".publish-module.",
            ) as staging_name:
                staging = Path(staging_name)
                staged_output = staging / destination.name
                staged_receipt = staging / receipt_path.name
                write_json_atomic(staged_output, payload)
                write_json_atomic(staged_receipt, receipt)
                publish_replacement_transaction(
                    [
                        (staged_output, destination),
                        (staged_receipt, receipt_path),
                    ]
                )
        except (OSError, ValueError) as exc:
            raise SystemExit(f"[publish-module] {exc}") from exc
    print(f"[publish-module] {len(entries)} entries -> {destination}")


def cmd_split_half(a):
    """单个 chunk×module 反复失败时，把 chunk 二分成更小的检查任务。

    不重新运行 pre-check 或 term_hits，直接继承原 chunk 的结果。
    产出 chunk_NN_p1.json / chunk_NN_p2.json，并按模块继承断点。
    按 id 归属把已判条目分别转给 p1/p2 的 ckpt.jsonl——已判过的不会因为二分而白费，
    两个新任务读取断点后会跳过已完成条目，只检查各自剩余内容。"""
    job = Path(a.job)
    state = load(job / "state.json")
    require_current_job_runtime(state, "split-half")
    outdir = job / "chunks"
    ci = int(a.chunk)
    data = load(outdir / f"chunk_{ci:02d}.json")
    segs = data["segments"]
    mid = len(segs) // 2
    parts = [segs[:mid], segs[mid:]]
    id_sets = [{s["id"] for s in part} for part in parts]
    for i, part in enumerate(parts, start=1):
        p = outdir / f"chunk_{ci:02d}_p{i}.json"
        payload = {
            key: data[key]
            for key in (
                "chunk_id",
                "iteration",
                "state_fingerprint",
                "split_fingerprint",
            )
            if key in data
        }
        payload["part"] = i
        payload["segments"] = part
        write_json_atomic(p, add_chunk_payload_digest(payload))
    print(f"[split-half] chunk_{ci:02d}（{len(segs)}段）-> "
          + " + ".join(f"chunk_{ci:02d}_p{i}({len(p)}段)" for i, p in enumerate(parts, start=1)))

    if a.module:
        module = a.module
        old_ckpt = outdir / f"chunk_{ci:02d}.{module}.ckpt.jsonl"
        if old_ckpt.exists():
            carried = [0, 0]
            buckets = [[], []]
            for line in old_ckpt.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                e = json.loads(line)
                idx = 0 if e["id"] in id_sets[0] else 1
                buckets[idx].append(line)
            for i in (1, 2):
                new_ckpt = outdir / f"chunk_{ci:02d}_p{i}.{module}.ckpt.jsonl"
                lines = buckets[i - 1]
                if lines:
                    new_ckpt.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    carried[i - 1] = len(lines)
            print(f"[split-half] 继承 {module} 断点：p1 带 {carried[0]} 条、p2 带 {carried[1]} 条"
                  f"（原 {old_ckpt.name} 共 {carried[0] + carried[1]} 条）")
        else:
            print(f"[split-half] {module} 无既有断点（{old_ckpt.name} 不存在）")


def cmd_join_parts(a):
    """Combine checked part outputs into one module output file."""
    review_policy = get_review_policy(
        {"review_policy": {"mode": a.review_mode, "source": "legacy-tool"}}
    )
    combined = []
    for p in a.parts:
        combined.extend(
            _normalize_module_output(load(p), p, review_policy=review_policy)
        )
    write_json_atomic(Path(a.out), combined)
    print(f"[join-parts] {len(a.parts)} 份 -> {len(combined)} 条 -> {a.out}")


def cmd_ckpt_append(a):
    """Validate and append one check entry to a JSONL checkpoint."""
    p = Path(a.file)
    review_policy = get_review_policy(
        {"review_policy": {"mode": a.review_mode, "source": "legacy-tool"}}
    )
    entry = _normalize_module_output(
        [json.loads(a.entry)], p, review_policy=review_policy
    )[0]
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[ckpt-append] id={entry['id']} -> {p}")


def cmd_ckpt_finalize(a):
    """全部段判完后调一次：把 ckpt.jsonl 去重(同 id 取最后一次出现，处理断点重叠)、
    按 id 排序，合并成标准 JSON 数组。"""
    p = Path(a.jsonl)
    by_id = {}
    line_count = 0
    review_policy = get_review_policy(
        {"review_policy": {"mode": a.review_mode, "source": "legacy-tool"}}
    )
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            line_count += 1
            e = _normalize_module_output(
                [json.loads(line)], p, review_policy=review_policy
            )[0]
            by_id[e["id"]] = e
    out = [by_id[i] for i in sorted(by_id)]
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[ckpt-finalize] {len(out)} 条（去重前 {line_count}）-> {a.out}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("split")
    s.add_argument("--state", required=True)
    s.add_argument("--errors", required=True)
    s.add_argument("--terms", default=None)
    s.add_argument("--outdir", required=True)
    s.add_argument(
        "--size",
        type=int,
        default=None,
        help=(
            "maximum segments per chunk; defaults to 5 for context enforce "
            "jobs and 100 for off/shadow jobs"
        ),
    )
    s.add_argument("--char-budget", type=int, default=0,
                   help="按源文和译文总字符数分块；0 表示固定按 --size 分块，--size 始终是段数上限")
    s.set_defaults(fn=cmd_split)
    m = sub.add_parser("merge")
    m.add_argument("--state", required=True)
    m.add_argument("--errors", required=True)
    m.add_argument("--outdir", required=True)
    m.add_argument("--out", required=True)
    m.set_defaults(fn=cmd_merge)
    mc = sub.add_parser("merge-checks")
    mc.add_argument("--job", required=True)
    mc.set_defaults(fn=cmd_merge_checks)
    vc = sub.add_parser("validate-checks")
    vc.add_argument("--job", required=True)
    vc.set_defaults(fn=cmd_validate_checks)
    rc = sub.add_parser("reconcile")
    rc.add_argument("--job", required=True)
    rc.set_defaults(fn=cmd_reconcile)
    pm = sub.add_parser("publish-module")
    pm.add_argument("--job", required=True)
    pm.add_argument("--chunk", required=True, type=int)
    pm.add_argument("--module", required=True)
    pm.add_argument("--input", required=True, help="raw {id,issues} JSON array")
    pm.add_argument("--split-fingerprint", required=True)
    pm.add_argument("--chunk-payload-digest", required=True)
    pm.set_defaults(fn=cmd_publish_module)
    sh = sub.add_parser("split-half")
    sh.add_argument("--job", required=True)
    sh.add_argument("--chunk", required=True, help="原 chunk 序号（如 0、3）")
    sh.add_argument("--module", default=None)
    sh.set_defaults(fn=cmd_split_half)
    jp = sub.add_parser("join-parts")
    jp.add_argument("--parts", required=True, nargs="+", help="两个 part 的输出文件路径")
    jp.add_argument("--out", required=True)
    jp.add_argument("--review-mode", choices=["optimized", "full"], default="optimized")
    jp.set_defaults(fn=cmd_join_parts)
    ca = sub.add_parser("ckpt-append")
    ca.add_argument("--file", required=True, help="ckpt jsonl 路径")
    ca.add_argument("--entry", required=True, help="JSON object {id,issues}")
    ca.add_argument("--review-mode", choices=["optimized", "full"], default="optimized")
    ca.set_defaults(fn=cmd_ckpt_append)
    cf = sub.add_parser("ckpt-finalize")    # 全部段判完后调1次，去重+排序+转成正式 JSON 数组
    cf.add_argument("--jsonl", required=True)
    cf.add_argument("--out", required=True)
    cf.add_argument("--review-mode", choices=["optimized", "full"], default="optimized")
    cf.set_defaults(fn=cmd_ckpt_finalize)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
