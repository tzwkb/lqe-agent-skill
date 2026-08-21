from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lqe_language_policies import resolve_language_policy
from lqe_suggestion_review import build_review_packet, validate_review_draft
from lqe_suggestions import (
    DRAFT_SCHEMA,
    DRAFT_VERSION,
    PACKET_NAME,
    REVIEW_DRAFT_SCHEMA,
    REVIEW_DRAFT_VERSION,
    SUGGESTION_CONTEXT_DIR,
    _publisher_receipt,
    _load_suggestion_candidate_rules,
    _require_rebuild_authorization,
    _with_digest,
    build_candidate_artifact,
    build_suggestion_packet,
    validate_candidate_artifact,
)
from lqe_split_contract import canonical_digest


PROVIDER = {"id": "ko.register", "api_version": 1}


def source_semantics():
    return {
        "subjects": ["speaker"],
        "actions": ["command"],
        "objects": ["addressee"],
        "negation": {"present": False, "scope": None},
        "polarity": "affirmative",
        "modality": ["imperative"],
        "speech_act": "command",
        "text_function": "direct",
        "intensity": "strong",
        "omitted_source_elements": [],
        "unsupported_additions": [],
    }


def tone_decision():
    return {
        "register": "plain",
        "politeness": "non-honorific",
        "depends_on_dialogue_context": False,
        "evidence": [{"type": "source_form", "value": "imperative"}],
        "uncertainties": [],
    }


def register_constraint():
    rules = {
        "schema": "lqe.context-rules",
        "version": 1,
        "authority_rank": ["client"],
        "rules": [{
            "id": "dialogue.plain",
            "capability": "language.register",
            "provider": PROVIDER,
            "target_lang": "ko",
            "rule_status": "confirmed",
            "priority": 100,
            "authority": {"issuer": "client"},
            "valid_from": None,
            "valid_until": None,
            "when": {"speaker_id": ["speaker"]},
            "expect": {
                "politeness": ["plain"],
                "ending_families": ["hae", "haera"],
                "forbidden_families": ["haeyo", "hapsyo"],
            },
            "provenance": {"source_id": "confirmed-rule"},
        }],
    }
    return resolve_language_policy(
        rules,
        {"speaker_id": "speaker"},
        provider=PROVIDER,
        target_lang="ko",
        as_of="2026-08-14",
    )


def packet_and_draft(
    reference_target,
    *,
    corrected=None,
    source="跟我回家！",
    term_hits=None,
    kind=None,
    state=None,
):
    segments = [{
        "id": 0,
        "source": source,
        "target": "기존 번역.",
        "resolved_constraints": [register_constraint()],
        **({"term_hits": term_hits} if term_hits is not None else {}),
        **({"kind": kind} if kind is not None else {}),
    }]
    results = [{
        "id": 0,
        "errors": [{
            "category": "Mistranslation",
            "severity": "Major",
            "comment": "Rewrite from the source.",
            "needs_confirmation": False,
            "edit": None,
        }],
        "corrected": corrected,
    }]
    packet = build_suggestion_packet(
        segments,
        None,
        results,
        state=state,
        worker_context_manifest_digest="a" * 64,
    )
    draft = {
        "schema": DRAFT_SCHEMA,
        "version": DRAFT_VERSION,
        "packet_digest": packet["packet_digest"],
        "worker_context_manifest_digest": packet[
            "worker_context_manifest_digest"
        ],
        "worker_receipt": {
            "worker_id": "generation-worker",
            "run_id": "generation-run",
        },
        "selection": packet["selection"],
        "reviewed_ids": packet["reviewed_ids"],
        "entries": [{
            "id": 0,
            "reference_target": reference_target,
            "source_semantics": source_semantics(),
            "tone_decision": tone_decision(),
        }],
        "abstained_ids": [],
        "abstention_reasons": [],
    }
    return segments, packet, draft


class CandidateConstraintGateTests(unittest.TestCase):
    def test_changed_basis_requires_explicit_rebuild_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            old_packet = {
                "job_id": "job-1",
                "packet_digest": "a" * 64,
                "results_basis_digest": "b" * 64,
                "reviewed_ids": [1],
            }
            new_packet = {
                "job_id": "job-1",
                "packet_digest": "c" * 64,
                "results_basis_digest": "d" * 64,
                "reviewed_ids": [1, 2],
            }
            (job / PACKET_NAME).write_text(
                json.dumps(old_packet), encoding="utf-8"
            )
            draft = job / SUGGESTION_CONTEXT_DIR / "batches" / "batch_0001"
            draft.mkdir(parents=True)
            (draft / "generation.draft.json").write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "explicit user authorization"):
                _require_rebuild_authorization(job, new_packet, None)
            authorization = job / "authorization.json"
            authorization.write_text(json.dumps({
                "schema": "lqe.suggestion-mutation-authorization",
                "version": 1,
                "authorization_id": "authorization-1",
                "authorized_by": "user",
                "reason": "User approved rebuilding the stale chain.",
                "action": "rebuild_suggestion_chain",
                "job_id": "job-1",
                "previous_digest": canonical_digest(old_packet),
                "current_digest": canonical_digest(new_packet),
                "results_basis_digest": "d" * 64,
            }), encoding="utf-8")
            audit = _require_rebuild_authorization(
                job, new_packet, authorization
            )
            with self.assertRaisesRegex(ValueError, "already consumed"):
                _require_rebuild_authorization(job, new_packet, authorization)

        self.assertEqual(audit["previous_results_basis_digest"], "b" * 64)
        self.assertEqual(audit["current_results_basis_digest"], "d" * 64)

    def test_confirmed_term_occurrence_mismatch_is_hard_rejected(self):
        segments, packet, draft = packet_and_draft(
            "포함하지 않음",
            source="정령와 정령",
            term_hits=[{
                "source": "정령",
                "target": "진니",
                "confirmed": True,
                "protected": False,
            }],
        )
        artifact = build_candidate_artifact(
            packet, draft, segments, candidate_guard_version=1
        )

        route = artifact["routes"][0]
        self.assertEqual(route["risk_route"], "hard_reject")
        self.assertIn(
            "CONFIRMED_TERM_OCCURRENCE_MISMATCH", route["reason_codes"]
        )

    def test_confirmed_term_occurrences_must_match_source_count(self):
        segments, packet, draft = packet_and_draft(
            "진니와 진니",
            source="정령와 정령",
            term_hits=[{
                "source": "정령",
                "target": "진니",
                "confirmed": True,
                "protected": False,
            }],
        )
        artifact = build_candidate_artifact(
            packet, draft, segments, candidate_guard_version=1
        )

        self.assertEqual(
            artifact["routes"][0]["risk_route"], "independent_verifier"
        )

    def test_confirmed_term_requires_independent_sense_attestation_in_v2(self):
        segments, packet, draft = packet_and_draft(
            "진니",
            source="정령",
            state={"suggestion_guard_version": 2},
            term_hits=[{
                "source": "정령",
                "target": "진니",
                "confirmed": True,
                "protected": False,
            }],
        )
        artifact = build_candidate_artifact(
            packet, draft, segments, candidate_guard_version=2
        )

        assertions = artifact["routes"][0]["applicable_rule_assertions"]
        self.assertIn(
            "CONFIRMED_TERM_SENSE_CONFLICT",
            [assertion["reason_code"] for assertion in assertions],
        )

    def test_structured_max_length_rule_is_hard_rejected_before_review(self):
        with tempfile.TemporaryDirectory() as directory:
            checks = Path(directory) / "checks.json"
            checks.write_text(json.dumps({
                "suggestion_candidate_rules": [{
                    "id": "creature-name-max-12",
                    "type": "max_target_length",
                    "max_characters": 12,
                    "source_regex": "精灵$",
                    "segment_kinds": ["name"],
                    "reason_code": "CREATURE_NAME_LENGTH_EXCEEDED",
                }],
            }), encoding="utf-8")
            segments, packet, draft = packet_and_draft(
                "1234567890123",
                source="休息的精灵",
                kind="name",
                state={"checks_path": str(checks)},
            )
            artifact = build_candidate_artifact(
                packet,
                draft,
                segments,
                candidate_rules=_load_suggestion_candidate_rules(
                    {"checks_path": str(checks)}
                ),
                candidate_guard_version=1,
            )

        route = artifact["routes"][0]
        self.assertEqual(route["risk_route"], "hard_reject")
        self.assertIn("STRUCTURED_CANDIDATE_RULE_MISMATCH", route["reason_codes"])

    def test_structured_regex_rule_is_hard_rejected_before_review(self):
        with tempfile.TemporaryDirectory() as directory:
            checks = Path(directory) / "checks.json"
            checks.write_text(json.dumps({
                "suggestion_candidate_rules": [{
                    "id": "required-project-token",
                    "type": "required_target_regex",
                    "pattern": "REQUIRED",
                    "source_regex": "跟我回家",
                    "reason_code": "REQUIRED_TOKEN_MISSING",
                }],
            }), encoding="utf-8")
            state = {"checks_path": str(checks), "suggestion_guard_version": 2}
            segments, packet, draft = packet_and_draft("지금 확인해!", state=state)
            packet["segments"][0]["context_projection"] = {}
            packet["segments"][0]["context_bundle_digest"] = "c" * 64
            packet = _with_digest(packet, "packet_digest")
            draft["packet_digest"] = packet["packet_digest"]
            artifact = build_candidate_artifact(
                packet,
                draft,
                segments,
                candidate_rules=_load_suggestion_candidate_rules(state),
                candidate_guard_version=2,
            )

        self.assertEqual(artifact["routes"][0]["risk_route"], "hard_reject")
        self.assertIn(
            "STRUCTURED_CANDIDATE_RULE_MISMATCH",
            artifact["routes"][0]["reason_codes"],
        )

    def test_reviewer_must_attest_every_applicable_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            checks = Path(directory) / "checks.json"
            checks.write_text(json.dumps({
                "suggestion_candidate_rules": [{
                    "id": "identity-choice",
                    "type": "reviewer_assertion",
                    "instruction": "Verify the project identity is authorized.",
                    "source_regex": "跟我回家",
                    "reason_code": "IDENTITY_UNAUTHORIZED",
                }],
            }), encoding="utf-8")
            state = {"checks_path": str(checks), "suggestion_guard_version": 2}
            segments, packet, draft = packet_and_draft("지금 확인해!", state=state)
            packet["segments"][0]["context_projection"] = {}
            packet["segments"][0]["context_bundle_digest"] = "c" * 64
            packet = _with_digest(packet, "packet_digest")
            draft["packet_digest"] = packet["packet_digest"]
            artifact = build_candidate_artifact(
                packet,
                draft,
                segments,
                candidate_rules=_load_suggestion_candidate_rules(state),
                candidate_guard_version=2,
            )
            review_packet = build_review_packet(packet, artifact)
            checks_payload = {
                field: {"status": "pass", "evidence": f"{field} passed."}
                for field in (
                    "subjects", "actions", "objects", "polarity_negation",
                    "modality", "speech_act", "text_function", "intensity",
                    "omissions", "unsupported_additions", "tone",
                )
            }
            review_draft = {
                "schema": REVIEW_DRAFT_SCHEMA,
                "version": REVIEW_DRAFT_VERSION,
                "review_packet_digest": review_packet["packet_digest"],
                "worker_context_manifest_digest": review_packet[
                    "worker_context_manifest_digest"
                ],
                "worker_receipt": {
                    "worker_id": "review-worker",
                    "run_id": "review-run",
                },
                "reviewed_ids": [0],
                "verdicts": [{
                    "id": 0,
                    "candidate_digest": artifact["entries"][0]["candidate_digest"],
                    "decision": "accept",
                    "reason_codes": [],
                    "evidence": "Candidate checked.",
                    "semantic_verification": checks_payload,
                }],
            }
            with self.assertRaisesRegex(ValueError, "rule verifications are missing"):
                validate_review_draft(review_draft, review_packet)
            review_draft["verdicts"][0]["rule_verifications"] = [
                {
                    "rule_id": assertion["rule_id"],
                    "status": "pass",
                    "evidence": "The applicable rule is satisfied.",
                }
                for assertion in review_packet["entries"][0][
                    "applicable_rule_assertions"
                ]
            ]
            validate_review_draft(review_draft, review_packet)

    def test_confirmed_constraint_mismatch_is_hard_rejected(self):
        segments, packet, draft = packet_and_draft("돌아가요!")
        artifact = build_candidate_artifact(packet, draft, segments)

        self.assertEqual(len(artifact["entries"]), 1)
        route = artifact["routes"][0]
        self.assertEqual(route["risk_route"], "hard_reject")
        self.assertIn("CONFIRMED_CONSTRAINT_MISMATCH", route["reason_codes"])
        self.assertEqual(
            route["candidate_constraint_evaluations"][0]["status"],
            "mismatch",
        )
        self.assertEqual(build_review_packet(packet, artifact)["reviewed_ids"], [])

    def test_matching_candidate_still_requires_independent_review(self):
        segments, packet, draft = packet_and_draft("지금 확인해!")
        artifact = build_candidate_artifact(packet, draft, segments)

        route = artifact["routes"][0]
        self.assertEqual(route["risk_route"], "independent_verifier")
        self.assertEqual(
            route["candidate_constraint_evaluations"][0]["status"],
            "match",
        )
        review_entry = {
            **deepcopy(packet["segments"][0]),
            "reference_target": artifact["entries"][0]["reference_target"],
            "candidate_digest": artifact["entries"][0]["candidate_digest"],
            "candidate_constraint_evaluations": deepcopy(
                route["candidate_constraint_evaluations"]
            ),
        }
        self.assertEqual(
            review_entry["candidate_constraint_evaluations"][0]["status"],
            "match",
        )

    def test_inconclusive_constraint_cannot_take_deterministic_route(self):
        ambiguous = "그는 말했다. ‘확인해!’라고."
        segments, packet, draft = packet_and_draft(
            ambiguous,
            corrected=ambiguous,
        )
        artifact = build_candidate_artifact(packet, draft, segments)

        route = artifact["routes"][0]
        self.assertEqual(route["risk_route"], "independent_verifier")
        self.assertIn(
            "CONSTRAINT_REQUIRES_INDEPENDENT_REVIEW",
            route["reason_codes"],
        )
        self.assertEqual(
            route["candidate_constraint_evaluations"][0]["status"],
            "inconclusive",
        )

    def test_resigned_route_tampering_is_rejected(self):
        segments, packet, draft = packet_and_draft("돌아가요!")
        artifact = build_candidate_artifact(packet, draft, segments)
        changed = deepcopy(artifact)
        changed["routes"][0]["risk_route"] = "deterministic_accept"
        changed["publisher_receipt"] = _publisher_receipt(
            "lqe_suggestions.publish-candidates", changed
        )
        changed = _with_digest(changed, "artifact_digest")

        with self.assertRaisesRegex(ValueError, "risk route is invalid"):
            validate_candidate_artifact(changed, packet, segments)


if __name__ == "__main__":
    unittest.main()
