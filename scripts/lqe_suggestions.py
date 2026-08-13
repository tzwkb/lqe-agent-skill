#!/usr/bin/env python3
"""Build, publish, and validate report-only reference suggestions."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from lqe_chunk import verification_generation_lease
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
    require_current_job_runtime,
    requires_bound_artifacts,
)
from lqe_paths import write_json_atomic
from lqe_result_contract import result_contract_path, validate_result_contract
from lqe_split_contract import canonical_digest
from lqe_context_bundle import (
    ContextBundleError,
    build_context_bundle_set,
    build_worker_context_manifest,
    validate_context_bundle_set,
    validate_worker_context_manifest,
)
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
MAX_SUGGESTION_CONTEXT_BYTES = 100_000
SUGGESTION_CONTEXT_DIR = "suggestion_context"
SUGGESTION_BUNDLE_SET_NAME = "bundle_set.json"
SUGGESTION_WORKER_MANIFEST_NAME = "worker_manifest.json"
SUGGESTION_REVIEW_INSTRUCTIONS_PATH = (
    Path(__file__).resolve().parents[1] / "references" / "suggestion_review.md"
)
_UNBOUND_WORKER_CONTEXT_DIGEST = "0" * 64

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


def enforce_suggestion_worker_budget(
    worker_manifest: dict,
    packet: dict,
    *,
    label: str,
    additional_raw_bytes: int = 0,
) -> int:
    if type(additional_raw_bytes) is not int or additional_raw_bytes < 0:
        raise ValueError(f"{label} additional raw bytes must be a non-negative integer")
    manifest = validate_worker_context_manifest(worker_manifest)
    budget = manifest["budget"]
    if budget["max_bytes"] != MAX_SUGGESTION_CONTEXT_BYTES:
        raise ValueError(
            f"{label} worker context budget is not "
            f"{MAX_SUGGESTION_CONTEXT_BYTES} bytes"
        )
    base_bytes = budget["measured_bytes"] - manifest["packet_payloads"]["bytes"]
    if base_bytes < 0:
        raise ValueError(f"{label} worker context byte accounting is invalid")
    measured = (
        base_bytes
        + _canonical_size([packet])
        + _canonical_size(manifest)
        + additional_raw_bytes
    )
    if measured > MAX_SUGGESTION_CONTEXT_BYTES:
        raise ValueError(
            f"{label} worker input is {measured} bytes, exceeding budget "
            f"{MAX_SUGGESTION_CONTEXT_BYTES}; split the batch"
        )
    return measured


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


def build_suggestion_review_packet(
    generation_packet: dict,
    candidate_artifact: dict,
) -> dict:
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
    ]
    entries = []
    for segment_id in reviewed_ids:
        segment = generation_segments[segment_id]
        candidate = candidate_map[segment_id]
        entries.append({
            **copy.deepcopy(segment),
            "reference_target": candidate["reference_target"],
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
            key: copy.deepcopy(candidate_artifact[key])
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
        "entries": entries,
        "instructions": {
            "decision_values": ["accept", "reject", "human_required"],
            "candidate_text_is_read_only": True,
            "verify_source_meaning": True,
            "verify_all_known_issues": True,
            "verify_confirmed_constraints": True,
            "verifier_instructions": verifier_instructions,
            "worker_context": {
                "manifest_path": "suggestion_context/worker_manifest.json",
                "manifest_digest": candidate_artifact[
                    "worker_context_manifest_digest"
                ],
                "bundle_set_path": "suggestion_context/bundle_set.json",
                "bundle_set_digest": candidate_artifact[
                    "context_bundle_set_digest"
                ],
                "same_evidence_as_generation": True,
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
) -> dict:
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
    bundle_digests = _bundle_digests_by_id(context_bundle_set)
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
        reviewed_ids.append(segment_id)
        reason_codes = _segment_block_reason_codes(segment, errors)
        if reason_codes:
            excluded_segments.append({
                "id": segment_id,
                "risk_route": HARD_REJECT,
                "reason_codes": reason_codes,
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
        for near in segment.get("term_near", []):
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
                    segment.get("resolved_constraints", [])
                ),
                "protected_texts": copy.deepcopy(segment.get("protected_texts", [])),
            },
        }
        if bundle_digests:
            if segment_id not in bundle_digests:
                raise ValueError(
                    f"suggestion context bundle is missing segment id {segment_id!r}"
                )
            item["context_bundle_digest"] = bundle_digests[segment_id]
        if project_segment_for_module is not None:
            context_projection = project_segment_for_module(
                segment, "suggestions", context_registry
            )
            if context_projection:
                item["context_projection"] = context_projection
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
                "manifest_path": (
                    f"{SUGGESTION_CONTEXT_DIR}/{SUGGESTION_WORKER_MANIFEST_NAME}"
                ),
                "manifest_digest": worker_context_manifest_digest,
                "bundle_set_path": (
                    f"{SUGGESTION_CONTEXT_DIR}/{SUGGESTION_BUNDLE_SET_NAME}"
                ),
                "bundle_set_digest": bindings["context_bundle_set_digest"],
                "required_for_worker": True,
            },
        },
    }
    if context_bundle_set is not None:
        payload["instructions"]["context_bundle_set"] = {
            "path": "suggestion_context/bundle_set.json",
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
    packet, _, _ = _build_live_packet_context(
        job,
        state,
        segments,
        manifest,
        results,
        selection,
    )
    return packet


def _build_live_packet_context(
    job: Path,
    state: dict,
    segments: list[dict],
    manifest: dict | None,
    results: list[dict],
    selection: object | None,
) -> tuple[dict, dict, dict]:
    if not isinstance(state.get("project_asset_snapshot"), dict) or not isinstance(
        state.get("capability_resolution"), dict
    ):
        raise ValueError(
            "suggestion worker context requires bound project assets and "
            "capability resolution"
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
    )
    selected_ids = {segment["id"] for segment in preliminary["segments"]}
    selected_segments = [
        segment for segment in segments if segment["id"] in selected_ids
    ]
    try:
        bundle_set = build_context_bundle_set(
            state, selected_segments, "suggestions"
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
    )
    try:
        worker_manifest = build_worker_context_manifest(
            state,
            "suggestions",
            bundle_set,
            max_worker_bytes=MAX_SUGGESTION_CONTEXT_BYTES,
            packet_payloads=[_worker_packet_basis(packet_basis)],
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
    enforce_suggestion_worker_budget(
        worker_manifest,
        packet,
        label="suggestion generation",
    )
    return packet, bundle_set, worker_manifest


def require_persisted_suggestion_context(
    job: Path,
    packet: dict,
    bundle_set: dict,
    worker_manifest: dict,
) -> None:
    context_dir = Path(job) / SUGGESTION_CONTEXT_DIR
    bundle_path = context_dir / SUGGESTION_BUNDLE_SET_NAME
    manifest_path = context_dir / SUGGESTION_WORKER_MANIFEST_NAME
    if not bundle_path.is_file() or not manifest_path.is_file():
        raise ValueError(
            "prepared suggestion worker context is missing; rerun suggestions prepare"
        )
    persisted_bundle = validate_context_bundle_set(read_json(bundle_path))
    persisted_manifest = validate_worker_context_manifest(read_json(manifest_path))
    if persisted_bundle != bundle_set:
        raise ValueError("prepared suggestion context bundle is stale")
    if persisted_manifest != worker_manifest:
        raise ValueError("prepared suggestion worker context manifest is stale")
    if (
        packet["worker_context_manifest_digest"]
        != persisted_manifest["worker_context_manifest_digest"]
    ):
        raise ValueError("suggestion packet worker context manifest is stale")


def validate_generation_draft(draft: object, packet: dict) -> dict:
    _validate_schema(draft, DRAFT_SCHEMA, DRAFT_VERSION)
    if draft["packet_digest"] != packet["packet_digest"]:
        raise ValueError("reference suggestion generation draft is stale")
    if (
        draft["worker_context_manifest_digest"]
        != packet["worker_context_manifest_digest"]
    ):
        raise ValueError(
            "reference suggestion generation draft worker context is stale"
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
    return draft


def _candidate_entry(segment: dict, reference_target: str) -> dict:
    entry = {
        "id": segment["id"],
        "reference_target": reference_target,
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
) -> tuple[dict, dict | None]:
    try:
        validated = validate_reference_target(
            segment,
            reference_target,
            label=f"reference suggestion candidate {segment['id']}",
        )
    except CheckFormatError:
        return ({
            "id": segment["id"],
            "risk_route": HARD_REJECT,
            "reason_codes": ["DETERMINISTIC_VALIDATION_FAILED"],
        }, None)
    candidate = _candidate_entry(segment, validated)
    constraint_evaluations, constraint_route = _candidate_constraint_evaluations(
        packet_segment,
        segment,
        validated,
    )
    deterministic = (
        isinstance(packet_segment.get("validated_target"), str)
        and validated == packet_segment["validated_target"]
    )
    route = DETERMINISTIC_ACCEPT if deterministic else INDEPENDENT_VERIFIER
    reason_codes = []
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
    elif constraint_route == INDEPENDENT_VERIFIER:
        route = INDEPENDENT_VERIFIER
        reason_codes.append("CONSTRAINT_REQUIRES_INDEPENDENT_REVIEW")
    return ({
        "id": segment["id"],
        "risk_route": route,
        "reason_codes": reason_codes,
        "candidate_digest": candidate["candidate_digest"],
        "candidate_constraint_evaluations": constraint_evaluations,
    }, candidate)


def build_candidate_artifact(
    packet: dict,
    draft: dict,
    segments: list[dict],
) -> dict:
    validate_generation_draft(draft, packet)
    segment_map = {segment["id"]: segment for segment in segments}
    packet_segments = {segment["id"]: segment for segment in packet["segments"]}
    draft_entries = {entry["id"]: entry for entry in draft["entries"]}
    excluded = {entry["id"]: entry for entry in packet["excluded_segments"]}
    abstained = set(draft["abstained_ids"])
    routes = []
    entries = []
    for segment_id in packet["reviewed_ids"]:
        if segment_id in excluded:
            routes.append(copy.deepcopy(excluded[segment_id]))
            continue
        if segment_id in abstained:
            packet_segment = packet_segments[segment_id]
            if isinstance(packet_segment.get("validated_target"), str):
                route, candidate = _candidate_route(
                    packet_segment,
                    segment_map[segment_id],
                    packet_segment["validated_target"],
                )
                routes.append(route)
                if candidate is not None:
                    entries.append(candidate)
            else:
                routes.append({
                    "id": segment_id,
                    "risk_route": HARD_REJECT,
                    "reason_codes": ["WORKER_ABSTAINED"],
                })
            continue
        route, candidate = _candidate_route(
            packet_segments[segment_id],
            segment_map[segment_id],
            draft_entries[segment_id]["reference_target"],
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
        "generation_packet_digest": packet["packet_digest"],
        "generation_draft_digest": canonical_digest(draft),
        "routes": routes,
        "entries": entries,
    }
    payload["publisher_receipt"] = _publisher_receipt(
        "lqe_suggestions.publish-candidates", payload
    )
    payload = _with_digest(payload, "artifact_digest")
    _validate_schema(payload, CANDIDATE_SCHEMA, CANDIDATE_VERSION)
    validate_candidate_artifact(payload, packet, segments)
    return payload


def validate_candidate_artifact(
    artifact: object,
    packet: dict,
    segments: list[dict],
) -> dict:
    _validate_schema(artifact, CANDIDATE_SCHEMA, CANDIDATE_VERSION)
    _validate_self_digest(artifact, "artifact_digest", "candidate artifact")
    _validate_publisher_receipt(
        artifact, "lqe_suggestions.publish-candidates", "candidate artifact"
    )
    if artifact["generation_packet_digest"] != packet["packet_digest"]:
        raise ValueError("candidate artifact generation packet is stale")
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
        base = {"id": entry["id"], "reference_target": entry["reference_target"]}
        if entry["candidate_digest"] != canonical_digest(base):
            raise ValueError(f"candidate artifact id {entry['id']} digest is invalid")
        validate_reference_target(
            segment_map[entry["id"]],
            entry["reference_target"],
            label=f"candidate artifact id {entry['id']}",
        )
        if route_map[entry["id"]].get("candidate_digest") != entry["candidate_digest"]:
            raise ValueError(f"candidate artifact id {entry['id']} route digest mismatch")
        expected_route, expected_candidate = _candidate_route(
            packet_segment_map[entry["id"]],
            segment_map[entry["id"]],
            entry["reference_target"],
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
) -> dict[int, str]:
    _validate_schema(artifact, ARTIFACT_SCHEMA, ARTIFACT_VERSION)
    _validate_self_digest(artifact, "artifact_digest", "final suggestion artifact")
    _validate_publisher_receipt(
        artifact, "lqe_suggestion_review.publish-final", "final suggestion artifact"
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
        )
        if entry["candidate_digest"] != canonical_digest({
            "id": entry["id"],
            "reference_target": entry["reference_target"],
        }):
            raise ValueError(f"final suggestion artifact id {entry['id']} digest is invalid")
    if candidate_artifact is not None:
        if artifact["candidate_artifact_digest"] != candidate_artifact["artifact_digest"]:
            raise ValueError("final suggestion artifact candidate digest is stale")
        candidate_map = {entry["id"]: entry for entry in candidate_artifact["entries"]}
        route_map = {
            route["id"]: route for route in candidate_artifact["routes"]
        }
        for entry in artifact["final_entries"]:
            if candidate_map.get(entry["id"]) != {
                "id": entry["id"],
                "reference_target": entry["reference_target"],
                "candidate_digest": entry["candidate_digest"],
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
            if review_artifact["candidate_artifact_digest"] != candidate_artifact["artifact_digest"]:
                raise ValueError("suggestion review artifact candidate digest is stale")
            live_review_packet = build_suggestion_review_packet(
                packet,
                candidate_artifact,
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
        expected_final_ids = [
            segment_id
            for segment_id in artifact["reviewed_ids"]
            if route_map[segment_id]["risk_route"] == DETERMINISTIC_ACCEPT
            or (
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
    packet, bundle_set, worker_manifest = _build_live_packet_context(
        Path(job), state, segments, manifest, results, selection
    )
    require_persisted_suggestion_context(
        Path(job), packet, bundle_set, worker_manifest
    )
    candidate_path = Path(job) / CANDIDATE_NAME
    if not candidate_path.is_file():
        raise ValueError("reference suggestion candidate artifact is missing")
    candidate = read_json(candidate_path)
    validate_candidate_artifact(candidate, packet, segments)
    review = None
    review_path = Path(job) / "suggestion_review.json"
    if review_path.is_file():
        review = read_json(review_path)
    return validate_suggestion_artifact(
        artifact,
        packet,
        segments,
        candidate_artifact=candidate,
        review_artifact=review,
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
    packet, bundle_set, worker_manifest = _build_live_packet_context(
        job,
        state,
        segments,
        manifest,
        results,
        selection,
    )
    output = Path(args.out) if args.out else job / PACKET_NAME
    context_dir = job / SUGGESTION_CONTEXT_DIR
    write_json_atomic(context_dir / SUGGESTION_BUNDLE_SET_NAME, bundle_set)
    write_json_atomic(
        context_dir / SUGGESTION_WORKER_MANIFEST_NAME,
        worker_manifest,
    )
    write_json_atomic(output, packet)
    print(
        f"[lqe_suggestions] Generation packet → {output} "
        f"({len(packet['segments'])} generation candidate(s), "
        f"{len(packet['excluded_segments'])} hard rejected)"
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
    draft = read_json(Path(args.input))
    selection = draft.get("selection") if isinstance(draft, dict) else None
    packet, bundle_set, worker_manifest = _build_live_packet_context(
        job, state, segments, manifest, results, selection
    )
    require_persisted_suggestion_context(
        job, packet, bundle_set, worker_manifest
    )
    artifact = build_candidate_artifact(packet, draft, segments)
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
    packet, bundle_set, worker_manifest = _build_live_packet_context(
        job, state, segments, manifest, results, artifact.get("selection")
    )
    require_persisted_suggestion_context(
        job, packet, bundle_set, worker_manifest
    )
    candidate = read_json(job / CANDIDATE_NAME)
    validate_candidate_artifact(candidate, packet, segments)
    review_path = job / "suggestion_review.json"
    review = read_json(review_path) if review_path.is_file() else None
    suggestions = validate_suggestion_artifact(
        artifact,
        packet,
        segments,
        candidate_artifact=candidate,
        review_artifact=review,
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
