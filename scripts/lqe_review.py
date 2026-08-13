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

from lqe_chunk import (
    _MODULE_ALLOWED_CATEGORIES,
    _load_verified_generation_unlocked,
    _module_issue_problem,
    _normalize_module_output,
    _precheck_provenance_problem,
    build_module_output,
    build_module_receipt,
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
from lqe_context_bundle import (
    ContextBundleError,
    WorkerContextBudgetError,
    build_context_bundle_set,
    build_worker_context_manifest,
    load_project_context_assets,
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
MAX_PACKETS_PER_WORKER = 4
MAX_REVIEW_TEXT_CHARS_PER_WORKER = 25_000
MAX_WORKER_INPUT_BYTES = 100_000
_DIGEST_PLACEHOLDER = "0" * 64

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
    summary = {
        "batch_id": batch_id,
        "packet_count": len(packets),
        "review_text_chars": sum(packet["review_text_chars"] for packet in packets),
        "packet_bytes": packet_bytes,
        "worker_input_bytes": (
            worker_manifest["budget"]["measured_bytes"]
            if worker_manifest is not None
            else packet_bytes
        ),
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
    worker_manifest = build_worker_context_manifest(
        state,
        module,
        bundle_set,
        max_worker_bytes=MAX_WORKER_INPUT_BYTES,
        packet_payloads=worker_payloads,
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
                f"worker review text is {chars} characters, exceeding budget "
                f"{MAX_REVIEW_TEXT_CHARS_PER_WORKER}; split the batch"
            )
        return _build_context_batch(
            state, module, batch_id, candidate, state_by_id
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
            current_bytes = 0

            def flush() -> None:
                nonlocal current, current_chars, current_bytes
                if not current:
                    return
                batches.append(_batch_summary(module, len(batches), current))
                current = []
                current_chars = 0
                current_bytes = 0

            for packet in module_packets:
                packet_chars = packet["review_text_chars"]
                packet_bytes = _json_bytes(packet)
                would_exceed = current and (
                    len(current) >= MAX_PACKETS_PER_WORKER
                    or current_chars + packet_chars
                    > MAX_REVIEW_TEXT_CHARS_PER_WORKER
                    or current_bytes + packet_bytes > MAX_WORKER_INPUT_BYTES
                )
                if would_exceed:
                    flush()
                if (
                    packet_chars > MAX_REVIEW_TEXT_CHARS_PER_WORKER
                    or packet_bytes > MAX_WORKER_INPUT_BYTES
                ):
                    raise WorkerContextBudgetError(
                        "single review packet exceeds worker input budget"
                    )
                current.append(packet)
                current_chars += packet_chars
                current_bytes += packet_bytes
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
            "max_worker_input_bytes": MAX_WORKER_INPUT_BYTES,
            "single_minimum_over_budget_fails": True,
        },
        "modules": batches_by_module,
    }
    return _with_digest(payload, "batch_plan_digest")


def _build_review_generation(
    state: dict,
    split_manifest: dict,
    bases: list[dict],
) -> tuple[list[str], list[dict], list[dict], dict]:
    load_project_context_assets(state)
    modules = required_modules(state)
    unsupported = sorted(set(modules) - _SUPPORTED_MODULES)
    if unsupported:
        raise ValueError(
            "unsupported required modules: " + ", ".join(unsupported)
        )
    packets = [
        build_review_packet(base, module, get_review_policy(state))
        for module in modules
        for base in bases
    ]
    context_batches = []
    state_by_id = {segment["id"]: segment for segment in state["segments"]}
    replacements = {}
    for module in modules:
        module_packets = [
            packet
            for packet in packets
            if packet["module"] == module and packet["requires_ai"]
        ]
        bound, batches = _partition_context_batches(
            state, module, module_packets, state_by_id
        )
        replacements.update(
            {(packet["module"], packet["chunk_id"]): packet for packet in bound}
        )
        context_batches.extend(batches)
    packets = [
        replacements.get((packet["module"], packet["chunk_id"]), packet)
        for packet in packets
    ]
    batch_plan = build_worker_batch_plan(
        split_manifest,
        modules,
        packets,
        context_batches=context_batches,
    )
    return modules, packets, context_batches, batch_plan


def _build_review_artifacts(
    state: dict,
    split_manifest: dict,
    bases: list[dict],
) -> tuple[list[str], list[dict], list[dict], dict, dict, dict]:
    modules, packets, context_batches, batch_plan = _build_review_generation(
        state, split_manifest, bases
    )
    cost_report = _build_cost_report(bases, modules, packets, batch_plan)
    packet_manifest = _build_packet_manifest(
        split_manifest,
        modules,
        packets,
        batch_plan,
        context_batches,
    )
    return (
        modules,
        packets,
        context_batches,
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
        "max_worker_input_bytes": max(
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
    return _with_digest(payload, "manifest_digest")


def _packet_tree_is_current(
    active: Path,
    packet_manifest: dict,
    packets: list[dict],
    cost_report: dict,
    batch_plan: dict,
    context_batches: list[dict] | None = None,
) -> bool:
    try:
        if load(active / "manifest.json") != packet_manifest:
            return False
        if load(active / "cost_report.json") != cost_report:
            return False
        if load(active / "batch_plan.json") != batch_plan:
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
            if load(
                active
                / _context_path(
                    item["module"], item["batch_id"], "worker_manifest.json"
                )
            ) != item["worker_manifest"]:
                return False
    except (OSError, ValueError):
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
                batch_plan,
                cost_report,
                packet_manifest,
            ) = _build_review_artifacts(state, split_manifest, bases)
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


def _live_review_packet_unlocked(
    job: Path,
    chunk_id: int,
    module: str,
) -> tuple[dict, dict, dict, dict]:
    chunks = job / "chunks"
    state = load(job / "state.json")
    require_current_job_runtime(state, "review-publish")
    split_manifest, bases, _, _, _ = _load_verified_generation_unlocked(
        job / "state.json",
        job / "errors_precheck.json",
        chunks,
        state=state,
    )
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
    try:
        (
            _,
            packets,
            context_batches,
            batch_plan,
            cost_report,
            packet_manifest,
        ) = _build_review_artifacts(state, split_manifest, bases)
    except (ContextBundleError, OSError, ValueError) as exc:
        raise SystemExit(f"[review-publish] worker context: {exc}") from exc
    if not _packet_tree_is_current(
        job / "review_packets",
        packet_manifest,
        packets,
        cost_report,
        batch_plan,
        context_batches,
    ):
        raise SystemExit(
            "[review-publish] prepared review packet tree is missing or stale; "
            "rerun lqe_review.py prepare"
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
) -> list[dict]:
    raw = load(path)
    if not isinstance(raw, dict):
        raise ValueError("compact draft must be an object")
    expected_fields = {
        "schema",
        "version",
        "module",
        "chunk_id",
        "packet_digest",
        "reviewed_ids",
        "findings",
    }
    context_binding_fields = {
        "worker_batch_id",
        "worker_packet_basis_digest",
        "context_bundle_set_digest",
        "worker_context_manifest_digest",
    }
    if "worker_context_manifest_digest" in packet:
        expected_fields.update(context_binding_fields)
    if set(raw) != expected_fields:
        raise ValueError("compact draft fields are invalid")
    expected_bindings = {
        "schema": COMPACT_DRAFT_SCHEMA,
        "version": COMPACT_DRAFT_VERSION,
        "module": packet["module"],
        "chunk_id": packet["chunk_id"],
        "packet_digest": packet["packet_digest"],
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
    return findings


def _publish_full_entries(
    job: Path,
    base: dict,
    module: str,
    entries: list[dict],
    packet: dict,
    *,
    draft_path: Path | None = None,
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
        expected_ids = {segment["id"] for segment in live_base["segments"]}
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
            for segment in live_base["segments"]
        }
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
                live_base,
                module,
                entries,
                label=destination.name,
            )
            receipt = build_module_receipt(
                payload,
                split_manifest,
                destination,
            )
            with tempfile.TemporaryDirectory(
                dir=outdir,
                prefix=".review-publish.",
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
            raise SystemExit(f"[review-publish] {exc}") from exc


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
        findings = _load_compact_draft(draft_path, packet)
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
    )
    print(
        f"[review-publish] reviewed {len(packet['reviewed_ids'])}, "
        f"findings {len(findings)}, formal coverage {len(entries)}"
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
                batch_plan,
                cost_report,
                packet_manifest,
            ) = _build_review_artifacts(state, split_manifest, bases)
        except (ContextBundleError, OSError, ValueError) as exc:
            raise SystemExit(f"[review-auto-publish] worker context: {exc}") from exc
        if not _packet_tree_is_current(
            job / "review_packets",
            packet_manifest,
            packets,
            cost_report,
            batch_plan,
            context_batches,
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

    auto_publish = sub.add_parser("auto-publish")
    auto_publish.add_argument("--job", required=True)
    auto_publish.set_defaults(function=cmd_auto_publish)

    args = parser.parse_args()
    args.function(args)


if __name__ == "__main__":
    main()
