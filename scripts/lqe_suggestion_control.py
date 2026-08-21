"""Fail-closed authorization and immutable publication controls for suggestions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re

from lqe_engine import read_json
from lqe_split_contract import canonical_digest


AUTHORIZATION_SCHEMA = "lqe.suggestion-mutation-authorization"
AUTHORIZATION_VERSION = 1
CONSUMPTION_DIR = "suggestion_context/authorization_consumptions"
_AUTHORIZATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def payload_digest(payload: object) -> str:
    return canonical_digest(payload)


def _consume_once(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("suggestion mutation authorization was already consumed") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        # The marker deliberately remains fail-closed after an interrupted write.
        raise


def consume_mutation_authorization(
    job: Path,
    authorization_path: object,
    *,
    action: str,
    job_id: str,
    previous_digest: str,
    current_digest: str,
    results_basis_digest: str,
) -> dict:
    if not isinstance(authorization_path, (str, Path)):
        raise ValueError(
            "explicit user authorization file is required for suggestion mutation "
            f"(action={action}, job_id={job_id}, previous_digest={previous_digest}, "
            f"current_digest={current_digest}, "
            f"results_basis_digest={results_basis_digest})"
        )
    path = Path(authorization_path).resolve()
    if not path.is_file() or path.is_symlink():
        raise ValueError("suggestion mutation authorization must be a regular file")
    authorization = read_json(path)
    required = {
        "schema",
        "version",
        "authorization_id",
        "authorized_by",
        "reason",
        "action",
        "job_id",
        "previous_digest",
        "current_digest",
        "results_basis_digest",
    }
    if not isinstance(authorization, dict) or set(authorization) != required:
        raise ValueError("suggestion mutation authorization shape is invalid")
    authorization_id = authorization["authorization_id"]
    if (
        not isinstance(authorization_id, str)
        or _AUTHORIZATION_ID.fullmatch(authorization_id) is None
    ):
        raise ValueError("suggestion mutation authorization_id is invalid")
    if authorization["schema"] != AUTHORIZATION_SCHEMA or authorization[
        "version"
    ] != AUTHORIZATION_VERSION:
        raise ValueError("suggestion mutation authorization contract is unsupported")
    if authorization["authorized_by"] != "user":
        raise ValueError("suggestion mutation authorization must be user-authorized")
    if not isinstance(authorization["reason"], str) or not authorization[
        "reason"
    ].strip():
        raise ValueError("suggestion mutation authorization reason is empty")
    expected = {
        "action": action,
        "job_id": job_id,
        "previous_digest": previous_digest,
        "current_digest": current_digest,
        "results_basis_digest": results_basis_digest,
    }
    for key, value in expected.items():
        if authorization[key] != value:
            raise ValueError(f"suggestion mutation authorization {key} is stale")
    consumption = {
        "schema": "lqe.suggestion-mutation-authorization-consumption",
        "version": 1,
        "authorization": authorization,
        "authorization_digest": canonical_digest(authorization),
    }
    _consume_once(
        job / CONSUMPTION_DIR / f"{authorization_id}.json",
        consumption,
    )
    return consumption


def require_immutable_publication(
    job: Path,
    output: Path,
    payload: dict,
    authorization_path: object,
    *,
    action: str,
    job_id: str,
    results_basis_digest: str,
) -> bool:
    """Return True for an idempotent existing publication; authorize any revision."""

    if not output.is_file():
        return False
    previous = read_json(output)
    if previous == payload:
        return True
    consume_mutation_authorization(
        job,
        authorization_path,
        action=action,
        job_id=job_id,
        previous_digest=payload_digest(previous),
        current_digest=payload_digest(payload),
        results_basis_digest=results_basis_digest,
    )
    return False
