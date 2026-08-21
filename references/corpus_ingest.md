# Finalized corpus ingestion

Use this path only when the user explicitly authorizes publication to an external corpus. It never participates in LQE evaluation and never makes RAG evidence authoritative.

## Preconditions

- The job uses the current runtime contract and contains `.finalized`.
- `state.pending_recheck` is false.
- Authentication, when required, comes from an environment variable named with `--auth-env`; credentials never belong in the URL or receipt.
- `corpus_ingest_receipt.json` makes an identical completed request idempotent. A different or incomplete existing receipt fails closed.

Inspect the versioned payload without network mutation:

```bash
python3 scripts/lqe_io.py ingest-corpus \
  --state '<job>/state.json' \
  --aipe-url 'https://example.invalid/corpus/ingest' \
  --dry-run --payload-out '<job>/corpus_ingest_request.json'
```

Publish with bounded asynchronous concurrency:

```bash
python3 scripts/lqe_io.py ingest-corpus \
  --state '<job>/state.json' \
  --aipe-url 'https://service.example/corpus/ingest' \
  --auth-env 'AIPE_API_TOKEN' \
  --batch-size 500 --concurrency 4 --timeout 30 --retries 2
```

`--changed-only` publishes only targets that differ from the imported target. The default publishes all finalized segments.

## HTTP contract v1

Each POST body uses schema `lqe.corpus-ingest-request`, version `1`, one deterministic `request_id`, job metadata, batch metadata, and segment entries. The client sends `Idempotency-Key: <request_id>:<batch-index>:<batch-payload-digest>`, so a failed run can be retried safely even if the later batch plan changes.

The service must return HTTP 2xx and a JSON object:

```json
{
  "status": "accepted",
  "accepted": 500,
  "request_id": "optional matching request id"
}
```

`status` may be `accepted`, `ok`, or `success`. `accepted` must exactly equal the submitted batch size. HTTP 429, 5xx, and transport failures are retried within the configured limit; other failures stop without writing a completed receipt. Query parameters are never copied into the receipt. A SHA-256 digest binds the receipt to the full endpoint without exposing its query; an existing receipt for another endpoint fails closed.
