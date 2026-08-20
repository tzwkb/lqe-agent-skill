#!/usr/bin/env python3
"""Build, publish, and validate report-only reference suggestions."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from lqe_chunk import (
    module_receipt_path,
    validate_module_receipt,
    verification_generation_lease,
)
from lqe_corrections import (
    CheckFormatError,
    validate_reference_target,
    verify_results,
)
from lqe_engine import (
    VALID_CATEGORIES,
    VALID_SEVERITIES,
    current_target,
    get_review_policy,
    read_json,
    required_modules,
    require_current_job_runtime,
    requires_bound_artifacts,
)
from lqe_paths import write_json_atomic
from lqe_result_contract import result_contract_path, validate_result_contract
from lqe_split_contract import canonical_digest
from lqe_target_form import load_target_form_policy
from lqe_context_bundle import (
    ContextBundleError,
    build_context_bundle,
    build_context_bundle_set,
    build_worker_context_manifest,
    load_project_context_assets,
    load_selected_context_evidence_index,
    measure_complete_worker_input_bytes,
    normalize_module_view,
    validate_context_bundle_set,
    validate_worker_context_manifest,
)
from lqe_profile_ingest import source_digest
from lqe_language_policies import (
    LanguagePolicyError,
    evaluate_resolved_constraint,
)

try:
    from lqe_context import project_segment_for_module
except ImportError:  # legacy bootstrap
    project_segment_for_module = None


PACKET_SCHEMA = "lqe.reference-suggestion-generation-packet"
PACKET_VERSION = 5
DRAFT_SCHEMA = "lqe.reference-suggestion-generation-draft"
DRAFT_VERSION = 5
CANDIDATE_SCHEMA = "lqe.reference-suggestion-candidates"
CANDIDATE_VERSION = 1
REVIEW_PACKET_SCHEMA = "lqe.suggestion-review-packet"
REVIEW_PACKET_VERSION = 1
REVIEW_DRAFT_SCHEMA = "lqe.suggestion-review-draft"
REVIEW_DRAFT_VERSION = 1
REVIEW_ARTIFACT_SCHEMA = "lqe.suggestion-review"
REVIEW_ARTIFACT_VERSION = 1
ARTIFACT_SCHEMA = "lqe.reference-suggestions"
ARTIFACT_VERSION = 5

PACKET_NAME = "reference_suggestions.packet.json"
DRAFT_NAME = "reference_suggestions.draft.json"
CANDIDATE_NAME = "reference_suggestions.candidates.json"
ARTIFACT_NAME = "reference_suggestions.json"
DEFAULT_SUGGESTION_SEVERITIES = ("Critical", "Major")
SUGGESTION_CONTEXT_DIR = "suggestion_context"
SUGGESTION_BUNDLE_SET_NAME = "bundle_set.json"
SUGGESTION_WORKER_MANIFEST_NAME = "worker_manifest.json"
SUGGESTION_CONTENT_INDEX_NAME = "content_index.json"
SUGGESTION_BATCH_PLAN_NAME = "batch_plan.json"
SUGGESTION_INPUT_MEASUREMENT_NAME = "input_measurement.json"
SUGGESTION_BATCH_ROOT = "suggestion_context/batches"
SELECTED_EVIDENCE_INDEX_PATH = "review_packets/selected_evidence_index.json"
SUGGESTION_REVIEW_INSTRUCTIONS_PATH = (
    Path(__file__).resolve().parents[1] / "references" / "suggestion_review.md"
)
_UNBOUND_WORKER_CONTEXT_DIGEST = "0" * 64
_WORKER_BATCH_SIZE_UNSET = object()

SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "schemas" / "suggestions"
SCHEMA_FILES = {
    (PACKET_SCHEMA, PACKET_VERSION): "generation_packet.schema.json",
    (DRAFT_SCHEMA, DRAFT_VERSION): "generation_draft.schema.json",
    (CANDIDATE_SCHEMA, CANDIDATE_VERSION): "candidates.schema.json",
    (REVIEW_PACKET_SCHEMA, REVIEW_PACKET_VERSION): "review_packet.schema.json",
    (REVIEW_DRAFT_SCHEMA, REVIEW_DRAFT_VERSION): "review_draft.schema.json",
    (REVIEW_ARTIFACT_SCHEMA, REVIEW_ARTIFACT_VERSION): "review_artifact.schema.json",
    (ARTIFACT_SCHEMA, ARTIFACT_VERSION): "final.schema.json",
}

HARD_REJECT = "hard_reject"
DETERMINISTIC_ACCEPT = "deterministic_accept"
INDEPENDENT_VERIFIER = "independent_verifier"
RISK_ROUTES = {HARD_REJECT, DETERMINISTIC_ACCEPT, INDEPENDENT_VERIFIER}
SEMANTIC_CHECK_FIELDS = (
    "subjects",
    "actions",
    "objects",
    "polarity_negation",
    "modality",
    "speech_act",
    "text_function",
    "intensity",
    "omissions",
    "unsupported_additions",
    "tone",
)


def _canonical_size(value: object) -> int:
    try:
        return len(json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"suggestion worker input is not canonical JSON: {exc}") from exc


def _require_digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _worker_packet_basis(packet: dict) -> dict:
    return _without_fields(
        packet,
        "packet_digest",
        "worker_context_manifest_digest",
    )


def measure_suggestion_worker_input(
    worker_manifest: dict,
    context_bundle_set: dict,
    packet: dict,
    *,
    label: str,
    additional_inputs: tuple[object, ...] | list[object] = (),
) -> int:
    try:
        return measure_complete_worker_input_bytes(
            worker_manifest,
            context_bundle_set,
            [packet],
            additional_inputs=additional_inputs,
        )
    except ContextBundleError as exc:
        raise ValueError(f"{label} worker context byte accounting: {exc}") from exc


def _normalize_worker_batch_size(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise ValueError("worker batch size must be a positive integer")
    return value


def _with_digest(payload: dict, field: str) -> dict:
    output = copy.deepcopy(payload)
    output.pop(field, None)
    output[field] = canonical_digest(output)
    return output


def _without_fields(payload: dict, *fields: str) -> dict:
    return {
        key: copy.deepcopy(value)
        for key, value in payload.items()
        if key not in fields
    }


def _worker_receipt(value: object, *, label: str) -> dict:
    if not isinstance(value, dict) or set(value) != {"worker_id", "run_id"}:
        raise ValueError(f"{label} must contain exactly worker_id and run_id")
    output = {}
    for field in ("worker_id", "run_id"):
        item = value.get(field)
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{label}.{field} must be a non-empty stable string")
        if item != item.strip():
            raise ValueError(f"{label}.{field} must not have surrounding whitespace")
        output[field] = item
    return output


def _selected_evidence_index_binding(value: object, *, label: str) -> dict:
    if not isinstance(value, dict) or set(value) != {"path", "digest"}:
        raise ValueError(f"{label} must contain exactly path and digest")
    path = value.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"{label}.path must be a non-empty relative path")
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{label}.path is unsafe")
    return {
        "path": path,
        "digest": _require_digest(value.get("digest"), f"{label}.digest"),
    }


def _merge_context_projection(target: dict, source: dict, *, label: str) -> None:
    for key, value in source.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
        elif isinstance(target[key], dict) and isinstance(value, dict):
            _merge_context_projection(target[key], value, label=f"{label}.{key}")
        elif target[key] != value:
            raise ValueError(f"{label}.{key} differs across required module views")


def build_suggestion_context_view(state: dict) -> dict:
    registry = state.get("resolved_context_descriptors")
    if not isinstance(registry, dict):
        raise ValueError("suggestion context requires resolved context descriptors")
    modules = list(dict.fromkeys((*required_modules(state), "suggestions")))
    normalized = {
        module: normalize_module_view(state, module, registry)
        for module in modules
    }
    merged = {
        "capabilities": sorted({
            capability
            for view in normalized.values()
            for capability in view["capabilities"]
        }),
        "dimensions": sorted({
            dimension
            for view in normalized.values()
            for dimension in view["dimensions"]
        }),
        "constraint_kinds": sorted({
            kind
            for view in normalized.values()
            for kind in view["constraint_kinds"]
        }),
        "include_constraints": any(
            view["include_constraints"] for view in normalized.values()
        ),
        "neighbors": {
            "before": max(view["neighbors"]["before"] for view in normalized.values()),
            "after": max(view["neighbors"]["after"] for view in normalized.values()),
            "include_target": any(
                view["neighbors"]["include_target"]
                for view in normalized.values()
            ),
            "boundary_mode": (
                "strict"
                if any(
                    view["neighbors"]["boundary_mode"] == "strict"
                    for view in normalized.values()
                )
                else "same_if_present"
            ),
        },
        "limits": {
            field: max(view["limits"][field] for view in normalized.values())
            for field in (
                "max_facts_per_entity",
                "max_relations",
                "max_runtime_examples",
            )
        },
    }
    merged = normalize_module_view(
        state,
        "suggestions",
        registry,
        view=merged,
    )
    return {
        "source_modules": modules,
        "module_views": normalized,
        "merged_view": merged,
        "basis_digest": canonical_digest({
            "source_modules": modules,
            "module_views": normalized,
            "merged_view": merged,
        }),
    }


def _projection_only_context_view(context_view_basis: dict) -> dict:
    view = copy.deepcopy(context_view_basis["merged_view"])
    view["capabilities"] = ["context.core@1"]
    view["dimensions"] = ["suggestions"]
    view["constraint_kinds"] = []
    view["include_constraints"] = False
    view["neighbors"] = {
        "before": 0,
        "after": 0,
        "include_target": False,
        "boundary_mode": view["neighbors"]["boundary_mode"],
    }
    view["limits"] = {
        "max_facts_per_entity": 0,
        "max_relations": 0,
        "max_runtime_examples": 0,
    }
    return view


def _safe_job_relative_path(job: Path, value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} path is missing")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{label} path is unsafe")
    path = (job / relative).resolve()
    try:
        path.relative_to(job.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} path escapes the job") from exc
    return path


def _selected_evidence_entry_from_bundle(bundle: dict) -> dict:
    identity = bundle["segment"]["identity"]
    neighbors = []
    for ordinal, neighbor in enumerate(bundle["neighbors"]):
        target = neighbor["target"]
        neighbors.append({
            "ordinal": ordinal,
            "id": copy.deepcopy(neighbor["id"]),
            "segment_key": neighbor["segment_key"],
            "source_digest": source_digest(neighbor["source"]),
            "context_digest": canonical_digest(neighbor["context"]),
            "target_digest": (
                source_digest(target) if isinstance(target, str) else None
            ),
            "target_status": neighbor["target_status"],
        })
    entry = {
        "module": bundle["module"],
        "segment_id": copy.deepcopy(identity["id"]),
        "segment_key": identity["segment_key"],
        "source_digest": identity["source_digest"],
        "segment_revision_digest": bundle["segment_revision_digest"],
        "context_bundle_digest": bundle["context_bundle_digest"],
        "context_status": bundle["context_status"],
        "module_view_digest": canonical_digest(bundle["module_view"]),
        "context_projection_digest": canonical_digest(
            bundle["segment"]["context"]
        ),
        "entity_fact_ids": list(bundle["entity_fact_ids"]),
        "relation_ids": list(bundle["relation_ids"]),
        "runtime_example_ids": list(bundle["runtime_example_ids"]),
        "term_evidence_ids": list(bundle["term_evidence_ids"]),
        "resolved_constraint_digests": sorted({
            canonical_digest(item) for item in bundle["resolved_constraints"]
        }),
        "neighbors": neighbors,
        "neighbor_selection_digest": canonical_digest(
            bundle["neighbor_selection"]
        ),
        "runtime_example_selection_digest": canonical_digest(
            bundle["runtime_example_selection"]
        ),
    }
    entry["selection_digest"] = canonical_digest(entry)
    return entry


def _put_selected_record(
    target: dict,
    identifier: str,
    value: object,
    *,
    label: str,
) -> None:
    if identifier in target and target[identifier] != value:
        raise ValueError(f"checker-selected {label} {identifier!r} conflicts")
    target[identifier] = copy.deepcopy(value)


def _strict_evidence_union(records: list[dict]) -> dict:
    facts = {}
    relations = {}
    examples = {}
    constraints = {}
    neighbors = {}
    module_selections = []
    context_projections = []
    context_projection = {}
    for record in records:
        entry = record["entry"]
        bundle = record["bundle"]
        shared = record["shared_context_assets"]
        selected_projection = bundle["segment"]["context"]
        if canonical_digest(selected_projection) != entry["context_projection_digest"]:
            raise ValueError(
                "checker-selected context projection receipt is stale"
            )
        _merge_context_projection(
            context_projection,
            selected_projection,
            label="checker-selected context projection union",
        )
        context_projections.append({
            "module": entry["module"],
            "context_projection_digest": entry["context_projection_digest"],
            "context_projection": copy.deepcopy(selected_projection),
        })
        if entry["term_evidence_ids"]:
            raise ValueError(
                "checker-selected term evidence has no bound suggestion asset"
            )
        for identifier in entry["entity_fact_ids"]:
            if identifier not in shared["entities"]:
                raise ValueError(
                    f"checker-selected entity fact {identifier!r} is missing"
                )
            _put_selected_record(
                facts,
                identifier,
                shared["entities"][identifier],
                label="entity fact",
            )
        for identifier in entry["relation_ids"]:
            if identifier not in shared["relations"]:
                raise ValueError(
                    f"checker-selected relation {identifier!r} is missing"
                )
            _put_selected_record(
                relations,
                identifier,
                shared["relations"][identifier],
                label="relation",
            )
        for identifier in entry["runtime_example_ids"]:
            if identifier not in shared["review_examples"]:
                raise ValueError(
                    f"checker-selected runtime example {identifier!r} is missing"
                )
            _put_selected_record(
                examples,
                identifier,
                shared["review_examples"][identifier],
                label="runtime example",
            )
        for constraint in bundle["resolved_constraints"]:
            digest = canonical_digest(constraint)
            _put_selected_record(
                constraints,
                digest,
                constraint,
                label="resolved constraint",
            )
        if sorted({
            canonical_digest(item) for item in bundle["resolved_constraints"]
        }) != entry["resolved_constraint_digests"]:
            raise ValueError(
                "checker-selected resolved constraint receipts are stale"
            )
        for neighbor in bundle["neighbors"]:
            digest = canonical_digest(neighbor)
            existing = neighbors.get(digest)
            if existing is None:
                neighbors[digest] = {
                    "evidence_digest": digest,
                    "selected_by_modules": [entry["module"]],
                    "evidence": copy.deepcopy(neighbor),
                }
            elif entry["module"] not in existing["selected_by_modules"]:
                existing["selected_by_modules"].append(entry["module"])
        module_selections.append({
            "module": entry["module"],
            "context_bundle_set_path": record["context_bundle_set_path"],
            "context_bundle_set_digest": record["context_bundle_set_digest"],
            "selection_receipt": copy.deepcopy(entry),
        })
    for item in neighbors.values():
        item["selected_by_modules"].sort()
    output = {
        "mode": "checker_selected_strict_union",
        "module_selections": sorted(
            module_selections,
            key=lambda item: item["module"],
        ),
        "context_projections": sorted(
            context_projections,
            key=lambda item: item["module"],
        ),
        "context_projection": context_projection,
        "entity_facts": {key: facts[key] for key in sorted(facts)},
        "relations": {key: relations[key] for key in sorted(relations)},
        "runtime_examples": {key: examples[key] for key in sorted(examples)},
        "resolved_constraints": [
            constraints[key] for key in sorted(constraints)
        ],
        "neighbors": [neighbors[key] for key in sorted(neighbors)],
    }
    output["strict_union_digest"] = canonical_digest(output)
    return output


def _load_checker_selected_evidence(
    job: Path,
    state: dict,
) -> dict | None:
    if not requires_bound_artifacts(state):
        return None
    index_path = _safe_job_relative_path(
        job,
        SELECTED_EVIDENCE_INDEX_PATH,
        label="checker-selected evidence index",
    )
    if not index_path.is_file():
        raise ValueError(
            "checker-selected evidence index is missing; rerun lqe_review.py prepare"
        )
    index = load_selected_context_evidence_index(index_path)
    expected_modules = sorted(required_modules(state))
    if index["modules"] != expected_modules:
        raise ValueError("checker-selected evidence module coverage is stale")
    live_bindings = {
        "profile": {
            "digest": state.get("profile_digest"),
            "overlay_digest": state.get("profile_overlay_digest"),
        },
        "project_asset_snapshot": {
            "digest": state.get("project_asset_snapshot_digest")
        },
        "capability_resolution": {
            "digest": state.get("capability_resolution_digest")
        },
    }
    for field, expected in live_bindings.items():
        if index[field] != expected:
            raise ValueError(f"checker-selected evidence {field} binding is stale")

    split_manifest_path = job / "chunks" / "split_manifest.json"
    dedup_path = job / "chunks" / "dedup_map.json"
    if not split_manifest_path.is_file() or not dedup_path.is_file():
        raise ValueError("checker-selected evidence split inputs are missing")
    split_manifest = read_json(split_manifest_path)
    if (
        index["split_fingerprint"] != split_manifest.get("split_fingerprint")
        or index["split_manifest_digest"] != split_manifest.get("manifest_digest")
    ):
        raise ValueError("checker-selected evidence split binding is stale")
    dedup_map = read_json(dedup_path)
    representative_by_id = {}
    if not isinstance(dedup_map, dict):
        raise ValueError("checker-selected evidence dedup map is invalid")
    for raw_representative, members in dedup_map.items():
        try:
            representative = int(raw_representative)
        except (TypeError, ValueError) as exc:
            raise ValueError("checker-selected evidence representative is invalid") from exc
        if (
            not isinstance(members, list)
            or any(type(item) is not int for item in members)
            or representative not in members
        ):
            raise ValueError("checker-selected evidence dedup members are invalid")
        for segment_id in members:
            if segment_id in representative_by_id:
                raise ValueError("checker-selected evidence dedup members overlap")
            representative_by_id[segment_id] = representative
    state_ids = {segment["id"] for segment in state["segments"]}
    if set(representative_by_id) != state_ids:
        raise ValueError("checker-selected evidence dedup coverage is stale")
    state_by_segment_key = {}
    for segment in state["segments"]:
        segment_key = segment.get("segment_key")
        if not isinstance(segment_key, str) or not segment_key:
            raise ValueError("checker-selected evidence state segment key is invalid")
        if segment_key in state_by_segment_key:
            raise ValueError("checker-selected evidence state segment key is duplicated")
        state_by_segment_key[segment_key] = segment
    try:
        loaded_assets = load_project_context_assets(state)
    except (ContextBundleError, OSError, ValueError) as exc:
        raise ValueError(f"checker-selected evidence assets: {exc}") from exc

    batch_plan_path = job / "review_packets" / "batch_plan.json"
    if not batch_plan_path.is_file():
        raise ValueError("checker-selected evidence review batch plan is missing")
    batch_plan = read_json(batch_plan_path)
    if not isinstance(batch_plan, dict) or batch_plan.get("modules") is None:
        raise ValueError("checker-selected evidence review batch plan is invalid")
    expected_plan_digest = canonical_digest({
        key: value
        for key, value in batch_plan.items()
        if key != "batch_plan_digest"
    })
    if batch_plan.get("batch_plan_digest") != expected_plan_digest:
        raise ValueError("checker-selected evidence review batch plan is stale")
    if sorted(batch_plan["modules"]) != expected_modules:
        raise ValueError("checker-selected evidence batch module coverage is stale")
    if (
        batch_plan.get("selected_evidence_index_path")
        != Path(SELECTED_EVIDENCE_INDEX_PATH).name
        or batch_plan.get("selected_evidence_index_digest")
        != index["index_digest"]
    ):
        raise ValueError("checker-selected evidence batch-plan binding is stale")

    bundle_records = {}
    review_packets = {}
    for module in expected_modules:
        batches = batch_plan["modules"].get(module)
        if not isinstance(batches, list):
            raise ValueError("checker-selected evidence module batches are invalid")
        for batch in batches:
            if not isinstance(batch, dict):
                raise ValueError("checker-selected evidence batch is invalid")
            relative = batch.get("context_bundle_set_path")
            if not isinstance(relative, str):
                raise ValueError("checker-selected evidence bundle path is missing")
            bundle_path = _safe_job_relative_path(
                job,
                f"review_packets/{relative}",
                label="checker-selected context bundle set",
            )
            if not bundle_path.is_file():
                raise ValueError("checker-selected context bundle set is missing")
            bundle_set = validate_context_bundle_set(read_json(bundle_path))
            if (
                bundle_set["module"] != module
                or bundle_set["context_bundle_set_digest"]
                != batch.get("context_bundle_set_digest")
            ):
                raise ValueError("checker-selected context bundle set is stale")
            for bundle in bundle_set["bundles"]:
                segment_key = bundle["segment"]["identity"]["segment_key"]
                live_segment = state_by_segment_key.get(segment_key)
                if live_segment is None:
                    raise ValueError(
                        "checker-selected context bundle segment is stale"
                    )
                try:
                    rebuilt_bundle = build_context_bundle(
                        state,
                        live_segment,
                        module,
                        loaded_assets=loaded_assets,
                        module_view=bundle["module_view"],
                        projection_modules=bundle["projection_modules"],
                    )
                except (ContextBundleError, OSError, ValueError) as exc:
                    raise ValueError(
                        f"checker-selected context bundle live rebuild: {exc}"
                    ) from exc
                if rebuilt_bundle != bundle:
                    raise ValueError(
                        "checker-selected context bundle differs from live state"
                    )
            packet_records = batch.get("packets")
            if not isinstance(packet_records, list) or not packet_records:
                raise ValueError("checker-selected evidence packet list is invalid")
            for packet_record in packet_records:
                packet_relative = packet_record.get("path")
                if not isinstance(packet_relative, str):
                    raise ValueError("checker-selected review packet path is missing")
                packet_path = _safe_job_relative_path(
                    job,
                    f"review_packets/{packet_relative}",
                    label="checker-selected review packet",
                )
                if not packet_path.is_file():
                    raise ValueError("checker-selected review packet is missing")
                review_packet = read_json(packet_path)
                if (
                    review_packet.get("packet_digest")
                    != canonical_digest({
                        key: value
                        for key, value in review_packet.items()
                        if key != "packet_digest"
                    })
                    or
                    review_packet.get("module") != module
                    or review_packet.get("packet_digest")
                    != packet_record.get("packet_digest")
                    or review_packet.get("selected_evidence_index_digest")
                    != index["index_digest"]
                    or review_packet.get("selected_evidence_index_path")
                    != Path(SELECTED_EVIDENCE_INDEX_PATH).name
                ):
                    raise ValueError("checker-selected review packet is stale")
                for segment_id in review_packet.get("reviewed_ids", []):
                    packet_key = (module, segment_id)
                    if packet_key in review_packets:
                        raise ValueError(
                            "checker-selected review packet coverage overlaps"
                        )
                    review_packets[packet_key] = review_packet
            for bundle in bundle_set["bundles"]:
                key = (module, bundle["context_bundle_digest"])
                if key in bundle_records:
                    raise ValueError("checker-selected context bundle is duplicated")
                bundle_records[key] = {
                    "bundle": bundle,
                    "shared_context_assets": bundle_set[
                        "shared_context_assets"
                    ],
                    "context_bundle_set_path": f"review_packets/{relative}",
                    "context_bundle_set_digest": bundle_set[
                        "context_bundle_set_digest"
                    ],
                }

    entries_by_segment = {}
    matched_bundle_keys = set()
    checker_worker_receipts = set()
    checker_worker_receipts_by_segment = {}
    for entry in index["entries"]:
        key = (entry["module"], entry["context_bundle_digest"])
        record = bundle_records.get(key)
        if record is None:
            raise ValueError("checker-selected evidence bundle receipt is unresolved")
        if _selected_evidence_entry_from_bundle(record["bundle"]) != entry:
            raise ValueError("checker-selected evidence entry differs from its bundle")
        review_packet = review_packets.get((entry["module"], entry["segment_id"]))
        if review_packet is None:
            raise ValueError("checker-selected evidence review packet is unresolved")
        chunk_id = review_packet.get("chunk_id")
        if type(chunk_id) is not int or chunk_id < 0:
            raise ValueError("checker-selected evidence chunk id is invalid")
        module_path = (
            job / "chunks" / f"chunk_{chunk_id:02d}.{entry['module']}.json"
        )
        if not module_path.is_file():
            raise ValueError("checker-selected module output is missing")
        module_payload = read_json(module_path)
        receipt_path = module_receipt_path(module_path)
        try:
            validate_module_receipt(
                receipt_path,
                module_payload,
                split_manifest,
                module_path,
            )
        except CheckFormatError as exc:
            raise ValueError(f"checker-selected module receipt: {exc}") from exc
        receipt = read_json(receipt_path)
        provenance = receipt.get("review_provenance")
        if (
            not isinstance(provenance, dict)
            or provenance.get("review_packet_digest")
            != review_packet["packet_digest"]
            or provenance.get("selected_evidence_index_digest")
            != index["index_digest"]
        ):
            raise ValueError("checker-selected module receipt provenance is stale")
        worker_receipt = _worker_receipt(
            provenance.get("worker_receipt"),
            label="checker worker receipt",
        )
        worker_id = worker_receipt["worker_id"]
        run_id = worker_receipt["run_id"]
        checker_worker_receipts.add((worker_id, run_id))
        checker_worker_receipts_by_segment.setdefault(
            entry["segment_id"], set()
        ).add((worker_id, run_id))
        matched_bundle_keys.add(key)
        entries_by_segment.setdefault(entry["segment_id"], []).append({
            **record,
            "entry": entry,
        })
    if matched_bundle_keys != set(bundle_records):
        raise ValueError("checker-selected evidence index coverage is incomplete")

    segment_unions = {
        segment_id: _strict_evidence_union(records)
        for segment_id, records in entries_by_segment.items()
    }
    return {
        "binding": {
            "path": SELECTED_EVIDENCE_INDEX_PATH,
            "digest": index["index_digest"],
        },
        "index": index,
        "representative_by_id": representative_by_id,
        "segment_unions": segment_unions,
        "checker_worker_receipts": [
            {"worker_id": worker_id, "run_id": run_id}
            for worker_id, run_id in sorted(checker_worker_receipts)
        ],
        "checker_worker_receipts_by_segment": {
            segment_id: [
                {"worker_id": worker_id, "run_id": run_id}
                for worker_id, run_id in sorted(receipts)
            ]
            for segment_id, receipts in sorted(
                checker_worker_receipts_by_segment.items()
            )
        },
    }


def _project_segment_for_modules(
    segment: dict,
    modules: list[str],
    registry: dict | None,
) -> dict:
    if project_segment_for_module is None:
        return {}
    output = {}
    for module in modules:
        projection = project_segment_for_module(segment, module, registry)
        _merge_context_projection(
            output,
            projection,
            label=f"segment {segment.get('id')!r} context projection",
        )
    return output


def _dialogue_context_readiness(
    projection: dict,
    context_view_basis: dict | None,
) -> dict:
    capabilities = set(
        (context_view_basis or {}).get("merged_view", {}).get("capabilities", [])
    )
    if "context.dialogue@1" not in capabilities:
        return {"applicable": False, "status": "not_enabled", "missing_fields": []}
    dialogue = (projection.get("extensions") or {}).get("dialogue")
    if not isinstance(dialogue, dict) or dialogue.get("status") == "not_applicable":
        return {"applicable": False, "status": "not_applicable", "missing_fields": []}
    missing = [
        field
        for field in ("speaker_id", "addressee_ids", "relationship_stage")
        if dialogue.get(field) in (None, "", [], {})
    ]
    status = dialogue.get("status")
    if status not in {"ready", "incomplete", "conflict"}:
        status = "incomplete" if missing else "ready"
    if missing and status == "ready":
        status = "incomplete"
    return {
        "applicable": True,
        "status": status,
        "missing_fields": missing,
    }


def _job_relative_or_embedded_resource(
    job: Path,
    identifier: str,
    path_value: object,
) -> dict:
    if not isinstance(path_value, str) or not path_value.strip():
        raise ValueError(f"suggestion context resource {identifier} has no path")
    path = Path(path_value)
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError(
            f"suggestion context resource {identifier} must be a regular bound file"
        )
    payload = path.read_bytes()
    result = {
        "id": identifier,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }
    try:
        relative = path.resolve().relative_to(job.resolve())
    except ValueError:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"external suggestion context resource {identifier} is not UTF-8"
            ) from exc
        result.update({"delivery": "embedded_text", "content": text})
    else:
        result.update({"delivery": "job_relative_path", "path": relative.as_posix()})
    return result


def build_suggestion_content_index(
    job: Path,
    state: dict,
    *,
    context_rel_dir: str = SUGGESTION_CONTEXT_DIR,
    selected_evidence_index: dict | None = None,
) -> dict:
    resources = []
    seen_paths = set()

    def add(identifier: str, path_value: object) -> None:
        if not isinstance(path_value, str) or not path_value.strip():
            return
        real = Path(path_value).resolve()
        if real in seen_paths:
            return
        seen_paths.add(real)
        resources.append(
            _job_relative_or_embedded_resource(job, identifier, path_value)
        )

    for field in (
        "sg_path",
        "background_path",
        "confirmed_rules_path",
        "lang_notes_path",
    ):
        add(field, state.get(field))
    payload = {
        "schema": "lqe.suggestion-context-content-index",
        "version": 1,
        "resources": sorted(resources, key=lambda item: item["id"]),
        "selected_context": {
            "bundle_set_path": (
                f"{context_rel_dir}/{SUGGESTION_BUNDLE_SET_NAME}"
            ),
            "worker_manifest_path": (
                f"{context_rel_dir}/{SUGGESTION_WORKER_MANIFEST_NAME}"
            ),
            **(
                {
                    "checker_selected_evidence_index_path":
                        selected_evidence_index["path"],
                    "checker_selected_evidence_index_digest":
                        selected_evidence_index["digest"],
                }
                if selected_evidence_index is not None
                else {}
            ),
        },
    }
    payload["content_index_digest"] = canonical_digest(payload)
    return payload


def _suggestion_verifier_instruction_input() -> tuple[dict, bytes]:
    path = SUGGESTION_REVIEW_INSTRUCTIONS_PATH
    if not path.is_file():
        raise ValueError(f"suggestion verifier instructions are missing: {path}")
    payload = path.read_bytes()
    return (
        {
            "path": "references/suggestion_review.md",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        },
        payload,
    )


def _review_packet_worker_receipts(
    generation_packet: dict,
    candidate_artifact: dict,
) -> tuple[list[dict], list[dict]]:
    def canonical_list(value: object, *, label: str) -> tuple[list[dict], set[tuple[str, str]]]:
        if not isinstance(value, list) or not value:
            raise ValueError(f"{label} are missing")
        receipts = [
            _worker_receipt(item, label=f"{label} item")
            for item in value
        ]
        pairs = {
            (item["worker_id"], item["run_id"])
            for item in receipts
        }
        if len(pairs) != len(receipts):
            raise ValueError(f"{label} contain duplicates")
        return receipts, pairs

    all_generation, all_generation_pairs = canonical_list(
        candidate_artifact.get("generation_worker_receipts"),
        label="candidate generation worker receipts",
    )
    packet_digest = generation_packet.get("packet_digest")
    if packet_digest == candidate_artifact.get("generation_packet_digest"):
        generation_receipts = all_generation
    else:
        generation_batches = candidate_artifact.get("generation_batches")
        if not isinstance(generation_batches, list):
            raise ValueError("candidate generation batch evidence is missing")
        matches = [
            item
            for item in generation_batches
            if isinstance(item, dict) and item.get("packet_digest") == packet_digest
        ]
        if len(matches) != 1:
            raise ValueError("generation packet receipt binding is unresolved")
        match = matches[0]
        if match.get("reviewed_ids") != generation_packet.get("reviewed_ids"):
            raise ValueError("generation packet receipt coverage is stale")
        generation_receipts = [
            _worker_receipt(
                match.get("worker_receipt"),
                label="generation batch worker receipt",
            )
        ]
    generation_pairs = {
        (item["worker_id"], item["run_id"])
        for item in generation_receipts
    }
    if (
        len(generation_pairs) != len(generation_receipts)
        or not generation_pairs.issubset(all_generation_pairs)
    ):
        raise ValueError("generation packet worker receipts are stale")

    if "selected_evidence_index" not in candidate_artifact:
        if (
            generation_packet.get("selected_evidence_index") is not None
            or generation_packet.get("checker_worker_receipts") is not None
        ):
            raise ValueError("generation packet has unexpected checker evidence")
        return copy.deepcopy(generation_receipts), []

    if generation_packet.get("selected_evidence_index") != candidate_artifact[
        "selected_evidence_index"
    ]:
        raise ValueError("generation packet selected evidence is stale")
    all_checkers, all_checker_pairs = canonical_list(
        candidate_artifact.get("checker_worker_receipts"),
        label="candidate checker worker receipts",
    )
    checker_receipts, checker_pairs = canonical_list(
        generation_packet.get("checker_worker_receipts"),
        label="generation packet checker worker receipts",
    )
    if not checker_pairs.issubset(all_checker_pairs):
        raise ValueError("generation packet checker worker receipts are stale")
    for generation in generation_receipts:
        for checker in checker_receipts:
            if (
                generation["worker_id"] == checker["worker_id"]
                or generation["run_id"] == checker["run_id"]
            ):
                raise ValueError("generation packet worker overlaps a checker worker")
    return copy.deepcopy(generation_receipts), copy.deepcopy(checker_receipts)


def build_suggestion_review_packet(
    generation_packet: dict,
    candidate_artifact: dict,
    *,
    included_review_ids: set[int] | None = None,
) -> dict:
    generation_worker_receipts, checker_worker_receipts = (
        _review_packet_worker_receipts(generation_packet, candidate_artifact)
    )
    generation_worker_context = generation_packet.get("instructions", {}).get(
        "worker_context", {}
    )
    candidate_map = {
        entry["id"]: entry for entry in candidate_artifact["entries"]
    }
    generation_segments = {
        entry["id"]: entry for entry in generation_packet["segments"]
    }
    candidate_routes = {
        entry["id"]: entry for entry in candidate_artifact["routes"]
    }
    reviewed_ids = [
        route["id"]
        for route in candidate_artifact["routes"]
        if route["risk_route"] == INDEPENDENT_VERIFIER
        and (
            included_review_ids is None
            or route["id"] in included_review_ids
        )
    ]
    missing_generation_segments = sorted(set(reviewed_ids) - set(generation_segments))
    if missing_generation_segments:
        raise ValueError(
            "suggestion review packet lacks generation batch segments: "
            f"{missing_generation_segments}"
        )
    entries = []
    for segment_id in reviewed_ids:
        segment = generation_segments[segment_id]
        candidate = candidate_map[segment_id]
        entries.append({
            **copy.deepcopy(segment),
            "reference_target": candidate["reference_target"],
            "source_semantics": copy.deepcopy(candidate["source_semantics"]),
            "tone_decision": copy.deepcopy(candidate["tone_decision"]),
            "candidate_digest": candidate["candidate_digest"],
            "candidate_constraint_evaluations": copy.deepcopy(
                candidate_routes[segment_id].get(
                    "candidate_constraint_evaluations", []
                )
            ),
        })
    verifier_instructions, _ = _suggestion_verifier_instruction_input()
    payload = {
        "schema": REVIEW_PACKET_SCHEMA,
        "version": REVIEW_PACKET_VERSION,
        **{
            key: copy.deepcopy(
                generation_packet[key]
                if key in {
                    "context_bundle_set_digest",
                    "worker_context_manifest_digest",
                }
                else candidate_artifact[key]
            )
            for key in (
                "job_id",
                "job_runtime_contract_version",
                "state_revision_digest",
                "results_basis_digest",
                "project_asset_snapshot_digest",
                "capability_resolution_digest",
                "context_bundle_set_digest",
                "worker_context_manifest_digest",
                "protected_signature_digest",
                "created_at",
            )
        },
        "reviewed_ids": reviewed_ids,
        "candidate_artifact_digest": candidate_artifact["artifact_digest"],
        "generation_worker_receipts": generation_worker_receipts,
        **(
            {
                "selected_evidence_index": copy.deepcopy(
                    candidate_artifact["selected_evidence_index"]
                ),
                "checker_worker_receipts": checker_worker_receipts,
            }
            if "selected_evidence_index" in candidate_artifact
            else {}
        ),
        **(
            {"generation_batch_id": generation_packet["batch"]["batch_id"]}
            if isinstance(generation_packet.get("batch"), dict)
            else {}
        ),
        "entries": entries,
        "instructions": {
            "decision_values": ["accept", "reject", "human_required"],
            "candidate_text_is_read_only": True,
            "verify_source_meaning": True,
            "verify_all_known_issues": True,
            "verify_confirmed_constraints": True,
            "verifier_instructions": verifier_instructions,
            "worker_context": {
                "input_measurement_path": (
                    "suggestion_review_context/input_measurement.json"
                ),
                "manifest_path": generation_worker_context.get(
                    "manifest_path",
                    "suggestion_context/worker_manifest.json",
                ),
                "manifest_digest": candidate_artifact[
                    "worker_context_manifest_digest"
                ],
                "bundle_set_path": generation_worker_context.get(
                    "bundle_set_path",
                    "suggestion_context/bundle_set.json",
                ),
                "bundle_set_digest": candidate_artifact[
                    "context_bundle_set_digest"
                ],
                "same_evidence_as_generation": True,
                **(
                    {
                        "checker_selected_evidence_index_path":
                            candidate_artifact["selected_evidence_index"]["path"],
                        "checker_selected_evidence_index_digest":
                            candidate_artifact["selected_evidence_index"]["digest"],
                        "checker_selected_evidence_is_embedded_per_segment": True,
                        "worker_reads_full_checker_index": False,
                    }
                    if "selected_evidence_index" in candidate_artifact
                    else {}
                ),
                **(
                    {
                        "content_index_path": generation_packet[
                            "content_index"
                        ]["path"],
                        "content_index_digest": generation_packet[
                            "content_index"
                        ]["digest"],
                    }
                    if isinstance(generation_packet.get("content_index"), dict)
                    else {}
                ),
            },
        },
    }
    payload = _with_digest(payload, "packet_digest")
    _validate_schema(payload, REVIEW_PACKET_SCHEMA, REVIEW_PACKET_VERSION)
    return payload


def _stable_created_at(state: dict | None) -> str:
    value = (state or {}).get("created_at")
    return value if isinstance(value, str) and value else "1970-01-01T00:00:00Z"


def _validate_schema(payload: object, schema: str, version: int) -> None:
    filename = SCHEMA_FILES.get((schema, version))
    if filename is None:
        raise ValueError(f"unsupported suggestion schema/version: {schema!r} v{version!r}")
    schema_path = SCHEMA_ROOT / filename
    definition = read_json(schema_path)
    errors = sorted(
        Draft202012Validator(definition).iter_errors(payload),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        raise ValueError(f"{schema} v{version} schema error at {location}: {error.message}")


def _validate_self_digest(payload: dict, field: str, label: str) -> None:
    expected = _with_digest(_without_fields(payload, field), field)
    if payload != expected:
        raise ValueError(f"{label} {field} is invalid")


def _publisher_receipt(publisher: str, payload: dict) -> dict:
    receipt = {
        "publisher": publisher,
        "basis_digest": canonical_digest(
            _without_fields(payload, "publisher_receipt", "artifact_digest")
        ),
    }
    return _with_digest(receipt, "receipt_digest")


def _validate_publisher_receipt(payload: dict, publisher: str, label: str) -> None:
    expected = _publisher_receipt(publisher, payload)
    if payload.get("publisher_receipt") != expected:
        raise ValueError(f"{label} publisher receipt is invalid")


def _results_basis(results: list[dict]) -> list[dict]:
    basis = copy.deepcopy(results)
    for entry in basis:
        for issue in entry.get("errors", []):
            issue.pop("repeated", None)
    return basis


def _issue_projection(issue: dict, index: int = 0) -> dict:
    provenance = issue.get("review_provenance")
    module = (
        provenance.get("review_module")
        if isinstance(provenance, dict)
        else None
    )
    module = module if isinstance(module, str) and module else "unknown"
    projected = {
        key: copy.deepcopy(issue.get(key))
        for key in (
            "category",
            "severity",
            "comment",
            "needs_confirmation",
            "edit",
            "term_source",
            "expected_targets",
            "term_spans",
            "protected",
            "resolution_status",
            "reason_codes",
            "non_authorizing_evidence",
            "review_provenance",
            "precheck_ref",
        )
        if key in issue
    }
    projected["issue_id"] = f"{module}:{index}"
    return projected


def _is_unresolved_terminology_review(issue: object) -> bool:
    if not isinstance(issue, dict) or issue.get("needs_confirmation") is not True:
        return False
    if issue.get("resolution_status") == "reference_allowed":
        return False
    provenance = issue.get("review_provenance")
    return issue.get("category") == "Terminology" or (
        isinstance(provenance, dict)
        and provenance.get("review_module") == "terminology"
    )


def _segment_block_reason_codes(segment: dict, errors: list[dict]) -> list[str]:
    reasons = []
    input_status = segment.get("input_status")
    if segment.get("blocked") is True or input_status in {
        "blocked",
        "input_blocked",
        "SOURCE_VERSION_MISMATCH",
    }:
        reasons.append("INPUT_BLOCKED")
    if segment.get("protected") is True:
        reasons.append("SEGMENT_PROTECTED")
    if any(_is_unresolved_terminology_review(issue) for issue in errors):
        reasons.append("UNRESOLVED_TERMINOLOGY_REVIEW")
    if any(
        issue.get("resolution_status") in {"human_choice_required", "conflict"}
        for issue in errors
        if isinstance(issue, dict)
    ):
        reasons.append("UNRESOLVED_REVIEW_DECISION")
    if any(
        constraint.get("status") == "conflict"
        for constraint in segment.get("resolved_constraints", [])
        if isinstance(constraint, dict)
    ):
        reasons.append("CONSTRAINT_CONFLICT")
    return list(dict.fromkeys(reasons))


def _normalize_selection(
    selection: object | None,
    review_policy: dict | None = None,
) -> dict:
    policy = get_review_policy(
        {"review_policy": review_policy} if review_policy is not None else {}
    )
    supported_severities = tuple(policy["suggestion_candidate_severities"])
    if selection is None:
        return {
            "categories": [],
            "severities": list(supported_severities),
            "only_missing": False,
        }
    if not isinstance(selection, dict) or set(selection) != {
        "categories",
        "severities",
        "only_missing",
    }:
        raise ValueError("reference suggestion selection is invalid")
    categories = selection["categories"]
    severities = selection["severities"]
    only_missing = selection["only_missing"]
    if (
        not isinstance(categories, list)
        or any(not isinstance(item, str) or not item.strip() for item in categories)
        or len(categories) != len(set(categories))
    ):
        raise ValueError("reference suggestion categories are invalid")
    unknown = sorted(set(categories) - set(VALID_CATEGORIES))
    if unknown:
        raise ValueError(f"reference suggestion categories are unknown: {unknown}")
    if (
        not isinstance(severities, list)
        or not severities
        or any(not isinstance(item, str) or not item.strip() for item in severities)
        or len(severities) != len(set(severities))
    ):
        raise ValueError("reference suggestion severities are invalid")
    unknown = sorted(set(severities) - VALID_SEVERITIES)
    if unknown:
        raise ValueError(f"reference suggestion severities are unknown: {unknown}")
    unsupported = sorted(set(severities) - set(supported_severities))
    if unsupported:
        if policy["mode"] == "optimized":
            raise ValueError(
                "reference suggestions only support Major/Critical candidates "
                f"in optimized review mode: {unsupported}"
            )
        raise ValueError(
            f"reference suggestion severities conflict with {policy['mode']} "
            f"review mode: {unsupported}"
        )
    if type(only_missing) is not bool:
        raise ValueError("reference suggestion only_missing must be boolean")
    return {
        "categories": sorted(categories),
        "severities": sorted(severities),
        "only_missing": only_missing,
    }


def _digest_or_fallback(value: object, fallback: object) -> str:
    if (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    ):
        return value
    return canonical_digest(fallback)


def _protected_signature(segments: list[dict]) -> str:
    return canonical_digest([
        {
            "id": segment.get("id"),
            "target": current_target(segment),
            "protected": segment.get("protected") is True,
            "protected_reason": segment.get("protected_reason"),
            "protected_texts": copy.deepcopy(segment.get("protected_texts", [])),
            "input_status": segment.get("input_status"),
        }
        for segment in segments
    ])


def _context_digest(segments: list[dict]) -> str:
    fields = (
        "content_type",
        "text_type_context",
        "context_note",
        "context",
        "resolved_constraints",
        "segment_revision_digest",
    )
    return canonical_digest([
        {
            "id": segment.get("id"),
            **{
                field: copy.deepcopy(segment.get(field))
                for field in fields
                if field in segment
            },
        }
        for segment in segments
    ])


def _bundle_digests_by_id(context_bundle_set: dict | None) -> dict[object, str]:
    if context_bundle_set is None:
        return {}
    bundle_set = validate_context_bundle_set(context_bundle_set)
    output = {}
    for bundle in bundle_set["bundles"]:
        segment_id = bundle["segment"]["identity"]["id"]
        if segment_id in output:
            raise ValueError(f"duplicate suggestion context bundle id {segment_id!r}")
        output[segment_id] = bundle["context_bundle_digest"]
    return output


def build_live_bindings(
    state: dict | None,
    segments: list[dict],
    manifest: dict | None,
    results: list[dict],
    *,
    job: Path | None = None,
) -> dict:
    state = state or {}
    manifest = manifest if isinstance(manifest, dict) else {}
    job_id = state.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        job_id = Path(job).name if job is not None else "unbound"
    state_revision_digest = _digest_or_fallback(
        manifest.get("state_fingerprint"),
        {
            "iteration": state.get("iteration", 0),
            "segments": segments,
            "review_policy": get_review_policy(state),
            "check_scope": state.get("check_scope"),
        },
    )
    return {
        "job_id": job_id,
        "job_runtime_contract_version": state.get("job_runtime_contract_version", 1),
        "state_revision_digest": state_revision_digest,
        "results_basis_digest": canonical_digest(_results_basis(results)),
        "project_asset_snapshot_digest": _digest_or_fallback(
            state.get("project_asset_snapshot_digest"),
            manifest.get("manifest_digest"),
        ),
        "capability_resolution_digest": _digest_or_fallback(
            state.get("capability_resolution_digest"),
            state.get("capability_resolution"),
        ),
        "context_bundle_set_digest": _digest_or_fallback(
            state.get("context_bundle_set_digest"),
            _context_digest(segments),
        ),
        "protected_signature_digest": _protected_signature(segments),
        "created_at": _stable_created_at(state),
    }


def build_suggestion_packet(
    segments: list[dict],
    manifest: dict | None,
    results: list[dict],
    *,
    selection: object | None = None,
    review_policy: dict | None = None,
    state: dict | None = None,
    job: Path | None = None,
    context_bundle_set: dict | None = None,
    worker_context_manifest_digest: str | None = None,
    context_view_basis: dict | None = None,
    content_index: dict | None = None,
    included_review_ids: set[int] | None = None,
    context_rel_dir: str = SUGGESTION_CONTEXT_DIR,
    selected_evidence_contract: dict | None = None,
    worker_batch_size: int | None = None,
) -> dict:
    worker_batch_size = _normalize_worker_batch_size(worker_batch_size)
    worker_context_manifest_digest = _require_digest(
        worker_context_manifest_digest,
        "worker context manifest digest",
    )
    review_policy = get_review_policy(
        {"review_policy": review_policy} if review_policy is not None else (state or {})
    )
    selection = _normalize_selection(selection, review_policy)
    selected_categories = set(selection["categories"])
    selected_severities = set(selection["severities"])
    by_id = {entry["id"]: entry for entry in results}
    projected = []
    excluded_segments = []
    reviewed_ids = []
    context_registry = (state or {}).get("resolved_context_descriptors")
    if not isinstance(context_registry, dict):
        context_registry = None
    projection_modules = (
        context_view_basis.get("source_modules")
        if isinstance(context_view_basis, dict)
        else ["suggestions"]
    )
    if (
        not isinstance(projection_modules, list)
        or not projection_modules
        or any(not isinstance(module, str) or not module for module in projection_modules)
    ):
        raise ValueError("suggestion context projection modules are invalid")
    bundle_digests = _bundle_digests_by_id(context_bundle_set)
    selected_evidence_binding = (
        _selected_evidence_index_binding(
            selected_evidence_contract["binding"],
            label="checker-selected evidence index",
        )
        if selected_evidence_contract is not None
        else None
    )
    all_checker_worker_receipts = (
        [
            _worker_receipt(receipt, label="checker worker receipt")
            for receipt in selected_evidence_contract["checker_worker_receipts"]
        ]
        if selected_evidence_contract is not None
        else []
    )
    all_checker_receipt_pairs = {
        (item["worker_id"], item["run_id"])
        for item in all_checker_worker_receipts
    }
    if len(all_checker_receipt_pairs) != len(
        all_checker_worker_receipts
    ):
        raise ValueError("checker worker receipts contain duplicates")
    checker_receipts_by_segment = {}
    if selected_evidence_contract is not None:
        raw_receipts_by_segment = selected_evidence_contract.get(
            "checker_worker_receipts_by_segment"
        )
        if not isinstance(raw_receipts_by_segment, dict):
            raise ValueError("checker worker receipt segment bindings are missing")
        for segment_id, raw_receipts in raw_receipts_by_segment.items():
            if type(segment_id) is not int or not isinstance(raw_receipts, list):
                raise ValueError("checker worker receipt segment bindings are invalid")
            receipts = [
                _worker_receipt(
                    receipt,
                    label=f"checker worker receipt for segment {segment_id}",
                )
                for receipt in raw_receipts
            ]
            receipt_pairs = {
                (receipt["worker_id"], receipt["run_id"])
                for receipt in receipts
            }
            if (
                not receipts
                or len(receipt_pairs) != len(receipts)
                or not receipt_pairs.issubset(all_checker_receipt_pairs)
            ):
                raise ValueError(
                    "checker worker receipt segment bindings are stale"
                )
            checker_receipts_by_segment[segment_id] = receipts
    selected_checker_receipt_pairs = set()
    required_checker_modules = set(required_modules(state or {}))
    for segment in segments:
        entry = by_id[segment["id"]]
        errors = entry.get("errors") or []
        if not errors:
            continue
        if selection["only_missing"] and entry.get("corrected") is not None:
            continue
        eligible_errors = [
            issue
            for issue in errors
            if issue.get("severity") in selected_severities
            and (not selected_categories or issue.get("category") in selected_categories)
        ]
        if not eligible_errors:
            continue
        segment_id = segment["id"]
        if included_review_ids is not None and segment_id not in included_review_ids:
            continue
        reviewed_ids.append(segment_id)
        reason_codes = _segment_block_reason_codes(segment, errors)
        selected_evidence = None
        if selected_evidence_contract is not None:
            representative = selected_evidence_contract[
                "representative_by_id"
            ].get(segment_id)
            selected_union = selected_evidence_contract["segment_unions"].get(
                representative
            )
            evidence_receipts = checker_receipts_by_segment.get(representative)
            if selected_union is not None:
                if not evidence_receipts:
                    raise ValueError(
                        f"suggestion segment {segment_id!r} lacks checker receipt bindings"
                    )
                selected_checker_receipt_pairs.update(
                    (receipt["worker_id"], receipt["run_id"])
                    for receipt in evidence_receipts
                )
            issue_modules = sorted({
                provenance.get("review_module")
                for issue in errors
                for provenance in [issue.get("review_provenance")]
                if isinstance(provenance, dict)
                and provenance.get("review_module") in required_checker_modules
            })
            selected_modules = (
                {
                    item["module"]
                    for item in selected_union["module_selections"]
                }
                if selected_union is not None
                else set()
            )
            missing_modules = sorted(set(issue_modules) - selected_modules)
            if selected_union is None or missing_modules:
                reason_codes.append("CHECKER_SELECTED_EVIDENCE_MISSING")
            else:
                selected_evidence = {
                    "applies_to_segment_id": segment_id,
                    "evidence_segment_id": representative,
                    "reporting_checker_modules": issue_modules,
                    "strict_union": copy.deepcopy(selected_union),
                }
        if reason_codes:
            excluded_segments.append({
                "id": segment_id,
                "risk_route": HARD_REJECT,
                "reason_codes": list(dict.fromkeys(reason_codes)),
            })
            continue
        known_issues = [
            _issue_projection(issue, index)
            for index, issue in enumerate(errors)
        ]
        non_authorizing_evidence = []
        for issue in known_issues:
            non_authorizing_evidence.extend(
                copy.deepcopy(issue.get("non_authorizing_evidence", []))
            )
        for near in (
            []
            if selected_evidence_contract is not None
            else segment.get("term_near", [])
        ):
            if not isinstance(near, dict):
                continue
            source_term = near.get("tb_src")
            current_source = near.get("seg")
            target = near.get("tb_tgt")
            if all(
                isinstance(value, str) and value.strip()
                for value in (source_term, current_source, target)
            ):
                non_authorizing_evidence.append({
                    "source_term": source_term,
                    "cannot_authorize_source": current_source,
                    "target": target,
                    "reason": (
                        "near or different source term cannot grant glossary authority"
                    ),
                })
        unique_non_authorizing = []
        seen_non_authorizing = set()
        for evidence in non_authorizing_evidence:
            digest = canonical_digest(evidence)
            if digest not in seen_non_authorizing:
                seen_non_authorizing.add(digest)
                unique_non_authorizing.append(evidence)
        trigger_indexes = {id(issue) for issue in eligible_errors}
        trigger_issue_ids = [
            known_issues[index]["issue_id"]
            for index, issue in enumerate(errors)
            if id(issue) in trigger_indexes
        ]
        item = {
            "id": segment_id,
            "source": segment.get("source", ""),
            "target": current_target(segment),
            "validated_target": entry.get("corrected"),
            "trigger_issue_ids": trigger_issue_ids,
            "known_issues": known_issues,
            "generation_constraints": {
                "term_actions": {
                    "term_resolution": (
                        "not_governed" if unique_non_authorizing else "none"
                    ),
                    "non_authorizing_evidence": unique_non_authorizing,
                },
                "resolved_constraints": copy.deepcopy(
                    selected_evidence["strict_union"]["resolved_constraints"]
                    if selected_evidence is not None
                    else segment.get("resolved_constraints", [])
                ),
                "protected_texts": copy.deepcopy(segment.get("protected_texts", [])),
            },
        }
        if selected_evidence is not None:
            item["checker_selected_evidence"] = selected_evidence
        if bundle_digests:
            if segment_id not in bundle_digests:
                raise ValueError(
                    f"suggestion context bundle is missing segment id {segment_id!r}"
                )
            item["context_bundle_digest"] = bundle_digests[segment_id]
        context_projection = (
            copy.deepcopy(
                selected_evidence["strict_union"]["context_projection"]
            )
            if selected_evidence is not None
            else _project_segment_for_modules(
                segment,
                projection_modules,
                context_registry,
            )
        )
        if context_projection or selected_evidence is not None:
            item["context_projection"] = context_projection
        item["dialogue_context_readiness"] = _dialogue_context_readiness(
            context_projection,
            context_view_basis,
        )
        for field in (
            "segment_key",
            "content_type",
            "text_type_context",
            "context_note",
            "kind",
        ):
            value = segment.get(field)
            if value not in (None, "", [], {}):
                item[field] = copy.deepcopy(value)
        projected.append(item)

    checker_worker_receipts = [
        {"worker_id": worker_id, "run_id": run_id}
        for worker_id, run_id in sorted(
            selected_checker_receipt_pairs or all_checker_receipt_pairs
        )
    ]

    bindings = build_live_bindings(state, segments, manifest, results, job=job)
    if context_bundle_set is not None:
        bindings["context_bundle_set_digest"] = context_bundle_set[
            "context_bundle_set_digest"
        ]
    payload = {
        "schema": PACKET_SCHEMA,
        "version": PACKET_VERSION,
        **bindings,
        "worker_context_manifest_digest": worker_context_manifest_digest,
        "manifest_digest": (
            manifest.get("manifest_digest") if isinstance(manifest, dict) else None
        ),
        "review_policy": review_policy,
        "selection": selection,
        "reviewed_ids": reviewed_ids,
        "review_receipts": [{
            "type": "verified_results_contract",
            "digest": bindings["results_basis_digest"],
        }],
        **(
            {"context_view_basis": copy.deepcopy(context_view_basis)}
            if context_view_basis is not None
            else {}
        ),
        **(
            {
                "content_index": {
                    "path": (
                        f"{context_rel_dir}/{SUGGESTION_CONTENT_INDEX_NAME}"
                    ),
                    "digest": content_index["content_index_digest"],
                }
            }
            if content_index is not None
            else {}
        ),
        **(
            {
                "selected_evidence_index": selected_evidence_binding,
                "checker_worker_receipts": checker_worker_receipts,
            }
            if selected_evidence_binding is not None
            else {}
        ),
        "excluded_segments": excluded_segments,
        "segments": projected,
        "instructions": {
            "purpose": "report_only_reference_translation_candidate",
            "sparse_suggestions_allowed": True,
            "agent_decides_reliability": True,
            "text_type_routing_enabled": review_policy["text_type_routing_enabled"],
            "preserve": ["variables", "tags", "line_breaks", "protected_texts"],
            "do_not_apply_to_corrected_export": True,
            "unresolved_terminology_segments_hard_rejected": True,
            "generation_mode": "source_first_full_rewrite",
            "source_first_contract": {
                "derive_semantic_propositions_from": "source",
                "do_not_use_current_target_as_semantic_skeleton": True,
                "current_target_use_is_limited_to": [
                    "variables",
                    "tags",
                    "line_breaks",
                    "protected_texts",
                    "validated_local_edits",
                ],
                "post_generation_checks": [
                    "Mistranslation",
                    "Omission",
                    "Addition",
                    "resolved_constraints",
                ],
            },
            "worker_context": {
                "worker_batch_size": worker_batch_size,
                "input_measurement_path": (
                    f"{SUGGESTION_CONTEXT_DIR}/"
                    f"{SUGGESTION_INPUT_MEASUREMENT_NAME}"
                ),
                "manifest_path": (
                    f"{context_rel_dir}/{SUGGESTION_WORKER_MANIFEST_NAME}"
                ),
                "manifest_digest": worker_context_manifest_digest,
                "bundle_set_path": (
                    f"{context_rel_dir}/{SUGGESTION_BUNDLE_SET_NAME}"
                ),
                "bundle_set_digest": bindings["context_bundle_set_digest"],
                "required_for_worker": True,
                **(
                    {
                        "checker_selected_evidence_index_path":
                            selected_evidence_binding["path"],
                        "checker_selected_evidence_index_digest":
                            selected_evidence_binding["digest"],
                        "checker_selected_evidence_is_embedded_per_segment": True,
                        "worker_reads_full_checker_index": False,
                    }
                    if selected_evidence_binding is not None
                    else {}
                ),
                **(
                    {
                        "content_index_path": (
                            f"{context_rel_dir}/"
                            f"{SUGGESTION_CONTENT_INDEX_NAME}"
                        ),
                        "content_index_digest": content_index[
                            "content_index_digest"
                        ],
                    }
                    if content_index is not None
                    else {}
                ),
            },
        },
    }
    if context_bundle_set is not None:
        payload["instructions"]["context_bundle_set"] = {
            "path": f"{context_rel_dir}/bundle_set.json",
            "digest": context_bundle_set["context_bundle_set_digest"],
        }
    payload = _with_digest(payload, "packet_digest")
    _validate_schema(payload, PACKET_SCHEMA, PACKET_VERSION)
    return payload


def _verify_results(
    job: Path,
    state: dict,
    segments: list[dict],
    manifest: dict | None,
    errors_path: Path,
) -> list[dict]:
    results = read_json(errors_path)
    bound = requires_bound_artifacts(state)
    if bound:
        contract_path = result_contract_path(errors_path)
        if manifest is None or not contract_path.is_file():
            raise CheckFormatError(f"{errors_path.name}: bound result contract is required")
        validate_result_contract(
            read_json(contract_path), manifest, results, label=errors_path.name
        )
    return verify_results(
        segments,
        results,
        str(errors_path),
        allow_internal_provenance=bound,
        require_internal_provenance=bound,
        review_policy=get_review_policy(state),
        target_form_policy=load_target_form_policy(state),
    )


def _load_live(
    job: Path,
    *,
    state_name: str = "state.json",
    errors_name: str = "errors.json",
) -> tuple[dict, list[dict], dict | None, list[dict]]:
    state_path = job / state_name
    errors_path = job / errors_name
    if not state_path.is_file():
        raise FileNotFoundError(f"state is missing: {state_path}")
    if not errors_path.is_file():
        raise FileNotFoundError(f"errors are missing: {errors_path}")
    with verification_generation_lease(state_path, exclusive=False) as (
        state,
        segments,
        manifest,
        _,
    ):
        results = _verify_results(job, state, segments, manifest, errors_path)
        return state, segments, manifest, results


def _build_live_packet(
    job: Path,
    state: dict,
    segments: list[dict],
    manifest: dict | None,
    results: list[dict],
    selection: object | None,
) -> dict:
    packet_plan = _build_live_packet_plan(
        job,
        state,
        segments,
        manifest,
        results,
        selection,
    )
    return packet_plan["root_packet"]


def _build_live_packet_context(
    job: Path,
    state: dict,
    segments: list[dict],
    manifest: dict | None,
    results: list[dict],
    selection: object | None,
    *,
    included_review_ids: set[int] | None = None,
    context_rel_dir: str = SUGGESTION_CONTEXT_DIR,
    selected_evidence_contract: dict | None = None,
    worker_batch_size: int | None = None,
) -> tuple[dict, dict, dict]:
    if not isinstance(state.get("project_asset_snapshot"), dict) or not isinstance(
        state.get("capability_resolution"), dict
    ):
        raise ValueError(
            "suggestion worker context requires bound project assets and "
            "capability resolution"
        )
    context_view_basis = build_suggestion_context_view(state)
    if selected_evidence_contract is not None:
        context_view_basis = {
            **context_view_basis,
            "evidence_mode": "checker_selected_strict_union",
            "selected_evidence_index_digest": selected_evidence_contract[
                "binding"
            ]["digest"],
            "projection_only_view": _projection_only_context_view(
                context_view_basis
            ),
        }
    content_index = build_suggestion_content_index(
        job,
        state,
        context_rel_dir=context_rel_dir,
        selected_evidence_index=(
            selected_evidence_contract["binding"]
            if selected_evidence_contract is not None
            else None
        ),
    )
    preliminary = build_suggestion_packet(
        segments,
        manifest,
        results,
        selection=selection,
        review_policy=get_review_policy(state),
        state=state,
        job=job,
        worker_context_manifest_digest=_UNBOUND_WORKER_CONTEXT_DIGEST,
        context_view_basis=context_view_basis,
        content_index=content_index,
        included_review_ids=included_review_ids,
        context_rel_dir=context_rel_dir,
        selected_evidence_contract=selected_evidence_contract,
        worker_batch_size=worker_batch_size,
    )
    selected_ids = {segment["id"] for segment in preliminary["segments"]}
    selected_segments = [
        segment for segment in segments if segment["id"] in selected_ids
    ]
    try:
        bundle_set = build_context_bundle_set(
            state,
            selected_segments,
            "suggestions",
            module_view=(
                context_view_basis["projection_only_view"]
                if selected_evidence_contract is not None
                else context_view_basis["merged_view"]
            ),
            projection_modules=context_view_basis["source_modules"],
        )
    except (ContextBundleError, OSError, ValueError) as exc:
        raise ValueError(f"suggestion context bundle: {exc}") from exc
    packet_basis = build_suggestion_packet(
        segments,
        manifest,
        results,
        selection=selection,
        review_policy=get_review_policy(state),
        state=state,
        job=job,
        context_bundle_set=bundle_set,
        worker_context_manifest_digest=_UNBOUND_WORKER_CONTEXT_DIGEST,
        context_view_basis=context_view_basis,
        content_index=content_index,
        included_review_ids=included_review_ids,
        context_rel_dir=context_rel_dir,
        selected_evidence_contract=selected_evidence_contract,
        worker_batch_size=worker_batch_size,
    )
    try:
        worker_manifest = build_worker_context_manifest(
            state,
            "suggestions",
            bundle_set,
            max_worker_bytes=None,
            packet_payloads=[_worker_packet_basis(packet_basis)],
            job_root=job,
        )
    except (ContextBundleError, OSError, ValueError) as exc:
        raise ValueError(f"suggestion worker context manifest: {exc}") from exc
    packet = build_suggestion_packet(
        segments,
        manifest,
        results,
        selection=selection,
        review_policy=get_review_policy(state),
        state=state,
        job=job,
        context_bundle_set=bundle_set,
        worker_context_manifest_digest=worker_manifest[
            "worker_context_manifest_digest"
        ],
        context_view_basis=context_view_basis,
        content_index=content_index,
        included_review_ids=included_review_ids,
        context_rel_dir=context_rel_dir,
        selected_evidence_contract=selected_evidence_contract,
        worker_batch_size=worker_batch_size,
    )
    bundle_digests = _bundle_digests_by_id(bundle_set)
    for segment in packet["segments"]:
        if not isinstance(segment.get("context_projection"), dict):
            raise ValueError(
                f"suggestion segment id {segment['id']!r} lacks canonical context projection"
            )
        if segment.get("context_bundle_digest") != bundle_digests.get(segment["id"]):
            raise ValueError(
                f"suggestion segment id {segment['id']!r} context bundle is stale"
            )
    measure_suggestion_worker_input(
        worker_manifest,
        bundle_set,
        packet,
        label="suggestion generation",
        additional_inputs=[content_index],
    )
    return packet, bundle_set, worker_manifest


def _prepared_worker_batch_size(job: Path) -> int | None:
    packet_path = job / PACKET_NAME
    if not packet_path.is_file():
        return None
    packet = read_json(packet_path)
    value = packet.get("instructions", {}).get("worker_context", {}).get(
        "worker_batch_size"
    )
    return _normalize_worker_batch_size(value)


def _generation_worker_input_bytes(
    job: Path,
    state: dict,
    packet: dict,
    bundle_set: dict,
    worker_manifest: dict,
) -> int:
    content_path = packet.get("content_index", {}).get("path")
    if not isinstance(content_path, str):
        raise ValueError("suggestion content index binding is missing")
    relative = Path(content_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("suggestion content index path is unsafe")
    content_index = build_suggestion_content_index(
        job,
        state,
        context_rel_dir=relative.parent.as_posix(),
        selected_evidence_index=packet.get("selected_evidence_index"),
    )
    return measure_suggestion_worker_input(
        worker_manifest,
        bundle_set,
        packet,
        label="suggestion generation",
        additional_inputs=[content_index],
    )


def _build_live_packet_plan(
    job: Path,
    state: dict,
    segments: list[dict],
    manifest: dict | None,
    results: list[dict],
    selection: object | None,
    *,
    worker_batch_size: object = _WORKER_BATCH_SIZE_UNSET,
) -> dict:
    if worker_batch_size is _WORKER_BATCH_SIZE_UNSET:
        worker_batch_size = _prepared_worker_batch_size(job)
    worker_batch_size = _normalize_worker_batch_size(worker_batch_size)
    selected_evidence_contract = _load_checker_selected_evidence(job, state)

    context_view_basis = build_suggestion_context_view(state)
    if selected_evidence_contract is not None:
        context_view_basis = {
            **context_view_basis,
            "evidence_mode": "checker_selected_strict_union",
            "selected_evidence_index_digest": selected_evidence_contract[
                "binding"
            ]["digest"],
            "projection_only_view": _projection_only_context_view(
                context_view_basis
            ),
        }
    root_content_index = build_suggestion_content_index(
        job,
        state,
        selected_evidence_index=(
            selected_evidence_contract["binding"]
            if selected_evidence_contract is not None
            else None
        ),
    )
    preliminary = build_suggestion_packet(
        segments,
        manifest,
        results,
        selection=selection,
        review_policy=get_review_policy(state),
        state=state,
        job=job,
        worker_context_manifest_digest=_UNBOUND_WORKER_CONTEXT_DIGEST,
        context_view_basis=context_view_basis,
        content_index=root_content_index,
        selected_evidence_contract=selected_evidence_contract,
        worker_batch_size=worker_batch_size,
    )
    viable_ids = [segment["id"] for segment in preliminary["segments"]]
    if worker_batch_size is None or len(viable_ids) <= worker_batch_size:
        packet, bundle_set, worker_manifest = _build_live_packet_context(
            job,
            state,
            segments,
            manifest,
            results,
            selection,
            selected_evidence_contract=selected_evidence_contract,
            worker_batch_size=worker_batch_size,
        )
        worker_input_bytes = _generation_worker_input_bytes(
            job, state, packet, bundle_set, worker_manifest
        )
        return {
            "mode": "single",
            "root_packet": packet,
            "batches": [{
                "batch_id": "batch_0001",
                "packet": packet,
                "bundle_set": bundle_set,
                "worker_manifest": worker_manifest,
                "context_rel_dir": SUGGESTION_CONTEXT_DIR,
                "worker_input_bytes": worker_input_bytes,
            }],
            "batch_plan": None,
        }

    grouped_ids = [
        viable_ids[index:index + worker_batch_size]
        for index in range(0, len(viable_ids), worker_batch_size)
    ]
    batches = []
    batch_records = []
    for index, reviewed_ids in enumerate(grouped_ids, start=1):
        batch_id = f"batch_{index:04d}"
        context_rel_dir = f"{SUGGESTION_BATCH_ROOT}/{batch_id}"
        packet, bundle_set, worker_manifest = _build_live_packet_context(
            job,
            state,
            segments,
            manifest,
            results,
            selection,
            included_review_ids=set(reviewed_ids),
            context_rel_dir=context_rel_dir,
            selected_evidence_contract=selected_evidence_contract,
            worker_batch_size=worker_batch_size,
        )
        worker_input_bytes = _generation_worker_input_bytes(
            job, state, packet, bundle_set, worker_manifest
        )
        batches.append({
            "batch_id": batch_id,
            "packet": packet,
            "bundle_set": bundle_set,
            "worker_manifest": worker_manifest,
            "context_rel_dir": context_rel_dir,
            "worker_input_bytes": worker_input_bytes,
        })
        batch_records.append({
            "batch_id": batch_id,
            "reviewed_ids": reviewed_ids,
            "packet_path": f"{context_rel_dir}/packet.json",
            "packet_digest": packet["packet_digest"],
            "bundle_set_path": f"{context_rel_dir}/{SUGGESTION_BUNDLE_SET_NAME}",
            "context_bundle_set_digest": bundle_set[
                "context_bundle_set_digest"
            ],
            "worker_manifest_path": (
                f"{context_rel_dir}/{SUGGESTION_WORKER_MANIFEST_NAME}"
            ),
            "worker_context_manifest_digest": worker_manifest[
                "worker_context_manifest_digest"
            ],
            "content_index_path": (
                f"{context_rel_dir}/{SUGGESTION_CONTENT_INDEX_NAME}"
            ),
            "content_index_digest": packet["content_index"]["digest"],
            "draft_path": f"{context_rel_dir}/generation.draft.json",
            "worker_input_bytes": worker_input_bytes,
        })
    plan = {
        "schema": "lqe.suggestion-generation-batch-plan",
        "version": 1,
        "worker_batch_size": worker_batch_size,
        "selection": copy.deepcopy(preliminary["selection"]),
        "reviewed_ids": copy.deepcopy(preliminary["reviewed_ids"]),
        "worker_reviewed_ids": viable_ids,
        "excluded_segments": copy.deepcopy(preliminary["excluded_segments"]),
        **(
            {
                "selected_evidence_index": copy.deepcopy(
                    selected_evidence_contract["binding"]
                )
            }
            if selected_evidence_contract is not None
            else {}
        ),
        "batches": batch_records,
    }
    plan["coverage_digest"] = canonical_digest({
        "reviewed_ids": plan["reviewed_ids"],
        "worker_reviewed_ids": plan["worker_reviewed_ids"],
        "excluded_segments": plan["excluded_segments"],
        "batch_ids": [record["reviewed_ids"] for record in batch_records],
    })
    plan["batch_plan_digest"] = canonical_digest(plan)

    root_packet = copy.deepcopy(preliminary)
    root_packet["context_bundle_set_digest"] = canonical_digest([
        record["context_bundle_set_digest"] for record in batch_records
    ])
    root_packet["worker_context_manifest_digest"] = canonical_digest([
        record["worker_context_manifest_digest"] for record in batch_records
    ])
    root_packet.pop("content_index", None)
    root_packet["batch_plan"] = {
        "path": f"{SUGGESTION_CONTEXT_DIR}/{SUGGESTION_BATCH_PLAN_NAME}",
        "digest": plan["batch_plan_digest"],
        "batch_count": len(batch_records),
    }
    root_packet["instructions"]["worker_context"] = {
        "worker_batch_size": worker_batch_size,
        "input_measurement_path": (
            f"{SUGGESTION_CONTEXT_DIR}/{SUGGESTION_INPUT_MEASUREMENT_NAME}"
        ),
        "batch_plan_path": root_packet["batch_plan"]["path"],
        "batch_plan_digest": plan["batch_plan_digest"],
        "worker_reads_root_packet": False,
        "required_for_worker": True,
        **(
            {
                "checker_selected_evidence_index_path":
                    selected_evidence_contract["binding"]["path"],
                "checker_selected_evidence_index_digest":
                    selected_evidence_contract["binding"]["digest"],
                "checker_selected_evidence_is_embedded_per_segment": True,
                "worker_reads_full_checker_index": False,
            }
            if selected_evidence_contract is not None
            else {}
        ),
    }
    root_packet.pop("packet_digest", None)
    root_packet = _with_digest(root_packet, "packet_digest")
    _validate_schema(root_packet, PACKET_SCHEMA, PACKET_VERSION)
    return {
        "mode": "batched",
        "root_packet": root_packet,
        "batches": batches,
        "batch_plan": plan,
    }


def _suggestion_input_measurement(packet_plan: dict) -> dict:
    batches = [{
        "batch_id": batch["batch_id"],
        "reviewed_ids": [
            segment["id"] for segment in batch["packet"]["segments"]
        ],
        "worker_input_bytes": batch["worker_input_bytes"],
    } for batch in packet_plan["batches"]]
    values = [batch["worker_input_bytes"] for batch in batches]
    payload = {
        "schema": "lqe.suggestion-worker-input-measurement",
        "version": 1,
        "mode": "advisory",
        "decision_owner": "main_agent",
        "worker_batch_size": packet_plan["root_packet"]["instructions"][
            "worker_context"
        ].get("worker_batch_size"),
        "batches": batches,
        "total_worker_input_bytes": sum(values),
        "largest_worker_input_bytes": max(values, default=0),
    }
    payload["measurement_digest"] = canonical_digest(payload)
    return payload


def _write_packet_plan(job: Path, state: dict, packet_plan: dict) -> None:
    write_json_atomic(
        job / SUGGESTION_CONTEXT_DIR / SUGGESTION_INPUT_MEASUREMENT_NAME,
        _suggestion_input_measurement(packet_plan),
    )
    if packet_plan["mode"] == "single":
        batch = packet_plan["batches"][0]
        context_dir = job / SUGGESTION_CONTEXT_DIR
        write_json_atomic(
            context_dir / SUGGESTION_BUNDLE_SET_NAME,
            batch["bundle_set"],
        )
        write_json_atomic(
            context_dir / SUGGESTION_WORKER_MANIFEST_NAME,
            batch["worker_manifest"],
        )
        write_json_atomic(
            context_dir / SUGGESTION_CONTENT_INDEX_NAME,
            build_suggestion_content_index(
                job,
                state,
                selected_evidence_index=batch["packet"].get(
                    "selected_evidence_index"
                ),
            ),
        )
        return
    write_json_atomic(
        job / SUGGESTION_CONTEXT_DIR / SUGGESTION_BATCH_PLAN_NAME,
        packet_plan["batch_plan"],
    )
    for batch in packet_plan["batches"]:
        context_dir = job / batch["context_rel_dir"]
        write_json_atomic(context_dir / "packet.json", batch["packet"])
        write_json_atomic(
            context_dir / SUGGESTION_BUNDLE_SET_NAME,
            batch["bundle_set"],
        )
        write_json_atomic(
            context_dir / SUGGESTION_WORKER_MANIFEST_NAME,
            batch["worker_manifest"],
        )
        write_json_atomic(
            context_dir / SUGGESTION_CONTENT_INDEX_NAME,
            build_suggestion_content_index(
                job,
                state,
                context_rel_dir=batch["context_rel_dir"],
                selected_evidence_index=batch["packet"].get(
                    "selected_evidence_index"
                ),
            ),
        )


def require_persisted_packet_plan(job: Path, packet_plan: dict) -> None:
    root_path = job / PACKET_NAME
    if not root_path.is_file() or read_json(root_path) != packet_plan["root_packet"]:
        raise ValueError("prepared suggestion root packet is stale")
    measurement_path = (
        job / SUGGESTION_CONTEXT_DIR / SUGGESTION_INPUT_MEASUREMENT_NAME
    )
    if (
        not measurement_path.is_file()
        or read_json(measurement_path) != _suggestion_input_measurement(packet_plan)
    ):
        raise ValueError("prepared suggestion input measurement is stale")
    if packet_plan["mode"] == "single":
        batch = packet_plan["batches"][0]
        require_persisted_suggestion_context(
            job,
            batch["packet"],
            batch["bundle_set"],
            batch["worker_manifest"],
        )
        return
    plan_path = job / SUGGESTION_CONTEXT_DIR / SUGGESTION_BATCH_PLAN_NAME
    if not plan_path.is_file() or read_json(plan_path) != packet_plan["batch_plan"]:
        raise ValueError("prepared suggestion generation batch plan is stale")
    root = packet_plan["root_packet"]
    if root.get("batch_plan", {}).get("digest") != packet_plan["batch_plan"][
        "batch_plan_digest"
    ]:
        raise ValueError("suggestion root packet batch plan is stale")
    for batch in packet_plan["batches"]:
        packet_path = job / batch["context_rel_dir"] / "packet.json"
        if not packet_path.is_file() or read_json(packet_path) != batch["packet"]:
            raise ValueError(
                f"prepared suggestion packet {batch['batch_id']} is stale"
            )
        require_persisted_suggestion_context(
            job,
            batch["packet"],
            batch["bundle_set"],
            batch["worker_manifest"],
        )


def require_persisted_suggestion_context(
    job: Path,
    packet: dict,
    bundle_set: dict,
    worker_manifest: dict,
) -> None:
    content_ref = packet.get("content_index")
    if not isinstance(content_ref, dict) or not isinstance(
        content_ref.get("path"), str
    ):
        raise ValueError("suggestion packet content index binding is missing")
    content_relative = Path(content_ref["path"])
    if content_relative.is_absolute() or ".." in content_relative.parts:
        raise ValueError("suggestion packet content index path is unsafe")
    context_dir = Path(job) / content_relative.parent
    bundle_path = context_dir / SUGGESTION_BUNDLE_SET_NAME
    manifest_path = context_dir / SUGGESTION_WORKER_MANIFEST_NAME
    content_index_path = Path(job) / content_relative
    if (
        not bundle_path.is_file()
        or not manifest_path.is_file()
        or not content_index_path.is_file()
    ):
        raise ValueError(
            "prepared suggestion worker context is missing; rerun suggestions prepare"
        )
    persisted_bundle = validate_context_bundle_set(read_json(bundle_path))
    persisted_manifest = validate_worker_context_manifest(read_json(manifest_path))
    persisted_content_index = read_json(content_index_path)
    if persisted_bundle != bundle_set:
        raise ValueError("prepared suggestion context bundle is stale")
    if persisted_manifest != worker_manifest:
        raise ValueError("prepared suggestion worker context manifest is stale")
    expected_content_index = packet.get("content_index")
    if not isinstance(expected_content_index, dict) or (
        persisted_content_index.get("content_index_digest")
        != expected_content_index.get("digest")
        or canonical_digest({
            key: value
            for key, value in persisted_content_index.items()
            if key != "content_index_digest"
        })
        != persisted_content_index.get("content_index_digest")
    ):
        raise ValueError("prepared suggestion content index is stale")
    if (
        packet["worker_context_manifest_digest"]
        != persisted_manifest["worker_context_manifest_digest"]
    ):
        raise ValueError("suggestion packet worker context manifest is stale")
    selected_binding = packet.get("selected_evidence_index")
    if selected_binding is not None:
        selected_context = persisted_content_index.get("selected_context", {})
        if (
            selected_context.get("checker_selected_evidence_index_path")
            != selected_binding["path"]
            or selected_context.get("checker_selected_evidence_index_digest")
            != selected_binding["digest"]
        ):
            raise ValueError("prepared checker-selected evidence binding is stale")


def _validate_source_semantics(value: object, *, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    expected = {
        "subjects",
        "actions",
        "objects",
        "negation",
        "polarity",
        "modality",
        "speech_act",
        "text_function",
        "intensity",
        "omitted_source_elements",
        "unsupported_additions",
    }
    if set(value) != expected:
        raise ValueError(f"{label} fields differ from the semantic receipt contract")
    for field in ("subjects", "actions"):
        items = value[field]
        if (
            not isinstance(items, list)
            or not items
            or any(not isinstance(item, str) or not item.strip() for item in items)
        ):
            raise ValueError(f"{label}.{field} must be a non-empty string array")
    for field in (
        "objects",
        "modality",
        "omitted_source_elements",
        "unsupported_additions",
    ):
        items = value[field]
        if (
            not isinstance(items, list)
            or any(not isinstance(item, str) or not item.strip() for item in items)
        ):
            raise ValueError(f"{label}.{field} must be a string array")
    negation = value["negation"]
    if (
        not isinstance(negation, dict)
        or set(negation) != {"present", "scope"}
        or type(negation["present"]) is not bool
        or not (
            negation["scope"] is None
            or isinstance(negation["scope"], str)
            and negation["scope"].strip()
        )
    ):
        raise ValueError(f"{label}.negation is invalid")
    for field in ("polarity", "speech_act", "text_function", "intensity"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise ValueError(f"{label}.{field} must be non-empty")
    return copy.deepcopy(value)


def _validate_tone_decision(value: object, *, label: str) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "register",
        "politeness",
        "depends_on_dialogue_context",
        "evidence",
        "uncertainties",
    }:
        raise ValueError(f"{label} fields differ from the tone receipt contract")
    if not isinstance(value["register"], str) or not value["register"].strip():
        raise ValueError(f"{label}.register must be non-empty")
    if not isinstance(value["politeness"], str) or not value["politeness"].strip():
        raise ValueError(f"{label}.politeness must be non-empty")
    if type(value["depends_on_dialogue_context"]) is not bool:
        raise ValueError(f"{label}.depends_on_dialogue_context must be boolean")
    evidence = value["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise ValueError(f"{label}.evidence must be non-empty")
    for index, item in enumerate(evidence):
        if (
            not isinstance(item, dict)
            or set(item) != {"type", "value"}
            or not isinstance(item["type"], str)
            or not item["type"].strip()
            or not isinstance(item["value"], str)
            or not item["value"].strip()
        ):
            raise ValueError(f"{label}.evidence[{index}] is invalid")
    uncertainties = value["uncertainties"]
    if (
        not isinstance(uncertainties, list)
        or any(
            not isinstance(item, str) or not item.strip()
            for item in uncertainties
        )
    ):
        raise ValueError(f"{label}.uncertainties must be a string array")
    if uncertainties:
        raise ValueError(
            f"{label}.uncertainties must be empty for a formal candidate; "
            "abstain when an unresolved choice would change the wording"
        )
    return copy.deepcopy(value)


def _validate_abstention_reasons(
    value: object,
    abstained_ids: list[int],
    *,
    label: str,
) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    reason_ids = [item.get("id") for item in value if isinstance(item, dict)]
    if len(reason_ids) != len(value) or reason_ids != abstained_ids:
        raise ValueError(f"{label} must exactly follow abstained_ids")
    output = []
    for index, item in enumerate(value):
        reason_codes = item["reason_codes"]
        if len(reason_codes) != len(set(reason_codes)) or any(
            code != code.strip() for code in reason_codes
        ):
            raise ValueError(f"{label}[{index}].reason_codes are not canonical")
        if "WORKER_ABSTAINED" in reason_codes:
            raise ValueError(
                f"{label}[{index}].reason_codes contains a reserved code"
            )
        if item["evidence"] != item["evidence"].strip():
            raise ValueError(f"{label}[{index}].evidence is not canonical")
        output.append(copy.deepcopy(item))
    return output


def validate_generation_draft(draft: object, packet: dict) -> dict:
    _validate_schema(draft, DRAFT_SCHEMA, DRAFT_VERSION)
    generation_receipt = _worker_receipt(
        draft["worker_receipt"], label="generation worker receipt"
    )
    if draft["packet_digest"] != packet["packet_digest"]:
        raise ValueError("reference suggestion generation draft is stale")
    if (
        draft["worker_context_manifest_digest"]
        != packet["worker_context_manifest_digest"]
    ):
        raise ValueError(
            "reference suggestion generation draft worker context is stale"
        )
    selected_binding = packet.get("selected_evidence_index")
    if selected_binding is not None:
        selected_binding = _selected_evidence_index_binding(
            selected_binding,
            label="generation packet selected evidence index",
        )
        if draft.get("selected_evidence_index_digest") != selected_binding["digest"]:
            raise ValueError(
                "reference suggestion generation draft selected evidence is stale"
            )
        for checker_receipt in packet.get("checker_worker_receipts", []):
            checker_receipt = _worker_receipt(
                checker_receipt,
                label="generation packet checker worker receipt",
            )
            if (
                generation_receipt["worker_id"] == checker_receipt["worker_id"]
                or generation_receipt["run_id"] == checker_receipt["run_id"]
            ):
                raise ValueError(
                    "suggestion generation worker must differ from checker workers"
                )
    elif draft.get("selected_evidence_index_digest") is not None:
        raise ValueError(
            "reference suggestion generation draft has unexpected selected evidence"
        )
    if draft["selection"] != packet["selection"]:
        raise ValueError("reference suggestion generation draft selection is stale")
    if draft["reviewed_ids"] != packet["reviewed_ids"]:
        raise ValueError("reference suggestion generation draft reviewed ids are stale")
    segment_ids = [segment["id"] for segment in packet["segments"]]
    entry_ids = [entry["id"] for entry in draft["entries"]]
    abstained_ids = draft["abstained_ids"]
    if len(entry_ids) != len(set(entry_ids)) or len(abstained_ids) != len(set(abstained_ids)):
        raise ValueError("reference suggestion generation draft has duplicate ids")
    if set(entry_ids) & set(abstained_ids):
        raise ValueError("reference suggestion generation draft id sets overlap")
    if set(entry_ids) | set(abstained_ids) != set(segment_ids):
        raise ValueError("reference suggestion generation draft id coverage is incomplete")
    _validate_abstention_reasons(
        draft["abstention_reasons"],
        abstained_ids,
        label="generation draft abstention_reasons",
    )
    for entry in draft["entries"]:
        _validate_source_semantics(
            entry["source_semantics"],
            label=f"generation draft id {entry['id']} source_semantics",
        )
        _validate_tone_decision(
            entry["tone_decision"],
            label=f"generation draft id {entry['id']} tone_decision",
        )
    return draft


def _candidate_entry(
    segment: dict,
    reference_target: str,
    source_semantics: dict,
    tone_decision: dict,
) -> dict:
    entry = {
        "id": segment["id"],
        "reference_target": reference_target,
        "source_semantics": _validate_source_semantics(
            source_semantics,
            label=f"candidate id {segment['id']} source_semantics",
        ),
        "tone_decision": _validate_tone_decision(
            tone_decision,
            label=f"candidate id {segment['id']} tone_decision",
        ),
    }
    entry["candidate_digest"] = canonical_digest(entry)
    return entry


def _candidate_constraint_evaluations(
    packet_segment: dict,
    segment: dict,
    reference_target: str,
) -> tuple[list[dict], str | None]:
    packet_constraints = (
        packet_segment.get("generation_constraints", {}).get(
            "resolved_constraints", []
        )
    )
    live_constraints = segment.get("resolved_constraints", [])
    if "checker_selected_evidence" in packet_segment:
        selected_digests = sorted({
            canonical_digest(item) for item in packet_constraints
        })
        live_by_digest = {
            canonical_digest(item): item for item in live_constraints
        }
        if any(digest not in live_by_digest for digest in selected_digests):
            raise ValueError(
                f"suggestion candidate id {segment['id']} selected constraint is stale"
            )
        live_constraints = [live_by_digest[digest] for digest in selected_digests]
    if packet_constraints != live_constraints:
        raise ValueError(
            f"suggestion candidate id {segment['id']} constraint evidence is stale"
        )
    if not isinstance(packet_constraints, list):
        raise ValueError(
            f"suggestion candidate id {segment['id']} constraints must be an array"
        )

    evaluations = []
    route_effect = None
    for index, constraint in enumerate(packet_constraints):
        if not isinstance(constraint, dict):
            evaluations.append({
                "constraint_index": index,
                "kind": None,
                "status": "invalid",
                "reason_codes": ["constraint_evidence_invalid"],
                "evaluation_digest": canonical_digest({
                    "constraint_index": index,
                    "status": "invalid",
                    "reason_codes": ["constraint_evidence_invalid"],
                }),
            })
            route_effect = HARD_REJECT
            continue
        kind = constraint.get("kind")
        if isinstance(kind, str) and kind.startswith("language."):
            try:
                evaluated = evaluate_resolved_constraint(
                    constraint, reference_target
                )
            except LanguagePolicyError:
                evaluated = {
                    "status": "invalid",
                    "reason_codes": ["constraint_evidence_invalid"],
                    "constraint_resolution_digest": constraint.get(
                        "resolution_digest"
                    ),
                    "observation": None,
                    "evaluation": None,
                }
                evaluated["evaluation_digest"] = canonical_digest(evaluated)
            item = {
                "constraint_index": index,
                "kind": kind,
                **copy.deepcopy(evaluated),
            }
            evaluations.append(item)
            if evaluated["status"] in {"mismatch", "conflict", "invalid"}:
                route_effect = HARD_REJECT
            elif evaluated["status"] == "inconclusive" and route_effect is None:
                route_effect = INDEPENDENT_VERIFIER
            continue

        item = {
            "constraint_index": index,
            "kind": kind if isinstance(kind, str) else None,
            "status": "inconclusive",
            "reason_codes": ["no_deterministic_constraint_evaluator"],
        }
        item["evaluation_digest"] = canonical_digest(item)
        evaluations.append(item)
        if route_effect is None:
            route_effect = INDEPENDENT_VERIFIER
    return evaluations, route_effect


def _candidate_route(
    packet_segment: dict,
    segment: dict,
    reference_target: object,
    source_semantics: dict,
    tone_decision: dict,
    target_form_policy: dict | None = None,
) -> tuple[dict, dict | None]:
    try:
        validated = validate_reference_target(
            segment,
            reference_target,
            label=f"reference suggestion candidate {segment['id']}",
            target_form_policy=target_form_policy,
        )
    except CheckFormatError:
        return ({
            "id": segment["id"],
            "risk_route": HARD_REJECT,
            "reason_codes": ["DETERMINISTIC_VALIDATION_FAILED"],
        }, None)
    candidate = _candidate_entry(
        segment,
        validated,
        source_semantics,
        tone_decision,
    )
    constraint_evaluations, constraint_route = _candidate_constraint_evaluations(
        packet_segment,
        segment,
        validated,
    )
    route = INDEPENDENT_VERIFIER
    reason_codes = []
    if candidate["source_semantics"]["omitted_source_elements"]:
        route = HARD_REJECT
        reason_codes.append("GENERATION_REPORTS_OMISSION")
    if candidate["source_semantics"]["unsupported_additions"]:
        route = HARD_REJECT
        reason_codes.append("GENERATION_REPORTS_UNSUPPORTED_ADDITION")
    readiness = packet_segment.get("dialogue_context_readiness", {})
    if (
        candidate["tone_decision"]["depends_on_dialogue_context"]
        and isinstance(readiness, dict)
        and readiness.get("status") != "ready"
    ):
        route = HARD_REJECT
        reason_codes.append("DIALOGUE_CONTEXT_INCOMPLETE")
    if constraint_route == HARD_REJECT:
        route = HARD_REJECT
        statuses = {
            evaluation["status"] for evaluation in constraint_evaluations
        }
        if "invalid" in statuses:
            reason_codes.append("CONSTRAINT_EVIDENCE_INVALID")
        if "conflict" in statuses:
            reason_codes.append("CONFIRMED_CONSTRAINT_CONFLICT")
        if "mismatch" in statuses:
            reason_codes.append("CONFIRMED_CONSTRAINT_MISMATCH")
    elif constraint_route == INDEPENDENT_VERIFIER and route != HARD_REJECT:
        route = INDEPENDENT_VERIFIER
        reason_codes.append("CONSTRAINT_REQUIRES_INDEPENDENT_REVIEW")
    return ({
        "id": segment["id"],
        "risk_route": route,
        "reason_codes": reason_codes,
        "candidate_digest": candidate["candidate_digest"],
        "candidate_constraint_evaluations": constraint_evaluations,
    }, candidate)


def _build_candidate_artifact_payload(
    packet: dict,
    draft_entries: dict[int, dict],
    abstained: set[int],
    abstention_reasons: dict[int, dict],
    generation_worker_receipts: list[dict],
    generation_draft_digest: str,
    segments: list[dict],
    *,
    generation_batches: list[dict] | None = None,
    target_form_policy: dict | None = None,
) -> dict:
    segment_map = {segment["id"]: segment for segment in segments}
    packet_segments = {segment["id"]: segment for segment in packet["segments"]}
    excluded = {entry["id"]: entry for entry in packet["excluded_segments"]}
    routes = []
    entries = []
    for segment_id in packet["reviewed_ids"]:
        if segment_id in excluded:
            routes.append(copy.deepcopy(excluded[segment_id]))
            continue
        if segment_id in abstained:
            reason = abstention_reasons[segment_id]
            routes.append({
                "id": segment_id,
                "risk_route": HARD_REJECT,
                "reason_codes": ["WORKER_ABSTAINED", *reason["reason_codes"]],
            })
            continue
        draft_entry = draft_entries[segment_id]
        route, candidate = _candidate_route(
            packet_segments[segment_id],
            segment_map[segment_id],
            draft_entry["reference_target"],
            draft_entry["source_semantics"],
            draft_entry["tone_decision"],
            target_form_policy,
        )
        routes.append(route)
        if candidate is not None:
            entries.append(candidate)
    payload = {
        "schema": CANDIDATE_SCHEMA,
        "version": CANDIDATE_VERSION,
        **{
            key: copy.deepcopy(packet[key])
            for key in (
                "job_id",
                "job_runtime_contract_version",
                "state_revision_digest",
                "results_basis_digest",
                "project_asset_snapshot_digest",
                "capability_resolution_digest",
                "context_bundle_set_digest",
                "worker_context_manifest_digest",
                "protected_signature_digest",
                "created_at",
                "reviewed_ids",
            )
        },
        "selection": copy.deepcopy(packet["selection"]),
        **(
            {
                "selected_evidence_index": copy.deepcopy(
                    packet["selected_evidence_index"]
                ),
                "checker_worker_receipts": copy.deepcopy(
                    packet["checker_worker_receipts"]
                ),
            }
            if "selected_evidence_index" in packet
            else {}
        ),
        "generation_packet_digest": packet["packet_digest"],
        "generation_draft_digest": generation_draft_digest,
        "generation_worker_receipts": copy.deepcopy(
            generation_worker_receipts
        ),
        "abstention_reasons": [
            copy.deepcopy(abstention_reasons[segment_id])
            for segment_id in packet["reviewed_ids"]
            if segment_id in abstention_reasons
        ],
        **(
            {"generation_batches": copy.deepcopy(generation_batches)}
            if generation_batches is not None
            else {}
        ),
        "routes": routes,
        "entries": entries,
    }
    payload["publisher_receipt"] = _publisher_receipt(
        "lqe_suggestions.publish-candidates", payload
    )
    payload = _with_digest(payload, "artifact_digest")
    _validate_schema(payload, CANDIDATE_SCHEMA, CANDIDATE_VERSION)
    validate_candidate_artifact(
        payload,
        packet,
        segments,
        target_form_policy=target_form_policy,
    )
    return payload


def build_candidate_artifact(
    packet: dict,
    draft: dict,
    segments: list[dict],
    *,
    target_form_policy: dict | None = None,
) -> dict:
    validate_generation_draft(draft, packet)
    return _build_candidate_artifact_payload(
        packet,
        {entry["id"]: entry for entry in draft["entries"]},
        set(draft["abstained_ids"]),
        {item["id"]: item for item in draft["abstention_reasons"]},
        [copy.deepcopy(draft["worker_receipt"])],
        canonical_digest(draft),
        segments,
        target_form_policy=target_form_policy,
    )


def build_candidate_artifact_from_batches(
    root_packet: dict,
    batches: list[dict],
    drafts: list[dict],
    segments: list[dict],
    *,
    target_form_policy: dict | None = None,
) -> dict:
    if len(batches) != len(drafts) or len(batches) < 2:
        raise ValueError("suggestion generation batch draft count is invalid")
    entries = {}
    abstained = set()
    abstention_reasons = {}
    receipts = []
    batch_evidence = []
    covered_ids = []
    for batch, draft in zip(batches, drafts):
        packet = batch["packet"]
        validate_generation_draft(draft, packet)
        receipt = _worker_receipt(
            draft["worker_receipt"],
            label=f"generation {batch['batch_id']} worker receipt",
        )
        if any(
            receipt["worker_id"] == prior["worker_id"]
            or receipt["run_id"] == prior["run_id"]
            for prior in receipts
        ):
            raise ValueError("suggestion generation batches must use fresh workers")
        receipts.append(receipt)
        for entry in draft["entries"]:
            if entry["id"] in entries:
                raise ValueError("suggestion generation batches overlap")
            entries[entry["id"]] = copy.deepcopy(entry)
        overlap = abstained.intersection(draft["abstained_ids"])
        if overlap:
            raise ValueError("suggestion generation batch abstentions overlap")
        abstained.update(draft["abstained_ids"])
        for item in draft["abstention_reasons"]:
            if item["id"] in abstention_reasons:
                raise ValueError("suggestion generation batch abstention reasons overlap")
            abstention_reasons[item["id"]] = copy.deepcopy(item)
        covered_ids.extend(packet["reviewed_ids"])
        batch_evidence.append({
            "batch_id": batch["batch_id"],
            "reviewed_ids": copy.deepcopy(packet["reviewed_ids"]),
            "packet_digest": packet["packet_digest"],
            "worker_context_manifest_digest": packet[
                "worker_context_manifest_digest"
            ],
            "draft_digest": canonical_digest(draft),
            "worker_receipt": copy.deepcopy(receipt),
        })
    root_worker_ids = [segment["id"] for segment in root_packet["segments"]]
    if covered_ids != root_worker_ids:
        raise ValueError("suggestion generation batch coverage is incomplete")
    if set(entries).intersection(abstained) or set(entries).union(abstained) != set(
        root_worker_ids
    ):
        raise ValueError("suggestion generation batch result coverage is incomplete")
    return _build_candidate_artifact_payload(
        root_packet,
        entries,
        abstained,
        abstention_reasons,
        receipts,
        canonical_digest([canonical_digest(draft) for draft in drafts]),
        segments,
        generation_batches=batch_evidence,
        target_form_policy=target_form_policy,
    )


def validate_candidate_artifact(
    artifact: object,
    packet: dict,
    segments: list[dict],
    *,
    target_form_policy: dict | None = None,
) -> dict:
    _validate_schema(artifact, CANDIDATE_SCHEMA, CANDIDATE_VERSION)
    _validate_self_digest(artifact, "artifact_digest", "candidate artifact")
    _validate_publisher_receipt(
        artifact, "lqe_suggestions.publish-candidates", "candidate artifact"
    )
    if artifact["generation_packet_digest"] != packet["packet_digest"]:
        raise ValueError("candidate artifact generation packet is stale")
    generation_receipts = artifact["generation_worker_receipts"]
    if not isinstance(generation_receipts, list) or not generation_receipts:
        raise ValueError("candidate generation worker receipts are missing")
    canonical_receipts = [
        _worker_receipt(
            receipt,
            label=f"candidate generation worker receipt {index}",
        )
        for index, receipt in enumerate(generation_receipts)
    ]
    if (
        generation_receipts != canonical_receipts
        or len({receipt["worker_id"] for receipt in generation_receipts})
        != len(generation_receipts)
        or len({receipt["run_id"] for receipt in generation_receipts})
        != len(generation_receipts)
    ):
        raise ValueError("candidate generation worker receipts are not canonical")
    if "selected_evidence_index" in packet:
        if artifact.get("selected_evidence_index") != packet[
            "selected_evidence_index"
        ] or artifact.get("checker_worker_receipts") != packet.get(
            "checker_worker_receipts"
        ):
            raise ValueError("candidate checker-selected evidence is stale")
        checker_receipts = [
            _worker_receipt(item, label="candidate checker worker receipt")
            for item in artifact["checker_worker_receipts"]
        ]
        for generation in canonical_receipts:
            for checker in checker_receipts:
                if (
                    generation["worker_id"] == checker["worker_id"]
                    or generation["run_id"] == checker["run_id"]
                ):
                    raise ValueError(
                        "candidate generation worker overlaps a checker worker"
                    )
    elif (
        artifact.get("selected_evidence_index") is not None
        or artifact.get("checker_worker_receipts") is not None
    ):
        raise ValueError("candidate has unexpected checker-selected evidence")
    generation_batches = artifact.get("generation_batches")
    if "batch_plan" in packet:
        if not isinstance(generation_batches, list) or len(generation_batches) < 2:
            raise ValueError("candidate generation batch evidence is missing")
        if [item.get("worker_receipt") for item in generation_batches] != generation_receipts:
            raise ValueError("candidate generation batch worker receipts are stale")
        covered = [
            segment_id
            for item in generation_batches
            for segment_id in item.get("reviewed_ids", [])
        ]
        if covered != [segment["id"] for segment in packet["segments"]]:
            raise ValueError("candidate generation batch evidence coverage is stale")
    elif generation_batches is not None:
        raise ValueError("single-batch candidate has unexpected batch evidence")
    for key in (
        "job_id",
        "job_runtime_contract_version",
        "state_revision_digest",
        "results_basis_digest",
        "project_asset_snapshot_digest",
        "capability_resolution_digest",
        "context_bundle_set_digest",
        "worker_context_manifest_digest",
        "protected_signature_digest",
        "reviewed_ids",
        "selection",
    ):
        if artifact[key] != packet[key]:
            raise ValueError(f"candidate artifact {key} is stale")
    route_ids = [route["id"] for route in artifact["routes"]]
    if route_ids != packet["reviewed_ids"] or len(route_ids) != len(set(route_ids)):
        raise ValueError("candidate artifact route id coverage is invalid")
    entries = artifact["entries"]
    entry_ids = [entry["id"] for entry in entries]
    if len(entry_ids) != len(set(entry_ids)):
        raise ValueError("candidate artifact has duplicate candidate ids")
    route_map = {route["id"]: route for route in artifact["routes"]}
    abstained_route_ids = [
        route["id"]
        for route in artifact["routes"]
        if "WORKER_ABSTAINED" in route["reason_codes"]
    ]
    abstention_reasons = _validate_abstention_reasons(
        artifact["abstention_reasons"],
        abstained_route_ids,
        label="candidate artifact abstention_reasons",
    )
    for reason in abstention_reasons:
        if route_map[reason["id"]]["reason_codes"] != [
            "WORKER_ABSTAINED",
            *reason["reason_codes"],
        ]:
            raise ValueError(
                f"candidate artifact id {reason['id']} abstention reason is stale"
            )
    if any(route["risk_route"] not in RISK_ROUTES for route in artifact["routes"]):
        raise ValueError("candidate artifact has an invalid risk route")
    expected_entry_ids = [
        segment_id
        for segment_id in packet["reviewed_ids"]
        if isinstance(route_map[segment_id].get("candidate_digest"), str)
    ]
    if entry_ids != expected_entry_ids:
        raise ValueError("candidate artifact entry coverage is invalid")
    segment_map = {segment["id"]: segment for segment in segments}
    packet_segment_map = {segment["id"]: segment for segment in packet["segments"]}
    excluded_map = {entry["id"]: entry for entry in packet["excluded_segments"]}
    for segment_id, excluded in excluded_map.items():
        if route_map.get(segment_id) != excluded:
            raise ValueError(
                f"candidate artifact id {segment_id} changed a deterministic hard reject"
            )
    for entry in entries:
        base = {
            key: copy.deepcopy(entry[key])
            for key in (
                "id",
                "reference_target",
                "source_semantics",
                "tone_decision",
            )
        }
        if entry["candidate_digest"] != canonical_digest(base):
            raise ValueError(f"candidate artifact id {entry['id']} digest is invalid")
        validate_reference_target(
            segment_map[entry["id"]],
            entry["reference_target"],
            label=f"candidate artifact id {entry['id']}",
            target_form_policy=target_form_policy,
        )
        if route_map[entry["id"]].get("candidate_digest") != entry["candidate_digest"]:
            raise ValueError(f"candidate artifact id {entry['id']} route digest mismatch")
        expected_route, expected_candidate = _candidate_route(
            packet_segment_map[entry["id"]],
            segment_map[entry["id"]],
            entry["reference_target"],
            entry["source_semantics"],
            entry["tone_decision"],
            target_form_policy,
        )
        if expected_candidate != entry:
            raise ValueError(
                f"candidate artifact id {entry['id']} candidate is invalid"
            )
        if route_map[entry["id"]] != expected_route:
            raise ValueError(
                f"candidate artifact id {entry['id']} risk route is invalid"
            )
    return artifact


def validate_suggestion_artifact(
    artifact: object,
    packet: dict,
    segments: list[dict],
    *,
    candidate_artifact: dict | None = None,
    review_artifact: dict | None = None,
    review_packet: dict | None = None,
    target_form_policy: dict | None = None,
) -> dict[int, str]:
    _validate_schema(artifact, ARTIFACT_SCHEMA, ARTIFACT_VERSION)
    _validate_self_digest(artifact, "artifact_digest", "final suggestion artifact")
    _validate_publisher_receipt(
        artifact, "lqe_suggestion_review.publish-final", "final suggestion artifact"
    )
    generation_receipts = [
        _worker_receipt(receipt, label="final generation worker receipt")
        for receipt in artifact["generation_worker_receipts"]
    ]
    review_receipts = [
        _worker_receipt(receipt, label="final review worker receipt")
        for receipt in artifact["review_worker_receipts"]
    ]
    if (
        len({receipt["worker_id"] for receipt in generation_receipts})
        != len(generation_receipts)
        or len({receipt["run_id"] for receipt in generation_receipts})
        != len(generation_receipts)
        or len({receipt["worker_id"] for receipt in review_receipts})
        != len(review_receipts)
        or len({receipt["run_id"] for receipt in review_receipts})
        != len(review_receipts)
    ):
        raise ValueError("final suggestion worker receipts are not unique")
    if "selected_evidence_index" in packet:
        if (
            artifact.get("selected_evidence_index")
            != packet["selected_evidence_index"]
            or artifact.get("checker_worker_receipts")
            != packet["checker_worker_receipts"]
        ):
            raise ValueError("final suggestion selected evidence is stale")
        checker_receipts = [
            _worker_receipt(item, label="final checker worker receipt")
            for item in artifact["checker_worker_receipts"]
        ]
    else:
        if (
            artifact.get("selected_evidence_index") is not None
            or artifact.get("checker_worker_receipts") is not None
        ):
            raise ValueError("final suggestion has unexpected selected evidence")
        checker_receipts = []
    for generation in generation_receipts:
        for review in review_receipts:
            if generation["worker_id"] == review["worker_id"] or generation[
                "run_id"
            ] == review["run_id"]:
                raise ValueError("final suggestion workers are not independent")
    for checker in checker_receipts:
        for worker in [*generation_receipts, *review_receipts]:
            if (
                checker["worker_id"] == worker["worker_id"]
                or checker["run_id"] == worker["run_id"]
            ):
                raise ValueError("final suggestion worker overlaps a checker worker")
    for key in (
        "job_id",
        "job_runtime_contract_version",
        "state_revision_digest",
        "results_basis_digest",
        "project_asset_snapshot_digest",
        "capability_resolution_digest",
        "context_bundle_set_digest",
        "worker_context_manifest_digest",
        "protected_signature_digest",
        "reviewed_ids",
        "selection",
    ):
        if artifact[key] != packet[key]:
            raise ValueError(f"final suggestion artifact {key} is stale")
    final_ids = [entry["id"] for entry in artifact["final_entries"]]
    excluded_ids = [entry["id"] for entry in artifact["excluded_ids"]]
    if len(final_ids) != len(set(final_ids)) or len(excluded_ids) != len(set(excluded_ids)):
        raise ValueError("final suggestion artifact has duplicate ids")
    if set(final_ids) & set(excluded_ids):
        raise ValueError("final suggestion artifact id sets overlap")
    if set(final_ids) | set(excluded_ids) != set(artifact["reviewed_ids"]):
        raise ValueError("final suggestion artifact id coverage is invalid")
    order = {segment_id: index for index, segment_id in enumerate(artifact["reviewed_ids"])}
    if final_ids != sorted(final_ids, key=order.get) or excluded_ids != sorted(
        excluded_ids, key=order.get
    ):
        raise ValueError("final suggestion artifact id order is invalid")
    segment_map = {segment["id"]: segment for segment in segments}
    output = {}
    for entry in artifact["final_entries"]:
        output[entry["id"]] = validate_reference_target(
            segment_map[entry["id"]],
            entry["reference_target"],
            label=f"final suggestion artifact id {entry['id']}",
            target_form_policy=target_form_policy,
        )
        if entry["candidate_digest"] != canonical_digest({
            key: copy.deepcopy(entry[key])
            for key in (
                "id",
                "reference_target",
                "source_semantics",
                "tone_decision",
            )
        }):
            raise ValueError(f"final suggestion artifact id {entry['id']} digest is invalid")
    if candidate_artifact is not None:
        if artifact["candidate_artifact_digest"] != candidate_artifact["artifact_digest"]:
            raise ValueError("final suggestion artifact candidate digest is stale")
        if artifact["generation_worker_receipts"] != candidate_artifact[
            "generation_worker_receipts"
        ]:
            raise ValueError("final suggestion artifact generation worker is stale")
        if (
            artifact.get("selected_evidence_index")
            != candidate_artifact.get("selected_evidence_index")
            or artifact.get("checker_worker_receipts")
            != candidate_artifact.get("checker_worker_receipts")
        ):
            raise ValueError("final suggestion candidate evidence is stale")
        candidate_map = {entry["id"]: entry for entry in candidate_artifact["entries"]}
        route_map = {
            route["id"]: route for route in candidate_artifact["routes"]
        }
        for entry in artifact["final_entries"]:
            if candidate_map.get(entry["id"]) != {
                key: copy.deepcopy(entry[key])
                for key in (
                    "id",
                    "reference_target",
                    "source_semantics",
                    "tone_decision",
                    "candidate_digest",
                )
            }:
                raise ValueError("final suggestion artifact changed a candidate")
            if entry["risk_route"] != route_map[entry["id"]]["risk_route"]:
                raise ValueError("final suggestion artifact changed a risk route")
        independent_ids = [
            segment_id
            for segment_id in artifact["reviewed_ids"]
            if route_map[segment_id]["risk_route"] == INDEPENDENT_VERIFIER
        ]
        if independent_ids and review_artifact is None:
            raise ValueError("final suggestion artifact independent review is missing")
        verdict_map = {}
        if review_artifact is not None:
            _validate_schema(
                review_artifact,
                REVIEW_ARTIFACT_SCHEMA,
                REVIEW_ARTIFACT_VERSION,
            )
            _validate_self_digest(
                review_artifact,
                "artifact_digest",
                "suggestion review artifact",
            )
            _validate_publisher_receipt(
                review_artifact,
                "lqe_suggestion_review.publish-review",
                "suggestion review artifact",
            )
            if artifact["review_artifact_digest"] != review_artifact["artifact_digest"]:
                raise ValueError("final suggestion artifact review digest is stale")
            if artifact["review_worker_receipts"] != review_artifact[
                "review_worker_receipts"
            ]:
                raise ValueError("final suggestion artifact review worker is stale")
            if review_artifact["generation_worker_receipts"] != candidate_artifact[
                "generation_worker_receipts"
            ]:
                raise ValueError("suggestion review generation worker is stale")
            if review_artifact["candidate_artifact_digest"] != candidate_artifact["artifact_digest"]:
                raise ValueError("suggestion review artifact candidate digest is stale")
            if (
                review_artifact.get("selected_evidence_index")
                != candidate_artifact.get("selected_evidence_index")
                or review_artifact.get("checker_worker_receipts")
                != candidate_artifact.get("checker_worker_receipts")
            ):
                raise ValueError("suggestion review selected evidence is stale")
            live_review_packet = (
                review_packet
                if review_packet is not None
                else build_suggestion_review_packet(
                    packet,
                    candidate_artifact,
                )
            )
            if (
                review_artifact["review_packet_digest"]
                != live_review_packet["packet_digest"]
            ):
                raise ValueError(
                    "suggestion review artifact verifier instructions are stale"
                )
            for key in (
                "job_id",
                "job_runtime_contract_version",
                "state_revision_digest",
                "results_basis_digest",
                "project_asset_snapshot_digest",
                "capability_resolution_digest",
                "context_bundle_set_digest",
                "worker_context_manifest_digest",
                "protected_signature_digest",
            ):
                if review_artifact[key] != candidate_artifact[key]:
                    raise ValueError(f"suggestion review artifact {key} is stale")
            if review_artifact["reviewed_ids"] != independent_ids:
                raise ValueError("suggestion review artifact id coverage is stale")
            verdict_map = {
                verdict["id"]: verdict for verdict in review_artifact["verdicts"]
            }
            if list(verdict_map) != independent_ids:
                raise ValueError("suggestion review artifact verdict coverage is stale")
            for segment_id in independent_ids:
                if (
                    verdict_map[segment_id]["candidate_digest"]
                    != candidate_map[segment_id]["candidate_digest"]
                ):
                    raise ValueError("suggestion review artifact changed a candidate")
                if any(
                    verdict_map[segment_id]["semantic_verification"][field][
                        "status"
                    ]
                    != "pass"
                    for field in SEMANTIC_CHECK_FIELDS
                ) and verdict_map[segment_id]["decision"] == "accept":
                    raise ValueError(
                        "suggestion review artifact accepted incomplete semantic verification"
                    )
            for entry in artifact["final_entries"]:
                if entry["semantic_verification"] != verdict_map[entry["id"]][
                    "semantic_verification"
                ]:
                    raise ValueError(
                        "final suggestion artifact changed semantic verification"
                    )
        expected_final_ids = [
            segment_id
            for segment_id in artifact["reviewed_ids"]
            if (
                route_map[segment_id]["risk_route"] == INDEPENDENT_VERIFIER
                and verdict_map.get(segment_id, {}).get("decision") == "accept"
            )
        ]
        if final_ids != expected_final_ids:
            raise ValueError("final suggestion artifact publication decisions are invalid")
        excluded_map = {entry["id"]: entry for entry in artifact["excluded_ids"]}
        for segment_id in excluded_ids:
            route = route_map[segment_id]
            if route["risk_route"] == HARD_REJECT:
                expected_reasons = route["reason_codes"]
            else:
                verdict = verdict_map[segment_id]
                expected_reasons = verdict["reason_codes"] or [
                    f"VERIFIER_{verdict['decision'].upper()}"
                ]
            if excluded_map[segment_id]["reason_codes"] != expected_reasons:
                raise ValueError("final suggestion artifact exclusion reasons are invalid")
    return output


def load_reference_suggestions(
    job: Path,
    segments: list[dict],
    manifest: dict | None,
    results: list[dict],
    review_policy: dict | None = None,
) -> dict[int, str]:
    artifact_path = Path(job) / ARTIFACT_NAME
    if not artifact_path.is_file():
        return {}
    artifact = read_json(artifact_path)
    if not isinstance(artifact, dict) or (
        artifact.get("schema") != ARTIFACT_SCHEMA
        or artifact.get("version") != ARTIFACT_VERSION
    ):
        raise ValueError(
            "reference suggestion artifact schema/version is invalid; "
            "only independently reviewed v5 final artifacts are accepted"
        )
    state_path = Path(job) / "state.json"
    state = read_json(state_path) if state_path.is_file() else {}
    selection = _normalize_selection(artifact.get("selection"), review_policy)
    packet_plan = _build_live_packet_plan(
        Path(job), state, segments, manifest, results, selection
    )
    require_persisted_packet_plan(Path(job), packet_plan)
    packet = packet_plan["root_packet"]
    candidate_path = Path(job) / CANDIDATE_NAME
    if not candidate_path.is_file():
        raise ValueError("reference suggestion candidate artifact is missing")
    candidate = read_json(candidate_path)
    target_form_policy = load_target_form_policy(state)
    validate_candidate_artifact(
        candidate,
        packet,
        segments,
        target_form_policy=target_form_policy,
    )
    review = None
    review_packet = None
    review_path = Path(job) / "suggestion_review.json"
    if review_path.is_file():
        review = read_json(review_path)
        from lqe_suggestion_review import (
            _build_review_packet_plan,
            _require_persisted_review_packet_plan,
        )

        review_plan = _build_review_packet_plan(Path(job), packet_plan, candidate)
        _require_persisted_review_packet_plan(Path(job), review_plan)
        review_packet = review_plan["root_packet"]
    return validate_suggestion_artifact(
        artifact,
        packet,
        segments,
        candidate_artifact=candidate,
        review_artifact=review,
        review_packet=review_packet,
        target_form_policy=target_form_policy,
    )


def cmd_prepare(args) -> None:
    job = Path(args.job).resolve()
    state, segments, manifest, results = _load_live(
        job, state_name=args.state, errors_name=args.errors
    )
    require_current_job_runtime(state, "suggestions-prepare")
    categories = [
        category.strip()
        for category in (args.categories or "").split(",")
        if category.strip()
    ]
    review_policy = get_review_policy(state)
    severities = (
        [item.strip() for item in args.severities.split(",") if item.strip()]
        if args.severities is not None
        else list(review_policy["suggestion_candidate_severities"])
    )
    selection = {
        "categories": categories,
        "severities": severities,
        "only_missing": args.only_missing,
    }
    packet_plan = _build_live_packet_plan(
        job,
        state,
        segments,
        manifest,
        results,
        selection,
        worker_batch_size=args.worker_batch_size,
    )
    packet = packet_plan["root_packet"]
    output = Path(args.out) if args.out else job / PACKET_NAME
    _write_packet_plan(job, state, packet_plan)
    write_json_atomic(output, packet)
    measurement = _suggestion_input_measurement(packet_plan)
    print(
        f"[lqe_suggestions] Generation packet → {output} "
        f"({len(packet['segments'])} generation candidate(s), "
        f"{len(packet['excluded_segments'])} hard rejected, "
        f"{len(packet_plan['batches'])} worker batch(es), "
        f"largest measured {measurement['largest_worker_input_bytes']} bytes, "
        f"total {measurement['total_worker_input_bytes']} bytes; advisory)"
    )


def cmd_publish(args) -> None:
    raise ValueError(
        "legacy 'publish' no longer creates a final suggestion artifact; "
        "use 'publish-candidates', then lqe_suggestion_review.py prepare / "
        "publish-review / publish-final"
    )


def cmd_publish_candidates(args) -> None:
    job = Path(args.job).resolve()
    state, segments, manifest, results = _load_live(
        job, state_name=args.state, errors_name=args.errors
    )
    require_current_job_runtime(state, "suggestions-publish-candidates")
    input_path = Path(args.input)
    draft = read_json(input_path) if input_path.is_file() else None
    if draft is not None:
        selection = draft.get("selection") if isinstance(draft, dict) else None
    else:
        prepared_packet_path = job / PACKET_NAME
        if not input_path.is_dir() or not prepared_packet_path.is_file():
            raise ValueError(
                "batched suggestion drafts input must be the prepared batch directory"
            )
        prepared_packet = read_json(prepared_packet_path)
        selection = prepared_packet.get("selection")
    packet_plan = _build_live_packet_plan(
        job, state, segments, manifest, results, selection
    )
    require_persisted_packet_plan(job, packet_plan)
    packet = packet_plan["root_packet"]
    if packet_plan["mode"] == "single":
        if draft is None:
            raise ValueError("single-batch suggestion draft must be a JSON file")
        artifact = build_candidate_artifact(
            packet,
            draft,
            segments,
            target_form_policy=load_target_form_policy(state),
        )
    else:
        if not input_path.is_dir():
            raise ValueError(
                "multiple suggestion batches require --input to name the batch directory"
            )
        drafts = []
        for batch in packet_plan["batches"]:
            draft_path = input_path / batch["batch_id"] / "generation.draft.json"
            if not draft_path.is_file():
                raise ValueError(
                    f"suggestion generation draft is missing: {draft_path}"
                )
            drafts.append(read_json(draft_path))
        artifact = build_candidate_artifact_from_batches(
            packet,
            packet_plan["batches"],
            drafts,
            segments,
            target_form_policy=load_target_form_policy(state),
        )
    output = Path(args.out) if args.out else job / CANDIDATE_NAME
    write_json_atomic(output, artifact)
    route_counts = {
        route: sum(item["risk_route"] == route for item in artifact["routes"])
        for route in sorted(RISK_ROUTES)
    }
    print(f"[lqe_suggestions] Candidates → {output} ({route_counts})")


def cmd_validate(args) -> None:
    job = Path(args.job).resolve()
    state, segments, manifest, results = _load_live(
        job, state_name=args.state, errors_name=args.errors
    )
    artifact_path = Path(args.input) if args.input else job / ARTIFACT_NAME
    artifact = read_json(artifact_path)
    if not isinstance(artifact, dict):
        raise ValueError("reference suggestion artifact must be an object")
    if artifact.get("schema") != ARTIFACT_SCHEMA or artifact.get("version") != 5:
        raise ValueError("only independently reviewed v5 final artifacts are accepted")
    packet_plan = _build_live_packet_plan(
        job, state, segments, manifest, results, artifact.get("selection")
    )
    require_persisted_packet_plan(job, packet_plan)
    packet = packet_plan["root_packet"]
    candidate = read_json(job / CANDIDATE_NAME)
    target_form_policy = load_target_form_policy(state)
    validate_candidate_artifact(
        candidate,
        packet,
        segments,
        target_form_policy=target_form_policy,
    )
    review_path = job / "suggestion_review.json"
    review = read_json(review_path) if review_path.is_file() else None
    review_packet = None
    if review is not None:
        from lqe_suggestion_review import (
            _build_review_packet_plan,
            _require_persisted_review_packet_plan,
        )

        review_plan = _build_review_packet_plan(job, packet_plan, candidate)
        _require_persisted_review_packet_plan(job, review_plan)
        review_packet = review_plan["root_packet"]
    suggestions = validate_suggestion_artifact(
        artifact,
        packet,
        segments,
        candidate_artifact=candidate,
        review_artifact=review,
        review_packet=review_packet,
        target_form_policy=target_form_policy,
    )
    print(f"[lqe_suggestions] Valid v5 final → {artifact_path} ({len(suggestions)} suggestion(s))")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage independently reviewed report-only reference suggestions."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "publish", "publish-candidates", "validate"):
        command = subparsers.add_parser(name)
        command.add_argument("--job", required=True)
        command.add_argument("--state", default="state.json")
        command.add_argument("--errors", default="errors.json")
        if name == "prepare":
            command.add_argument("--out")
            command.add_argument("--categories")
            command.add_argument("--severities", default=None)
            command.add_argument("--only-missing", action="store_true")
            command.add_argument(
                "--worker-batch-size",
                type=int,
                help=(
                    "agent-selected maximum candidate count per worker; "
                    "omit for one advisory-measured batch"
                ),
            )
            command.set_defaults(func=cmd_prepare)
        elif name == "publish-candidates":
            command.add_argument("--input", required=True)
            command.add_argument("--out")
            command.set_defaults(func=cmd_publish_candidates)
        elif name == "publish":
            command.add_argument("--input")
            command.add_argument("--out")
            command.set_defaults(func=cmd_publish)
        else:
            command.add_argument("--input")
            command.set_defaults(func=cmd_validate)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except (CheckFormatError, OSError, ValueError) as exc:
        raise SystemExit(f"[lqe_suggestions] {exc}") from exc


if __name__ == "__main__":
    main()
