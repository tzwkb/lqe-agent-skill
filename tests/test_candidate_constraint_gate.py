from copy import deepcopy
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lqe_language_policies import resolve_language_policy
from lqe_suggestion_review import build_review_packet
from lqe_suggestions import (
    DRAFT_SCHEMA,
    DRAFT_VERSION,
    _publisher_receipt,
    _with_digest,
    build_candidate_artifact,
    build_suggestion_packet,
    validate_candidate_artifact,
)


PROVIDER = {"id": "ko.register", "api_version": 1}


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


def packet_and_draft(reference_target, *, corrected=None):
    segments = [{
        "id": 0,
        "source": "跟我回家！",
        "target": "기존 번역.",
        "resolved_constraints": [register_constraint()],
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
        worker_context_manifest_digest="a" * 64,
    )
    draft = {
        "schema": DRAFT_SCHEMA,
        "version": DRAFT_VERSION,
        "packet_digest": packet["packet_digest"],
        "worker_context_manifest_digest": packet[
            "worker_context_manifest_digest"
        ],
        "selection": packet["selection"],
        "reviewed_ids": packet["reviewed_ids"],
        "entries": [{"id": 0, "reference_target": reference_target}],
        "abstained_ids": [],
    }
    return segments, packet, draft


class CandidateConstraintGateTests(unittest.TestCase):
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
