#!/usr/bin/env python3
"""Independently review suggestion candidates and publish the v5 final artifact."""

from __future__ import annotations

import argparse
import copy
from pathlib import Path

from lqe_corrections import CheckFormatError
from lqe_engine import read_json, require_current_job_runtime
from lqe_paths import write_json_atomic
from lqe_suggestions import (
    ARTIFACT_NAME,
    ARTIFACT_SCHEMA,
    ARTIFACT_VERSION,
    CANDIDATE_NAME,
    DETERMINISTIC_ACCEPT,
    HARD_REJECT,
    INDEPENDENT_VERIFIER,
    REVIEW_ARTIFACT_SCHEMA,
    REVIEW_ARTIFACT_VERSION,
    REVIEW_DRAFT_SCHEMA,
    REVIEW_DRAFT_VERSION,
    REVIEW_PACKET_SCHEMA,
    REVIEW_PACKET_VERSION,
    SEMANTIC_CHECK_FIELDS,
    _build_live_packet_plan,
    _canonical_size,
    _suggestion_verifier_instruction_input,
    _load_live,
    _load_suggestion_candidate_rules,
    _suggestion_guard_version,
    _publisher_receipt,
    _review_packet_worker_receipts,
    _validate_publisher_receipt,
    _validate_schema,
    _validate_self_digest,
    _with_digest,
    _worker_receipt,
    build_suggestion_review_packet,
    measure_suggestion_worker_input,
    require_persisted_packet_plan,
    validate_candidate_artifact,
    validate_suggestion_artifact,
)
from lqe_split_contract import canonical_digest
from lqe_suggestion_control import require_immutable_publication
from lqe_target_form import load_target_form_policy


REVIEW_PACKET_NAME = "suggestion_review.packet.json"
REVIEW_DRAFT_NAME = "suggestion_review.draft.json"
REVIEW_ARTIFACT_NAME = "suggestion_review.json"
REVIEW_CONTEXT_DIR = "suggestion_review_context"
REVIEW_BATCH_PLAN_NAME = "batch_plan.json"
REVIEW_INPUT_MEASUREMENT_NAME = "input_measurement.json"


def build_review_packet(
    generation_packet: dict,
    candidate_artifact: dict,
    *,
    included_review_ids: set[int] | None = None,
) -> dict:
    return build_suggestion_review_packet(
        generation_packet,
        candidate_artifact,
        included_review_ids=included_review_ids,
    )


def validate_review_packet(
    packet: object,
    candidate_artifact: dict,
    generation_packet: dict | None = None,
) -> dict:
    _validate_schema(packet, REVIEW_PACKET_SCHEMA, REVIEW_PACKET_VERSION)
    _validate_self_digest(packet, "packet_digest", "suggestion review packet")
    if packet["candidate_artifact_digest"] != candidate_artifact["artifact_digest"]:
        raise ValueError("suggestion review packet candidate artifact is stale")
    expected_generation_receipts = candidate_artifact[
        "generation_worker_receipts"
    ]
    expected_checker_receipts = candidate_artifact.get(
        "checker_worker_receipts"
    )
    if generation_packet is not None:
        (
            expected_generation_receipts,
            expected_checker_receipts,
        ) = _review_packet_worker_receipts(
            generation_packet,
            candidate_artifact,
        )
    if packet["generation_worker_receipts"] != expected_generation_receipts:
        raise ValueError("suggestion review packet generation worker is stale")
    if "selected_evidence_index" in candidate_artifact:
        if (
            packet.get("selected_evidence_index")
            != candidate_artifact["selected_evidence_index"]
            or packet.get("checker_worker_receipts")
            != expected_checker_receipts
        ):
            raise ValueError("suggestion review packet selected evidence is stale")
        if generation_packet is not None and (
            packet["selected_evidence_index"]
            != generation_packet.get("selected_evidence_index")
            or packet["checker_worker_receipts"]
            != generation_packet.get("checker_worker_receipts")
        ):
            raise ValueError("suggestion review packet checker evidence differs")
    elif (
        packet.get("selected_evidence_index") is not None
        or packet.get("checker_worker_receipts") is not None
    ):
        raise ValueError("suggestion review packet has unexpected selected evidence")
    expected_ids = [
        route["id"]
        for route in candidate_artifact["routes"]
        if route["risk_route"] == INDEPENDENT_VERIFIER
    ]
    if generation_packet is not None:
        generation_ids = {entry["id"] for entry in generation_packet["segments"]}
        expected_ids = [
            segment_id for segment_id in expected_ids if segment_id in generation_ids
        ]
    if packet["reviewed_ids"] != expected_ids:
        raise ValueError("suggestion review packet reviewed ids are stale")
    if [entry.get("id") for entry in packet["entries"]] != expected_ids:
        raise ValueError("suggestion review packet entry coverage is invalid")
    guard_version = packet.get("instructions", {}).get(
        "candidate_guard_version", 0
    )
    verifier_instructions, _ = _suggestion_verifier_instruction_input(
        guard_version
    )
    if packet["instructions"].get("verifier_instructions") != verifier_instructions:
        raise ValueError("suggestion review packet verifier instructions are stale")
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
        expected = (
            generation_packet[key]
            if generation_packet is not None
            and key in {
                "context_bundle_set_digest",
                "worker_context_manifest_digest",
            }
            else candidate_artifact[key]
        )
        if packet[key] != expected:
            raise ValueError(f"suggestion review packet {key} is stale")
    if generation_packet is not None and packet != build_review_packet(
        generation_packet,
        candidate_artifact,
        included_review_ids=set(packet["reviewed_ids"]),
    ):
        raise ValueError("suggestion review packet differs from live generation evidence")
    return packet


def _validate_rule_verifications(verdict: dict, entry: dict, required: bool) -> None:
    if not required:
        return
    assertions = entry.get("applicable_rule_assertions", [])
    verifications = verdict.get("rule_verifications")
    if not isinstance(verifications, list):
        raise ValueError(
            f"suggestion review id {verdict['id']} rule verifications are missing"
        )
    expected_rule_ids = [item["rule_id"] for item in assertions]
    actual_rule_ids = [item.get("rule_id") for item in verifications]
    if actual_rule_ids != expected_rule_ids:
        raise ValueError(
            f"suggestion review id {verdict['id']} rule coverage is incomplete"
        )
    if verdict["decision"] == "accept" and any(
        item.get("status") != "pass" for item in verifications
    ):
        raise ValueError(
            f"suggestion review id {verdict['id']} cannot accept without passing "
            "every applicable project rule"
        )


def validate_review_draft(draft: object, packet: dict) -> dict:
    _validate_schema(draft, REVIEW_DRAFT_SCHEMA, REVIEW_DRAFT_VERSION)
    receipt = _worker_receipt(
        draft["worker_receipt"],
        label="suggestion review worker receipt",
    )
    generation_receipts = [
        _worker_receipt(item, label="generation worker receipt")
        for item in packet["generation_worker_receipts"]
    ]
    if any(
        receipt["worker_id"] == item["worker_id"]
        for item in generation_receipts
    ):
        raise ValueError("suggestion review worker_id must differ from generation")
    if any(receipt["run_id"] == item["run_id"] for item in generation_receipts):
        raise ValueError("suggestion review run_id must differ from generation")
    for checker in packet.get("checker_worker_receipts", []):
        checker = _worker_receipt(checker, label="checker worker receipt")
        if (
            receipt["worker_id"] == checker["worker_id"]
            or receipt["run_id"] == checker["run_id"]
        ):
            raise ValueError("suggestion review worker must differ from checker workers")
    if draft["review_packet_digest"] != packet["packet_digest"]:
        raise ValueError("suggestion review draft is stale")
    if (
        draft["worker_context_manifest_digest"]
        != packet["worker_context_manifest_digest"]
    ):
        raise ValueError("suggestion review draft worker context is stale")
    selected_binding = packet.get("selected_evidence_index")
    if selected_binding is not None:
        if draft.get("selected_evidence_index_digest") != selected_binding["digest"]:
            raise ValueError("suggestion review draft selected evidence is stale")
    elif draft.get("selected_evidence_index_digest") is not None:
        raise ValueError("suggestion review draft has unexpected selected evidence")
    if draft["reviewed_ids"] != packet["reviewed_ids"]:
        raise ValueError("suggestion review draft reviewed ids are stale")
    verdict_ids = [verdict["id"] for verdict in draft["verdicts"]]
    if verdict_ids != packet["reviewed_ids"] or len(verdict_ids) != len(set(verdict_ids)):
        raise ValueError("suggestion review draft verdict coverage is invalid")
    packet_entries = {entry["id"]: entry for entry in packet["entries"]}
    rule_attestation_required = packet.get("instructions", {}).get(
        "rule_attestation_required", False
    )
    for verdict in draft["verdicts"]:
        entry = packet_entries[verdict["id"]]
        if verdict["candidate_digest"] != entry["candidate_digest"]:
            raise ValueError(
                f"suggestion review draft id {verdict['id']} candidate is stale"
            )
        if verdict["decision"] != "accept" and not verdict["reason_codes"]:
            raise ValueError(
                f"suggestion review draft id {verdict['id']} needs reason codes"
            )
        if not verdict["evidence"].strip():
            raise ValueError(
                f"suggestion review draft id {verdict['id']} evidence is empty"
            )
        checks = verdict["semantic_verification"]
        if set(checks) != set(SEMANTIC_CHECK_FIELDS):
            raise ValueError(
                f"suggestion review draft id {verdict['id']} semantic checks are incomplete"
            )
        if verdict["decision"] == "accept" and any(
            checks[field]["status"] != "pass"
            for field in SEMANTIC_CHECK_FIELDS
        ):
            raise ValueError(
                f"suggestion review draft id {verdict['id']} cannot accept "
                "without passing every semantic and tone check"
            )
        _validate_rule_verifications(verdict, entry, rule_attestation_required)
    return draft


def _build_review_artifact_payload(
    packet: dict,
    verdicts: list[dict],
    review_worker_receipts: list[dict],
    review_draft_digest: str,
    *,
    review_batches: list[dict] | None = None,
) -> dict:
    payload = {
        "schema": REVIEW_ARTIFACT_SCHEMA,
        "version": REVIEW_ARTIFACT_VERSION,
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
                "candidate_artifact_digest",
            )
        },
        "review_packet_digest": packet["packet_digest"],
        "review_draft_digest": review_draft_digest,
        "generation_worker_receipts": copy.deepcopy(
            packet["generation_worker_receipts"]
        ),
        "review_worker_receipts": copy.deepcopy(review_worker_receipts),
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
        **(
            {"review_batches": copy.deepcopy(review_batches)}
            if review_batches is not None
            else {}
        ),
        "verdicts": copy.deepcopy(verdicts),
    }
    payload["publisher_receipt"] = _publisher_receipt(
        "lqe_suggestion_review.publish-review", payload
    )
    payload = _with_digest(payload, "artifact_digest")
    _validate_schema(payload, REVIEW_ARTIFACT_SCHEMA, REVIEW_ARTIFACT_VERSION)
    validate_review_artifact(payload, packet)
    return payload


def build_review_artifact(packet: dict, draft: dict) -> dict:
    validate_review_draft(draft, packet)
    return _build_review_artifact_payload(
        packet,
        draft["verdicts"],
        [draft["worker_receipt"]],
        canonical_digest(draft),
    )


def build_review_artifact_from_batches(
    root_packet: dict,
    review_batches: list[dict],
    drafts: list[dict],
) -> dict:
    if len(review_batches) != len(drafts) or not review_batches:
        raise ValueError("suggestion review batch draft count is invalid")
    receipts = []
    verdicts = []
    evidence = []
    for batch, draft in zip(review_batches, drafts):
        validate_review_draft(draft, batch["packet"])
        receipt = _worker_receipt(
            draft["worker_receipt"],
            label=f"review {batch['batch_id']} worker receipt",
        )
        if any(
            receipt["worker_id"] == prior["worker_id"]
            or receipt["run_id"] == prior["run_id"]
            for prior in receipts
        ):
            raise ValueError("suggestion review batches must use fresh workers")
        receipts.append(receipt)
        verdicts.extend(copy.deepcopy(draft["verdicts"]))
        evidence.append({
            "batch_id": batch["batch_id"],
            "generation_batch_id": batch["generation_batch_id"],
            "reviewed_ids": copy.deepcopy(batch["packet"]["reviewed_ids"]),
            "packet_digest": batch["packet"]["packet_digest"],
            "worker_context_manifest_digest": batch["packet"][
                "worker_context_manifest_digest"
            ],
            "draft_digest": canonical_digest(draft),
            "worker_receipt": copy.deepcopy(receipt),
        })
    if [verdict["id"] for verdict in verdicts] != root_packet["reviewed_ids"]:
        raise ValueError("suggestion review batch verdict coverage is incomplete")
    return _build_review_artifact_payload(
        root_packet,
        verdicts,
        receipts,
        canonical_digest([canonical_digest(draft) for draft in drafts]),
        review_batches=evidence,
    )


def validate_review_artifact(artifact: object, packet: dict) -> dict:
    _validate_schema(artifact, REVIEW_ARTIFACT_SCHEMA, REVIEW_ARTIFACT_VERSION)
    _validate_self_digest(artifact, "artifact_digest", "suggestion review artifact")
    _validate_publisher_receipt(
        artifact,
        "lqe_suggestion_review.publish-review",
        "suggestion review artifact",
    )
    if artifact["review_packet_digest"] != packet["packet_digest"]:
        raise ValueError("suggestion review artifact packet is stale")
    if artifact["candidate_artifact_digest"] != packet["candidate_artifact_digest"]:
        raise ValueError("suggestion review artifact candidate is stale")
    if artifact["generation_worker_receipts"] != packet[
        "generation_worker_receipts"
    ]:
        raise ValueError("suggestion review artifact generation worker is stale")
    if "selected_evidence_index" in packet:
        if (
            artifact.get("selected_evidence_index")
            != packet["selected_evidence_index"]
            or artifact.get("checker_worker_receipts")
            != packet["checker_worker_receipts"]
        ):
            raise ValueError("suggestion review artifact selected evidence is stale")
    elif (
        artifact.get("selected_evidence_index") is not None
        or artifact.get("checker_worker_receipts") is not None
    ):
        raise ValueError("suggestion review artifact has unexpected selected evidence")
    generation_receipts = [
        _worker_receipt(item, label="generation worker receipt")
        for item in artifact["generation_worker_receipts"]
    ]
    review_receipts = [
        _worker_receipt(item, label="suggestion review artifact worker receipt")
        for item in artifact["review_worker_receipts"]
    ]
    if not review_receipts:
        raise ValueError("suggestion review artifact worker receipt is missing")
    if (
        len({item["worker_id"] for item in review_receipts})
        != len(review_receipts)
        or len({item["run_id"] for item in review_receipts})
        != len(review_receipts)
    ):
        raise ValueError("suggestion review artifact workers are not unique")
    for generation in generation_receipts:
        for review in review_receipts:
            if generation["worker_id"] == review["worker_id"] or generation[
                "run_id"
            ] == review["run_id"]:
                raise ValueError("suggestion review artifact worker is not independent")
    checker_receipts = [
        _worker_receipt(item, label="checker worker receipt")
        for item in artifact.get("checker_worker_receipts", [])
    ]
    for checker in checker_receipts:
        for worker in [*generation_receipts, *review_receipts]:
            if (
                checker["worker_id"] == worker["worker_id"]
                or checker["run_id"] == worker["run_id"]
            ):
                raise ValueError("suggestion worker overlaps a checker worker")
    review_batches = artifact.get("review_batches")
    if "batch_plan" in packet:
        if not isinstance(review_batches, list) or not review_batches:
            raise ValueError("suggestion review batch evidence is missing")
        if [item.get("worker_receipt") for item in review_batches] != review_receipts:
            raise ValueError("suggestion review batch worker receipts are stale")
        covered = [
            segment_id
            for item in review_batches
            for segment_id in item.get("reviewed_ids", [])
        ]
        if covered != packet["reviewed_ids"]:
            raise ValueError("suggestion review batch evidence coverage is stale")
    elif review_batches is not None:
        raise ValueError("single-batch review has unexpected batch evidence")
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
        if artifact[key] != packet[key]:
            raise ValueError(f"suggestion review artifact {key} is stale")
    if artifact["reviewed_ids"] != packet["reviewed_ids"]:
        raise ValueError("suggestion review artifact reviewed ids are stale")
    verdict_ids = [verdict["id"] for verdict in artifact["verdicts"]]
    if verdict_ids != packet["reviewed_ids"]:
        raise ValueError("suggestion review artifact verdict coverage is invalid")
    packet_entries = {entry["id"]: entry for entry in packet["entries"]}
    rule_attestation_required = packet.get("instructions", {}).get(
        "rule_attestation_required", False
    )
    for verdict in artifact["verdicts"]:
        packet_entry = packet_entries[verdict["id"]]
        if verdict["candidate_digest"] != packet_entry["candidate_digest"]:
            raise ValueError("suggestion review artifact changed a candidate")
        if verdict["decision"] != "accept" and not verdict["reason_codes"]:
            raise ValueError("suggestion review artifact is missing reason codes")
        if not verdict["evidence"].strip():
            raise ValueError("suggestion review artifact evidence is empty")
        if verdict["decision"] == "accept" and any(
            verdict["semantic_verification"][field]["status"] != "pass"
            for field in SEMANTIC_CHECK_FIELDS
        ):
            raise ValueError(
                "suggestion review artifact accepted an unverified semantic field"
            )
        _validate_rule_verifications(
            verdict, packet_entry, rule_attestation_required
        )
    return artifact


def build_final_artifact(
    generation_packet: dict,
    candidate_artifact: dict,
    review_artifact: dict | None,
) -> dict:
    if candidate_artifact["generation_packet_digest"] != generation_packet["packet_digest"]:
        raise ValueError("candidate artifact generation packet is stale")
    route_map = {route["id"]: route for route in candidate_artifact["routes"]}
    candidate_map = {
        entry["id"]: entry for entry in candidate_artifact["entries"]
    }
    independent_ids = [
        segment_id
        for segment_id in candidate_artifact["reviewed_ids"]
        if route_map[segment_id]["risk_route"] == INDEPENDENT_VERIFIER
    ]
    if independent_ids and review_artifact is None:
        raise ValueError("independent suggestion review artifact is required")
    verdict_map = (
        {verdict["id"]: verdict for verdict in review_artifact["verdicts"]}
        if review_artifact is not None
        else {}
    )
    final_entries = []
    excluded_ids = []
    for segment_id in candidate_artifact["reviewed_ids"]:
        route = route_map[segment_id]
        risk_route = route["risk_route"]
        if risk_route == HARD_REJECT:
            excluded_ids.append({
                "id": segment_id,
                "reason_codes": copy.deepcopy(route["reason_codes"]),
            })
            continue
        candidate = candidate_map[segment_id]
        if risk_route == DETERMINISTIC_ACCEPT:
            raise ValueError(
                "all generated suggestion candidates require independent review"
            )
        verdict = verdict_map[segment_id]
        if verdict["decision"] == "accept":
            final_entries.append({
                **copy.deepcopy(candidate),
                "risk_route": risk_route,
                "semantic_verification": copy.deepcopy(
                    verdict["semantic_verification"]
                ),
            })
        else:
            reason_codes = verdict["reason_codes"] or [
                f"VERIFIER_{verdict['decision'].upper()}"
            ]
            excluded_ids.append({
                "id": segment_id,
                "reason_codes": copy.deepcopy(reason_codes),
            })
    payload = {
        "schema": ARTIFACT_SCHEMA,
        "version": ARTIFACT_VERSION,
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
                "reviewed_ids",
                "selection",
            )
        },
        "candidate_artifact_digest": candidate_artifact["artifact_digest"],
        "review_artifact_digest": (
            review_artifact["artifact_digest"] if review_artifact is not None else None
        ),
        "generation_worker_receipts": copy.deepcopy(
            candidate_artifact["generation_worker_receipts"]
        ),
        "review_worker_receipts": (
            copy.deepcopy(review_artifact["review_worker_receipts"])
            if review_artifact is not None
            else []
        ),
        **(
            {
                "selected_evidence_index": copy.deepcopy(
                    candidate_artifact["selected_evidence_index"]
                ),
                "checker_worker_receipts": copy.deepcopy(
                    candidate_artifact["checker_worker_receipts"]
                ),
            }
            if "selected_evidence_index" in candidate_artifact
            else {}
        ),
        "final_entries": final_entries,
        "excluded_ids": excluded_ids,
    }
    payload["publisher_receipt"] = _publisher_receipt(
        "lqe_suggestion_review.publish-final", payload
    )
    payload = _with_digest(payload, "artifact_digest")
    _validate_schema(payload, ARTIFACT_SCHEMA, ARTIFACT_VERSION)
    return payload


def _review_packet_input_bytes(
    job: Path,
    generation_batch: dict,
    packet: dict,
) -> int:
    content_path = packet.get("instructions", {}).get("worker_context", {}).get(
        "content_index_path"
    )
    if not isinstance(content_path, str):
        raise ValueError("suggestion verifier content index binding is missing")
    relative = Path(content_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("suggestion verifier content index path is unsafe")
    content_index = read_json(job / relative)
    guard_version = packet.get("instructions", {}).get(
        "candidate_guard_version", 0
    )
    verifier_summary, verifier_instructions = _suggestion_verifier_instruction_input(
        guard_version
    )
    if verifier_summary != packet["instructions"]["verifier_instructions"]:
        raise ValueError("suggestion verifier instructions are stale")
    return measure_suggestion_worker_input(
        generation_batch["worker_manifest"],
        generation_batch["bundle_set"],
        packet,
        label="suggestion verifier",
        additional_inputs=[content_index, verifier_instructions],
    )


def _build_review_packet_plan(
    job: Path,
    generation_plan: dict,
    candidate: dict,
) -> dict:
    independent_ids = [
        route["id"]
        for route in candidate["routes"]
        if route["risk_route"] == INDEPENDENT_VERIFIER
    ]
    review_batches = []
    batch_records = []
    for generation_batch in generation_plan["batches"]:
        generation_ids = {
            segment["id"] for segment in generation_batch["packet"]["segments"]
        }
        pending = [
            segment_id
            for segment_id in independent_ids
            if segment_id in generation_ids
        ]
        if not pending:
            continue
        batch_id = f"batch_{len(review_batches) + 1:04d}"
        packet = build_review_packet(
            generation_batch["packet"],
            candidate,
            included_review_ids=set(pending),
        )
        measured = _review_packet_input_bytes(job, generation_batch, packet)
        review_batches.append({
            "batch_id": batch_id,
            "generation_batch_id": generation_batch["batch_id"],
            "packet": packet,
            "generation_batch": generation_batch,
            "worker_input_bytes": measured,
        })
        batch_records.append({
            "batch_id": batch_id,
            "generation_batch_id": generation_batch["batch_id"],
            "reviewed_ids": copy.deepcopy(pending),
            "packet_path": f"{REVIEW_CONTEXT_DIR}/{batch_id}/packet.json",
            "packet_digest": packet["packet_digest"],
            "draft_path": f"{REVIEW_CONTEXT_DIR}/{batch_id}/review.draft.json",
            "worker_input_bytes": measured,
        })

    covered = [
        segment_id
        for record in batch_records
        for segment_id in record["reviewed_ids"]
    ]
    if covered != independent_ids:
        raise ValueError("suggestion review batch coverage is incomplete")
    if len(review_batches) == 1 and generation_plan["mode"] == "single":
        return {
            "mode": "single",
            "root_packet": review_batches[0]["packet"],
            "batches": review_batches,
            "batch_plan": None,
        }
    if not review_batches:
        root_packet = build_review_packet(
            generation_plan["root_packet"],
            candidate,
            included_review_ids=set(),
        )
        return {
            "mode": "single",
            "root_packet": root_packet,
            "batches": [],
            "batch_plan": None,
        }

    plan = {
        "schema": "lqe.suggestion-review-batch-plan",
        "version": 1,
        "candidate_artifact_digest": candidate["artifact_digest"],
        "reviewed_ids": independent_ids,
        "batches": batch_records,
    }
    plan["coverage_digest"] = canonical_digest({
        "reviewed_ids": independent_ids,
        "batch_ids": [record["reviewed_ids"] for record in batch_records],
    })
    plan["batch_plan_digest"] = canonical_digest(plan)

    entries = [
        copy.deepcopy(entry)
        for batch in review_batches
        for entry in batch["packet"]["entries"]
    ]
    root_generation = generation_plan["root_packet"]
    root_packet = {
        "schema": REVIEW_PACKET_SCHEMA,
        "version": REVIEW_PACKET_VERSION,
        **{
            key: copy.deepcopy(candidate[key])
            for key in (
                "job_id",
                "job_runtime_contract_version",
                "state_revision_digest",
                "results_basis_digest",
                "project_asset_snapshot_digest",
                "capability_resolution_digest",
                "protected_signature_digest",
                "created_at",
            )
        },
        "context_bundle_set_digest": root_generation[
            "context_bundle_set_digest"
        ],
        "worker_context_manifest_digest": root_generation[
            "worker_context_manifest_digest"
        ],
        "reviewed_ids": independent_ids,
        "candidate_artifact_digest": candidate["artifact_digest"],
        "generation_worker_receipts": copy.deepcopy(
            candidate["generation_worker_receipts"]
        ),
        **(
            {
                "selected_evidence_index": copy.deepcopy(
                    candidate["selected_evidence_index"]
                ),
                "checker_worker_receipts": copy.deepcopy(
                    candidate["checker_worker_receipts"]
                ),
            }
            if "selected_evidence_index" in candidate
            else {}
        ),
        "entries": entries,
        "batch_plan": {
            "path": f"{REVIEW_CONTEXT_DIR}/{REVIEW_BATCH_PLAN_NAME}",
            "digest": plan["batch_plan_digest"],
            "batch_count": len(review_batches),
        },
        "instructions": {
            "decision_values": ["accept", "reject", "human_required"],
            "candidate_text_is_read_only": True,
            "verify_source_meaning": True,
            "verify_all_known_issues": True,
            "verify_confirmed_constraints": True,
            "verifier_instructions": _suggestion_verifier_instruction_input(
                root_generation.get("instructions", {}).get(
                    "candidate_guard_version", 0
                )
            )[0],
            **(
                {
                    "candidate_guard_version": root_generation["instructions"][
                        "candidate_guard_version"
                    ],
                    "rule_attestation_required": True,
                }
                if root_generation.get("instructions", {}).get(
                    "candidate_guard_version", 0
                ) >= 2
                else {}
            ),
            "worker_context": {
                "input_measurement_path": (
                    f"{REVIEW_CONTEXT_DIR}/{REVIEW_INPUT_MEASUREMENT_NAME}"
                ),
                "batch_plan_path": f"{REVIEW_CONTEXT_DIR}/{REVIEW_BATCH_PLAN_NAME}",
                "batch_plan_digest": plan["batch_plan_digest"],
                "same_evidence_as_generation": True,
                "worker_reads_root_packet": False,
                **(
                    {
                        "checker_selected_evidence_index_path":
                            candidate["selected_evidence_index"]["path"],
                        "checker_selected_evidence_index_digest":
                            candidate["selected_evidence_index"]["digest"],
                        "checker_selected_evidence_is_embedded_per_segment": True,
                        "worker_reads_full_checker_index": False,
                    }
                    if "selected_evidence_index" in candidate
                    else {}
                ),
            },
        },
    }
    root_packet = _with_digest(root_packet, "packet_digest")
    _validate_schema(root_packet, REVIEW_PACKET_SCHEMA, REVIEW_PACKET_VERSION)
    return {
        "mode": "batched",
        "root_packet": root_packet,
        "batches": review_batches,
        "batch_plan": plan,
    }


def _review_input_measurement(review_plan: dict) -> dict:
    batches = [{
        "batch_id": batch["batch_id"],
        "generation_batch_id": batch["generation_batch_id"],
        "reviewed_ids": copy.deepcopy(batch["packet"]["reviewed_ids"]),
        "worker_input_bytes": batch["worker_input_bytes"],
    } for batch in review_plan["batches"]]
    values = [batch["worker_input_bytes"] for batch in batches]
    payload = {
        "schema": "lqe.suggestion-review-worker-input-measurement",
        "version": 1,
        "mode": "advisory",
        "decision_owner": "main_agent",
        "batches": batches,
        "total_worker_input_bytes": sum(values),
        "largest_worker_input_bytes": max(values, default=0),
    }
    payload["measurement_digest"] = canonical_digest(payload)
    return payload


def _write_review_packet_plan(job: Path, plan: dict) -> None:
    write_json_atomic(
        job / REVIEW_CONTEXT_DIR / REVIEW_INPUT_MEASUREMENT_NAME,
        _review_input_measurement(plan),
    )
    if plan["mode"] == "batched":
        write_json_atomic(
            job / REVIEW_CONTEXT_DIR / REVIEW_BATCH_PLAN_NAME,
            plan["batch_plan"],
        )
        for batch in plan["batches"]:
            write_json_atomic(
                job / REVIEW_CONTEXT_DIR / batch["batch_id"] / "packet.json",
                batch["packet"],
            )


def _require_persisted_review_packet_plan(job: Path, plan: dict) -> None:
    measurement_path = (
        job / REVIEW_CONTEXT_DIR / REVIEW_INPUT_MEASUREMENT_NAME
    )
    if (
        not measurement_path.is_file()
        or read_json(measurement_path) != _review_input_measurement(plan)
    ):
        raise ValueError("prepared suggestion review input measurement is stale")
    if plan["mode"] == "single":
        return
    plan_path = job / REVIEW_CONTEXT_DIR / REVIEW_BATCH_PLAN_NAME
    if not plan_path.is_file() or read_json(plan_path) != plan["batch_plan"]:
        raise ValueError("prepared suggestion review batch plan is stale")
    for batch in plan["batches"]:
        path = job / REVIEW_CONTEXT_DIR / batch["batch_id"] / "packet.json"
        if not path.is_file() or read_json(path) != batch["packet"]:
            raise ValueError(
                f"prepared suggestion review packet {batch['batch_id']} is stale"
            )


def _load_live_chain_plan(
    job: Path,
    state_name: str,
    errors_name: str,
    *,
    command: str,
):
    state, segments, manifest, results = _load_live(
        job, state_name=state_name, errors_name=errors_name
    )
    require_current_job_runtime(state, command)
    candidate = read_json(job / CANDIDATE_NAME)
    generation_plan = _build_live_packet_plan(
        job,
        state,
        segments,
        manifest,
        results,
        candidate.get("selection") if isinstance(candidate, dict) else None,
    )
    require_persisted_packet_plan(job, generation_plan)
    generation_packet = generation_plan["root_packet"]
    validate_candidate_artifact(
        candidate,
        generation_packet,
        segments,
        target_form_policy=load_target_form_policy(state),
        candidate_rules=_load_suggestion_candidate_rules(state),
        candidate_guard_version=_suggestion_guard_version(state),
    )
    review_plan = _build_review_packet_plan(job, generation_plan, candidate)
    return (
        state,
        segments,
        generation_packet,
        candidate,
        review_plan["root_packet"],
        generation_plan,
        review_plan,
    )


def _load_live_chain(
    job: Path,
    state_name: str,
    errors_name: str,
    *,
    command: str,
):
    chain = _load_live_chain_plan(
        job,
        state_name,
        errors_name,
        command=command,
    )
    return chain[:5]


def cmd_prepare(args) -> None:
    job = Path(args.job).resolve()
    (
        state,
        _,
        generation_packet,
        candidate,
        packet,
        _,
        review_plan,
    ) = _load_live_chain_plan(
        job,
        args.state,
        args.errors,
        command="suggestion-review-prepare",
    )
    output = Path(args.out) if args.out else job / REVIEW_PACKET_NAME
    _write_review_packet_plan(job, review_plan)
    write_json_atomic(output, packet)
    measurement = _review_input_measurement(review_plan)
    print(
        f"[lqe_suggestion_review] Review packet → {output} "
        f"({len(packet['reviewed_ids'])} independent candidate(s), "
        f"{len(review_plan['batches'])} worker batch(es), "
        f"largest measured {measurement['largest_worker_input_bytes']} bytes, "
        f"total {measurement['total_worker_input_bytes']} bytes; advisory)"
    )


def cmd_publish_review(args) -> None:
    job = Path(args.job).resolve()
    (
        _,
        _,
        _,
        _,
        packet,
        _,
        review_plan,
    ) = _load_live_chain_plan(
        job,
        args.state,
        args.errors,
        command="suggestion-review-publish-review",
    )
    _require_persisted_review_packet_plan(job, review_plan)
    if review_plan["mode"] == "single":
        draft_path = Path(args.input) if args.input else job / REVIEW_DRAFT_NAME
        draft = read_json(draft_path)
        artifact = build_review_artifact(packet, draft)
    else:
        input_path = Path(args.input) if args.input else job / REVIEW_CONTEXT_DIR
        if not input_path.is_dir():
            raise ValueError(
                "multiple suggestion review batches require a batch directory"
            )
        drafts = []
        for batch in review_plan["batches"]:
            draft_path = input_path / batch["batch_id"] / "review.draft.json"
            if not draft_path.is_file():
                raise ValueError(f"suggestion review draft is missing: {draft_path}")
            drafts.append(read_json(draft_path))
        artifact = build_review_artifact_from_batches(
            packet,
            review_plan["batches"],
            drafts,
        )
    output = Path(args.out) if args.out else job / REVIEW_ARTIFACT_NAME
    if require_immutable_publication(
        job,
        output,
        artifact,
        getattr(args, "authorization_file", None),
        action="revise_suggestion_review",
        job_id=artifact["job_id"],
        results_basis_digest=artifact["results_basis_digest"],
    ):
        print(f"[lqe_suggestion_review] Review artifact unchanged → {output}")
        return
    write_json_atomic(output, artifact)
    print(
        f"[lqe_suggestion_review] Review artifact → {output} "
        f"({len(artifact['verdicts'])} verdict(s))"
    )


def cmd_publish_final(args) -> None:
    job = Path(args.job).resolve()
    (
        state,
        segments,
        generation_packet,
        candidate,
        review_packet,
        _,
        review_plan,
    ) = _load_live_chain_plan(
        job,
        args.state,
        args.errors,
        command="suggestion-review-publish-final",
    )
    review = None
    if review_packet["reviewed_ids"]:
        review_path = Path(args.review) if args.review else job / REVIEW_ARTIFACT_NAME
        if not review_path.is_file():
            raise ValueError("independent suggestion review artifact is required")
        review = read_json(review_path)
        validate_review_artifact(review, review_packet)
    artifact = build_final_artifact(generation_packet, candidate, review)
    validate_suggestion_artifact(
        artifact,
        generation_packet,
        segments,
        candidate_artifact=candidate,
        review_artifact=review,
        review_packet=review_packet,
        target_form_policy=load_target_form_policy(state),
    )
    output = Path(args.out) if args.out else job / ARTIFACT_NAME
    if require_immutable_publication(
        job,
        output,
        artifact,
        getattr(args, "authorization_file", None),
        action="revise_final_suggestions",
        job_id=artifact["job_id"],
        results_basis_digest=artifact["results_basis_digest"],
    ):
        print(f"[lqe_suggestion_review] Final v5 unchanged → {output}")
        return
    write_json_atomic(output, artifact)
    print(
        f"[lqe_suggestion_review] Final v5 → {output} "
        f"({len(artifact['final_entries'])} final, "
        f"{len(artifact['excluded_ids'])} excluded)"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Independently review and publish reference suggestions."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "publish-review", "publish-final"):
        command = commands.add_parser(name)
        command.add_argument("--job", required=True)
        command.add_argument("--state", default="state.json")
        command.add_argument("--errors", default="errors.json")
        command.add_argument("--out")
        if name == "prepare":
            command.set_defaults(func=cmd_prepare)
        elif name == "publish-review":
            command.add_argument("--input")
            command.add_argument(
                "--authorization-file",
                help="one-time user authorization required to revise an artifact",
            )
            command.set_defaults(func=cmd_publish_review)
        else:
            command.add_argument("--review")
            command.add_argument(
                "--authorization-file",
                help="one-time user authorization required to revise an artifact",
            )
            command.set_defaults(func=cmd_publish_final)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except (CheckFormatError, OSError, ValueError) as exc:
        raise SystemExit(f"[lqe_suggestion_review] {exc}") from exc


if __name__ == "__main__":
    main()
