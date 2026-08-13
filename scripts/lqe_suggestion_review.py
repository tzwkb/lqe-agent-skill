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
    _build_live_packet_context,
    _suggestion_verifier_instruction_input,
    _load_live,
    _publisher_receipt,
    _validate_publisher_receipt,
    _validate_schema,
    _validate_self_digest,
    _with_digest,
    build_suggestion_review_packet,
    enforce_suggestion_worker_budget,
    require_persisted_suggestion_context,
    validate_candidate_artifact,
    validate_suggestion_artifact,
)
from lqe_split_contract import canonical_digest


REVIEW_PACKET_NAME = "suggestion_review.packet.json"
REVIEW_DRAFT_NAME = "suggestion_review.draft.json"
REVIEW_ARTIFACT_NAME = "suggestion_review.json"


def build_review_packet(
    generation_packet: dict,
    candidate_artifact: dict,
) -> dict:
    return build_suggestion_review_packet(generation_packet, candidate_artifact)


def validate_review_packet(
    packet: object,
    candidate_artifact: dict,
    generation_packet: dict | None = None,
) -> dict:
    _validate_schema(packet, REVIEW_PACKET_SCHEMA, REVIEW_PACKET_VERSION)
    _validate_self_digest(packet, "packet_digest", "suggestion review packet")
    if packet["candidate_artifact_digest"] != candidate_artifact["artifact_digest"]:
        raise ValueError("suggestion review packet candidate artifact is stale")
    expected_ids = [
        route["id"]
        for route in candidate_artifact["routes"]
        if route["risk_route"] == INDEPENDENT_VERIFIER
    ]
    if packet["reviewed_ids"] != expected_ids:
        raise ValueError("suggestion review packet reviewed ids are stale")
    if [entry.get("id") for entry in packet["entries"]] != expected_ids:
        raise ValueError("suggestion review packet entry coverage is invalid")
    verifier_instructions, _ = _suggestion_verifier_instruction_input()
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
        if packet[key] != candidate_artifact[key]:
            raise ValueError(f"suggestion review packet {key} is stale")
    if generation_packet is not None and packet != build_review_packet(
        generation_packet,
        candidate_artifact,
    ):
        raise ValueError("suggestion review packet differs from live generation evidence")
    return packet


def validate_review_draft(draft: object, packet: dict) -> dict:
    _validate_schema(draft, REVIEW_DRAFT_SCHEMA, REVIEW_DRAFT_VERSION)
    if draft["review_packet_digest"] != packet["packet_digest"]:
        raise ValueError("suggestion review draft is stale")
    if (
        draft["worker_context_manifest_digest"]
        != packet["worker_context_manifest_digest"]
    ):
        raise ValueError("suggestion review draft worker context is stale")
    if draft["reviewed_ids"] != packet["reviewed_ids"]:
        raise ValueError("suggestion review draft reviewed ids are stale")
    verdict_ids = [verdict["id"] for verdict in draft["verdicts"]]
    if verdict_ids != packet["reviewed_ids"] or len(verdict_ids) != len(set(verdict_ids)):
        raise ValueError("suggestion review draft verdict coverage is invalid")
    packet_entries = {entry["id"]: entry for entry in packet["entries"]}
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
    return draft


def build_review_artifact(packet: dict, draft: dict) -> dict:
    validate_review_draft(draft, packet)
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
        "review_draft_digest": canonical_digest(draft),
        "verdicts": copy.deepcopy(draft["verdicts"]),
    }
    payload["publisher_receipt"] = _publisher_receipt(
        "lqe_suggestion_review.publish-review", payload
    )
    payload = _with_digest(payload, "artifact_digest")
    _validate_schema(payload, REVIEW_ARTIFACT_SCHEMA, REVIEW_ARTIFACT_VERSION)
    validate_review_artifact(payload, packet)
    return payload


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
    for verdict in artifact["verdicts"]:
        if verdict["candidate_digest"] != packet_entries[verdict["id"]]["candidate_digest"]:
            raise ValueError("suggestion review artifact changed a candidate")
        if verdict["decision"] != "accept" and not verdict["reason_codes"]:
            raise ValueError("suggestion review artifact is missing reason codes")
        if not verdict["evidence"].strip():
            raise ValueError("suggestion review artifact evidence is empty")
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
            final_entries.append({
                **copy.deepcopy(candidate),
                "risk_route": risk_route,
            })
            continue
        verdict = verdict_map[segment_id]
        if verdict["decision"] == "accept":
            final_entries.append({
                **copy.deepcopy(candidate),
                "risk_route": risk_route,
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
        "final_entries": final_entries,
        "excluded_ids": excluded_ids,
    }
    payload["publisher_receipt"] = _publisher_receipt(
        "lqe_suggestion_review.publish-final", payload
    )
    payload = _with_digest(payload, "artifact_digest")
    _validate_schema(payload, ARTIFACT_SCHEMA, ARTIFACT_VERSION)
    return payload


def _load_live_chain(
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
    generation_packet, bundle_set, worker_manifest = _build_live_packet_context(
        job,
        state,
        segments,
        manifest,
        results,
        candidate.get("selection") if isinstance(candidate, dict) else None,
    )
    require_persisted_suggestion_context(
        job,
        generation_packet,
        bundle_set,
        worker_manifest,
    )
    validate_candidate_artifact(candidate, generation_packet, segments)
    review_packet = build_review_packet(generation_packet, candidate)
    validate_review_packet(review_packet, candidate, generation_packet)
    enforce_suggestion_worker_budget(
        worker_manifest,
        review_packet,
        label="suggestion verifier",
        additional_raw_bytes=review_packet["instructions"][
            "verifier_instructions"
        ]["bytes"],
    )
    return state, segments, generation_packet, candidate, review_packet


def cmd_prepare(args) -> None:
    job = Path(args.job).resolve()
    _, _, generation_packet, candidate, packet = _load_live_chain(
        job,
        args.state,
        args.errors,
        command="suggestion-review-prepare",
    )
    output = Path(args.out) if args.out else job / REVIEW_PACKET_NAME
    write_json_atomic(output, packet)
    print(
        f"[lqe_suggestion_review] Review packet → {output} "
        f"({len(packet['reviewed_ids'])} independent candidate(s))"
    )


def cmd_publish_review(args) -> None:
    job = Path(args.job).resolve()
    _, _, _, _, packet = _load_live_chain(
        job,
        args.state,
        args.errors,
        command="suggestion-review-publish-review",
    )
    draft_path = Path(args.input) if args.input else job / REVIEW_DRAFT_NAME
    draft = read_json(draft_path)
    artifact = build_review_artifact(packet, draft)
    output = Path(args.out) if args.out else job / REVIEW_ARTIFACT_NAME
    write_json_atomic(output, artifact)
    print(
        f"[lqe_suggestion_review] Review artifact → {output} "
        f"({len(artifact['verdicts'])} verdict(s))"
    )


def cmd_publish_final(args) -> None:
    job = Path(args.job).resolve()
    _, segments, generation_packet, candidate, review_packet = _load_live_chain(
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
    )
    output = Path(args.out) if args.out else job / ARTIFACT_NAME
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
            command.set_defaults(func=cmd_publish_review)
        else:
            command.add_argument("--review")
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
