"""Delivery receipt for native LQE report attempts."""

from __future__ import annotations

from datetime import datetime, timezone
import secrets
import json
from pathlib import Path

from lqe_paths import file_sha256, write_json_atomic
from lqe_split_contract import canonical_digest


SCHEMA = "lqe.report-delivery"
VERSION = 1
RECEIPT_NAME = "report_delivery.json"


def receipt_path(job_dir: Path) -> Path:
    return Path(job_dir) / RECEIPT_NAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def begin_report_attempt(job_dir: Path, state: dict) -> str:
    generation_id = secrets.token_hex(16)
    payload = {
        "schema": SCHEMA,
        "version": VERSION,
        "status": "writing",
        "generation_id": generation_id,
        "job_id": state.get("job_id") or "unbound",
        "started_at": _now(),
    }
    write_json_atomic(receipt_path(job_dir), payload)
    return generation_id


def complete_report_attempt(
    job_dir: Path,
    state: dict,
    results: list[dict],
    report_path: Path,
    generation_id: str,
) -> None:
    path = receipt_path(job_dir)
    if not path.is_file():
        raise ValueError("report delivery receipt disappeared during write")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("report delivery receipt is invalid") from exc
    if (
        receipt.get("generation_id") != generation_id
        or receipt.get("status") != "writing"
    ):
        raise ValueError("report delivery receipt was changed during write")
    report_path = Path(report_path).resolve()
    payload = {
        **receipt,
        "status": "written",
        "report_path": str(report_path),
        "report_sha256": file_sha256(report_path),
        "state_digest": canonical_digest(state),
        "results_digest": canonical_digest(results),
        "completed_at": _now(),
    }
    write_json_atomic(path, payload)


def validate_report_receipt(
    job_dir: Path,
    state: dict,
    results: list[dict],
    report_path: Path,
) -> dict:
    path = receipt_path(job_dir)
    if not path.is_file():
        raise ValueError(f"report delivery receipt is missing: {path}")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("report delivery receipt is invalid") from exc
    if receipt.get("schema") != SCHEMA or receipt.get("version") != VERSION:
        raise ValueError("report delivery receipt has an unsupported contract")
    if receipt.get("status") != "written":
        raise ValueError(
            f"report delivery is not complete (status={receipt.get('status')!r})"
        )
    report_path = Path(report_path).resolve()
    expected = {
        "job_id": state.get("job_id") or "unbound",
        "report_path": str(report_path),
        "state_digest": canonical_digest(state),
        "results_digest": canonical_digest(results),
        "report_sha256": file_sha256(report_path),
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise ValueError(
                f"report delivery receipt mismatch: {key}"
            )
    return receipt
