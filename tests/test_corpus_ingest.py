import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import httpx

from lqe_corpus_ingest import (
    CorpusIngestError,
    _batches,
    _post_batch,
    build_corpus_request,
    ingest_corpus,
)


class CorpusIngestTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.job = Path(self.tempdir.name) / "job"
        self.job.mkdir()
        self.state_path = self.job / "state.json"
        self.state = {
            "job_runtime_contract_version": 2,
            "language_pair": "en-fr",
            "source_lang": "en",
            "target_lang": "fr",
            "project": "demo/en-fr",
            "review_policy": {"mode": "full"},
            "input_format": "tabular",
            "pending_recheck": False,
            "segments": [
                {
                    "id": 0,
                    "segment_key": "greeting",
                    "source": "Hello",
                    "target": "Bonjour",
                    "current_target": "Salut",
                    "content_type": "Dialogue",
                },
                {
                    "id": 1,
                    "segment_key": "farewell",
                    "source": "Bye",
                    "target": "Au revoir",
                    "protected": True,
                },
            ],
        }
        self.state_path.write_text(
            json.dumps(self.state, ensure_ascii=False), encoding="utf-8"
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_requires_finalized_job(self):
        with self.assertRaisesRegex(CorpusIngestError, "not finalized"):
            ingest_corpus(
                state_path=self.state_path,
                endpoint="https://aipe.example/ingest",
                dry_run=True,
            )

    def test_rejects_symlink_finalization_marker(self):
        (self.job / ".finalized").symlink_to(self.state_path)
        with self.assertRaisesRegex(CorpusIngestError, "regular marker"):
            ingest_corpus(
                state_path=self.state_path,
                endpoint="https://aipe.example/ingest",
                dry_run=True,
            )

    def test_cli_dry_run_writes_versioned_payload(self):
        (self.job / ".finalized").touch()
        payload_path = self.job / "payload.json"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "lqe_io.py"),
                "ingest-corpus",
                "--state",
                str(self.state_path),
                "--aipe-url",
                "https://aipe.example/ingest?signature=secret",
                "--dry-run",
                "--payload-out",
                str(payload_path),
                "--changed-only",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "dry-run")
        self.assertEqual(summary["segments"], 1)
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], "lqe.corpus-ingest-request")
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["segments"][0]["target"], "Salut")
        self.assertNotIn("signature", json.dumps(summary))

    def test_publish_is_batched_and_idempotent(self):
        (self.job / ".finalized").touch()
        receipt_path = self.job / "receipt.json"

        async def accepted(_endpoint, batches, **_kwargs):
            return [
                {
                    "index": payload["batch"]["index"],
                    "accepted": payload["batch"]["segment_count"],
                    "status": "accepted",
                    "response_digest": "a" * 64,
                }
                for payload in batches
            ]

        with mock.patch(
            "lqe_corpus_ingest._publish_batches", side_effect=accepted
        ) as publish:
            first = ingest_corpus(
                state_path=self.state_path,
                endpoint="https://aipe.example/ingest?signature=secret",
                batch_size=1,
                receipt_path=receipt_path,
            )
            second = ingest_corpus(
                state_path=self.state_path,
                endpoint="https://aipe.example/ingest?signature=secret",
                batch_size=1,
                receipt_path=receipt_path,
            )

        self.assertEqual(publish.call_count, 1)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "complete")
        self.assertEqual(first["accepted"], 2)
        self.assertEqual(len(first["batches"]), 2)
        self.assertEqual(first["endpoint"], "https://aipe.example/ingest")
        self.assertRegex(first["endpoint_digest"], r"^[0-9a-f]{64}$")
        self.assertNotIn("secret", receipt_path.read_text(encoding="utf-8"))

    def test_existing_receipt_is_bound_to_full_endpoint(self):
        (self.job / ".finalized").touch()
        receipt_path = self.job / "receipt.json"

        async def accepted(_endpoint, batches, **_kwargs):
            return [
                {
                    "index": payload["batch"]["index"],
                    "accepted": payload["batch"]["segment_count"],
                    "status": "accepted",
                    "response_digest": "a" * 64,
                }
                for payload in batches
            ]

        with mock.patch(
            "lqe_corpus_ingest._publish_batches", side_effect=accepted
        ):
            ingest_corpus(
                state_path=self.state_path,
                endpoint="https://aipe.example/ingest?signature=one",
                receipt_path=receipt_path,
            )
            with self.assertRaisesRegex(CorpusIngestError, "different"):
                ingest_corpus(
                    state_path=self.state_path,
                    endpoint="https://aipe.example/ingest?signature=two",
                    receipt_path=receipt_path,
                )

    def test_batch_idempotency_key_binds_payload(self):
        (self.job / ".finalized").touch()
        request = build_corpus_request(self.state_path, self.state)
        payload = _batches(request, 1)[0]
        seen_headers = []

        def handler(request):
            seen_headers.append(dict(request.headers))
            return httpx.Response(
                200,
                json={
                    "status": "accepted",
                    "accepted": 1,
                    "request_id": payload["request_id"],
                },
            )

        async def run():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                return await _post_batch(
                    client,
                    "https://aipe.example/ingest",
                    payload,
                    retries=0,
                )

        result = asyncio.run(run())
        key = seen_headers[0]["idempotency-key"]
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(key.count(":"), 2)
        self.assertTrue(key.startswith(f"{payload['request_id']}:0:"))
        self.assertRegex(key.rsplit(":", 1)[1], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
