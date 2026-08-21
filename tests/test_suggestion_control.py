import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lqe_suggestion_control import payload_digest, require_immutable_publication


class SuggestionControlTests(unittest.TestCase):
    def _authorization(self, path, previous, current, authorization_id="auth-1"):
        path.write_text(json.dumps({
            "schema": "lqe.suggestion-mutation-authorization",
            "version": 1,
            "authorization_id": authorization_id,
            "authorized_by": "user",
            "reason": "User approved this exact revision.",
            "action": "revise_suggestion_candidates",
            "job_id": "job-1",
            "previous_digest": payload_digest(previous),
            "current_digest": payload_digest(current),
            "results_basis_digest": "b" * 64,
        }), encoding="utf-8")

    def test_existing_publication_is_immutable_without_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            output = job / "candidate.json"
            output.write_text(json.dumps({"value": 1}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "authorization file is required"):
                require_immutable_publication(
                    job,
                    output,
                    {"value": 2},
                    None,
                    action="revise_suggestion_candidates",
                    job_id="job-1",
                    results_basis_digest="b" * 64,
                )

    def test_authorization_is_bound_and_consumed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            previous = {"value": 1}
            current = {"value": 2}
            output = job / "candidate.json"
            output.write_text(json.dumps(previous), encoding="utf-8")
            authorization = job / "authorization.json"
            self._authorization(authorization, previous, current)
            self.assertFalse(require_immutable_publication(
                job,
                output,
                current,
                authorization,
                action="revise_suggestion_candidates",
                job_id="job-1",
                results_basis_digest="b" * 64,
            ))
            with self.assertRaisesRegex(ValueError, "already consumed"):
                require_immutable_publication(
                    job,
                    output,
                    current,
                    authorization,
                    action="revise_suggestion_candidates",
                    job_id="job-1",
                    results_basis_digest="b" * 64,
                )

    def test_identical_publication_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            output = job / "candidate.json"
            payload = {"value": 1}
            output.write_text(json.dumps(payload), encoding="utf-8")
            self.assertTrue(require_immutable_publication(
                job,
                output,
                payload,
                None,
                action="revise_suggestion_candidates",
                job_id="job-1",
                results_basis_digest="b" * 64,
            ))


if __name__ == "__main__":
    unittest.main()
