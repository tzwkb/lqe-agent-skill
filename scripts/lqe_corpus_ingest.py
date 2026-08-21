"""Versioned, fail-closed corpus ingestion for finalized LQE jobs."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import random
import re
import stat
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx

from lqe_engine import current_target, require_current_job_runtime
from lqe_paths import file_sha256, paths_alias, write_json_atomic


REQUEST_SCHEMA = "lqe.corpus-ingest-request"
RECEIPT_SCHEMA = "lqe.corpus-ingest-receipt"
CONTRACT_VERSION = 1
SUCCESS_STATUSES = {"accepted", "ok", "success"}


class CorpusIngestError(ValueError):
    pass


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CorpusIngestError(f"corpus payload is not canonical JSON: {exc}") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _endpoint(value: str) -> tuple[str, str]:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CorpusIngestError("--aipe-url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise CorpusIngestError("--aipe-url must not contain credentials")
    if parsed.fragment:
        raise CorpusIngestError("--aipe-url must not contain a fragment")
    safe = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return value, safe


def _require_regular_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise CorpusIngestError(f"{label} is missing or unreadable: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CorpusIngestError(f"{label} must be a regular non-symlink file: {path}")


def _job_metadata(state_path: Path, state: Mapping[str, object]) -> dict:
    source_lang = state.get("source_lang")
    target_lang = state.get("target_lang")
    language_pair = state.get("language_pair")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (source_lang, target_lang, language_pair)
    ):
        raise CorpusIngestError(
            "state must define source_lang, target_lang, and language_pair"
        )
    review_policy = state.get("review_policy")
    if review_policy is not None and not isinstance(review_policy, Mapping):
        raise CorpusIngestError("state.review_policy must be an object")
    xml_input_format = state.get("xml_input_format")
    if xml_input_format is not None and (
        not isinstance(xml_input_format, str) or not xml_input_format.strip()
    ):
        raise CorpusIngestError("state.xml_input_format must be a non-empty string")
    return {
        "job": state_path.parent.name,
        "project": state.get("project"),
        "language_pair": language_pair,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "review_mode": (review_policy or {}).get("mode"),
        "input_format": xml_input_format
        or state.get("input_format", "tabular"),
    }


def build_corpus_request(
    state_path: Path,
    state: Mapping[str, object],
    *,
    changed_only: bool = False,
) -> dict:
    require_current_job_runtime(dict(state), "ingest-corpus")
    finalized_path = state_path.parent / ".finalized"
    try:
        _require_regular_file(finalized_path, "finalization marker")
    except CorpusIngestError as exc:
        raise CorpusIngestError("job is not finalized with a regular marker") from exc
    if state.get("pending_recheck"):
        raise CorpusIngestError("job has pending recheck state")
    raw_segments = state.get("segments")
    if not isinstance(raw_segments, list):
        raise CorpusIngestError("state.segments must be an array")
    entries = []
    seen_ids = set()
    for index, raw in enumerate(raw_segments):
        if not isinstance(raw, dict) or type(raw.get("id")) is not int:
            raise CorpusIngestError(f"state.segments[{index}] has an invalid id")
        segment_id = raw["id"]
        if segment_id in seen_ids:
            raise CorpusIngestError(f"duplicate segment id {segment_id}")
        seen_ids.add(segment_id)
        source = raw.get("source")
        original_target = raw.get("target")
        if not isinstance(source, str) or not isinstance(original_target, str):
            raise CorpusIngestError(
                f"segment {segment_id} source and target must be strings"
            )
        target = current_target(raw)
        if not isinstance(target, str):
            raise CorpusIngestError(
                f"segment {segment_id} current target must be a string"
            )
        changed = target != original_target
        if changed_only and not changed:
            continue
        entry = {
            "id": segment_id,
            "segment_key": raw.get("segment_key"),
            "source": source,
            "target": target,
            "original_target": original_target,
            "changed": changed,
            "protected": bool(raw.get("protected")),
            "content_type": raw.get("content_type"),
            "source_ref": deepcopy(raw.get("source_ref")),
        }
        entry["digest"] = _digest(entry)
        entries.append(entry)
    if not entries:
        raise CorpusIngestError("no corpus segments match the requested scope")
    base = {
        "schema": REQUEST_SCHEMA,
        "version": CONTRACT_VERSION,
        "job": _job_metadata(state_path, state),
        "changed_only": changed_only,
        "segments": entries,
    }
    base["request_id"] = _digest(base)
    return base


def _batches(request: Mapping[str, object], batch_size: int) -> list[dict]:
    if type(batch_size) is not int or batch_size < 1 or batch_size > 5000:
        raise CorpusIngestError("--batch-size must be between 1 and 5000")
    segments = request["segments"]
    count = (len(segments) + batch_size - 1) // batch_size
    output = []
    for index in range(count):
        batch_segments = segments[index * batch_size : (index + 1) * batch_size]
        output.append(
            {
                "schema": request["schema"],
                "version": request["version"],
                "request_id": request["request_id"],
                "job": request["job"],
                "changed_only": request["changed_only"],
                "batch": {
                    "index": index,
                    "count": count,
                    "segment_count": len(batch_segments),
                },
                "segments": batch_segments,
            }
        )
    return output


def _auth_header(auth_env: str | None) -> dict[str, str]:
    if auth_env is None:
        return {}
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", auth_env):
        raise CorpusIngestError("--auth-env must be an environment variable name")
    token = os.environ.get(auth_env)
    if not token:
        raise CorpusIngestError(
            f"authentication environment variable is missing: {auth_env}"
        )
    return {"Authorization": f"Bearer {token}"}


async def _post_batch(
    client: httpx.AsyncClient,
    endpoint: str,
    payload: dict,
    *,
    retries: int,
) -> dict:
    batch_index = payload["batch"]["index"]
    idempotency_key = (
        f"{payload['request_id']}:{batch_index}:{_digest(payload)}"
    )
    for attempt in range(retries + 1):
        try:
            response = await client.post(
                endpoint,
                json=payload,
                headers={"Idempotency-Key": idempotency_key},
            )
        except httpx.RequestError as exc:
            if attempt >= retries:
                raise CorpusIngestError(
                    f"batch {batch_index} request failed: {exc}"
                ) from exc
        else:
            if 200 <= response.status_code < 300:
                try:
                    body = response.json()
                except ValueError as exc:
                    raise CorpusIngestError(
                        f"batch {batch_index} returned non-JSON success response"
                    ) from exc
                if not isinstance(body, dict):
                    raise CorpusIngestError(
                        f"batch {batch_index} response must be an object"
                    )
                status = str(body.get("status", "")).casefold()
                if status not in SUCCESS_STATUSES:
                    raise CorpusIngestError(
                        f"batch {batch_index} response status is not accepted: "
                        f"{body.get('status')!r}"
                    )
                accepted = body.get("accepted")
                expected = payload["batch"]["segment_count"]
                if type(accepted) is not int or accepted != expected:
                    raise CorpusIngestError(
                        f"batch {batch_index} accepted count mismatch: "
                        f"{accepted!r} != {expected}"
                    )
                response_request_id = body.get("request_id")
                if response_request_id not in {None, payload["request_id"]}:
                    raise CorpusIngestError(
                        f"batch {batch_index} response request_id mismatch"
                    )
                return {
                    "index": batch_index,
                    "accepted": accepted,
                    "status": status,
                    "response_digest": _digest(body),
                }
            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or attempt >= retries:
                raise CorpusIngestError(
                    f"batch {batch_index} HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )
        await asyncio.sleep((2**attempt) * 0.25 + random.random() * 0.1)
    raise CorpusIngestError(f"batch {batch_index} exhausted retries")


async def _publish_batches(
    endpoint: str,
    batches: list[dict],
    *,
    auth_env: str | None,
    concurrency: int,
    timeout: float,
    retries: int,
) -> list[dict]:
    if type(concurrency) is not int or concurrency < 1 or concurrency > 16:
        raise CorpusIngestError("--concurrency must be between 1 and 16")
    if not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > 300:
        raise CorpusIngestError("--timeout must be greater than 0 and at most 300")
    if type(retries) is not int or retries < 0 or retries > 8:
        raise CorpusIngestError("--retries must be between 0 and 8")
    semaphore = asyncio.Semaphore(concurrency)
    headers = {
        "Accept": "application/json",
        "User-Agent": "lqe-translator/corpus-ingest-v1",
        **_auth_header(auth_env),
    }

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        headers=headers,
        follow_redirects=False,
    ) as client:
        async def publish(payload: dict) -> dict:
            async with semaphore:
                return await _post_batch(
                    client, endpoint, payload, retries=retries
                )

        return await asyncio.gather(*(publish(payload) for payload in batches))


def ingest_corpus(
    *,
    state_path: Path,
    endpoint: str,
    auth_env: str | None = None,
    batch_size: int = 500,
    concurrency: int = 4,
    timeout: float = 30.0,
    retries: int = 2,
    changed_only: bool = False,
    dry_run: bool = False,
    payload_out: Path | None = None,
    receipt_path: Path | None = None,
) -> dict:
    state_path = Path(state_path).absolute()
    _require_regular_file(state_path, "state file")
    state_path = state_path.resolve()
    endpoint, safe_endpoint = _endpoint(endpoint)
    endpoint_digest = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
    state_digest = file_sha256(state_path)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusIngestError(f"cannot read state: {exc}") from exc
    if not isinstance(state, dict):
        raise CorpusIngestError("state must be an object")
    request = build_corpus_request(
        state_path, state, changed_only=changed_only
    )
    batches = _batches(request, batch_size)
    if payload_out is not None:
        payload_out = Path(payload_out).resolve()
        if paths_alias(payload_out, state_path):
            raise CorpusIngestError("--payload-out conflicts with --state")
        write_json_atomic(payload_out, request)
    if dry_run:
        return {
            "status": "dry-run",
            "request_id": request["request_id"],
            "segments": len(request["segments"]),
            "batches": len(batches),
            "payload_out": str(payload_out) if payload_out else None,
        }

    receipt_path = (
        Path(receipt_path).resolve()
        if receipt_path is not None
        else state_path.parent / "corpus_ingest_receipt.json"
    )
    if paths_alias(receipt_path, state_path):
        raise CorpusIngestError("receipt path conflicts with state")
    if os.path.lexists(receipt_path):
        _require_regular_file(receipt_path, "existing receipt")
        try:
            existing = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CorpusIngestError(f"cannot read existing receipt: {exc}") from exc
        if (
            isinstance(existing, dict)
            and existing.get("schema") == RECEIPT_SCHEMA
            and existing.get("request_id") == request["request_id"]
            and existing.get("endpoint_digest") == endpoint_digest
            and existing.get("status") == "complete"
        ):
            return existing
        raise CorpusIngestError(
            f"receipt already exists for a different or incomplete request: {receipt_path}"
        )

    results = asyncio.run(
        _publish_batches(
            endpoint,
            batches,
            auth_env=auth_env,
            concurrency=concurrency,
            timeout=timeout,
            retries=retries,
        )
    )
    if file_sha256(state_path) != state_digest:
        raise CorpusIngestError("state changed during corpus ingestion")
    results.sort(key=lambda item: item["index"])
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "version": CONTRACT_VERSION,
        "status": "complete",
        "request_id": request["request_id"],
        "endpoint": safe_endpoint,
        "endpoint_digest": endpoint_digest,
        "state_digest": state_digest,
        "segments": len(request["segments"]),
        "batches": results,
        "accepted": sum(item["accepted"] for item in results),
    }
    write_json_atomic(receipt_path, receipt)
    return receipt
