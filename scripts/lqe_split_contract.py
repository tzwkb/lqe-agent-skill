"""Content-bound revision contract and atomic generation switch for LQE chunks."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Callable

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX fallback
    msvcrt = None

from lqe_engine import current_target, get_review_policy


CONTRACT_VERSION = 1


class SplitContractError(ValueError):
    pass


@contextmanager
def generation_lock(active: Path, *, exclusive: bool):
    """Serialize generation swaps against verified readers."""
    active = Path(active)
    active.parent.mkdir(parents=True, exist_ok=True)
    lock_path = active.parent / f".{active.name}.lock"
    with lock_path.open("a+b") as handle:
        windows_locked = False
        if fcntl is not None:
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(handle.fileno(), operation)
        elif msvcrt is not None:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            windows_locked = True
        else:  # pragma: no cover - every supported platform has one backend
            raise SplitContractError(
                "generation locking is unsupported on this platform"
            )
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            elif windows_locked:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def canonical_digest(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SplitContractError(f"split contract value is not canonical JSON: {exc}") from exc
    return hashlib.sha256(encoded).hexdigest()


def _state_segment_payload(segment: dict) -> dict:
    if not isinstance(segment, dict):
        raise SplitContractError("state segment must be an object")
    payload = {
        key: deepcopy(value)
        for key, value in segment.items()
        if key
        not in {
            "target",
            "current_target",
            "corrected",
            "iter",
            "kind",
            "precheck",
            "term_hits",
            "term_near",
            "shadow_context",
        }
    }
    payload["target"] = current_target(segment)
    return payload


def _asset_snapshot(raw_path: object, field: str) -> dict:
    if not isinstance(raw_path, str):
        raise SplitContractError(f"state.{field} must be a string")
    snapshot = {"path": raw_path}
    if not raw_path.strip():
        snapshot["status"] = "unconfigured"
        return snapshot

    path = Path(raw_path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        snapshot["status"] = "missing"
        return snapshot
    except OSError as exc:
        raise SplitContractError(
            f"cannot inspect context asset state.{field}: {path}: {exc}"
        ) from exc

    if stat.S_ISLNK(info.st_mode):
        try:
            snapshot["link_target"] = os.readlink(path)
            target_info = path.stat()
        except FileNotFoundError:
            snapshot["status"] = "dangling-symlink"
            return snapshot
        except OSError as exc:
            raise SplitContractError(
                f"cannot inspect context asset state.{field}: {path}: {exc}"
            ) from exc
        if not stat.S_ISREG(target_info.st_mode):
            snapshot["status"] = "symlink-non-file"
            return snapshot
        snapshot["status"] = "symlink-file"
    elif stat.S_ISREG(info.st_mode):
        snapshot["status"] = "file"
    elif stat.S_ISDIR(info.st_mode):
        snapshot["status"] = "directory"
        return snapshot
    else:
        snapshot["status"] = "non-file"
        return snapshot

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise SplitContractError(
            f"cannot read context asset state.{field}: {path}: {exc}"
        ) from exc
    snapshot["sha256"] = digest.hexdigest()
    return snapshot


def state_revision_payload(state: dict) -> dict:
    if not isinstance(state, dict):
        raise SplitContractError("state must be an object")
    segments = state.get("segments")
    if not isinstance(segments, list):
        raise SplitContractError("state.segments must be an array")
    return {
        "artifact_contract_version": state.get("artifact_contract_version"),
        "job_runtime_contract_version": state.get("job_runtime_contract_version"),
        "context_contract_version": state.get("context_contract_version"),
        "input_guard_version": state.get("input_guard_version"),
        "iteration": state.get("iteration", 0),
        "source_lang": state.get("source_lang"),
        "target_lang": state.get("target_lang"),
        "wordcount": state.get("wordcount"),
        "review_wordcount": state.get("review_wordcount"),
        "review_wordcount_basis_digest": state.get(
            "review_wordcount_basis_digest"
        ),
        "check_scope": deepcopy(state.get("check_scope")),
        "review_policy": get_review_policy(state),
        "capability_resolution_digest": state.get(
            "capability_resolution_digest"
        ),
        "project_asset_snapshot_digest": state.get(
            "project_asset_snapshot_digest"
        ),
        "context_pipeline": deepcopy(state.get("context_pipeline")),
        "asset_paths": {
            key: _asset_snapshot(state.get(key), key)
            for key in (
                "terms_path",
                "sg_path",
                "checks_path",
                "confirmed_rules_path",
                "lang_notes_path",
                "background_path",
                "capability_resolution_path",
                "project_asset_snapshot_path",
                "tabular_source_manifest_path",
                "source_manifest_path",
                "project_source_manifest_path",
            )
            if state.get(key) is not None
        },
        "segments": [_state_segment_payload(segment) for segment in segments],
    }


def state_fingerprint(state: dict) -> str:
    return canonical_digest(state_revision_payload(state))


def build_split_revision(
    state: dict,
    precheck: list[dict],
    terms: list[dict],
    scope: dict,
    *,
    size: int,
    char_budget: int,
) -> dict:
    if type(size) is not int or size <= 0:
        raise SplitContractError("split size must be a positive integer")
    if type(char_budget) is not int or char_budget < 0:
        raise SplitContractError("split char budget must be a non-negative integer")
    if not isinstance(precheck, list):
        raise SplitContractError("precheck input must be an array")
    if not isinstance(terms, list):
        raise SplitContractError("terms input must be an array")
    if not isinstance(scope, dict):
        raise SplitContractError("resolved scope must be an object")

    revision = {
        "contract_version": CONTRACT_VERSION,
        "iteration": state.get("iteration", 0),
        "state_fingerprint": state_fingerprint(state),
        "precheck_digest": canonical_digest(precheck),
        "terms_digest": canonical_digest(terms),
        "scope_digest": canonical_digest(scope),
        "size": size,
        "char_budget": char_budget,
    }
    revision["split_fingerprint"] = canonical_digest(revision)
    return revision


def _without_digest(payload: dict, field: str) -> dict:
    return {key: value for key, value in payload.items() if key != field}


def add_chunk_payload_digest(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise SplitContractError("chunk payload must be an object")
    output = deepcopy(_without_digest(payload, "payload_digest"))
    output["payload_digest"] = canonical_digest(output)
    return output


def _chunk_name(chunk: dict) -> str:
    chunk_id = chunk.get("chunk_id")
    if type(chunk_id) is not int or chunk_id < 0:
        raise SplitContractError("chunk_id must be a non-negative integer")
    return f"chunk_{chunk_id:02d}.json"


def _manifest_digest(manifest: dict) -> str:
    return canonical_digest(_without_digest(manifest, "manifest_digest"))


def build_split_manifest(
    revision: dict,
    *,
    chunks: list[dict],
    dedup_map: dict,
    input_references: dict,
) -> dict:
    if not isinstance(revision, dict):
        raise SplitContractError("split revision must be an object")
    if revision.get("contract_version") != CONTRACT_VERSION:
        raise SplitContractError("unsupported split revision contract version")
    chunk_digests = {}
    for chunk in chunks:
        name = _chunk_name(chunk)
        digest = chunk.get("payload_digest")
        if not isinstance(digest, str) or not digest:
            raise SplitContractError(f"{name}: payload_digest is required")
        if digest != canonical_digest(_without_digest(chunk, "payload_digest")):
            raise SplitContractError(f"{name}: payload digest mismatch")
        if name in chunk_digests:
            raise SplitContractError(f"duplicate chunk id in manifest: {name}")
        chunk_digests[name] = digest

    manifest = {
        "contract_version": CONTRACT_VERSION,
        "iteration": revision.get("iteration", None),
        "state_fingerprint": revision["state_fingerprint"],
        "split_fingerprint": revision["split_fingerprint"],
        "revision": deepcopy(revision),
        "inputs": deepcopy(input_references),
        "chunks": len(chunks),
        "chunk_digests": chunk_digests,
        "dedup_map_digest": canonical_digest(dedup_map),
    }
    manifest["manifest_digest"] = _manifest_digest(manifest)
    return manifest


def validate_manifest_structure(manifest: object) -> dict:
    if not isinstance(manifest, dict):
        raise SplitContractError("split manifest is required")
    if manifest.get("contract_version") != CONTRACT_VERSION:
        raise SplitContractError("split manifest contract version is missing or unsupported")
    for field in (
        "state_fingerprint",
        "split_fingerprint",
        "manifest_digest",
        "dedup_map_digest",
    ):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            raise SplitContractError(f"split manifest {field} is required")
    if manifest["manifest_digest"] != _manifest_digest(manifest):
        raise SplitContractError("split manifest payload digest mismatch")
    revision = manifest.get("revision")
    if not isinstance(revision, dict):
        raise SplitContractError("split manifest revision is required")
    if revision.get("contract_version") != CONTRACT_VERSION:
        raise SplitContractError("split manifest revision version mismatch")
    if revision.get("state_fingerprint") != manifest["state_fingerprint"]:
        raise SplitContractError("split manifest state fingerprint mismatch")
    if revision.get("split_fingerprint") != manifest["split_fingerprint"]:
        raise SplitContractError("split manifest split fingerprint mismatch")
    chunks = manifest.get("chunks")
    chunk_digests = manifest.get("chunk_digests")
    if type(chunks) is not int or chunks < 0:
        raise SplitContractError("split manifest chunks must be a non-negative integer")
    if not isinstance(chunk_digests, dict) or len(chunk_digests) != chunks:
        raise SplitContractError("split manifest chunk digest coverage mismatch")
    expected_names = {f"chunk_{index:02d}.json" for index in range(chunks)}
    if set(chunk_digests) != expected_names:
        raise SplitContractError("split manifest chunk names are not contiguous")
    return manifest


def validate_live_manifest(
    manifest: object,
    state: dict,
    precheck: list[dict],
    terms: list[dict],
    scope: dict,
) -> dict:
    manifest = validate_manifest_structure(manifest)
    revision = manifest["revision"]
    try:
        expected = build_split_revision(
            state,
            precheck,
            terms,
            scope,
            size=revision["size"],
            char_budget=revision["char_budget"],
        )
    except KeyError as exc:
        raise SplitContractError(
            f"split manifest revision field is missing: {exc.args[0]}"
        ) from exc
    if expected != revision:
        differing = sorted(
            key for key in expected if expected.get(key) != revision.get(key)
        )
        raise SplitContractError(
            "stale split manifest; live inputs changed: " + ", ".join(differing)
        )
    return manifest


def validate_chunk_payload(manifest: object, chunk: object) -> dict:
    manifest = validate_manifest_structure(manifest)
    if not isinstance(chunk, dict):
        raise SplitContractError("chunk payload must be an object")
    name = _chunk_name(chunk)
    for field in ("state_fingerprint", "split_fingerprint", "payload_digest"):
        if not isinstance(chunk.get(field), str) or not chunk[field]:
            raise SplitContractError(f"{name}: {field} is required")
    actual_digest = canonical_digest(_without_digest(chunk, "payload_digest"))
    if chunk["payload_digest"] != actual_digest:
        raise SplitContractError(f"{name}: payload digest mismatch")
    expected_digest = manifest["chunk_digests"].get(name)
    if expected_digest != actual_digest:
        raise SplitContractError(f"{name}: payload digest differs from manifest")
    if chunk["state_fingerprint"] != manifest["state_fingerprint"]:
        raise SplitContractError(f"{name}: state fingerprint differs from manifest")
    if chunk["split_fingerprint"] != manifest["split_fingerprint"]:
        raise SplitContractError(f"{name}: split fingerprint differs from manifest")
    segments = chunk.get("segments")
    if not isinstance(segments, list):
        raise SplitContractError(f"{name}: segments must be an array")
    return chunk


def validate_dedup_payload(manifest: object, dedup_map: object) -> dict:
    manifest = validate_manifest_structure(manifest)
    if not isinstance(dedup_map, dict):
        raise SplitContractError("dedup_map.json must be an object")
    if canonical_digest(dedup_map) != manifest["dedup_map_digest"]:
        raise SplitContractError("dedup_map.json payload digest mismatch")
    return dedup_map


def validate_generation_payloads(
    manifest: object,
    chunks: list[dict],
    dedup_map: dict,
    state: dict,
) -> None:
    manifest = validate_manifest_structure(manifest)
    validate_dedup_payload(manifest, dedup_map)
    if len(chunks) != manifest["chunks"]:
        raise SplitContractError("chunk file coverage differs from manifest")

    representatives = set()
    for chunk in chunks:
        validate_chunk_payload(manifest, chunk)
        for segment in chunk["segments"]:
            if not isinstance(segment, dict) or type(segment.get("id")) is not int:
                raise SplitContractError("chunk segment id must be an integer")
            segment_id = segment["id"]
            if segment_id in representatives:
                raise SplitContractError(f"duplicate representative id {segment_id}")
            representatives.add(segment_id)

    dedup_representatives = set()
    members = []
    for raw_representative, raw_members in dedup_map.items():
        try:
            representative = int(raw_representative)
        except (TypeError, ValueError) as exc:
            raise SplitContractError("dedup representative id must be an integer") from exc
        if not isinstance(raw_members, list) or not all(
            type(member) is int for member in raw_members
        ):
            raise SplitContractError(
                f"dedup members for {representative} must be integer ids"
            )
        if representative not in raw_members:
            raise SplitContractError(
                f"dedup representative {representative} is not in its member list"
            )
        dedup_representatives.add(representative)
        members.extend(raw_members)
    if dedup_representatives != representatives:
        raise SplitContractError("dedup representatives differ from chunk segments")
    if len(members) != len(set(members)):
        raise SplitContractError("dedup member ids overlap")
    state_ids = [segment.get("id") for segment in state.get("segments", [])]
    if any(type(segment_id) is not int for segment_id in state_ids):
        raise SplitContractError("state segment id must be an integer")
    if set(members) != set(state_ids) or len(members) != len(state_ids):
        raise SplitContractError("dedup member coverage differs from state")


def make_path_reference(path: Path, job_root: Path) -> dict:
    resolved = Path(path).resolve()
    root = Path(job_root).resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return {"base": "absolute", "path": str(resolved)}
    return {"base": "job", "path": str(relative)}


def resolve_path_reference(reference: object, job_root: Path) -> Path:
    if not isinstance(reference, dict):
        raise SplitContractError("manifest input path reference must be an object")
    base = reference.get("base")
    raw_path = reference.get("path")
    if base not in {"job", "absolute"} or not isinstance(raw_path, str) or not raw_path:
        raise SplitContractError("manifest input path reference is invalid")
    path = Path(raw_path)
    if base == "job":
        if path.is_absolute() or ".." in path.parts:
            raise SplitContractError("job-relative manifest input escapes the job")
        return Path(job_root) / path
    if not path.is_absolute():
        raise SplitContractError("absolute manifest input path is not absolute")
    return path


def _archive_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._")
    return cleaned or "unknown"


def _publish_generation_unlocked(
    staging: Path,
    active: Path,
    *,
    archive_label: str,
) -> Path | None:
    staging = Path(staging)
    active = Path(active)
    if not staging.is_dir() or staging.is_symlink():
        raise SplitContractError(f"staged chunk generation is not a directory: {staging}")
    if staging.parent.resolve() != active.parent.resolve():
        raise SplitContractError("staged and active chunk directories must be siblings")
    if os.path.lexists(active) and (active.is_symlink() or not active.is_dir()):
        raise SplitContractError(f"active chunk path is not a directory: {active}")

    archived = None
    archive_root = active.parent / f"{active.name}_archive"
    try:
        if active.exists():
            if os.path.lexists(archive_root) and (
                archive_root.is_symlink() or not archive_root.is_dir()
            ):
                raise SplitContractError(
                    f"chunk archive path is not a directory: {archive_root}"
                )
            archive_root.mkdir(parents=True, exist_ok=True)
            stem = _archive_name(archive_label)
            archived = archive_root / stem
            suffix = 1
            while os.path.lexists(archived):
                archived = archive_root / f"{stem}_{suffix}"
                suffix += 1
            active.replace(archived)
        staging.replace(active)
    except BaseException:
        if archived is not None and archived.exists() and not os.path.lexists(active):
            archived.replace(active)
            try:
                archive_root.rmdir()
            except OSError:
                pass
        raise
    return archived


def publish_generation(
    staging: Path,
    active: Path,
    *,
    archive_label: str,
    pre_publish: Callable[[], None] | None = None,
) -> Path | None:
    with generation_lock(active, exclusive=True):
        if pre_publish is not None:
            pre_publish()
        return _publish_generation_unlocked(
            staging,
            active,
            archive_label=archive_label,
        )
