#!/usr/bin/env python3
"""Build compact, module-specific review packets and publish sparse AI drafts.

The formal module artifacts are unchanged. Compact drafts prove which packet was
reviewed, list every reviewed id, and include only ids with findings. This script
expands them to the complete ``{id, issues}`` array before delegating publication
to ``lqe_chunk.py``.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import tempfile

from lqe_corrections import CheckFormatError, build_segment_result
from lqe_chunk import (
    _MODULE_ALLOWED_CATEGORIES,
    _load_verified_generation_unlocked,
    _module_issue_problem,
    _normalize_module_output,
    _precheck_provenance_problem,
    build_module_output,
    build_module_receipt,
    load_module_output,
    module_receipt_path,
)
from lqe_engine import (
    get_review_policy,
    read_json as load,
    require_current_job_runtime,
    required_modules,
)
from lqe_paths import (
    publish_replacement_transaction,
    state_reference_paths,
    validate_artifact_paths,
    write_json_atomic,
)
from lqe_split_contract import canonical_digest, generation_lock, publish_generation
from lqe_target_form import load_target_form_policy
from lqe_context_bundle import (
    ContextBundleError,
    WorkerContextBudgetError,
    build_context_bundle_set,
    build_selected_context_evidence_index,
    build_worker_context_manifest,
    load_project_context_assets,
    measure_complete_worker_input_bytes,
    validate_selected_context_evidence_index,
    verify_worker_context_manifest_resources,
)

try:
    from lqe_context import project_segment_for_module
except ImportError:  # legacy bootstrap
    project_segment_for_module = None


PACKET_SCHEMA = "lqe.review-packet"
PACKET_VERSION = 3
PACKET_MANIFEST_SCHEMA = "lqe.review-packet-manifest"
BATCH_PLAN_SCHEMA = "lqe.review-worker-batch-plan"
COMPACT_DRAFT_SCHEMA = "lqe.compact-module-draft"
COMPACT_DRAFT_VERSION = 1
MIGRATION_RECEIPT_SCHEMA = "lqe.compact-draft-migration-receipt"
MIGRATION_RECEIPT_VERSION = 1
REUSE_REPORT_SCHEMA = "lqe.compact-draft-reuse-report"
REUSE_REPORT_VERSION = 1
MAX_PACKETS_PER_WORKER = 4
MAX_REVIEW_TEXT_CHARS_PER_WORKER = 25_000
_DIGEST_PLACEHOLDER = "0" * 64
SELECTED_EVIDENCE_INDEX_PATH = "selected_evidence_index.json"
CHECKER_V2_INSTRUCTIONS = (
    Path(__file__).resolve().parents[1] / "references" / "check_modules_v2"
)

_BASE_FIELDS = (
    "id",
    "source",
    "target",
    "content_type",
    "text_type_context",
    "context_note",
    "kind",
    "protected_texts",
)
_TERM_MODULES = {"terminology"}
_PRECHECK_MODULES = {"terminology", "precheck_review"}
_SUPPORTED_MODULES = {
    "terminology",
    "precheck_review",
    "accuracy",
    "grammar",
    "naturalness",
}

_REUSE_INPUT_FIELDS = (
    "input_format",
    "sheet_name",
    "source_col",
    "target_col",
    "no_header",
    "headers",
)
_REUSE_CONTEXT_FIELDS = (
    "project",
    "language_pair",
    "source_lang",
    "target_lang",
    "profile_digest",
    "profile_overlay_digest",
    "project_asset_snapshot_digest",
    "capability_resolution_digest",
    "context_contract_version",
    "check_scope",
)


def _json_bytes(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _with_digest(payload: dict, field: str) -> dict:
    output = copy.deepcopy(payload)
    output.pop(field, None)
    output[field] = canonical_digest(output)
    return output


def _nonempty(value: object) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _normalize_worker_receipt(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"worker_id", "run_id"}:
        raise ValueError("worker_receipt must contain worker_id and run_id")
    output = {}
    for field in ("worker_id", "run_id"):
        text = value.get(field)
        if (
            not isinstance(text, str)
            or not text
            or text != text.strip()
            or "\x00" in text
        ):
            raise ValueError(f"worker_receipt.{field} must be a non-empty string")
        output[field] = text
    return output


def _digest_value(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _reuse_state_identity(state: object, label: str) -> dict:
    if not isinstance(state, dict):
        raise ValueError(f"{label} must be an object")
    input_sha256 = _digest_value(
        state.get("input_sha256"), f"{label}.input_sha256"
    )
    segments = state.get("segments")
    if not isinstance(segments, list):
        raise ValueError(f"{label}.segments must be an array")
    seen_ids = set()
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise ValueError(f"{label}.segments[{index}] must be an object")
        segment_id = segment.get("id")
        if type(segment_id) is not int or segment_id < 0:
            raise ValueError(f"{label}.segments[{index}].id is invalid")
        if segment_id in seen_ids:
            raise ValueError(f"{label}.segments contains duplicate ids")
        seen_ids.add(segment_id)
        if not isinstance(segment.get("source"), str):
            raise ValueError(f"{label}.segments[{index}].source is invalid")
        if not isinstance(segment.get("target"), str):
            raise ValueError(f"{label}.segments[{index}].target is invalid")
    return {
        "input_sha256": input_sha256,
        "input_selection": {
            field: copy.deepcopy(state.get(field)) for field in _REUSE_INPUT_FIELDS
        },
        "review_context": {
            field: copy.deepcopy(state.get(field))
            for field in _REUSE_CONTEXT_FIELDS
        },
        "segment_count": len(segments),
        "segment_identity_digest": canonical_digest(segments),
    }


def _matching_reuse_identity(source_state: dict, target_state: dict) -> dict:
    source = _reuse_state_identity(source_state, "source state")
    target = _reuse_state_identity(target_state, "target state")
    if source["input_sha256"] != target["input_sha256"]:
        raise ValueError("source and target input_sha256 differ")
    if source["input_selection"] != target["input_selection"]:
        raise ValueError("source and target input selection differ")
    if source["review_context"] != target["review_context"]:
        raise ValueError("source and target review context differ")
    if source["segment_count"] != target["segment_count"]:
        raise ValueError("source and target segment counts differ")
    if source["segment_identity_digest"] != target["segment_identity_digest"]:
        raise ValueError("source and target segment identity differs")
    return source


def _canonical_payload_digest(value: dict, digest_field: str, label: str) -> str:
    digest = _digest_value(value.get(digest_field), f"{label}.{digest_field}")
    payload = copy.deepcopy(value)
    payload.pop(digest_field, None)
    if canonical_digest(payload) != digest:
        raise ValueError(f"{label} {digest_field} mismatch")
    return digest


def _source_relative_path(root: Path, path: Path, label: str) -> str:
    root = root.resolve()
    path = path.resolve()
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"{label} must stay inside the source job") from exc


def _load_source_packet(
    source_job: Path,
    packet_path: Path,
    source_state: dict,
    validation_cache: dict | None = None,
) -> tuple[dict, str]:
    source_job = source_job.resolve()
    relative = _source_relative_path(source_job, packet_path, "source packet")
    if not relative.startswith("review_packets/"):
        raise ValueError("source packet must be under review_packets")
    packet = load(packet_path)
    if not isinstance(packet, dict):
        raise ValueError("source packet must be an object")
    if packet.get("schema") != PACKET_SCHEMA or packet.get("version") != PACKET_VERSION:
        raise ValueError("source packet schema/version is unsupported")
    packet_digest = _canonical_payload_digest(
        packet, "packet_digest", "source packet"
    )
    manifest_path = source_job / "review_packets" / "manifest.json"
    manifest = (
        validation_cache.get("manifest")
        if validation_cache is not None
        else None
    )
    if manifest is None:
        manifest = load(manifest_path)
        if not isinstance(manifest, dict):
            raise ValueError("source review packet manifest must be an object")
        _canonical_payload_digest(
            manifest, "manifest_digest", "source packet manifest"
        )
        if validation_cache is not None:
            validation_cache["manifest"] = manifest
    packet_manifest_key = Path(relative).relative_to("review_packets").as_posix()
    if manifest.get("packets", {}).get(packet_manifest_key) != packet_digest:
        raise ValueError("source packet is not bound by its packet manifest")

    reviewed_ids = packet.get("reviewed_ids")
    packet_segments = packet.get("segments")
    if (
        not isinstance(reviewed_ids, list)
        or not isinstance(packet_segments, list)
        or reviewed_ids != [segment.get("id") for segment in packet_segments]
    ):
        raise ValueError("source packet reviewed_ids/segments are inconsistent")
    state_by_id = {segment["id"]: segment for segment in source_state["segments"]}
    for packet_segment in packet_segments:
        if not isinstance(packet_segment, dict):
            raise ValueError("source packet segment is invalid")
        segment = state_by_id.get(packet_segment.get("id"))
        if segment is None:
            raise ValueError("source packet references an unknown segment")
        for field in ("source", "target", "segment_key", "segment_revision_digest"):
            if field in packet_segment and packet_segment[field] != segment.get(field):
                raise ValueError(
                    f"source packet segment {packet_segment.get('id')} {field} differs from state"
                )

    index_relative = packet.get("selected_evidence_index_path")
    if not isinstance(index_relative, str) or Path(index_relative).name != index_relative:
        raise ValueError("source packet selected evidence path is invalid")
    index_cache_key = f"selected-index:{index_relative}"
    index = (
        validation_cache.get(index_cache_key)
        if validation_cache is not None
        else None
    )
    if index is None:
        index = load(source_job / "review_packets" / index_relative)
        try:
            validate_selected_context_evidence_index(index)
        except ContextBundleError as exc:
            raise ValueError(f"source selected evidence index: {exc}") from exc
        if validation_cache is not None:
            validation_cache[index_cache_key] = index
    if index.get("index_digest") != packet.get("selected_evidence_index_digest"):
        raise ValueError("source packet selected evidence binding differs")
    return packet, relative


def _set_reused_issue_confirmation(issue: dict) -> None:
    issue["needs_confirmation"] = True
    issue["edit"] = None
    if issue.get("resolution_status") == "resolved":
        if issue.get("category") == "Terminology":
            issue["resolution_status"] = "reference_allowed"
        else:
            issue.pop("resolution_status", None)


def _optimized_reuse_issue(issue: dict, target_policy: dict) -> tuple[dict, bool]:
    output = copy.deepcopy(issue)
    changed = False
    if output.get("severity") == "Minor" and not target_policy["minor_edits_allowed"]:
        changed = output.get("needs_confirmation") is not True or output.get("edit") is not None
        _set_reused_issue_confirmation(output)
    return output, changed


def _text_occurrences(text: str, value: str) -> list[tuple[int, int]]:
    output = []
    start = text.find(value)
    while start >= 0:
        output.append((start, start + len(value)))
        start = text.find(value, start + 1)
    return output


def _realign_term_span_group(spans: object, text: str) -> tuple[object, bool]:
    if not isinstance(spans, list) or not spans:
        return spans, False
    if all(
        isinstance(span, dict)
        and type(span.get("start")) is int
        and type(span.get("end")) is int
        and isinstance(span.get("text"), str)
        and text[span["start"]:span["end"]] == span["text"]
        for span in spans
    ):
        return spans, False
    edge_rebuilt = []
    for span in spans:
        if not isinstance(span, dict) or not isinstance(span.get("text"), str):
            return spans, False
        value = span["text"]
        candidates = set()
        start = span.get("start")
        end = span.get("end")
        if type(start) is int and text[start:start + len(value)] == value:
            candidates.add((start, start + len(value)))
        if type(end) is int:
            candidate_start = end - len(value)
            if candidate_start >= 0 and text[candidate_start:end] == value:
                candidates.add((candidate_start, end))
        if len(candidates) != 1:
            edge_rebuilt = []
            break
        corrected_start, corrected_end = next(iter(candidates))
        edge_rebuilt.append(
            {"start": corrected_start, "end": corrected_end, "text": value}
        )
    if edge_rebuilt:
        edge_rebuilt.sort(
            key=lambda span: (span["start"], span["end"], span["text"])
        )
        if not any(
            current["start"] < previous["end"]
            for previous, current in zip(edge_rebuilt, edge_rebuilt[1:])
        ):
            return edge_rebuilt, edge_rebuilt != spans
    spans_by_text = {}
    for span in spans:
        if not isinstance(span, dict) or not isinstance(span.get("text"), str):
            return spans, False
        spans_by_text.setdefault(span["text"], []).append(span)
    rebuilt = []
    for value, original_spans in spans_by_text.items():
        occurrences = _text_occurrences(text, value)
        if len(occurrences) != len(original_spans):
            return spans, False
        rebuilt.extend(
            {"start": start, "end": end, "text": value}
            for start, end in occurrences
        )
    rebuilt.sort(key=lambda span: (span["start"], span["end"], span["text"]))
    if any(
        current["start"] < previous["end"]
        for previous, current in zip(rebuilt, rebuilt[1:])
    ):
        return spans, False
    return rebuilt, rebuilt != spans


def _precheck_semantic_identity(issue: object) -> object:
    if not isinstance(issue, dict):
        return None
    category = issue.get("category")
    if not isinstance(category, str) or not category:
        return None
    identity = {
        "category": category,
        "severity": copy.deepcopy(issue.get("severity")),
    }
    if category == "Terminology":
        term_spans = issue.get("term_spans")
        identity.update(
            {
                "term_source": copy.deepcopy(issue.get("term_source")),
                "expected_targets": copy.deepcopy(
                    issue.get("expected_targets")
                ),
                "source_term_spans": copy.deepcopy(
                    term_spans.get("source")
                    if isinstance(term_spans, dict)
                    else None
                ),
            }
        )
    else:
        identity["comment"] = copy.deepcopy(issue.get("comment"))
    return identity


def _equivalent_target_precheck_ref(
    old_ref: str,
    source_precheck: object,
    target_precheck: object,
) -> str | None:
    if not isinstance(source_precheck, list) or not isinstance(
        target_precheck, list
    ):
        return None
    source_matches = [
        issue
        for issue in source_precheck
        if isinstance(issue, dict) and issue.get("precheck_ref") == old_ref
    ]
    if len(source_matches) != 1:
        return None
    semantic_identity = _precheck_semantic_identity(source_matches[0])
    if semantic_identity is None:
        return None
    target_matches = [
        issue
        for issue in target_precheck
        if _precheck_semantic_identity(issue) == semantic_identity
    ]
    if len(target_matches) != 1:
        return None
    target_ref = target_matches[0].get("precheck_ref")
    if not isinstance(target_ref, str) or not target_ref:
        return None
    return target_ref


def _transform_reused_findings(
    findings: list[dict],
    target_policy: dict,
    packet: dict,
    *,
    source_precheck_by_id: dict[int, list[dict]] | None = None,
) -> tuple[list[dict], dict]:
    transformed = []
    counts = {
        "minor_policy": 0,
        "precheck_ref_rebound": 0,
        "precheck_edit_cleared": 0,
        "term_span_reanchored": 0,
    }
    packet_segment_by_id = {
        segment["id"]: segment for segment in packet.get("segments", [])
    }
    for finding in findings:
        issues = []
        for issue in finding["issues"]:
            normalized, changed = _optimized_reuse_issue(issue, target_policy)
            counts["minor_policy"] += int(changed)
            packet_segment = packet_segment_by_id.get(finding["id"], {})
            precheck_ref = normalized.get("precheck_ref")
            target_precheck = packet_segment.get("precheck", [])
            if precheck_ref and source_precheck_by_id is not None:
                rebound_ref = _equivalent_target_precheck_ref(
                    precheck_ref,
                    source_precheck_by_id.get(finding["id"], []),
                    target_precheck,
                )
                if rebound_ref is not None and rebound_ref != precheck_ref:
                    normalized["precheck_ref"] = rebound_ref
                    precheck_ref = rebound_ref
                    counts["precheck_ref_rebound"] += 1
            if precheck_ref and normalized.get("edit") is not None:
                original = next(
                    (
                        candidate
                        for candidate in target_precheck
                        if candidate.get("precheck_ref") == precheck_ref
                        and candidate.get("category") == normalized.get("category")
                    ),
                    None,
                )
                if isinstance(original, dict) and original.get("edit") is None:
                    _set_reused_issue_confirmation(normalized)
                    counts["precheck_edit_cleared"] += 1
            term_spans = normalized.get("term_spans")
            if isinstance(term_spans, dict):
                changed_groups = 0
                target_spans, target_changed = _realign_term_span_group(
                    term_spans.get("target"),
                    packet_segment.get("target", ""),
                )
                if target_changed:
                    term_spans["target"] = target_spans
                    changed_groups += 1
                if not precheck_ref:
                    source_spans, source_changed = _realign_term_span_group(
                        term_spans.get("source"),
                        packet_segment.get("source", ""),
                    )
                    if source_changed:
                        term_spans["source"] = source_spans
                        changed_groups += 1
                counts["term_span_reanchored"] += changed_groups
            issues.append(normalized)
        transformed.append({"id": finding["id"], "issues": issues})
    return transformed, counts


def _validate_reused_findings(
    state: dict,
    base: dict,
    packet: dict,
    findings: list[dict],
) -> list[dict]:
    normalized = _normalize_module_output(
        findings,
        Path(f"reused-{packet['module']}-{packet['chunk_id']}.json"),
        review_policy=packet["review_policy"],
    )
    segment_by_id = {segment["id"]: segment for segment in base["segments"]}
    precheck_by_id = {
        segment["id"]: (
            segment.get("precheck")
            if isinstance(segment.get("precheck"), list)
            else []
        )
        for segment in base["segments"]
    }
    for entry in normalized:
        segment = segment_by_id.get(entry["id"])
        if segment is None:
            raise ValueError(f"reused finding id {entry['id']} is outside target chunk")
        try:
            build_segment_result(
                segment,
                entry["issues"],
                review_policy=get_review_policy(state),
                target_form_policy=load_target_form_policy(state),
            )
        except CheckFormatError as exc:
            raise ValueError(
                f"reused finding id {entry['id']} violates correction contract: {exc}"
            ) from exc
        if packet["module"] in {"precheck_review", "terminology"}:
            reviewed_issues = (
                entry["issues"]
                if packet["module"] == "precheck_review"
                else [
                    issue
                    for issue in entry["issues"]
                    if issue.get("precheck_ref") is not None
                ]
            )
            problem = _precheck_provenance_problem(
                precheck_by_id.get(entry["id"], []), reviewed_issues
            )
            if problem:
                raise ValueError(f"{problem} for id {entry['id']}")
        for issue in entry["issues"]:
            problem = _module_issue_problem(state, packet["module"], issue)
            if problem:
                raise ValueError(problem)
    return normalized


def _owned_precheck(segment: dict, module: str) -> list[dict]:
    allowed = _MODULE_ALLOWED_CATEGORIES[module]
    raw = segment.get("precheck")
    if not isinstance(raw, list):
        return []
    return [
        copy.deepcopy(issue)
        for issue in raw
        if isinstance(issue, dict) and issue.get("category") in allowed
    ]


def _project_segment(
    segment: dict,
    module: str,
    context_registry: dict | None = None,
) -> dict | None:
    if segment.get("protected") is True or segment.get("input_status") == "blocked":
        return None

    precheck = _owned_precheck(segment, module)
    if module == "precheck_review" and not precheck:
        return None

    projected = {}
    for field in _BASE_FIELDS:
        value = segment.get(field)
        if field in {"id", "source", "target", "kind"} or _nonempty(value):
            projected[field] = copy.deepcopy(value)

    if project_segment_for_module is not None:
        context_projection = project_segment_for_module(
            segment, module, context_registry
        )
        if _nonempty(context_projection):
            projected["context_projection"] = context_projection
    elif _nonempty(segment.get("context")):
        projected["context_projection"] = copy.deepcopy(segment["context"])
    if _nonempty(segment.get("resolved_constraints")):
        projected["resolved_constraints"] = copy.deepcopy(
            segment["resolved_constraints"]
        )
    for field in (
        "segment_key",
        "input_status",
        "segment_revision_digest",
        "module_review_equivalence_keys",
    ):
        value = segment.get(field)
        if _nonempty(value):
            projected[field] = copy.deepcopy(value)

    if module in _PRECHECK_MODULES and precheck:
        projected["precheck"] = precheck
    if module in _TERM_MODULES:
        for field in ("term_hits", "term_near"):
            value = segment.get(field)
            if _nonempty(value):
                projected[field] = copy.deepcopy(value)
    return projected


def build_review_packet(
    base: dict,
    module: str,
    review_policy: dict | None = None,
) -> dict:
    if module not in _SUPPORTED_MODULES:
        raise ValueError(f"compact review does not support module {module!r}")

    review_policy = get_review_policy(
        {
            "review_policy": (
                review_policy
                if review_policy is not None
                else base.get("review_policy")
            )
        }
        if review_policy is not None or base.get("review_policy") is not None
        else {}
    )
    segments = []
    context_registry = base.get("resolved_context_descriptors")
    if not isinstance(context_registry, dict):
        context_registry = None
    protected = 0
    not_applicable = 0
    for segment in base["segments"]:
        if segment.get("protected") is True:
            protected += 1
            continue
        if segment.get("input_status") == "blocked":
            continue
        projected = _project_segment(segment, module, context_registry)
        if projected is None:
            not_applicable += 1
            continue
        segments.append(projected)

    reviewed_ids = [segment["id"] for segment in segments]
    review_text_chars = sum(
        len(segment.get("source") or "") + len(segment.get("target") or "")
        for segment in segments
    )
    payload = {
        "schema": PACKET_SCHEMA,
        "version": PACKET_VERSION,
        "module": module,
        "chunk_id": base["chunk_id"],
        "iteration": base.get("iteration", 0),
        "split_fingerprint": base["split_fingerprint"],
        "chunk_payload_digest": base["payload_digest"],
        "review_policy": review_policy,
        "reviewed_ids": reviewed_ids,
        "review_text_chars": review_text_chars,
        "segments": segments,
        "auto_empty": {
            "protected": protected,
            "blocked": sum(
                segment.get("input_status") == "blocked"
                for segment in base["segments"]
            ),
            "not_applicable": not_applicable,
        },
        "requires_ai": bool(reviewed_ids),
    }
    return _with_digest(payload, "packet_digest")


def _packet_name(packet: dict) -> str:
    return f"{packet['module']}/chunk_{packet['chunk_id']:02d}.json"


def _context_path(module: str, batch_id: int, filename: str) -> str:
    return f"context/{module}/batch_{batch_id:02d}/{filename}"


def _bind_packet_to_selected_evidence(packet: dict, index_digest: str) -> dict:
    payload = copy.deepcopy(packet)
    payload.pop("packet_digest", None)
    payload.update(
        {
            "selected_evidence_index_path": SELECTED_EVIDENCE_INDEX_PATH,
            "selected_evidence_index_digest": index_digest,
        }
    )
    return _with_digest(payload, "packet_digest")


def _worker_packet_basis(
    packet: dict,
    *,
    batch_id: int,
    context_bundle_set_digest: str,
) -> dict:
    basis = copy.deepcopy(packet)
    basis.pop("packet_digest", None)
    basis.update(
        {
            "worker_batch_id": batch_id,
            "context_bundle_set_digest": context_bundle_set_digest,
        }
    )
    return basis


def _worker_packet_payload(
    packet: dict,
    *,
    batch_id: int,
    context_bundle_set_digest: str,
) -> dict:
    basis = _worker_packet_basis(
        packet,
        batch_id=batch_id,
        context_bundle_set_digest=context_bundle_set_digest,
    )
    output = {
        **basis,
        "worker_packet_basis_digest": canonical_digest(basis),
        "worker_context_manifest_digest": _DIGEST_PLACEHOLDER,
        "packet_digest": _DIGEST_PLACEHOLDER,
    }
    return output


def _bind_packet_to_worker_context(
    packet: dict,
    *,
    batch_id: int,
    context_bundle_set_digest: str,
    worker_context_manifest_digest: str,
) -> dict:
    basis = _worker_packet_basis(
        packet,
        batch_id=batch_id,
        context_bundle_set_digest=context_bundle_set_digest,
    )
    return _with_digest(
        {
            **basis,
            "worker_packet_basis_digest": canonical_digest(basis),
            "worker_context_manifest_digest": worker_context_manifest_digest,
        },
        "packet_digest",
    )


def _batch_summary(
    module: str,
    batch_id: int,
    packets: list[dict],
    *,
    context_bundle_set: dict | None = None,
    worker_manifest: dict | None = None,
) -> dict:
    packet_bytes = sum(_json_bytes(packet) for packet in packets)
    worker_input_bytes = packet_bytes
    if context_bundle_set is not None and worker_manifest is not None:
        worker_input_bytes = measure_complete_worker_input_bytes(
            worker_manifest,
            context_bundle_set,
            packets,
        )
    summary = {
        "batch_id": batch_id,
        "packet_count": len(packets),
        "review_text_chars": sum(packet["review_text_chars"] for packet in packets),
        "packet_bytes": packet_bytes,
        "worker_input_bytes": worker_input_bytes,
        "packets": [
            {
                "path": _packet_name(packet),
                "chunk_id": packet["chunk_id"],
                "packet_digest": packet["packet_digest"],
                "review_text_chars": packet["review_text_chars"],
                "packet_bytes": _json_bytes(packet),
            }
            for packet in packets
        ],
    }
    if context_bundle_set is not None and worker_manifest is not None:
        summary.update(
            {
                "context_bundle_set_path": _context_path(
                    module, batch_id, "bundle_set.json"
                ),
                "context_bundle_set_digest": context_bundle_set[
                    "context_bundle_set_digest"
                ],
                "worker_context_manifest_path": _context_path(
                    module, batch_id, "worker_manifest.json"
                ),
                "worker_context_manifest_digest": worker_manifest[
                    "worker_context_manifest_digest"
                ],
            }
        )
    return summary


def _build_context_batch(
    state: dict,
    module: str,
    batch_id: int,
    packets: list[dict],
    state_by_id: dict[int, dict],
    job_root: Path | None = None,
) -> tuple[list[dict], dict, dict]:
    reviewed_ids = sorted(
        {
            segment_id
            for packet in packets
            for segment_id in packet["reviewed_ids"]
        }
    )
    bundle_set = build_context_bundle_set(
        state,
        [state_by_id[segment_id] for segment_id in reviewed_ids],
        module,
    )
    bundle_digest = bundle_set["context_bundle_set_digest"]
    worker_payloads = [
        _worker_packet_payload(
            packet,
            batch_id=batch_id,
            context_bundle_set_digest=bundle_digest,
        )
        for packet in packets
    ]
    instruction_version = state.get("checker_instruction_version", 0)
    if not isinstance(instruction_version, int) or isinstance(instruction_version, bool):
        raise ContextBundleError("state.checker_instruction_version must be an integer")
    use_v2 = instruction_version >= 2
    worker_manifest = build_worker_context_manifest(
        state,
        module,
        bundle_set,
        max_worker_bytes=None,
        packet_payloads=worker_payloads,
        common_instructions_path=(
            CHECKER_V2_INSTRUCTIONS / "common.md" if use_v2 else None
        ),
        module_instructions_path=(
            CHECKER_V2_INSTRUCTIONS / f"{module}.md" if use_v2 else None
        ),
        job_root=job_root,
    )
    bound_packets = [
        _bind_packet_to_worker_context(
            packet,
            batch_id=batch_id,
            context_bundle_set_digest=bundle_digest,
            worker_context_manifest_digest=worker_manifest[
                "worker_context_manifest_digest"
            ],
        )
        for packet in packets
    ]
    if sum(_json_bytes(packet) for packet in bound_packets) != sum(
        _json_bytes(packet) for packet in worker_payloads
    ):
        raise ContextBundleError("worker packet binding changed measured input bytes")
    return bound_packets, bundle_set, worker_manifest


def _partition_context_batches(
    state: dict,
    module: str,
    packets: list[dict],
    state_by_id: dict[int, dict],
    job_root: Path | None = None,
) -> tuple[list[dict], list[dict]]:
    batches = []
    bound_packets = []
    current: list[dict] = []

    def build_current(candidate: list[dict], batch_id: int):
        chars = sum(packet["review_text_chars"] for packet in candidate)
        if len(candidate) > MAX_PACKETS_PER_WORKER:
            raise WorkerContextBudgetError("worker packet count exceeds 4; split the batch")
        if chars > MAX_REVIEW_TEXT_CHARS_PER_WORKER:
            raise WorkerContextBudgetError(
                f"worker review text is {chars} characters, exceeding structural limit "
                f"{MAX_REVIEW_TEXT_CHARS_PER_WORKER}; split the batch"
            )
        return _build_context_batch(
            state,
            module,
            batch_id,
            candidate,
            state_by_id,
            job_root,
        )

    def flush(candidate: list[dict]) -> None:
        batch_id = len(batches)
        finalized, bundle_set, worker_manifest = build_current(candidate, batch_id)
        bound_packets.extend(finalized)
        batches.append(
            {
                "module": module,
                "batch_id": batch_id,
                "packets": finalized,
                "context_bundle_set": bundle_set,
                "worker_manifest": worker_manifest,
            }
        )

    for packet in packets:
        candidate = [*current, packet]
        try:
            build_current(candidate, len(batches))
        except WorkerContextBudgetError:
            if not current:
                raise
            flush(current)
            current = [packet]
            build_current(current, len(batches))
        else:
            current = candidate
    if current:
        flush(current)
    return bound_packets, batches


def build_worker_batch_plan(
    split_manifest: dict,
    modules: list[str],
    packets: list[dict],
    context_batches: list[dict] | None = None,
    selected_evidence_index: dict | None = None,
) -> dict:
    batches_by_module = {module: [] for module in modules}
    if context_batches is not None:
        for item in context_batches:
            module = item["module"]
            batches_by_module[module].append(
                _batch_summary(
                    module,
                    item["batch_id"],
                    item["packets"],
                    context_bundle_set=item["context_bundle_set"],
                    worker_manifest=item["worker_manifest"],
                )
            )
    else:
        for module in modules:
            module_packets = sorted(
                (
                    packet
                    for packet in packets
                    if packet["module"] == module and packet["requires_ai"]
                ),
                key=lambda packet: packet["chunk_id"],
            )
            batches = []
            current = []
            current_chars = 0

            def flush() -> None:
                nonlocal current, current_chars
                if not current:
                    return
                batches.append(_batch_summary(module, len(batches), current))
                current = []
                current_chars = 0

            for packet in module_packets:
                packet_chars = packet["review_text_chars"]
                would_exceed = current and (
                    len(current) >= MAX_PACKETS_PER_WORKER
                    or current_chars + packet_chars
                    > MAX_REVIEW_TEXT_CHARS_PER_WORKER
                )
                if would_exceed:
                    flush()
                if packet_chars > MAX_REVIEW_TEXT_CHARS_PER_WORKER:
                    raise WorkerContextBudgetError(
                        "single review packet exceeds worker review-text structural limit"
                    )
                current.append(packet)
                current_chars += packet_chars
            flush()
            batches_by_module[module] = batches

    payload = {
        "schema": BATCH_PLAN_SCHEMA,
        "version": PACKET_VERSION,
        "split_fingerprint": split_manifest["split_fingerprint"],
        "policy": {
            "max_packets_per_worker": MAX_PACKETS_PER_WORKER,
            "max_review_text_chars_per_worker": (
                MAX_REVIEW_TEXT_CHARS_PER_WORKER
            ),
            "worker_input_bytes": {
                "mode": "advisory",
                "measurement": "complete_utf8_bytes",
            },
        },
        "modules": batches_by_module,
    }
    if selected_evidence_index is not None:
        payload.update(
            {
                "selected_evidence_index_path": SELECTED_EVIDENCE_INDEX_PATH,
                "selected_evidence_index_digest": selected_evidence_index[
                    "index_digest"
                ],
            }
        )
    return _with_digest(payload, "batch_plan_digest")


def _build_review_generation(
    state: dict,
    split_manifest: dict,
    bases: list[dict],
    job_root: Path | None = None,
) -> tuple[list[str], list[dict], list[dict], dict, dict]:
    load_project_context_assets(state)
    modules = required_modules(state)
    unsupported = sorted(set(modules) - _SUPPORTED_MODULES)
    if unsupported:
        raise ValueError(
            "unsupported required modules: " + ", ".join(unsupported)
        )
    unbound_packets = [
        build_review_packet(base, module, get_review_policy(state))
        for module in modules
        for base in bases
    ]
    state_by_id = {segment["id"]: segment for segment in state["segments"]}

    def partition_all(input_packets: list[dict]) -> tuple[list[dict], list[dict]]:
        context_batches = []
        replacements = {}
        for module in modules:
            module_packets = [
                packet
                for packet in input_packets
                if packet["module"] == module and packet["requires_ai"]
            ]
            bound, batches = _partition_context_batches(
                state,
                module,
                module_packets,
                state_by_id,
                job_root,
            )
            replacements.update(
                {
                    (packet["module"], packet["chunk_id"]): packet
                    for packet in bound
                }
            )
            context_batches.extend(batches)
        return (
            [
                replacements.get(
                    (packet["module"], packet["chunk_id"]),
                    packet,
                )
                for packet in input_packets
            ],
            context_batches,
        )

    _, provisional_batches = partition_all(unbound_packets)
    selected_evidence_index = build_selected_context_evidence_index(
        state,
        split_fingerprint=split_manifest["split_fingerprint"],
        split_manifest_digest=split_manifest["manifest_digest"],
        modules=modules,
        context_bundle_sets=[
            item["context_bundle_set"] for item in provisional_batches
        ],
    )
    evidence_bound_packets = [
        _bind_packet_to_selected_evidence(
            packet,
            selected_evidence_index["index_digest"],
        )
        for packet in unbound_packets
    ]
    packets, context_batches = partition_all(evidence_bound_packets)
    final_index = build_selected_context_evidence_index(
        state,
        split_fingerprint=split_manifest["split_fingerprint"],
        split_manifest_digest=split_manifest["manifest_digest"],
        modules=modules,
        context_bundle_sets=[
            item["context_bundle_set"] for item in context_batches
        ],
    )
    if final_index != selected_evidence_index:
        raise ContextBundleError(
            "selected evidence changed after worker packet binding"
        )
    batch_plan = build_worker_batch_plan(
        split_manifest,
        modules,
        packets,
        context_batches=context_batches,
        selected_evidence_index=selected_evidence_index,
    )
    return (
        modules,
        packets,
        context_batches,
        selected_evidence_index,
        batch_plan,
    )


def _build_review_artifacts(
    state: dict,
    split_manifest: dict,
    bases: list[dict],
    job_root: Path | None = None,
) -> tuple[list[str], list[dict], list[dict], dict, dict, dict, dict]:
    (
        modules,
        packets,
        context_batches,
        selected_evidence_index,
        batch_plan,
    ) = _build_review_generation(
        state,
        split_manifest,
        bases,
        job_root,
    )
    cost_report = _build_cost_report(bases, modules, packets, batch_plan)
    packet_manifest = _build_packet_manifest(
        split_manifest,
        modules,
        packets,
        batch_plan,
        context_batches,
        selected_evidence_index,
    )
    return (
        modules,
        packets,
        context_batches,
        selected_evidence_index,
        batch_plan,
        cost_report,
        packet_manifest,
    )


def _build_cost_report(
    bases: list[dict],
    modules: list[str],
    packets: list[dict],
    batch_plan: dict,
) -> dict:
    full_bytes = sum(_json_bytes(base) for base in bases) * len(modules)
    packet_bytes = sum(_json_bytes(packet) for packet in packets)
    reviews_before = (
        sum(len(base.get("segments", [])) for base in bases) * len(modules)
    )
    reviews_after = sum(len(packet["reviewed_ids"]) for packet in packets)
    reduction = 0.0 if full_bytes == 0 else (1 - packet_bytes / full_bytes) * 100
    worker_input_bytes = sum(
        batch["worker_input_bytes"]
        for batches in batch_plan["modules"].values()
        for batch in batches
    )
    return {
        "basis": (
            "complete worker input bytes including instructions, project documents, "
            "shared assets, context bundles, and packets"
        ),
        "required_modules": list(modules),
        "chunks": len(bases),
        "full_chunk_input_bytes": full_bytes,
        "review_packet_input_bytes": packet_bytes,
        "complete_worker_input_bytes": worker_input_bytes,
        "largest_worker_input_bytes": max(
            (
                batch["worker_input_bytes"]
                for batches in batch_plan["modules"].values()
                for batch in batches
            ),
            default=0,
        ),
        "input_reduction_percent": round(reduction, 1),
        "segment_reviews_before": reviews_before,
        "segment_reviews_after": reviews_after,
        "segment_review_reduction": reviews_before - reviews_after,
        "no_ai_packets": sum(not packet["requires_ai"] for packet in packets),
        "worker_batches": sum(
            len(batches) for batches in batch_plan["modules"].values()
        ),
    }


def _build_packet_manifest(
    split_manifest: dict,
    modules: list[str],
    packets: list[dict],
    batch_plan: dict,
    context_batches: list[dict] | None = None,
    selected_evidence_index: dict | None = None,
) -> dict:
    payload = {
        "schema": PACKET_MANIFEST_SCHEMA,
        "version": PACKET_VERSION,
        "split_fingerprint": split_manifest["split_fingerprint"],
        "split_manifest_digest": split_manifest["manifest_digest"],
        "required_modules": list(modules),
        "batch_plan_digest": batch_plan["batch_plan_digest"],
        "packets": {
            _packet_name(packet): packet["packet_digest"] for packet in packets
        },
        "worker_contexts": {
            _context_path(item["module"], item["batch_id"], "worker_manifest.json"):
                item["worker_manifest"]["worker_context_manifest_digest"]
            for item in (context_batches or [])
        },
    }
    if selected_evidence_index is not None:
        payload.update(
            {
                "selected_evidence_index_path": SELECTED_EVIDENCE_INDEX_PATH,
                "selected_evidence_index_digest": selected_evidence_index[
                    "index_digest"
                ],
            }
        )
    return _with_digest(payload, "manifest_digest")


def _packet_tree_is_current(
    active: Path,
    packet_manifest: dict,
    packets: list[dict],
    cost_report: dict,
    batch_plan: dict,
    context_batches: list[dict] | None = None,
    selected_evidence_index: dict | None = None,
) -> bool:
    try:
        if load(active / "manifest.json") != packet_manifest:
            return False
        if load(active / "cost_report.json") != cost_report:
            return False
        if load(active / "batch_plan.json") != batch_plan:
            return False
        if selected_evidence_index is not None:
            persisted_index = load(active / SELECTED_EVIDENCE_INDEX_PATH)
            if persisted_index != selected_evidence_index:
                return False
            if validate_selected_context_evidence_index(
                persisted_index
            ) != selected_evidence_index:
                return False
        for packet in packets:
            if load(active / _packet_name(packet)) != packet:
                return False
        for item in context_batches or []:
            if load(
                active
                / _context_path(item["module"], item["batch_id"], "bundle_set.json")
            ) != item["context_bundle_set"]:
                return False
            persisted_manifest = load(
                active
                / _context_path(
                    item["module"], item["batch_id"], "worker_manifest.json"
                )
            )
            if persisted_manifest != item["worker_manifest"]:
                return False
            if verify_worker_context_manifest_resources(
                persisted_manifest,
                job_root=active.parent,
            ) != item["worker_manifest"]:
                return False
    except (ContextBundleError, OSError, ValueError):
        return False
    return True


def cmd_prepare(args) -> None:
    job = Path(args.job)
    chunks = job / "chunks"
    with generation_lock(chunks, exclusive=False):
        state = load(job / "state.json")
        require_current_job_runtime(state, "review-prepare")
        split_manifest, bases, _, _, _ = _load_verified_generation_unlocked(
            job / "state.json",
            job / "errors_precheck.json",
            chunks,
            state=state,
        )
        try:
            (
                modules,
                packets,
                context_batches,
                selected_evidence_index,
                batch_plan,
                cost_report,
                packet_manifest,
            ) = _build_review_artifacts(state, split_manifest, bases, job)
        except (ContextBundleError, OSError, ValueError) as exc:
            raise SystemExit(f"[review-prepare] worker context: {exc}") from exc
        active = job / "review_packets"
        if _packet_tree_is_current(
            active,
            packet_manifest,
            packets,
            cost_report,
            batch_plan,
            context_batches,
            selected_evidence_index,
        ):
            print(f"[review-prepare] existing packets are current: {active}")
            print(
                "[review-prepare] input payload reduction "
                f"{cost_report['input_reduction_percent']}%"
            )
            return

        with tempfile.TemporaryDirectory(
            dir=job,
            prefix=".review_packets.generation.",
        ) as staging_name:
            staging = Path(staging_name)
            for packet in packets:
                write_json_atomic(staging / _packet_name(packet), packet)
            write_json_atomic(
                staging / SELECTED_EVIDENCE_INDEX_PATH,
                selected_evidence_index,
            )
            for item in context_batches:
                write_json_atomic(
                    staging
                    / _context_path(
                        item["module"], item["batch_id"], "bundle_set.json"
                    ),
                    item["context_bundle_set"],
                )
                write_json_atomic(
                    staging
                    / _context_path(
                        item["module"], item["batch_id"], "worker_manifest.json"
                    ),
                    item["worker_manifest"],
                )
            write_json_atomic(staging / "manifest.json", packet_manifest)
            write_json_atomic(staging / "cost_report.json", cost_report)
            write_json_atomic(staging / "batch_plan.json", batch_plan)
            publish_generation(
                staging,
                active,
                archive_label=split_manifest["split_fingerprint"][:12],
            )

    print(
        f"[review-prepare] {len(packets)} packets -> {active}; "
        f"input payload reduction {cost_report['input_reduction_percent']}%"
    )
    print(
        "[review-prepare] AI segment reviews "
        f"{cost_report['segment_reviews_before']} -> "
        f"{cost_report['segment_reviews_after']}; "
        f"no-AI packets {cost_report['no_ai_packets']}; "
        f"worker batches {cost_report['worker_batches']}"
    )


def _live_review_generation_unlocked(
    job: Path,
) -> tuple[dict, dict, list[dict], list[dict]]:
    chunks = job / "chunks"
    state = load(job / "state.json")
    require_current_job_runtime(state, "review-publish")
    split_manifest, bases, _, _, _ = _load_verified_generation_unlocked(
        job / "state.json",
        job / "errors_precheck.json",
        chunks,
        state=state,
    )
    try:
        (
            _,
            packets,
            context_batches,
            selected_evidence_index,
            batch_plan,
            cost_report,
            packet_manifest,
        ) = _build_review_artifacts(state, split_manifest, bases, job)
    except (ContextBundleError, OSError, ValueError) as exc:
        raise SystemExit(f"[review-publish] worker context: {exc}") from exc
    if not _packet_tree_is_current(
        job / "review_packets",
        packet_manifest,
        packets,
        cost_report,
        batch_plan,
        context_batches,
        selected_evidence_index,
    ):
        raise SystemExit(
            "[review-publish] prepared review packet tree is missing or stale; "
            "rerun lqe_review.py prepare"
        )
    return state, split_manifest, bases, packets


def _live_review_packet_unlocked(
    job: Path,
    chunk_id: int,
    module: str,
) -> tuple[dict, dict, dict, dict]:
    state, split_manifest, bases, packets = _live_review_generation_unlocked(job)
    if module not in required_modules(state):
        raise SystemExit(
            f"[review-publish] module {module!r} is not required by scope"
        )
    base = next(
        (item for item in bases if item["chunk_id"] == chunk_id),
        None,
    )
    if base is None:
        raise SystemExit(
            f"[review-publish] chunk {chunk_id} is not in the live generation"
        )
    packet = next(
        (
            item
            for item in packets
            if item["module"] == module and item["chunk_id"] == chunk_id
        ),
        None,
    )
    if packet is None:
        raise SystemExit("[review-publish] live review packet is missing")
    return state, split_manifest, base, packet


def _live_review_packet(
    job: Path,
    chunk_id: int,
    module: str,
) -> tuple[dict, dict]:
    with generation_lock(job / "chunks", exclusive=False):
        _, _, base, packet = _live_review_packet_unlocked(
            job, chunk_id, module
        )
        return base, packet


def _load_compact_draft(
    path: Path,
    packet: dict,
    *,
    target_job: Path | None = None,
    allow_migration: bool = True,
    target_state: dict | None = None,
    migration_validation_cache: dict | None = None,
) -> tuple[list[dict], dict, dict | None]:
    raw = load(path)
    if not isinstance(raw, dict):
        raise ValueError("compact draft must be an object")
    if "worker_receipt" not in raw:
        raise ValueError("compact draft worker_receipt is required")
    expected_fields = {
        "schema",
        "version",
        "module",
        "chunk_id",
        "packet_digest",
        "reviewed_ids",
        "findings",
        "selected_evidence_index_path",
        "selected_evidence_index_digest",
        "worker_receipt",
    }
    context_binding_fields = {
        "worker_batch_id",
        "worker_packet_basis_digest",
        "context_bundle_set_digest",
        "worker_context_manifest_digest",
    }
    if "worker_context_manifest_digest" in packet:
        expected_fields.update(context_binding_fields)
    migration_receipt = raw.get("migration_receipt")
    if migration_receipt is not None:
        if not allow_migration:
            raise ValueError("chained compact draft migration is not allowed")
        expected_fields.add("migration_receipt")
    if set(raw) != expected_fields:
        raise ValueError("compact draft fields are invalid")
    expected_bindings = {
        "schema": COMPACT_DRAFT_SCHEMA,
        "version": COMPACT_DRAFT_VERSION,
        "module": packet["module"],
        "chunk_id": packet["chunk_id"],
        "packet_digest": packet["packet_digest"],
        "selected_evidence_index_path": packet[
            "selected_evidence_index_path"
        ],
        "selected_evidence_index_digest": packet[
            "selected_evidence_index_digest"
        ],
    }
    for field in context_binding_fields:
        if field in packet:
            expected_bindings[field] = packet[field]
    for field, value in expected_bindings.items():
        if raw.get(field) != value:
            raise ValueError(f"compact draft {field} mismatch")
    reviewed_ids = raw.get("reviewed_ids")
    if reviewed_ids != packet["reviewed_ids"]:
        raise ValueError(
            "compact draft reviewed_ids must exactly match the review packet"
        )

    findings = _normalize_module_output(
        raw.get("findings"),
        path,
        review_policy=packet["review_policy"],
    )
    reviewed_set = set(reviewed_ids)
    for entry in findings:
        if entry["id"] not in reviewed_set:
            raise ValueError(
                f"compact draft finding id {entry['id']} was not reviewed"
            )
        if not entry["issues"]:
            raise ValueError(
                f"compact draft finding id {entry['id']} has no issues; omit it"
            )
    worker_receipt = _normalize_worker_receipt(raw["worker_receipt"])
    if migration_receipt is not None:
        if target_job is None:
            raise ValueError("migrated compact draft requires a target job")
        _validate_migration_receipt(
            raw,
            packet,
            target_job=target_job,
            worker_receipt=worker_receipt,
            findings=findings,
            target_state=target_state,
            validation_cache=migration_validation_cache,
        )
    return findings, worker_receipt, copy.deepcopy(migration_receipt)


def _build_migration_receipt(
    *,
    source_job: Path,
    source_state: dict,
    target_state: dict,
    input_identity: dict,
    packet: dict,
    source_records: list[dict],
    source_review_modes: list[str],
    normalizations: dict,
) -> dict:
    payload = {
        "schema": MIGRATION_RECEIPT_SCHEMA,
        "version": MIGRATION_RECEIPT_VERSION,
        "source_job": str(source_job.resolve()),
        "source_state_digest": canonical_digest(source_state),
        "target_state_digest": canonical_digest(target_state),
        "input_identity": copy.deepcopy(input_identity),
        "module": packet["module"],
        "chunk_id": packet["chunk_id"],
        "target_packet_digest": packet["packet_digest"],
        "source_review_modes": sorted(source_review_modes),
        "target_review_mode": packet["review_policy"]["mode"],
        "normalizations": copy.deepcopy(normalizations),
        "source_drafts": copy.deepcopy(source_records),
    }
    return _with_digest(payload, "receipt_digest")


def _cache_source_artifact_digest(
    validation_cache: dict | None,
    path: Path,
    value: object,
) -> None:
    if validation_cache is None:
        return
    key = str(path.resolve())
    digest = canonical_digest(value)
    previous = validation_cache.setdefault("source_artifact_digests", {}).get(
        key
    )
    if previous is not None and previous != digest:
        raise ValueError(f"source artifact changed during validation: {path}")
    validation_cache["source_artifact_digests"][key] = digest


def _revalidate_cached_source_artifacts(validation_cache: dict) -> None:
    for raw_path, expected_digest in validation_cache.get(
        "source_artifact_digests", {}
    ).items():
        path = Path(raw_path)
        if canonical_digest(load(path)) != expected_digest:
            raise ValueError(f"source artifact changed during validation: {path}")


def _validate_migration_receipt(
    raw_draft: dict,
    packet: dict,
    *,
    target_job: Path,
    worker_receipt: dict,
    findings: list[dict],
    target_state: dict | None = None,
    validation_cache: dict | None = None,
) -> None:
    receipt = raw_draft.get("migration_receipt")
    expected_fields = {
        "schema",
        "version",
        "source_job",
        "source_state_digest",
        "target_state_digest",
        "input_identity",
        "module",
        "chunk_id",
        "target_packet_digest",
        "source_review_modes",
        "target_review_mode",
        "normalizations",
        "source_drafts",
        "receipt_digest",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected_fields:
        raise ValueError("migration receipt fields are invalid")
    if (
        receipt.get("schema") != MIGRATION_RECEIPT_SCHEMA
        or receipt.get("version") != MIGRATION_RECEIPT_VERSION
    ):
        raise ValueError("migration receipt schema/version is unsupported")
    _canonical_payload_digest(receipt, "receipt_digest", "migration receipt")
    if (
        receipt.get("module") != packet["module"]
        or receipt.get("chunk_id") != packet["chunk_id"]
        or receipt.get("target_packet_digest") != packet["packet_digest"]
        or receipt.get("target_review_mode") != packet["review_policy"]["mode"]
    ):
        raise ValueError("migration receipt target packet binding differs")

    target_job = target_job.resolve()
    if target_state is None:
        target_state = load(target_job / "state.json")
    if receipt.get("target_state_digest") != canonical_digest(target_state):
        raise ValueError("migration receipt target state is stale")
    source_job_value = receipt.get("source_job")
    if not isinstance(source_job_value, str) or not source_job_value.strip():
        raise ValueError("migration receipt source_job is invalid")
    source_job = Path(source_job_value).resolve()
    if source_job == target_job:
        raise ValueError("migration receipt source and target jobs must differ")
    source_cache_key = str(source_job)
    source_states = (
        validation_cache.setdefault("source_states", {})
        if validation_cache is not None
        else {}
    )
    source_state = source_states.get(source_cache_key)
    if source_state is None:
        source_state = load(source_job / "state.json")
        if validation_cache is not None:
            source_states[source_cache_key] = source_state
    _cache_source_artifact_digest(
        validation_cache, source_job / "state.json", source_state
    )
    if receipt.get("source_state_digest") != canonical_digest(source_state):
        raise ValueError("migration receipt source state is stale")
    identity = _matching_reuse_identity(source_state, target_state)
    if receipt.get("input_identity") != identity:
        raise ValueError("migration receipt input identity differs")

    source_records = receipt.get("source_drafts")
    if not isinstance(source_records, list) or not source_records:
        raise ValueError("migration receipt source_drafts must be non-empty")
    record_fields = {
        "draft_path",
        "draft_digest",
        "packet_path",
        "packet_digest",
        "worker_receipt",
        "contributed_ids",
    }
    target_ids = packet["reviewed_ids"]
    target_id_set = set(target_ids)
    covered_ids = set()
    findings_by_id = {}
    source_precheck_by_id = {}
    source_modes = set()
    packet_validation_cache = (
        validation_cache.setdefault("source_packet_validation", {}).setdefault(
            source_cache_key, {}
        )
        if validation_cache is not None
        else None
    )
    for index, record in enumerate(source_records):
        if not isinstance(record, dict) or set(record) != record_fields:
            raise ValueError(f"migration receipt source_drafts[{index}] is invalid")
        draft_relative = record.get("draft_path")
        packet_relative = record.get("packet_path")
        if not isinstance(draft_relative, str) or not isinstance(packet_relative, str):
            raise ValueError("migration receipt source paths are invalid")
        source_draft_path = source_job / draft_relative
        if _source_relative_path(source_job, source_draft_path, "source draft") != draft_relative:
            raise ValueError("migration receipt source draft path is not canonical")
        source_packet_path = source_job / packet_relative
        source_packet, canonical_packet_relative = _load_source_packet(
            source_job,
            source_packet_path,
            source_state,
            validation_cache=packet_validation_cache,
        )
        _cache_source_artifact_digest(
            validation_cache, source_packet_path, source_packet
        )
        if validation_cache is not None:
            source_manifest_path = (
                source_job / "review_packets" / "manifest.json"
            )
            source_index_path = (
                source_job
                / "review_packets"
                / source_packet["selected_evidence_index_path"]
            )
            cached_paths = validation_cache.setdefault(
                "source_artifact_digests", {}
            )
            for bound_path in (source_manifest_path, source_index_path):
                if str(bound_path.resolve()) not in cached_paths:
                    _cache_source_artifact_digest(
                        validation_cache,
                        bound_path,
                        load(bound_path),
                    )
        if canonical_packet_relative != packet_relative:
            raise ValueError("migration receipt source packet path is not canonical")
        if (
            source_packet.get("module") != packet["module"]
            or source_packet.get("packet_digest") != record.get("packet_digest")
        ):
            raise ValueError("migration receipt source packet binding differs")
        source_raw = load(source_draft_path)
        _cache_source_artifact_digest(
            validation_cache, source_draft_path, source_raw
        )
        if canonical_digest(source_raw) != record.get("draft_digest"):
            raise ValueError("migration receipt source draft is stale")
        source_findings, source_worker, source_migration = _load_compact_draft(
            source_draft_path,
            source_packet,
            allow_migration=False,
        )
        if source_migration is not None:
            raise ValueError("chained compact draft migration is not allowed")
        if source_worker != record.get("worker_receipt"):
            raise ValueError("migration receipt source worker differs")
        if source_worker != worker_receipt:
            raise ValueError(
                "one target packet cannot combine different checker workers"
            )
        contributed_ids = record.get("contributed_ids")
        if (
            not isinstance(contributed_ids, list)
            or not contributed_ids
            or any(type(segment_id) is not int for segment_id in contributed_ids)
            or len(contributed_ids) != len(set(contributed_ids))
            or not set(contributed_ids).issubset(set(source_packet["reviewed_ids"]))
            or not set(contributed_ids).issubset(target_id_set)
        ):
            raise ValueError("migration receipt contributed_ids are invalid")
        if covered_ids.intersection(contributed_ids):
            raise ValueError("migration receipt source coverage overlaps")
        covered_ids.update(contributed_ids)
        source_findings_map = {
            finding["id"]: finding["issues"] for finding in source_findings
        }
        source_packet_segments = {
            segment["id"]: segment
            for segment in source_packet.get("segments", [])
        }
        for segment_id in contributed_ids:
            issues = source_findings_map.get(segment_id)
            if issues:
                findings_by_id[segment_id] = issues
            source_precheck_by_id[segment_id] = copy.deepcopy(
                source_packet_segments[segment_id].get("precheck", [])
            )
        source_modes.add(source_packet["review_policy"]["mode"])

    if covered_ids != target_id_set:
        raise ValueError("migration receipt does not cover the target packet")
    if receipt.get("source_review_modes") != sorted(source_modes):
        raise ValueError("migration receipt source review modes differ")
    source_findings = [
        {"id": segment_id, "issues": findings_by_id[segment_id]}
        for segment_id in target_ids
        if segment_id in findings_by_id
    ]
    expected_findings, normalizations = _transform_reused_findings(
        source_findings,
        packet["review_policy"],
        packet,
        source_precheck_by_id=source_precheck_by_id,
    )
    if receipt.get("normalizations") != normalizations:
        raise ValueError("migration receipt normalization counts differ")
    expected_findings = _normalize_module_output(
        expected_findings,
        Path("expected-migrated-draft.json"),
        review_policy=packet["review_policy"],
    )
    if findings != expected_findings:
        raise ValueError("migrated compact draft findings differ from source drafts")


def _validate_full_entries_for_publication(
    job: Path,
    state: dict,
    split_manifest: dict,
    base: dict,
    module: str,
    entries: list[dict],
    packet: dict,
    *,
    draft_path: Path | None = None,
    worker_receipt: dict | None = None,
    migration_receipt: dict | None = None,
) -> dict:
    outdir = job / "chunks"
    if packet["requires_ai"] and worker_receipt is None:
        raise SystemExit(
            "[review-publish] AI-reviewed packet requires worker_receipt"
        )
    if worker_receipt is not None:
        try:
            worker_receipt = _normalize_worker_receipt(worker_receipt)
        except ValueError as exc:
            raise SystemExit(f"[review-publish] {exc}") from exc
    expected_ids = {segment["id"] for segment in base["segments"]}
    actual_ids = {entry["id"] for entry in entries}
    if actual_ids != expected_ids:
        raise SystemExit(
            "[review-publish] id coverage differs from chunk: "
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
    segment_by_id = {segment["id"]: segment for segment in base["segments"]}
    for entry in entries:
        try:
            build_segment_result(
                segment_by_id[entry["id"]],
                entry["issues"],
                review_policy=get_review_policy(state),
                target_form_policy=load_target_form_policy(state),
            )
        except CheckFormatError as exc:
            raise SystemExit(
                "[review-publish] invalid correction contract for "
                f"id {entry['id']}: {exc}"
            ) from exc
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
            problem = _precheck_provenance_problem(
                precheck_by_id.get(entry["id"], []), reviewed_issues
            )
            if problem:
                raise SystemExit(
                    f"[review-publish] {problem} for id {entry['id']}"
                )
        for issue in entry["issues"]:
            problem = _module_issue_problem(state, module, issue)
            if problem:
                raise SystemExit(f"[review-publish] {problem}")

    destination = outdir / f"chunk_{base['chunk_id']:02d}.{module}.json"
    receipt_path = module_receipt_path(destination)
    input_paths = {
        "state": job / "state.json",
        "precheck": job / "errors_precheck.json",
        **state_reference_paths(state),
    }
    if draft_path is not None:
        input_paths["compact module draft"] = draft_path
    try:
        validate_artifact_paths(
            {
                "module output": destination,
                "module publication receipt": receipt_path,
            },
            input_paths,
            context="review-publish",
        )
        payload = build_module_output(
            base,
            module,
            entries,
            label=destination.name,
        )
        review_provenance = {
            "review_packet_digest": packet["packet_digest"],
            "selected_evidence_index_digest": packet[
                "selected_evidence_index_digest"
            ],
            "worker_receipt": worker_receipt,
        }
        if migration_receipt is not None:
            review_provenance["migration_receipt"] = copy.deepcopy(
                migration_receipt
            )
        receipt = build_module_receipt(
            payload,
            split_manifest,
            destination,
            review_provenance=review_provenance,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(f"[review-publish] {exc}") from exc
    return {
        "destination": destination,
        "receipt_path": receipt_path,
        "payload": payload,
        "receipt": receipt,
    }


def _publish_validated_module(validated: dict) -> None:
    destination = validated["destination"]
    receipt_path = validated["receipt_path"]
    try:
        with tempfile.TemporaryDirectory(
            dir=destination.parent,
            prefix=".review-publish.",
        ) as staging_name:
            staging = Path(staging_name)
            staged_output = staging / destination.name
            staged_receipt = staging / receipt_path.name
            write_json_atomic(staged_output, validated["payload"])
            write_json_atomic(staged_receipt, validated["receipt"])
            publish_replacement_transaction(
                [
                    (staged_output, destination),
                    (staged_receipt, receipt_path),
                ]
            )
    except (OSError, ValueError) as exc:
        raise SystemExit(f"[review-publish] {exc}") from exc


def _publish_full_entries(
    job: Path,
    base: dict,
    module: str,
    entries: list[dict],
    packet: dict,
    *,
    draft_path: Path | None = None,
    worker_receipt: dict | None = None,
    migration_receipt: dict | None = None,
) -> None:
    outdir = job / "chunks"
    with generation_lock(outdir, exclusive=True):
        state, split_manifest, live_base, live_packet = _live_review_packet_unlocked(
            job, base["chunk_id"], module
        )
        if live_packet != packet or live_base != base:
            raise SystemExit(
                "[review-publish] stale task: live review packet changed; "
                "rerun lqe_review.py prepare"
            )
        validated = _validate_full_entries_for_publication(
            job,
            state,
            split_manifest,
            live_base,
            module,
            entries,
            live_packet,
            draft_path=draft_path,
            worker_receipt=worker_receipt,
            migration_receipt=migration_receipt,
        )
        _publish_validated_module(validated)


def _expand_findings(base: dict, findings: list[dict]) -> list[dict]:
    issues_by_id = {entry["id"]: entry["issues"] for entry in findings}
    return [
        {
            "id": segment["id"],
            "issues": issues_by_id.get(segment["id"], []),
        }
        for segment in base["segments"]
    ]


def cmd_publish(args) -> None:
    job = Path(args.job)
    base, packet = _live_review_packet(job, args.chunk, args.module)
    draft_path = Path(args.input)
    try:
        findings, worker_receipt, migration_receipt = _load_compact_draft(
            draft_path,
            packet,
            target_job=job,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(f"[review-publish] {exc}") from exc
    entries = _expand_findings(base, findings)
    _publish_full_entries(
        job,
        base,
        args.module,
        entries,
        packet,
        draft_path=draft_path,
        worker_receipt=worker_receipt,
        migration_receipt=migration_receipt,
    )
    print(
        f"[review-publish] reviewed {len(packet['reviewed_ids'])}, "
        f"findings {len(findings)}, formal coverage {len(entries)}"
    )


def _directory_compact_drafts(input_root: Path) -> list[Path]:
    if not input_root.is_dir():
        raise ValueError(f"compact draft directory is missing: {input_root}")
    drafts = []
    for path in sorted(input_root.rglob("*.json")):
        raw = load(path)
        if (
            path.name == "report.json"
            and isinstance(raw, dict)
            and raw.get("schema") == REUSE_REPORT_SCHEMA
        ):
            continue
        if not isinstance(raw, dict) or raw.get("schema") != COMPACT_DRAFT_SCHEMA:
            raise ValueError(f"unexpected JSON in compact draft directory: {path}")
        drafts.append(path)
    if not drafts:
        raise ValueError("compact draft directory contains no drafts")
    return drafts


def cmd_publish_directory(args) -> None:
    job = Path(args.job).resolve()
    input_root = Path(args.input_dir).resolve()
    chunks = job / "chunks"
    try:
        draft_paths = _directory_compact_drafts(input_root)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"[review-publish-directory] {exc}") from exc

    with generation_lock(chunks, exclusive=True):
        state, split_manifest, bases, packets = _live_review_generation_unlocked(job)
        bases_by_chunk = {base["chunk_id"]: base for base in bases}
        packets_by_key = {
            (packet["module"], packet["chunk_id"]): packet
            for packet in packets
        }
        seen_keys = set()
        pending = []
        skipped = []
        migration_validation_cache = {}
        for draft_path in draft_paths:
            try:
                raw = load(draft_path)
                module = raw.get("module")
                chunk_id = raw.get("chunk_id")
                key = (module, chunk_id)
                if key in seen_keys:
                    raise ValueError(
                        f"duplicate compact drafts for module/chunk {key!r}"
                    )
                seen_keys.add(key)
                packet = packets_by_key.get(key)
                base = bases_by_chunk.get(chunk_id)
                if packet is None or base is None:
                    raise ValueError(
                        f"compact draft {draft_path} is outside the live packet tree"
                    )
                destination = (
                    chunks / f"chunk_{chunk_id:02d}.{module}.json"
                )
                receipt_path = module_receipt_path(destination)
                if receipt_path.exists():
                    if not destination.is_file():
                        raise ValueError(
                            f"publication receipt exists without output: {receipt_path}"
                        )
                    load_module_output(
                        destination,
                        base,
                        module,
                        state,
                        publication_manifest=split_manifest,
                    )
                    skipped.append((module, chunk_id, draft_path))
                    continue
                findings, worker_receipt, migration_receipt = (
                    _load_compact_draft(
                        draft_path,
                        packet,
                        target_job=job,
                        target_state=state,
                        migration_validation_cache=migration_validation_cache,
                    )
                )
                entries = _expand_findings(base, findings)
                validated = _validate_full_entries_for_publication(
                    job,
                    state,
                    split_manifest,
                    base,
                    module,
                    entries,
                    packet,
                    draft_path=draft_path,
                    worker_receipt=worker_receipt,
                    migration_receipt=migration_receipt,
                )
                pending.append(
                    (
                        module,
                        chunk_id,
                        draft_path,
                        canonical_digest(raw),
                        validated,
                    )
                )
            except (CheckFormatError, OSError, ValueError) as exc:
                raise SystemExit(
                    f"[review-publish-directory] {draft_path}: {exc}"
                ) from exc

        try:
            _revalidate_cached_source_artifacts(migration_validation_cache)
            for _, _, draft_path, expected_digest, _ in pending:
                if canonical_digest(load(draft_path)) != expected_digest:
                    raise ValueError(
                        f"compact draft changed during validation: {draft_path}"
                    )
        except (OSError, ValueError) as exc:
            raise SystemExit(f"[review-publish-directory] {exc}") from exc

        for _, _, _, _, validated in pending:
            _publish_validated_module(validated)

    print(
        f"[review-publish-directory] validated {len(pending)} pending drafts "
        f"before publication; published {len(pending)}; "
        f"skipped valid existing receipts {len(skipped)}"
    )


def _source_draft_inventory(
    source_job: Path,
    drafts_dir: Path,
    source_state: dict,
    target_modules: set[str],
) -> tuple[dict[str, dict[int, list[dict]]], list[dict], list[dict]]:
    source_job = source_job.resolve()
    drafts_dir = drafts_dir.resolve()
    _source_relative_path(source_job, drafts_dir, "source drafts directory")
    coverage = {module: {} for module in target_modules}
    valid = []
    invalid = []
    validation_cache = {}
    for draft_path in sorted(drafts_dir.rglob("*.json")):
        try:
            draft_relative = _source_relative_path(
                source_job, draft_path, "source draft"
            )
            raw = load(draft_path)
            if not isinstance(raw, dict):
                raise ValueError("compact draft must be an object")
            module = raw.get("module")
            chunk_id = raw.get("chunk_id")
            if module not in target_modules:
                raise ValueError("draft module is not required by the target job")
            if type(chunk_id) is not int or chunk_id < 0:
                raise ValueError("draft chunk_id is invalid")
            packet_path = (
                source_job
                / "review_packets"
                / module
                / f"chunk_{chunk_id:02d}.json"
            )
            packet, packet_relative = _load_source_packet(
                source_job,
                packet_path,
                source_state,
                validation_cache=validation_cache,
            )
            if packet.get("review_policy") != get_review_policy(source_state):
                raise ValueError("source packet review policy differs from source state")
            if not packet.get("requires_ai"):
                raise ValueError("source draft is attached to a no-AI packet")
            findings, worker_receipt, migration_receipt = _load_compact_draft(
                draft_path,
                packet,
                allow_migration=False,
            )
            if migration_receipt is not None:
                raise ValueError("chained compact draft migration is not allowed")
            record = {
                "draft_path": draft_relative,
                "draft_digest": canonical_digest(raw),
                "packet_path": packet_relative,
                "packet_digest": packet["packet_digest"],
                "worker_receipt": worker_receipt,
                "reviewed_ids": list(packet["reviewed_ids"]),
                "findings_by_id": {
                    finding["id"]: finding["issues"] for finding in findings
                },
                "precheck_by_id": {
                    segment["id"]: copy.deepcopy(segment.get("precheck", []))
                    for segment in packet.get("segments", [])
                },
                "source_review_mode": packet["review_policy"]["mode"],
            }
            valid.append(record)
            for segment_id in packet["reviewed_ids"]:
                coverage[module].setdefault(segment_id, []).append(record)
        except (ContextBundleError, OSError, ValueError) as exc:
            invalid.append(
                {
                    "path": str(draft_path),
                    "reason": str(exc),
                }
            )
    return coverage, valid, invalid


def _compact_draft_for_packet(
    packet: dict,
    findings: list[dict],
    worker_receipt: dict,
    migration_receipt: dict,
) -> dict:
    output = {
        "schema": COMPACT_DRAFT_SCHEMA,
        "version": COMPACT_DRAFT_VERSION,
        "module": packet["module"],
        "chunk_id": packet["chunk_id"],
        "packet_digest": packet["packet_digest"],
        "selected_evidence_index_path": packet[
            "selected_evidence_index_path"
        ],
        "selected_evidence_index_digest": packet[
            "selected_evidence_index_digest"
        ],
        "worker_receipt": copy.deepcopy(worker_receipt),
        "reviewed_ids": list(packet["reviewed_ids"]),
        "findings": copy.deepcopy(findings),
        "migration_receipt": copy.deepcopy(migration_receipt),
    }
    for field in (
        "worker_batch_id",
        "worker_packet_basis_digest",
        "context_bundle_set_digest",
        "worker_context_manifest_digest",
    ):
        if field in packet:
            output[field] = copy.deepcopy(packet[field])
    return output


def cmd_reuse_drafts(args) -> None:
    source_job = Path(args.source_job).resolve()
    target_job = Path(args.job).resolve()
    if source_job == target_job:
        raise SystemExit("[review-reuse] source and target jobs must differ")
    drafts_dir = (
        Path(args.drafts).resolve()
        if args.drafts
        else source_job / "drafts"
    )
    output_root = (
        Path(args.out).resolve()
        if args.out
        else target_job / "reused_drafts"
    )
    if not drafts_dir.is_dir():
        raise SystemExit(f"[review-reuse] source drafts directory is missing: {drafts_dir}")
    output_root.parent.mkdir(parents=True, exist_ok=True)

    chunks = target_job / "chunks"
    try:
        with generation_lock(chunks, exclusive=False):
            source_state = load(source_job / "state.json")
            target_state = load(target_job / "state.json")
            require_current_job_runtime(source_state, "review-reuse-source")
            require_current_job_runtime(target_state, "review-reuse-target")
            input_identity = _matching_reuse_identity(source_state, target_state)
            (
                split_manifest,
                bases,
                _,
                _,
                _,
            ) = _load_verified_generation_unlocked(
                target_job / "state.json",
                target_job / "errors_precheck.json",
                chunks,
                state=target_state,
            )
            (
                modules,
                packets,
                context_batches,
                selected_evidence_index,
                batch_plan,
                cost_report,
                packet_manifest,
            ) = _build_review_artifacts(
                target_state, split_manifest, bases, target_job
            )
            if not _packet_tree_is_current(
                target_job / "review_packets",
                packet_manifest,
                packets,
                cost_report,
                batch_plan,
                context_batches,
                selected_evidence_index,
            ):
                raise ValueError(
                    "target review packet tree is missing or stale; rerun prepare"
                )

            coverage, valid_source, invalid_source = _source_draft_inventory(
                source_job,
                drafts_dir,
                source_state,
                set(modules),
            )
            bases_by_chunk = {base["chunk_id"]: base for base in bases}
            generated_payloads = []
            generated_report = []
            skipped_packets = []
            target_ai_packets = [packet for packet in packets if packet["requires_ai"]]
            for packet in target_ai_packets:
                module_coverage = coverage[packet["module"]]
                missing_ids = [
                    segment_id
                    for segment_id in packet["reviewed_ids"]
                    if not module_coverage.get(segment_id)
                ]
                duplicate_ids = [
                    segment_id
                    for segment_id in packet["reviewed_ids"]
                    if len(module_coverage.get(segment_id, [])) > 1
                ]
                if missing_ids or duplicate_ids:
                    skipped_packets.append(
                        {
                            "module": packet["module"],
                            "chunk_id": packet["chunk_id"],
                            "reason": "source coverage is incomplete or ambiguous",
                            "missing_ids": missing_ids,
                            "duplicate_ids": duplicate_ids,
                        }
                    )
                    continue

                records_by_path = {}
                for segment_id in packet["reviewed_ids"]:
                    record = module_coverage[segment_id][0]
                    records_by_path[record["draft_path"]] = record
                source_records = list(records_by_path.values())
                workers = {
                    canonical_digest(record["worker_receipt"])
                    for record in source_records
                }
                if len(workers) != 1:
                    skipped_packets.append(
                        {
                            "module": packet["module"],
                            "chunk_id": packet["chunk_id"],
                            "reason": (
                                "target packet combines different checker workers; "
                                "independent review is required"
                            ),
                            "missing_ids": [],
                            "duplicate_ids": [],
                        }
                    )
                    continue

                findings = []
                migration_sources = []
                for record in sorted(
                    source_records, key=lambda item: item["draft_path"]
                ):
                    contributed_ids = [
                        segment_id
                        for segment_id in packet["reviewed_ids"]
                        if segment_id in record["reviewed_ids"]
                    ]
                    migration_sources.append(
                        {
                            "draft_path": record["draft_path"],
                            "draft_digest": record["draft_digest"],
                            "packet_path": record["packet_path"],
                            "packet_digest": record["packet_digest"],
                            "worker_receipt": copy.deepcopy(
                                record["worker_receipt"]
                            ),
                            "contributed_ids": contributed_ids,
                        }
                    )
                for segment_id in packet["reviewed_ids"]:
                    record = module_coverage[segment_id][0]
                    issues = record["findings_by_id"].get(segment_id)
                    if issues:
                        findings.append(
                            {"id": segment_id, "issues": copy.deepcopy(issues)}
                        )
                source_precheck_by_id = {
                    segment_id: copy.deepcopy(
                        module_coverage[segment_id][0]["precheck_by_id"].get(
                            segment_id, []
                        )
                    )
                    for segment_id in packet["reviewed_ids"]
                }
                findings, normalizations = _transform_reused_findings(
                    findings,
                    packet["review_policy"],
                    packet,
                    source_precheck_by_id=source_precheck_by_id,
                )
                try:
                    findings = _validate_reused_findings(
                        target_state,
                        bases_by_chunk[packet["chunk_id"]],
                        packet,
                        findings,
                    )
                except (CheckFormatError, ValueError) as exc:
                    skipped_packets.append(
                        {
                            "module": packet["module"],
                            "chunk_id": packet["chunk_id"],
                            "reason": f"target contract rejected reused content: {exc}",
                            "missing_ids": [],
                            "duplicate_ids": [],
                        }
                    )
                    continue
                migration_receipt = _build_migration_receipt(
                    source_job=source_job,
                    source_state=source_state,
                    target_state=target_state,
                    input_identity=input_identity,
                    packet=packet,
                    source_records=migration_sources,
                    source_review_modes=sorted(
                        {
                            record["source_review_mode"]
                            for record in source_records
                        }
                    ),
                    normalizations=normalizations,
                )
                worker_receipt = source_records[0]["worker_receipt"]
                draft = _compact_draft_for_packet(
                    packet,
                    findings,
                    worker_receipt,
                    migration_receipt,
                )
                relative = f"{packet['module']}/chunk_{packet['chunk_id']:02d}.json"
                generated_payloads.append((relative, draft))
                generated_report.append(
                    {
                        "path": relative,
                        "module": packet["module"],
                        "chunk_id": packet["chunk_id"],
                        "reviewed_ids": list(packet["reviewed_ids"]),
                        "finding_count": len(findings),
                        "normalizations": copy.deepcopy(normalizations),
                        "source_drafts": [
                            record["draft_path"] for record in source_records
                        ],
                    }
                )

            report_payload = {
                "schema": REUSE_REPORT_SCHEMA,
                "version": REUSE_REPORT_VERSION,
                "source_job": str(source_job),
                "target_job": str(target_job),
                "input_identity": input_identity,
                "source_state_digest": canonical_digest(source_state),
                "target_state_digest": canonical_digest(target_state),
                "source_drafts_scanned": len(valid_source) + len(invalid_source),
                "source_drafts_valid": len(valid_source),
                "source_drafts_invalid": invalid_source,
                "target_ai_packets": len(target_ai_packets),
                "generated": generated_report,
                "skipped_packets": skipped_packets,
                "publication_required": True,
                "publication_command": (
                    "lqe_review.py publish --job <target> --chunk <NN> "
                    "--module <module> --input <reused draft>"
                ),
            }
            report = _with_digest(report_payload, "report_digest")
            target_state_digest = canonical_digest(target_state)
            target_manifest_digest = packet_manifest["manifest_digest"]
            with tempfile.TemporaryDirectory(
                dir=output_root.parent,
                prefix=".review-reuse.",
            ) as staging_name:
                staging = Path(staging_name)
                for relative, draft in generated_payloads:
                    write_json_atomic(staging / relative, draft)
                write_json_atomic(staging / "report.json", report)

                def verify_target() -> None:
                    if canonical_digest(load(target_job / "state.json")) != target_state_digest:
                        raise ValueError("target state changed during draft reuse")
                    live_manifest = load(target_job / "review_packets" / "manifest.json")
                    if live_manifest.get("manifest_digest") != target_manifest_digest:
                        raise ValueError("target review packet tree changed during draft reuse")

                publish_generation(
                    staging,
                    output_root,
                    archive_label=input_identity["segment_identity_digest"][:12],
                    pre_publish=verify_target,
                )
    except (ContextBundleError, OSError, ValueError) as exc:
        raise SystemExit(f"[review-reuse] {exc}") from exc

    print(
        f"[review-reuse] generated {len(generated_report)}/"
        f"{len(target_ai_packets)} rebound drafts; "
        f"invalid source drafts {len(invalid_source)}; output {output_root}"
    )


def cmd_auto_publish(args) -> None:
    job = Path(args.job)
    chunks = job / "chunks"
    with generation_lock(chunks, exclusive=False):
        state = load(job / "state.json")
        require_current_job_runtime(state, "review-auto-publish")
        split_manifest, bases, _, _, _ = _load_verified_generation_unlocked(
            job / "state.json",
            job / "errors_precheck.json",
            chunks,
            state=state,
        )
        try:
            (
                _,
                packets,
                context_batches,
                selected_evidence_index,
                batch_plan,
                cost_report,
                packet_manifest,
            ) = _build_review_artifacts(state, split_manifest, bases, job)
        except (ContextBundleError, OSError, ValueError) as exc:
            raise SystemExit(f"[review-auto-publish] worker context: {exc}") from exc
        if not _packet_tree_is_current(
            job / "review_packets",
            packet_manifest,
            packets,
            cost_report,
            batch_plan,
            context_batches,
            selected_evidence_index,
        ):
            raise SystemExit(
                "[review-auto-publish] prepared review packet tree is missing or "
                "stale; rerun lqe_review.py prepare"
            )
        bases_by_chunk = {base["chunk_id"]: base for base in bases}
        pending = [
            (bases_by_chunk[packet["chunk_id"]], packet)
            for packet in packets
            if not packet["requires_ai"]
        ]

    for base, packet in pending:
        entries = [
            {"id": segment["id"], "issues": []}
            for segment in base["segments"]
        ]
        _publish_full_entries(job, base, packet["module"], entries, packet)
    print(f"[review-auto-publish] published {len(pending)} no-AI packets")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--job", required=True)
    prepare.set_defaults(function=cmd_prepare)

    publish = sub.add_parser("publish")
    publish.add_argument("--job", required=True)
    publish.add_argument("--chunk", required=True, type=int)
    publish.add_argument("--module", required=True)
    publish.add_argument("--input", required=True)
    publish.set_defaults(function=cmd_publish)

    publish_directory = sub.add_parser("publish-directory")
    publish_directory.add_argument("--job", required=True)
    publish_directory.add_argument("--input-dir", required=True)
    publish_directory.set_defaults(function=cmd_publish_directory)

    reuse = sub.add_parser("reuse-drafts")
    reuse.add_argument("--source-job", required=True)
    reuse.add_argument("--job", required=True)
    reuse.add_argument("--drafts")
    reuse.add_argument("--out")
    reuse.set_defaults(function=cmd_reuse_drafts)

    auto_publish = sub.add_parser("auto-publish")
    auto_publish.add_argument("--job", required=True)
    auto_publish.set_defaults(function=cmd_auto_publish)

    args = parser.parse_args()
    args.function(args)


if __name__ == "__main__":
    main()
