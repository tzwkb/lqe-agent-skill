"""Deterministic, capability-scoped context bundles for LQE workers.

The module loads only canonical project assets that are declared ``present``,
bound in ``state.project_asset_paths``, and explicitly referenced by an enabled
capability.  It never scans project directories, performs fuzzy retrieval, or
silently truncates a worker payload.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import unicodedata
from typing import Mapping, Sequence

from lqe_capabilities import (
    canonical_digest as _capability_digest,
    validate_capability_descriptor,
    validate_capability_resolution,
)
from lqe_context import ContextContractError, project_context_for_modules
from lqe_profile_ingest import (
    ProfileIngestError,
    load_project_source_manifest,
    source_digest,
    validate_context_rules,
    validate_entity_registry,
    validate_json_schema,
    validate_project_source_manifest,
    validate_review_examples,
)
from lqe_project_assets import (
    ProjectAssetError,
    validate_project_asset_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]
CONTEXT_BUNDLE_SCHEMA_PATH = (
    ROOT / "schemas" / "context" / "context_bundle_v1.json"
)
WORKER_CONTEXT_MANIFEST_SCHEMA_PATH = (
    ROOT / "schemas" / "context" / "worker_context_manifest_v1.json"
)
SELECTED_CONTEXT_EVIDENCE_INDEX_SCHEMA_PATH = (
    ROOT / "schemas" / "context" / "selected_context_evidence_index_v1.json"
)
CONTEXT_BUNDLE_SCHEMA = "lqe.context-bundle"
CONTEXT_BUNDLE_VERSION = 1
WORKER_CONTEXT_MANIFEST_SCHEMA = "lqe.worker-context-manifest"
WORKER_CONTEXT_MANIFEST_VERSION = 1
SELECTED_CONTEXT_EVIDENCE_INDEX_SCHEMA = "lqe.selected-context-evidence-index"
SELECTED_CONTEXT_EVIDENCE_INDEX_VERSION = 1

LOADABLE_ASSET_SCHEMAS = {
    "entity_registry": "lqe.entities",
    "review_examples": "lqe.review-examples",
    "context_rules": "lqe.context-rules",
}
KNOWN_CAPABILITY_ASSET_KINDS = {
    "assets.entity_registry@1": "entity_registry",
    "assets.review_examples@1": "review_examples",
}
FOUNDATION_ASSET_KINDS = frozenset(
    {
        "checks",
        "confirmed_rules",
        "style_guide",
        "terminology",
        "project_source_manifest",
        "segment_context_overrides",
    }
)
FORMAL_CAPABILITY_EFFECTS = frozenset({"foundation", "enforce"})
VERIFIED_STATUSES = frozenset({"verified", "source_backed"})
_ASSET_RECORD_FIELDS = (
    "entities",
    "facts",
    "relations",
    "review_examples",
    "context_rules",
)
_ASSET_KIND_RECORD_FIELDS = {
    "entity_registry": frozenset({"entities", "facts", "relations"}),
    "review_examples": frozenset({"review_examples"}),
    "context_rules": frozenset({"context_rules"}),
}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_MODULE_VIEW_KEYS = frozenset(
    {
        "capabilities",
        "dimensions",
        "constraint_kinds",
        "include_constraints",
        "neighbors",
        "limits",
    }
)
_NEIGHBOR_KEYS = frozenset(
    {"before", "after", "include_target", "boundary_mode"}
)
_LIMIT_KEYS = frozenset(
    {"max_facts_per_entity", "max_relations", "max_runtime_examples"}
)
_WORKER_DOCUMENT_FIELDS = (
    "sg_path",
    "background_path",
    "confirmed_rules_path",
)
_SOURCE_MANIFEST_FIELDS = (
    "source_manifest_path",
    "tabular_source_manifest_path",
    "project_source_manifest_path",
)


class ContextBundleError(ValueError):
    """Raised when a bundle cannot be derived from bound canonical inputs."""


class WorkerContextBudgetError(ContextBundleError):
    """Raised when a caller-specific worker batching policy rejects an input."""


def _canonical_bytes(value: object, *, label: str = "value") -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContextBundleError(f"{label} is not canonical JSON: {exc}") from exc


def canonical_digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _digest_without(value: Mapping, field: str) -> str:
    return canonical_digest(
        {key: deepcopy(item) for key, item in value.items() if key != field}
    )


def _nonempty_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextBundleError(f"{field} must be a non-empty string")
    output = value.strip()
    if "\x00" in output:
        raise ContextBundleError(f"{field} must not contain NUL")
    return output


def _digest(value: object, field: str) -> str:
    output = _nonempty_text(value, field)
    if not _HEX64.fullmatch(output):
        raise ContextBundleError(f"{field} must be a lowercase SHA-256 digest")
    return output


def _string_list(value: object, field: str, *, default: Sequence[str] = ()) -> list[str]:
    if value is None:
        value = list(default)
    if not isinstance(value, list):
        raise ContextBundleError(f"{field} must be an array")
    output = []
    for item in value:
        normalized = _nonempty_text(item, field)
        if normalized in output:
            raise ContextBundleError(f"{field} must not contain duplicates")
        output.append(normalized)
    return sorted(output)


def _decode_json(payload: bytes, *, label: str) -> object:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
        output = {}
        for key, value in pairs:
            if key in output:
                raise ContextBundleError(
                    f"duplicate JSON key {key!r} in {label}"
                )
            output[key] = value
        return output

    try:
        return json.loads(
            payload.decode("utf-8"), object_pairs_hook=reject_duplicates
        )
    except ContextBundleError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContextBundleError(f"cannot load {label}: {exc}") from exc


def _load_json(path: Path, *, label: str) -> object:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ContextBundleError(f"cannot load {label}: {exc}") from exc
    return _decode_json(payload, label=label)


def _load_schema(path: Path) -> dict:
    value = _load_json(path, label=str(path))
    if not isinstance(value, dict):
        raise ContextBundleError(f"bundled schema is not an object: {path}")
    return value


def _regular_file_bytes(path_value: object, *, label: str) -> tuple[Path, bytes]:
    if isinstance(path_value, os.PathLike):
        path_value = os.fspath(path_value)
    text = _nonempty_text(path_value, label)
    path = Path(text)
    if not path.is_absolute():
        raise ContextBundleError(f"{label} must be an absolute bound path")
    try:
        info = path.lstat()
    except OSError as exc:
        raise ContextBundleError(f"cannot inspect {label}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ContextBundleError(f"{label} must be a regular non-symlink file")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ContextBundleError(f"cannot read {label}: {exc}") from exc
    return path, payload


def _canonical_relative_path(value: object, *, label: str) -> PurePosixPath:
    text = _nonempty_text(value, label)
    if "\\" in text:
        raise ContextBundleError(f"{label} must use POSIX separators")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ContextBundleError(f"{label} must be a canonical relative path")
    if path.as_posix() != text:
        raise ContextBundleError(f"{label} must be a canonical relative path")
    return path


def _safe_root_file(
    root_value: object,
    relative_value: object,
    *,
    label: str,
) -> tuple[Path, bytes]:
    root = Path(root_value).resolve(strict=True)
    if not root.is_dir():
        raise ContextBundleError(f"{label} root must be a directory")
    relative = _canonical_relative_path(relative_value, label=label)
    candidate = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise ContextBundleError(f"cannot inspect {label}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ContextBundleError(f"{label} must not traverse a symlink")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ContextBundleError(f"{label} escapes its bound root") from exc
    if not stat.S_ISREG(resolved.stat().st_mode):
        raise ContextBundleError(f"{label} must resolve to a regular file")
    try:
        return resolved, resolved.read_bytes()
    except OSError as exc:
        raise ContextBundleError(f"cannot read {label}: {exc}") from exc


def _path_locator(
    path: Path,
    payload: bytes,
    *,
    job_root: object | None,
) -> dict:
    roots = []
    if job_root is not None:
        roots.append(("job_relative", Path(job_root).resolve(strict=True)))
    roots.append(("skill_relative", ROOT.resolve(strict=True)))
    absolute = path.resolve(strict=True)
    for kind, root in roots:
        try:
            relative = absolute.relative_to(root)
        except ValueError:
            continue
        relative_text = PurePosixPath(*relative.parts).as_posix()
        resolved, reread = _safe_root_file(
            root,
            relative_text,
            label=f"{kind} resource",
        )
        if resolved != path.resolve(strict=True) or reread != payload:
            raise ContextBundleError("worker resource changed while building locator")
        return {"kind": kind, "path": relative_text}
    try:
        content = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContextBundleError(
            f"worker resource outside bound roots is not UTF-8 text: {path}"
        ) from exc
    return {"kind": "embedded_text", "content": content}


def _locator_payload(
    locator: Mapping,
    *,
    job_root: object | None,
    skill_root: object,
    verify_paths: bool,
    label: str,
) -> bytes | None:
    kind = locator.get("kind")
    if kind == "embedded_text":
        content = locator.get("content")
        if not isinstance(content, str):
            raise ContextBundleError(f"{label} embedded content must be text")
        return content.encode("utf-8")
    if kind not in {"job_relative", "skill_relative"}:
        raise ContextBundleError(f"{label} locator kind is invalid")
    _canonical_relative_path(locator.get("path"), label=f"{label}.path")
    if not verify_paths:
        return None
    if kind == "job_relative":
        if job_root is None:
            raise ContextBundleError(
                f"{label} job-relative locator requires an explicit job root"
            )
        root = job_root
    else:
        root = skill_root
    _, payload = _safe_root_file(root, locator["path"], label=label)
    return payload


def _validate_state_bindings(state: Mapping) -> tuple[dict, dict]:
    if not isinstance(state, Mapping):
        raise ContextBundleError("state must be an object")
    try:
        snapshot = validate_project_asset_snapshot(
            state.get("project_asset_snapshot")
        )
        resolution = validate_capability_resolution(
            state.get("capability_resolution")
        )
    except (ProjectAssetError, ValueError) as exc:
        raise ContextBundleError(str(exc)) from exc
    if state.get("project_asset_snapshot_digest") != snapshot["digest"]:
        raise ContextBundleError(
            "state.project_asset_snapshot_digest does not match the snapshot"
        )
    if state.get("capability_resolution_digest") != resolution["digest"]:
        raise ContextBundleError(
            "state.capability_resolution_digest does not match the resolution"
        )
    if state.get("profile_digest") is not None and state.get(
        "profile_digest"
    ) != resolution.get("source_profile_digest"):
        raise ContextBundleError(
            "state.profile_digest does not match capability resolution source profile"
        )
    for asset_id, entry in snapshot["assets"].items():
        if not isinstance(asset_id, str) or not asset_id or not isinstance(
            entry, Mapping
        ):
            raise ContextBundleError("project asset snapshot entry is invalid")
        for field in (
            "kind",
            "path",
            "required",
            "distribution",
            "availability",
            "status",
        ):
            if field not in entry:
                raise ContextBundleError(
                    f"project asset snapshot {asset_id!r} lacks {field}"
                )
        if entry["status"] not in {"present", "missing", "external"}:
            raise ContextBundleError(
                f"project asset snapshot {asset_id!r} has invalid status"
            )
        if entry["status"] == "present":
            _digest(entry.get("sha256"), f"snapshot asset {asset_id}.sha256")
            if entry.get("availability") != "included":
                raise ContextBundleError(
                    f"present snapshot asset {asset_id!r} is not included"
                )
        elif entry.get("sha256") is not None:
            raise ContextBundleError(
                f"non-present snapshot asset {asset_id!r} must not have sha256"
            )
        if entry["status"] == "external" and entry.get("availability") != "external":
            raise ContextBundleError(
                f"external snapshot asset {asset_id!r} has inconsistent availability"
            )
    return snapshot, resolution


def _project_source_manifest(
    state: Mapping,
    snapshot: Mapping,
    raw_paths: Mapping,
) -> dict | None:
    candidates: list[tuple[str, object, bool]] = []
    if state.get("project_source_manifest") is not None:
        candidates.append(
            (
                "state.project_source_manifest",
                state["project_source_manifest"],
                False,
            )
        )
    raw_path = state.get("project_source_manifest_path")
    if isinstance(raw_path, str) and raw_path.strip():
        path, _ = _regular_file_bytes(
            raw_path, label="state.project_source_manifest_path"
        )
        try:
            bound_manifest = load_project_source_manifest(path)
        except ProfileIngestError as exc:
            raise ContextBundleError(
                f"state.project_source_manifest_path: {exc}"
            ) from exc
        candidates.append((
            "state.project_source_manifest_path",
            bound_manifest,
            True,
        ))
    elif raw_path not in (None, ""):
        raise ContextBundleError(
            "state.project_source_manifest_path must be a path or empty"
        )

    manifest_asset_ids = [
        asset_id
        for asset_id, entry in sorted(snapshot["assets"].items())
        if entry.get("kind") == "project_source_manifest"
        and entry.get("status") == "present"
    ]
    if len(manifest_asset_ids) > 1:
        raise ContextBundleError(
            "only one present project_source_manifest asset is allowed"
        )
    if manifest_asset_ids:
        asset_id = manifest_asset_ids[0]
        if asset_id not in raw_paths:
            raise ContextBundleError(
                f"present project source manifest {asset_id!r} has no runtime path"
            )
        path, payload = _regular_file_bytes(
            raw_paths[asset_id], label=f"project_asset_paths.{asset_id}"
        )
        if hashlib.sha256(payload).hexdigest() != snapshot["assets"][asset_id].get(
            "sha256"
        ):
            raise ContextBundleError(
                f"project source manifest digest mismatch: {asset_id}"
            )
        try:
            bound_manifest = load_project_source_manifest(path)
        except ProfileIngestError as exc:
            raise ContextBundleError(f"project asset {asset_id}: {exc}") from exc
        candidates.append((f"project asset {asset_id}", bound_manifest, True))

    validated = []
    for label, raw, is_bound in candidates:
        try:
            validated.append((label, validate_project_source_manifest(raw), is_bound))
        except ProfileIngestError as exc:
            raise ContextBundleError(f"{label}: {exc}") from exc
    if not validated:
        return None
    if not any(is_bound for _, _, is_bound in validated):
        raise ContextBundleError(
            "project source manifest requires a bound manifest path and coverage details"
        )
    reference = validated[0][1]
    if any(document != reference for _, document, _ in validated[1:]):
        raise ContextBundleError(
            "state and project asset source manifests do not match"
        )
    return reference


def _validate_generated_asset_bindings(
    project_manifest: Mapping | None,
    snapshot: Mapping,
    enabled_assets: Mapping[str, list[str]],
) -> None:
    required_ids = {
        asset_id
        for asset_id in enabled_assets
        if snapshot["assets"][asset_id].get("kind") in LOADABLE_ASSET_SCHEMAS
    }
    if not required_ids:
        return
    if project_manifest is None:
        raise ContextBundleError(
            "enabled canonical context assets require a bound project source manifest"
        )
    generated = {
        item["asset_id"]: item for item in project_manifest["generated_assets"]
    }
    for asset_id, record in sorted(generated.items()):
        entry = snapshot["assets"].get(asset_id)
        if not isinstance(entry, Mapping):
            raise ContextBundleError(
                f"project source manifest generated asset {asset_id!r} is undeclared"
            )
        for field in ("kind", "path", "distribution"):
            if record[field] != entry[field]:
                raise ContextBundleError(
                    f"project source manifest generated asset {asset_id!r} "
                    f"{field} differs from the project asset snapshot"
                )
        if entry.get("sha256") is not None and record["sha256"] != entry["sha256"]:
            raise ContextBundleError(
                f"project source manifest generated asset {asset_id!r} "
                "sha256 differs from the project asset snapshot"
            )
    missing = sorted(required_ids - set(generated))
    if missing:
        raise ContextBundleError(
            f"project source manifest lacks derived_from records for assets: {missing}"
        )


def _enabled_asset_bindings(
    snapshot: Mapping,
    resolution: Mapping,
    *,
    effects: frozenset[str] | None = None,
) -> dict[str, list[str]]:
    """Return enabled capability references after checking snapshot binding."""

    output: dict[str, list[str]] = {}
    for capability_id, raw_item in sorted(resolution["enabled"].items()):
        if not isinstance(raw_item, Mapping):
            raise ContextBundleError(
                f"enabled capability {capability_id!r} must be an object"
            )
        if effects is not None and raw_item.get("effect") not in effects:
            continue
        if "asset" not in raw_item:
            continue
        asset_id = _nonempty_text(
            raw_item.get("asset"), f"enabled capability {capability_id}.asset"
        )
        entry = snapshot["assets"].get(asset_id)
        if not isinstance(entry, Mapping):
            raise ContextBundleError(
                f"enabled capability {capability_id!r} references undeclared asset {asset_id!r}"
            )
        if entry.get("status") != "present":
            raise ContextBundleError(
                f"enabled capability {capability_id!r} references non-present asset {asset_id!r}"
            )
        expected_kind = KNOWN_CAPABILITY_ASSET_KINDS.get(capability_id)
        if capability_id.startswith("language_policy."):
            expected_kind = "context_rules"
        if expected_kind is not None and entry.get("kind") != expected_kind:
            raise ContextBundleError(
                f"enabled capability {capability_id!r} requires asset kind "
                f"{expected_kind!r}, not {entry.get('kind')!r}"
            )
        asset_digest = _digest(
            raw_item.get("asset_digest"),
            f"enabled capability {capability_id}.asset_digest",
        )
        if asset_digest != entry.get("sha256"):
            raise ContextBundleError(
                f"enabled capability {capability_id!r} asset digest differs from snapshot"
            )
        output.setdefault(asset_id, []).append(capability_id)
    return {asset_id: sorted(ids) for asset_id, ids in sorted(output.items())}


def _validate_loaded_asset_document(
    document: object,
    *,
    kind: str,
    target_lang: str | None,
    source_ids: Sequence[str] | None,
    held_out_segment_keys: Sequence[str],
) -> dict:
    try:
        if kind == "entity_registry":
            return validate_entity_registry(document, source_ids=source_ids)
        if kind == "review_examples":
            return validate_review_examples(
                document, held_out_segment_keys=held_out_segment_keys
            )
        if kind == "context_rules":
            return validate_context_rules(document, target_lang=target_lang)
    except ProfileIngestError as exc:
        raise ContextBundleError(str(exc)) from exc
    raise ContextBundleError(f"unsupported project context asset kind: {kind}")


def _put_unique(index: dict, key: str, value: object, *, label: str) -> None:
    if key in index:
        raise ContextBundleError(f"duplicate {label} across project assets: {key}")
    index[key] = deepcopy(value)


def load_project_context_assets(state: Mapping) -> dict:
    """Load and index only enabled, bound, present canonical context assets."""

    snapshot, resolution = _validate_state_bindings(state)
    enabled_assets = _enabled_asset_bindings(snapshot, resolution)
    raw_paths = state.get("project_asset_paths")
    if not isinstance(raw_paths, Mapping):
        raise ContextBundleError("state.project_asset_paths must be an object")
    for asset_id, path in raw_paths.items():
        if not isinstance(asset_id, str) or asset_id not in snapshot["assets"]:
            raise ContextBundleError(
                f"project_asset_paths references undeclared asset {asset_id!r}"
            )
        if not isinstance(path, str) or not path.strip():
            raise ContextBundleError(
                f"project_asset_paths.{asset_id} must be a non-empty path"
            )

    project_manifest = _project_source_manifest(state, snapshot, raw_paths)
    _validate_generated_asset_bindings(
        project_manifest,
        snapshot,
        enabled_assets,
    )
    source_ids = (
        [source["id"] for source in project_manifest["sources"]]
        if project_manifest is not None
        else None
    )
    segments = state.get("segments", [])
    if not isinstance(segments, list):
        raise ContextBundleError("state.segments must be an array")
    held_out_keys = [
        segment.get("segment_key")
        for segment in segments
        if isinstance(segment, Mapping)
        and isinstance(segment.get("segment_key"), str)
        and segment.get("segment_key")
    ]

    asset_bindings = {}
    entities = {}
    facts = {}
    relations = {}
    examples = {}
    rules = {}
    entity_record_ids: set[str] = set()
    loaded_paths: set[Path] = set()
    for asset_id in sorted(snapshot["assets"]):
        entry = snapshot["assets"][asset_id]
        kind = entry.get("kind")
        if kind not in LOADABLE_ASSET_SCHEMAS:
            continue
        if asset_id not in enabled_assets:
            continue
        if entry.get("status") != "present":
            raise ContextBundleError(
                f"enabled context asset {asset_id!r} is not present"
            )
        if asset_id not in raw_paths:
            raise ContextBundleError(
                f"present context asset {asset_id!r} has no bound runtime path"
            )
        path, payload = _regular_file_bytes(
            raw_paths[asset_id], label=f"project_asset_paths.{asset_id}"
        )
        real_path = path.resolve()
        if real_path in loaded_paths:
            raise ContextBundleError(
                f"project context assets alias the same runtime file: {asset_id}"
            )
        loaded_paths.add(real_path)
        actual_digest = hashlib.sha256(payload).hexdigest()
        if actual_digest != entry.get("sha256"):
            raise ContextBundleError(
                f"project context asset digest mismatch: {asset_id}"
            )
        document = _decode_json(
            payload, label=f"project context asset {asset_id}"
        )
        if not isinstance(document, dict) or document.get("schema") != LOADABLE_ASSET_SCHEMAS[kind]:
            raise ContextBundleError(
                f"project context asset {asset_id!r} does not match kind {kind!r}"
            )
        if (
            kind == "entity_registry"
            and source_ids is None
            and (document.get("entities") or document.get("relations"))
        ):
            raise ContextBundleError(
                "entity registry requires a bound project source manifest"
            )
        validated = _validate_loaded_asset_document(
            document,
            kind=kind,
            target_lang=state.get("target_lang"),
            source_ids=source_ids,
            held_out_segment_keys=held_out_keys,
        )
        record_ids = {field: [] for field in _ASSET_RECORD_FIELDS}
        asset_bindings[asset_id] = {
            "asset_id": asset_id,
            "kind": kind,
            "sha256": actual_digest,
            "document_digest": canonical_digest(validated),
            "capability_ids": enabled_assets[asset_id],
            "record_ids": record_ids,
        }
        if kind == "entity_registry":
            for entity in validated["entities"]:
                entity_id = entity["id"]
                if entity_id in entity_record_ids:
                    raise ContextBundleError(
                        f"duplicate entity record id across assets: {entity_id}"
                    )
                entity_record_ids.add(entity_id)
                _put_unique(entities, entity_id, entity, label="entity id")
                record_ids["entities"].append(entity_id)
                for fact in entity["facts"]:
                    fact_id = fact["id"]
                    if fact_id in entity_record_ids:
                        raise ContextBundleError(
                            f"duplicate entity record id across assets: {fact_id}"
                        )
                    entity_record_ids.add(fact_id)
                    _put_unique(
                        facts,
                        fact_id,
                        {
                            "entity_id": entity_id,
                            "entity_type": entity["entity_type"],
                            "names": entity["names"],
                            "tags": entity["tags"],
                            "fact": fact,
                        },
                        label="entity fact id",
                    )
                    record_ids["facts"].append(fact_id)
            for relation in validated["relations"]:
                relation_id = relation["id"]
                if relation_id in entity_record_ids:
                    raise ContextBundleError(
                        f"duplicate entity record id across assets: {relation_id}"
                    )
                entity_record_ids.add(relation_id)
                _put_unique(
                    relations, relation_id, relation, label="relation id"
                )
                record_ids["relations"].append(relation_id)
        elif kind == "review_examples":
            for example in validated["examples"]:
                _put_unique(
                    examples,
                    example["id"],
                    example,
                    label="review example id",
                )
                record_ids["review_examples"].append(example["id"])
        else:
            for rule in validated["rules"]:
                _put_unique(rules, rule["id"], rule, label="context rule id")
                record_ids["context_rules"].append(rule["id"])
        for field in record_ids:
            record_ids[field].sort()

    output = {
        "asset_snapshot_digest": snapshot["digest"],
        "capability_resolution_digest": resolution["digest"],
        "asset_bindings": asset_bindings,
        "entities": entities,
        "facts": facts,
        "relations": relations,
        "review_examples": examples,
        "context_rules": rules,
    }
    output["loaded_assets_digest"] = canonical_digest(output)
    return validate_loaded_project_context_assets(output)


def validate_loaded_project_context_assets(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise ContextBundleError("loaded project context assets must be an object")
    required = {
        "asset_snapshot_digest",
        "capability_resolution_digest",
        "asset_bindings",
        "entities",
        "facts",
        "relations",
        "review_examples",
        "context_rules",
        "loaded_assets_digest",
    }
    if set(value) != required:
        difference = sorted(set(value) ^ required)
        raise ContextBundleError(
            f"loaded project context assets fields differ from contract: {difference}"
        )
    _digest(value["asset_snapshot_digest"], "asset_snapshot_digest")
    _digest(
        value["capability_resolution_digest"],
        "capability_resolution_digest",
    )
    for field in required - {
        "asset_snapshot_digest",
        "capability_resolution_digest",
        "loaded_assets_digest",
    }:
        if not isinstance(value[field], Mapping):
            raise ContextBundleError(f"loaded project context assets {field} must be an object")
    indexes = {
        "entities": value["entities"],
        "facts": value["facts"],
        "relations": value["relations"],
        "review_examples": value["review_examples"],
        "context_rules": value["context_rules"],
    }
    claimed = {field: set() for field in _ASSET_RECORD_FIELDS}
    for asset_id, binding in value["asset_bindings"].items():
        if not isinstance(binding, Mapping) or binding.get("asset_id") != asset_id:
            raise ContextBundleError(
                f"loaded project context asset binding key mismatch: {asset_id!r}"
            )
        capability_ids = binding.get("capability_ids")
        if capability_ids != _string_list(
            capability_ids, f"asset_bindings.{asset_id}.capability_ids"
        ):
            raise ContextBundleError(
                f"loaded project context asset {asset_id!r} capabilities are not canonical"
            )
        if not capability_ids:
            raise ContextBundleError(
                f"loaded project context asset {asset_id!r} has no capability binding"
            )
        kind = binding.get("kind")
        if kind not in _ASSET_KIND_RECORD_FIELDS:
            raise ContextBundleError(
                f"loaded project context asset {asset_id!r} has invalid kind"
            )
        record_ids = binding.get("record_ids")
        if not isinstance(record_ids, Mapping) or set(record_ids) != set(
            _ASSET_RECORD_FIELDS
        ):
            raise ContextBundleError(
                f"loaded project context asset {asset_id!r} record_ids are invalid"
            )
        for field in _ASSET_RECORD_FIELDS:
            ids = _string_list(
                record_ids[field],
                f"asset_bindings.{asset_id}.record_ids.{field}",
            )
            if ids != record_ids[field]:
                raise ContextBundleError(
                    f"loaded project context asset {asset_id!r} record ids are not canonical"
                )
            duplicates = claimed[field].intersection(ids)
            if duplicates:
                raise ContextBundleError(
                    f"loaded project context records have multiple owning assets: "
                    f"{sorted(duplicates)}"
                )
            unknown = sorted(set(ids) - set(indexes[field]))
            if unknown:
                raise ContextBundleError(
                    f"loaded project context asset {asset_id!r} claims unknown {field}: "
                    f"{unknown}"
                )
            claimed[field].update(ids)
            if ids and field not in _ASSET_KIND_RECORD_FIELDS[kind]:
                raise ContextBundleError(
                    f"loaded project context asset {asset_id!r} kind {kind!r} "
                    f"cannot own {field} records"
                )
    for field in _ASSET_RECORD_FIELDS:
        unclaimed = sorted(set(indexes[field]) - claimed[field])
        if unclaimed:
            raise ContextBundleError(
                f"loaded project context {field} records lack an owning asset: {unclaimed}"
            )
    expected = _digest_without(value, "loaded_assets_digest")
    if value["loaded_assets_digest"] != expected:
        raise ContextBundleError("loaded project context assets digest mismatch")
    return deepcopy(dict(value))


def _formal_project_context_assets(
    assets: Mapping,
    resolution: Mapping,
    snapshot: Mapping,
) -> dict:
    formal_capabilities = {
        capability_id
        for capability_id, item in resolution["enabled"].items()
        if isinstance(item, Mapping)
        and item.get("effect") in FORMAL_CAPABILITY_EFFECTS
    }
    allowed = {field: set() for field in _ASSET_RECORD_FIELDS}
    for asset_id, binding in assets["asset_bindings"].items():
        snapshot_entry = snapshot["assets"].get(asset_id)
        if (
            not isinstance(snapshot_entry, Mapping)
            or snapshot_entry.get("kind") != binding.get("kind")
        ):
            raise ContextBundleError(
                f"loaded asset binding differs from project snapshot: {asset_id}"
            )
        capability_ids = set(binding["capability_ids"])
        for capability_id in capability_ids:
            item = resolution["enabled"].get(capability_id)
            if not isinstance(item, Mapping) or item.get("asset") != asset_id:
                raise ContextBundleError(
                    f"loaded asset binding differs from capability resolution: "
                    f"{asset_id}/{capability_id}"
                )
        if not capability_ids.intersection(formal_capabilities):
            continue
        for field in _ASSET_RECORD_FIELDS:
            allowed[field].update(binding["record_ids"][field])
    return {
        "entities": {
            key: deepcopy(value)
            for key, value in assets["entities"].items()
            if key in allowed["entities"]
        },
        "facts": {
            key: deepcopy(value)
            for key, value in assets["facts"].items()
            if key in allowed["facts"]
        },
        "relations": {
            key: deepcopy(value)
            for key, value in assets["relations"].items()
            if key in allowed["relations"]
        },
        "review_examples": {
            key: deepcopy(value)
            for key, value in assets["review_examples"].items()
            if key in allowed["review_examples"]
        },
        "context_rules": {
            key: deepcopy(value)
            for key, value in assets["context_rules"].items()
            if key in allowed["context_rules"]
        },
    }


def _active_context_registry(state: Mapping) -> dict[str, dict]:
    _, resolution = _validate_state_bindings(state)
    raw = state.get("resolved_context_descriptors")
    if not isinstance(raw, Mapping):
        raise ContextBundleError(
            "state.resolved_context_descriptors must be an object"
        )
    expected = {
        capability_id
        for capability_id, item in resolution["enabled"].items()
        if capability_id.startswith("context.")
        and item.get("effect") in {"foundation", "enforce"}
    }
    if set(raw) != expected:
        raise ContextBundleError(
            "resolved context descriptor coverage differs from active capabilities"
        )
    output = {}
    for capability_id in sorted(raw):
        raw_descriptor = raw[capability_id]
        if not isinstance(raw_descriptor, Mapping):
            raise ContextBundleError(
                f"resolved context descriptor {capability_id} must be an object"
            )
        declarative = {
            key: deepcopy(value)
            for key, value in raw_descriptor.items()
            if key not in {"identity", "runtime_budget"}
        }
        try:
            validate_capability_descriptor(declarative)
        except ValueError as exc:
            raise ContextBundleError(str(exc)) from exc
        for runtime_field in ("identity", "runtime_budget"):
            if runtime_field in raw_descriptor and not isinstance(
                raw_descriptor[runtime_field], Mapping
            ):
                raise ContextBundleError(
                    f"resolved context descriptor {capability_id}.{runtime_field} must be an object"
                )
        descriptor = deepcopy(dict(raw_descriptor))
        _canonical_bytes(descriptor, label=f"resolved descriptor {capability_id}")
        if descriptor["id"] != capability_id:
            raise ContextBundleError(
                f"resolved context descriptor id mismatch: {capability_id}"
            )
        expected_digest = resolution["enabled"][capability_id].get(
            "descriptor_digest"
        )
        if expected_digest != _capability_digest(descriptor):
            raise ContextBundleError(
                f"resolved context descriptor digest mismatch: {capability_id}"
            )
        output[capability_id] = descriptor
    if "context.core@1" not in output:
        raise ContextBundleError("active context registry lacks context.core@1")
    return output


def _formal_capability_summary(resolution: Mapping) -> dict[str, list[str]]:
    enabled = {
        capability_id
        for capability_id, item in resolution["enabled"].items()
        if isinstance(item, Mapping)
        and item.get("effect") in FORMAL_CAPABILITY_EFFECTS
    }
    return {
        "enabled": sorted(enabled),
        "disabled": [],
    }


def _extension_name(capability_id: str) -> str:
    raw = capability_id.removeprefix("context.")
    return re.sub(r"@[1-9][0-9]*$", "", raw)


def normalize_module_view(
    state: Mapping,
    module: str,
    registry: Mapping[str, Mapping],
    view: Mapping | None = None,
) -> dict:
    module = _nonempty_text(module, "module")
    if view is None:
        views = state.get("module_context_views", {})
        if views is None:
            views = {}
        if not isinstance(views, Mapping):
            raise ContextBundleError("state.module_context_views must be an object")
        view = views.get(module, {})
    if not isinstance(view, Mapping):
        raise ContextBundleError(f"module view {module!r} must be an object")
    unknown = sorted(set(view) - _MODULE_VIEW_KEYS)
    if unknown:
        raise ContextBundleError(f"module view has unknown fields: {unknown}")

    inferred_capabilities = [
        capability_id
        for capability_id, descriptor in registry.items()
        if capability_id == "context.core@1"
        or bool(descriptor.get("module_views", {}).get(module))
    ]
    capabilities = _string_list(
        view.get("capabilities"),
        "module_view.capabilities",
        default=inferred_capabilities,
    )
    _, resolution = _validate_state_bindings(state)
    formal_capabilities = set(_formal_capability_summary(resolution)["enabled"])
    inactive = sorted(set(capabilities) - formal_capabilities)
    if inactive:
        raise ContextBundleError(
            f"module view references inactive capabilities: {inactive}"
        )
    if "context.core@1" not in capabilities:
        raise ContextBundleError("module view must include context.core@1")
    missing_descriptors = sorted(
        capability_id
        for capability_id in capabilities
        if capability_id.startswith("context.") and capability_id not in registry
    )
    if missing_descriptors:
        raise ContextBundleError(
            f"module view lacks active context descriptors: {missing_descriptors}"
        )
    dimensions = _string_list(
        view.get("dimensions"),
        "module_view.dimensions",
        default=[module],
    )
    constraint_kinds = _string_list(
        view.get("constraint_kinds"), "module_view.constraint_kinds"
    )
    include_constraints = view.get("include_constraints", True)
    if type(include_constraints) is not bool:
        raise ContextBundleError("module_view.include_constraints must be boolean")

    raw_neighbors = view.get("neighbors", {})
    if not isinstance(raw_neighbors, Mapping):
        raise ContextBundleError("module_view.neighbors must be an object")
    unknown_neighbors = sorted(set(raw_neighbors) - _NEIGHBOR_KEYS)
    if unknown_neighbors:
        raise ContextBundleError(
            f"module_view.neighbors has unknown fields: {unknown_neighbors}"
        )
    neighbors = {}
    for field in ("before", "after"):
        value = raw_neighbors.get(field, 0)
        if type(value) is not int or value < 0:
            raise ContextBundleError(
                f"module_view.neighbors.{field} must be a non-negative integer"
            )
        neighbors[field] = value
    include_target = raw_neighbors.get("include_target", False)
    if type(include_target) is not bool:
        raise ContextBundleError(
            "module_view.neighbors.include_target must be boolean"
        )
    neighbors["include_target"] = include_target
    boundary_mode = raw_neighbors.get("boundary_mode", "same_if_present")
    if boundary_mode not in {"same_if_present", "strict"}:
        raise ContextBundleError(
            "module_view.neighbors.boundary_mode must be same_if_present or strict"
        )
    neighbors["boundary_mode"] = boundary_mode

    raw_limits = view.get("limits", {})
    if not isinstance(raw_limits, Mapping):
        raise ContextBundleError("module_view.limits must be an object")
    unknown_limits = sorted(set(raw_limits) - _LIMIT_KEYS)
    if unknown_limits:
        raise ContextBundleError(
            f"module_view.limits has unknown fields: {unknown_limits}"
        )
    limits = {}
    for field in sorted(_LIMIT_KEYS):
        value = raw_limits.get(field, 0)
        if type(value) is not int or value < 0:
            raise ContextBundleError(
                f"module_view.limits.{field} must be a non-negative integer"
            )
        limits[field] = value
    return {
        "capabilities": capabilities,
        "dimensions": dimensions,
        "constraint_kinds": constraint_kinds,
        "include_constraints": include_constraints,
        "neighbors": neighbors,
        "limits": limits,
    }


def _context_state(segment: Mapping) -> dict:
    raw = segment.get("context")
    if not isinstance(raw, Mapping):
        raw = segment.get("segment_context")
    if isinstance(raw, Mapping) and isinstance(raw.get("context"), Mapping):
        raw = raw["context"]
    return deepcopy(dict(raw)) if isinstance(raw, Mapping) else {}


def _normalize_projection_modules(
    module: str,
    projection_modules: Sequence[str] | None,
) -> list[str]:
    if projection_modules is None:
        return [module]
    if not isinstance(projection_modules, Sequence) or isinstance(
        projection_modules, (str, bytes)
    ):
        raise ContextBundleError("projection_modules must be an array")
    normalized = sorted(
        {
            _nonempty_text(item, "projection_modules item")
            for item in projection_modules
        }
    )
    if not normalized:
        raise ContextBundleError("projection_modules must not be empty")
    if module not in normalized:
        raise ContextBundleError(
            "projection_modules must include the bundle module"
        )
    return normalized


def _context_projection(
    segment: Mapping,
    projection_modules: Sequence[str],
    registry: Mapping[str, dict],
    view: Mapping,
    assets: Mapping,
) -> dict:
    context = _context_state(segment)
    try:
        projection = project_context_for_modules(
            context, projection_modules, registry
        )
    except ContextContractError as exc:
        raise ContextBundleError(str(exc)) from exc
    allowed_extensions = {
        _extension_name(capability_id)
        for capability_id in view["capabilities"]
        if capability_id.startswith("context.")
        and capability_id != "context.core@1"
    }
    extensions = projection.get("extensions")
    if isinstance(extensions, Mapping):
        projection["extensions"] = {
            name: deepcopy(item)
            for name, item in extensions.items()
            if name in allowed_extensions
        }
        if not projection["extensions"]:
            projection.pop("extensions")
    raw_core = context.get("core", {})
    raw_core = raw_core if isinstance(raw_core, Mapping) else {}
    for field_name in ("content_type", "group_id"):
        value = raw_core.get(field_name, segment.get(field_name))
        if value not in (None, ""):
            projection.setdefault("core", {})[field_name] = deepcopy(value)
    _resolve_dialogue_entities(projection, assets, view)
    if not projection.get("core"):
        projection.pop("core", None)
    return projection


def _normalized_entity_alias(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _entity_alias_index(
    entities: Mapping[str, Mapping],
) -> dict[str, dict[str, set[str]]]:
    index: dict[str, dict[str, set[str]]] = {}
    for entity_id in sorted(entities):
        entity = entities[entity_id]
        names = entity.get("names", {})
        if not isinstance(names, Mapping):
            raise ContextBundleError(
                f"entity {entity_id!r} names must be an object"
            )
        for language_field in ("source", "target"):
            values = names.get(language_field, [])
            if not isinstance(values, list):
                raise ContextBundleError(
                    f"entity {entity_id!r} names.{language_field} must be an array"
                )
            for value in values:
                if not isinstance(value, str) or not value.strip():
                    raise ContextBundleError(
                        f"entity {entity_id!r} has an invalid alias"
                    )
                normalized = _normalized_entity_alias(value)
                index.setdefault(normalized, {}).setdefault(
                    entity_id, set()
                ).add(f"names.{language_field}")
    return index


def _resolve_entity_reference(
    value: object,
    entities: Mapping[str, Mapping],
    alias_index: Mapping[str, Mapping[str, set[str]]],
) -> tuple[object, dict]:
    if value in (None, ""):
        return value, {
            "input": None,
            "raw_label": None,
            "normalized_alias": None,
            "status": "missing",
            "resolution_status": "missing",
            "reason": "no_entity_label",
            "canonical_id": None,
            "candidates": [],
            "candidate_ids": [],
            "matched_name_fields": [],
        }
    if not isinstance(value, str):
        raise ContextBundleError("dialogue entity reference must be a string")
    if value in entities:
        return value, {
            "input": value,
            "raw_label": value,
            "normalized_alias": _normalized_entity_alias(value),
            "status": "canonical_id",
            "resolution_status": "canonical_id",
            "reason": "exact_canonical_id",
            "canonical_id": value,
            "candidates": [value],
            "candidate_ids": [value],
            "matched_name_fields": ["id"],
        }
    normalized = _normalized_entity_alias(value)
    matches = alias_index.get(normalized, {})
    candidate_ids = sorted(matches)
    if len(candidate_ids) == 1:
        canonical_id = candidate_ids[0]
        return canonical_id, {
            "input": value,
            "raw_label": value,
            "normalized_alias": normalized,
            "status": "unique_alias",
            "resolution_status": "unique_alias",
            "reason": "unique_normalized_name_alias",
            "canonical_id": canonical_id,
            "candidates": candidate_ids,
            "candidate_ids": candidate_ids,
            "matched_name_fields": sorted(matches[canonical_id]),
        }
    resolution_status = "ambiguous" if candidate_ids else "unknown"
    return value, {
        "input": value,
        "raw_label": value,
        "normalized_alias": normalized,
        "status": resolution_status,
        "resolution_status": resolution_status,
        "reason": (
            "normalized_alias_matches_multiple_entities"
            if candidate_ids
            else "no_canonical_id_or_unique_name_alias"
        ),
        "canonical_id": None,
        "candidates": candidate_ids,
        "candidate_ids": candidate_ids,
        "matched_name_fields": sorted(
            {field for fields in matches.values() for field in fields}
        ),
    }


def _resolve_dialogue_entities(
    projection: dict,
    assets: Mapping,
    view: Mapping,
) -> None:
    if "assets.entity_registry@1" not in view["capabilities"]:
        return
    extensions = projection.get("extensions")
    if not isinstance(extensions, dict):
        return
    dialogue = extensions.get("dialogue")
    if not isinstance(dialogue, dict) or dialogue.get("status") in {
        "not_applicable",
        "disabled",
    }:
        return
    entities = assets.get("entities", {})
    if not isinstance(entities, Mapping):
        raise ContextBundleError("formal entity registry must be an object")
    alias_index = _entity_alias_index(entities)

    resolved_speaker, speaker_resolution = _resolve_entity_reference(
        dialogue.get("speaker_id"), entities, alias_index
    )
    if resolved_speaker not in (None, ""):
        dialogue["speaker_id"] = resolved_speaker

    raw_addressees = dialogue.get("addressee_ids", [])
    if isinstance(raw_addressees, str):
        raw_addressees = [raw_addressees]
    if not isinstance(raw_addressees, list):
        raise ContextBundleError("dialogue addressee_ids must be an array")
    resolved_addressees = []
    addressee_resolutions = []
    for raw_value in raw_addressees:
        resolved_value, resolution = _resolve_entity_reference(
            raw_value, entities, alias_index
        )
        addressee_resolutions.append(resolution)
        if resolved_value not in (None, "") and resolved_value not in resolved_addressees:
            resolved_addressees.append(resolved_value)
    if raw_addressees:
        dialogue["addressee_ids"] = resolved_addressees
    dialogue["entity_resolution"] = {
        "speaker": speaker_resolution,
        "addressees": addressee_resolutions,
    }

    statuses = {
        speaker_resolution["status"],
        *(item["status"] for item in addressee_resolutions),
    }
    if "ambiguous" in statuses:
        dialogue["status"] = "conflict"
    elif statuses.intersection({"unknown", "missing"}):
        dialogue["status"] = "incomplete"


def _dialogue_entity_ids(projection: Mapping) -> list[str]:
    extensions = projection.get("extensions")
    if not isinstance(extensions, Mapping):
        return []
    dialogue = extensions.get("dialogue")
    if not isinstance(dialogue, Mapping):
        return []
    resolution = dialogue.get("entity_resolution")
    if isinstance(resolution, Mapping):
        output = []
        speaker_resolution = resolution.get("speaker")
        if isinstance(speaker_resolution, Mapping):
            canonical_id = speaker_resolution.get("canonical_id")
            if isinstance(canonical_id, str) and canonical_id:
                output.append(canonical_id)
        addressees = resolution.get("addressees", [])
        if not isinstance(addressees, list):
            raise ContextBundleError(
                "dialogue entity_resolution.addressees must be an array"
            )
        for item in addressees:
            if not isinstance(item, Mapping):
                raise ContextBundleError(
                    "dialogue addressee resolution must be an object"
                )
            canonical_id = item.get("canonical_id")
            if (
                isinstance(canonical_id, str)
                and canonical_id
                and canonical_id not in output
            ):
                output.append(canonical_id)
        return output
    output = []
    speaker = dialogue.get("speaker_id")
    if isinstance(speaker, str) and speaker:
        output.append(speaker)
    raw_addressees = dialogue.get("addressee_ids", [])
    if isinstance(raw_addressees, str):
        raw_addressees = [raw_addressees]
    if isinstance(raw_addressees, list):
        for value in raw_addressees:
            if isinstance(value, str) and value and value not in output:
                output.append(value)
    return output


def _select_entity_records(
    projection: Mapping,
    assets: Mapping,
    view: Mapping,
) -> tuple[list[str], list[str]]:
    requested = _dialogue_entity_ids(projection)
    facts_per_entity = view["limits"]["max_facts_per_entity"]
    fact_ids = []
    if facts_per_entity:
        for entity_id in requested:
            if entity_id not in assets["entities"]:
                continue
            candidates = sorted(
                fact_id
                for fact_id, record in assets["facts"].items()
                if record["entity_id"] == entity_id
                and record["fact"]["verification_status"] in VERIFIED_STATUSES
            )
            fact_ids.extend(candidates[:facts_per_entity])
    relation_ids = []
    max_relations = view["limits"]["max_relations"]
    requested_set = set(requested)
    if max_relations and len(requested_set) >= 2:
        relation_ids = sorted(
            relation_id
            for relation_id, relation in assets["relations"].items()
            if relation["verification_status"] in VERIFIED_STATUSES
            and not (
                isinstance(relation.get("attributes"), Mapping)
                and relation["attributes"].get("runtime_rule") is False
            )
            and relation["from"] in requested_set
            and relation["to"] in requested_set
        )[:max_relations]
    return sorted(set(fact_ids)), relation_ids


def _content_type(projection: Mapping) -> str | None:
    core = projection.get("core")
    value = core.get("content_type") if isinstance(core, Mapping) else None
    return value if isinstance(value, str) and value else None


def _lexical_units(value: object) -> set[str]:
    if not isinstance(value, str):
        return set()
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if character.isalnum()
    )
    if not normalized:
        return set()
    if len(normalized) == 1:
        return {normalized}
    return {
        normalized[index : index + 2]
        for index in range(len(normalized) - 1)
    }


def _lexical_overlap(source: object, example_source: object) -> dict:
    source_units = _lexical_units(source)
    example_units = _lexical_units(example_source)
    intersection = len(source_units.intersection(example_units))
    union = len(source_units.union(example_units))
    score_ppm = (intersection * 1_000_000 // union) if union else 0
    return {
        "score_ppm": score_ppm,
        "intersection_count": intersection,
        "union_count": union,
        "source_unit_count": len(source_units),
        "example_unit_count": len(example_units),
    }


def _select_runtime_examples(
    segment: Mapping,
    projection: Mapping,
    assets: Mapping,
    view: Mapping,
) -> tuple[list[str], list[dict]]:
    limit = view["limits"]["max_runtime_examples"]
    if not limit or not view["dimensions"]:
        return [], []
    content_type = _content_type(projection)
    capabilities = set(view["capabilities"])
    dimensions = set(view["dimensions"])
    segment_key = segment.get("segment_key")
    if not isinstance(segment_key, str) or not segment_key:
        raise ContextBundleError(
            "runtime example selection requires segment.segment_key"
        )
    source = _source_text(segment)
    ranked = []
    for example_id in sorted(assets["review_examples"]):
        example = assets["review_examples"][example_id]
        if example["review_status"] != "reviewed":
            continue
        if "runtime_reference" not in example["uses"]:
            continue
        if not dimensions.intersection(example["dimensions"]):
            continue
        declared_capabilities = set(example.get("capabilities", []))
        if not declared_capabilities or not capabilities.intersection(
            declared_capabilities
        ):
            continue
        segment_match = (
            example["scope"] == "segment"
            and example.get("segment_key") == segment_key
        )
        if example["scope"] == "segment" and not segment_match:
            continue
        declared_types = set(example.get("content_types", []))
        if not segment_match and (
            not declared_types or content_type not in declared_types
        ):
            continue
        overlap = _lexical_overlap(source, example.get("source"))
        if segment_match:
            tier = 0
            reason = "exact_segment_key"
        elif overlap["score_ppm"] > 0:
            tier = 1
            reason = "lexical_overlap"
        else:
            tier = 2
            reason = "content_type_fallback"
        evidence = {
            "example_id": example_id,
            "scope": example["scope"],
            "selection_reason": reason,
            "segment_key_match": segment_match,
            "content_type": content_type,
            "matched_capabilities": sorted(
                capabilities.intersection(declared_capabilities)
            ),
            "matched_dimensions": sorted(
                dimensions.intersection(example["dimensions"])
            ),
            "lexical_overlap": overlap,
        }
        ranked.append(
            (
                tier,
                -overlap["score_ppm"],
                example_id,
                evidence,
            )
        )
    ranked.sort(key=lambda item: item[:3])
    selected_evidence = []
    for rank, item in enumerate(ranked[:limit], start=1):
        evidence = item[3]
        evidence["rank"] = rank
        selected_evidence.append(evidence)
    return (
        sorted(item["example_id"] for item in selected_evidence),
        selected_evidence,
    )


def _constraint_rule_ids(constraint: Mapping) -> list[str]:
    output = []
    singular = constraint.get("rule_id")
    if isinstance(singular, str) and singular:
        output.append(singular)
    plural = constraint.get("rule_ids", [])
    if isinstance(plural, str):
        plural = [plural]
    if plural not in (None, []) and not isinstance(plural, list):
        raise ContextBundleError("resolved constraint rule_ids must be an array")
    for item in plural or []:
        value = _nonempty_text(item, "resolved constraint rule id")
        if value not in output:
            output.append(value)
    return output


def _select_constraints(
    segment: Mapping,
    assets: Mapping,
    view: Mapping,
) -> tuple[list[dict], list[str]]:
    if not view["include_constraints"]:
        return [], []
    raw = segment.get("resolved_constraints", [])
    if not isinstance(raw, list):
        raise ContextBundleError("segment.resolved_constraints must be an array")
    allowed_kinds = set(view["constraint_kinds"])
    selected = []
    rule_ids = []
    for index, constraint in enumerate(raw):
        if not isinstance(constraint, Mapping):
            raise ContextBundleError(
                f"segment resolved constraint {index} must be an object"
            )
        kind = constraint.get("kind", constraint.get("capability"))
        if kind not in allowed_kinds:
            continue
        normalized = deepcopy(dict(constraint))
        for rule_id in _constraint_rule_ids(normalized):
            if rule_id not in assets["context_rules"]:
                raise ContextBundleError(
                    f"resolved constraint references unknown rule id {rule_id!r}"
                )
            if assets["context_rules"][rule_id].get("rule_status") != "confirmed":
                raise ContextBundleError(
                    f"resolved constraint references non-confirmed rule id {rule_id!r}"
                )
            if assets["context_rules"][rule_id].get("capability") != kind:
                raise ContextBundleError(
                    f"resolved constraint kind does not match rule id {rule_id!r}"
                )
            if rule_id not in rule_ids:
                rule_ids.append(rule_id)
        selected.append(normalized)
    selected.sort(key=lambda item: _canonical_bytes(item, label="constraint"))
    return selected, sorted(rule_ids)


def _source_text(segment: Mapping) -> str:
    value = segment.get("source", "")
    if not isinstance(value, str):
        raise ContextBundleError("segment.source must be a string")
    return value


def _current_target(segment: Mapping) -> str:
    for field in ("current_target", "corrected", "target"):
        value = segment.get(field)
        if isinstance(value, str):
            return value
        if value is not None:
            raise ContextBundleError(f"segment.{field} must be a string or null")
    return ""


def _protected(segment: Mapping) -> bool:
    value = segment.get("protected", False)
    if type(value) is not bool:
        raise ContextBundleError("segment.protected must be boolean")
    return value


def _target_status(segment: Mapping) -> str:
    original = segment.get("target")
    current = _current_target(segment)
    if not isinstance(original, str) or current != original:
        candidates = [segment.get("current_target_provenance_status")]
        provenance = segment.get("current_target_provenance")
    else:
        candidates = [segment.get("target_provenance_status")]
        provenance = segment.get("target_provenance")
    if isinstance(provenance, Mapping):
        candidates.append(provenance.get("status"))
    return "verified" if any(item in VERIFIED_STATUSES for item in candidates) else "unverified"


def _neighbor_projection(
    segment: Mapping,
    projection_modules: Sequence[str],
    registry: Mapping[str, dict],
    view: Mapping,
    assets: Mapping,
) -> dict:
    include_target = view["neighbors"]["include_target"]
    return {
        "id": deepcopy(segment["id"]),
        "segment_key": segment["segment_key"],
        "source": _source_text(segment),
        "input_status": "ready",
        "protected": False,
        "context": _context_projection(
            segment, projection_modules, registry, view, assets
        ),
        "target": _current_target(segment) if include_target else None,
        "target_status": _target_status(segment) if include_target else "omitted",
    }


def _segment_boundary(segment: Mapping) -> dict[str, str]:
    context = _context_state(segment)
    containers: list[tuple[str, Mapping]] = [("segment", segment)]
    core = context.get("core")
    if isinstance(core, Mapping):
        containers.append(("context.core", core))
    extensions = context.get("extensions")
    if isinstance(extensions, Mapping):
        for extension_name in sorted(extensions):
            extension = extensions[extension_name]
            if isinstance(extension, Mapping):
                containers.append(
                    (f"context.extensions.{extension_name}", extension)
                )

    output = {}
    for field_name in ("scene_id", "group_id"):
        found = []
        for path, container in containers:
            raw = container.get(field_name)
            if raw in (None, ""):
                continue
            if not isinstance(raw, str) or not raw.strip():
                raise ContextBundleError(
                    f"{path}.{field_name} must be a non-empty string"
                )
            value = raw.strip()
            if value not in [item[1] for item in found]:
                found.append((path, value))
        if len(found) > 1:
            raise ContextBundleError(
                f"segment has conflicting {field_name} boundaries: {found}"
            )
        if found:
            output[field_name] = found[0][1]
    return output


def _neighbor_selection(
    *,
    boundary_mode: str,
    boundary_status: str,
    boundary: Mapping[str, str],
    scanned_count: int,
    selected_count: int,
) -> dict:
    return {
        "boundary_mode": boundary_mode,
        "boundary_status": boundary_status,
        "boundary": deepcopy(dict(boundary)),
        "scanned_count": scanned_count,
        "selected_count": selected_count,
    }


def _select_neighbors(
    state: Mapping,
    segment: Mapping,
    projection_modules: Sequence[str],
    registry: Mapping[str, dict],
    view: Mapping,
    assets: Mapping,
) -> tuple[list[dict], dict]:
    boundary_mode = view["neighbors"]["boundary_mode"]
    if segment.get("input_status", "ready") == "blocked" or _protected(segment):
        return [], _neighbor_selection(
            boundary_mode=boundary_mode,
            boundary_status="disabled",
            boundary={},
            scanned_count=0,
            selected_count=0,
        )
    before = view["neighbors"]["before"]
    after = view["neighbors"]["after"]
    if before == 0 and after == 0:
        return [], _neighbor_selection(
            boundary_mode=boundary_mode,
            boundary_status="disabled",
            boundary={},
            scanned_count=0,
            selected_count=0,
        )
    boundary = _segment_boundary(segment)
    if not boundary and boundary_mode == "strict":
        return [], _neighbor_selection(
            boundary_mode=boundary_mode,
            boundary_status="strict_missing_boundary",
            boundary={},
            scanned_count=0,
            selected_count=0,
        )
    segments = state.get("segments")
    if not isinstance(segments, list):
        raise ContextBundleError("state.segments must be an array")
    key = segment.get("segment_key")
    matches = [
        index
        for index, item in enumerate(segments)
        if isinstance(item, Mapping) and item.get("segment_key") == key
    ]
    if len(matches) != 1:
        raise ContextBundleError(
            f"segment_key {key!r} does not identify exactly one state segment"
        )
    position = matches[0]
    candidates = segments[max(0, position - before) : position] + segments[
        position + 1 : position + 1 + after
    ]
    output = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise ContextBundleError("state segment must be an object")
        if candidate.get("input_status", "ready") == "blocked" or _protected(
            candidate
        ):
            continue
        if boundary:
            candidate_boundary = _segment_boundary(candidate)
            if any(
                candidate_boundary.get(field_name) != value
                for field_name, value in boundary.items()
            ):
                continue
        if "id" not in candidate or not isinstance(
            candidate.get("segment_key"), str
        ):
            raise ContextBundleError("neighbor segment lacks stable identity")
        _segment_identity(candidate)
        output.append(
            _neighbor_projection(
                candidate,
                projection_modules,
                registry,
                view,
                assets,
            )
        )
    return output, _neighbor_selection(
        boundary_mode=boundary_mode,
        boundary_status="bounded" if boundary else "fallback_no_boundary",
        boundary=boundary,
        scanned_count=len(candidates),
        selected_count=len(output),
    )


def _context_status(
    segment: Mapping,
    context: Mapping,
    neighbor_selection: Mapping | None = None,
) -> str:
    if segment.get("input_status", "ready") == "blocked":
        return "blocked"
    if _protected(segment):
        return "protected"
    if (
        isinstance(neighbor_selection, Mapping)
        and neighbor_selection.get("boundary_status")
        == "strict_missing_boundary"
    ):
        return "incomplete"
    extensions = context.get("extensions")
    if isinstance(extensions, Mapping):
        statuses = {
            item.get("status")
            for item in extensions.values()
            if isinstance(item, Mapping)
        }
        if "conflict" in statuses:
            return "conflict"
        if "incomplete" in statuses:
            return "incomplete"
        if "disabled" in statuses:
            return "disabled"
    raw = context.get("status")
    mapping = {
        "ready": "ready",
        "context_incomplete": "incomplete",
        "incomplete": "incomplete",
        "conflict": "conflict",
        "disabled": "disabled",
    }
    if raw in mapping:
        return mapping[raw]
    return "ready" if context else "unknown"


def _segment_identity(segment: Mapping) -> dict:
    if "id" not in segment or type(segment["id"]) not in (int, str):
        raise ContextBundleError("segment.id must be an integer or string")
    if isinstance(segment["id"], str) and not segment["id"]:
        raise ContextBundleError("segment.id must not be empty")
    segment_key = _nonempty_text(segment.get("segment_key"), "segment.segment_key")
    key_origin = segment.get("key_origin", "generated")
    if key_origin not in {"input", "generated"}:
        raise ContextBundleError("segment.key_origin must be input or generated")
    source = _source_text(segment)
    recorded_source_digest = _digest(
        segment.get("source_digest"), "segment.source_digest"
    )
    if source_digest(source) != recorded_source_digest:
        raise ContextBundleError("segment.source_digest does not match source")
    return {
        "id": deepcopy(segment["id"]),
        "segment_key": segment_key,
        "key_origin": key_origin,
        "source_digest": recorded_source_digest,
    }


def validate_context_bundle(value: object) -> dict:
    try:
        document = validate_json_schema(
            value, _load_schema(CONTEXT_BUNDLE_SCHEMA_PATH)
        )
    except ProfileIngestError as exc:
        raise ContextBundleError(str(exc)) from exc
    if document["context_bundle_digest"] != _digest_without(
        document, "context_bundle_digest"
    ):
        raise ContextBundleError("context bundle digest mismatch")
    segment = document["segment"]
    if source_digest(segment["source"]) != segment["identity"]["source_digest"]:
        raise ContextBundleError("context bundle source digest mismatch")
    if document["term_evidence_ids"]:
        raise ContextBundleError(
            "term evidence IDs require a bound shared term-evidence asset"
        )
    projection_modules = _string_list(
        document["projection_modules"], "projection_modules"
    )
    if document["projection_modules"] != projection_modules:
        raise ContextBundleError(
            "context bundle projection_modules is not canonical"
        )
    if document["module"] not in projection_modules:
        raise ContextBundleError(
            "context bundle projection_modules omits its module"
        )
    for field in (
        "entity_fact_ids",
        "relation_ids",
        "runtime_example_ids",
        "term_evidence_ids",
    ):
        if document[field] != _string_list(document[field], field):
            raise ContextBundleError(f"context bundle {field} is not canonical")
    selection = document["runtime_example_selection"]
    selected_ids = [item["example_id"] for item in selection]
    if sorted(selected_ids) != document["runtime_example_ids"]:
        raise ContextBundleError(
            "runtime example selection differs from selected example IDs"
        )
    if len(selected_ids) != len(set(selected_ids)):
        raise ContextBundleError(
            "runtime example selection contains duplicate example IDs"
        )
    if [item["rank"] for item in selection] != list(
        range(1, len(selection) + 1)
    ):
        raise ContextBundleError(
            "runtime example selection ranks are not canonical"
        )
    for item in selection:
        overlap = item["lexical_overlap"]
        if overlap["intersection_count"] > overlap["union_count"]:
            raise ContextBundleError(
                "runtime example lexical overlap counts are invalid"
            )
        expected_score = (
            overlap["intersection_count"] * 1_000_000
            // overlap["union_count"]
            if overlap["union_count"]
            else 0
        )
        if overlap["score_ppm"] != expected_score:
            raise ContextBundleError(
                "runtime example lexical overlap score is invalid"
            )
        if (
            item["selection_reason"] == "exact_segment_key"
            and not item["segment_key_match"]
        ):
            raise ContextBundleError(
                "exact segment example lacks a segment key match"
            )
        if (
            item["selection_reason"] != "exact_segment_key"
            and item["segment_key_match"]
        ):
            raise ContextBundleError(
                "segment key match lacks exact-segment priority"
            )
    for field in ("capabilities", "dimensions", "constraint_kinds"):
        values = document["module_view"][field]
        if values != _string_list(values, f"module_view.{field}"):
            raise ContextBundleError(
                f"context bundle module_view.{field} is not canonical"
            )
    for neighbor in document["neighbors"]:
        if neighbor["input_status"] == "blocked" or neighbor["protected"]:
            raise ContextBundleError("blocked/protected segment appears as neighbor")
        if neighbor["target_status"] == "omitted" and neighbor["target"] is not None:
            raise ContextBundleError("omitted neighbor target must be null")
        if neighbor["target_status"] != "omitted" and neighbor["target"] is None:
            raise ContextBundleError("included neighbor target must be a string")
    neighbor_selection = document["neighbor_selection"]
    if neighbor_selection["selected_count"] != len(document["neighbors"]):
        raise ContextBundleError(
            "neighbor selection count differs from context bundle neighbors"
        )
    if neighbor_selection["selected_count"] > neighbor_selection["scanned_count"]:
        raise ContextBundleError(
            "neighbor selection count exceeds the scanned window"
        )
    if (
        neighbor_selection["boundary_status"] == "strict_missing_boundary"
        and neighbor_selection["boundary_mode"] != "strict"
    ):
        raise ContextBundleError(
            "strict missing-boundary status requires strict boundary mode"
        )
    if (
        neighbor_selection["boundary_status"] == "fallback_no_boundary"
        and neighbor_selection["boundary_mode"] != "same_if_present"
    ):
        raise ContextBundleError(
            "fallback status requires same_if_present boundary mode"
        )
    if (
        neighbor_selection["boundary_status"] == "bounded"
        and not neighbor_selection["boundary"]
    ):
        raise ContextBundleError("bounded neighbor selection lacks a boundary")
    if (
        neighbor_selection["boundary_status"] != "bounded"
        and neighbor_selection["boundary"]
    ):
        raise ContextBundleError(
            "unbounded neighbor selection unexpectedly declares a boundary"
        )
    return document


def build_context_bundle(
    state: Mapping,
    segment: Mapping,
    module: str,
    *,
    loaded_assets: Mapping | None = None,
    module_view: Mapping | None = None,
    projection_modules: Sequence[str] | None = None,
) -> dict:
    """Build one module-scoped canonical bundle from state and bound assets."""

    snapshot, resolution = _validate_state_bindings(state)
    registry = _active_context_registry(state)
    assets = (
        load_project_context_assets(state)
        if loaded_assets is None
        else validate_loaded_project_context_assets(loaded_assets)
    )
    if assets["asset_snapshot_digest"] != snapshot["digest"]:
        raise ContextBundleError("loaded assets belong to another asset snapshot")
    if assets["capability_resolution_digest"] != resolution["digest"]:
        raise ContextBundleError(
            "loaded assets belong to another capability resolution"
        )
    formal_assets = _formal_project_context_assets(assets, resolution, snapshot)
    if not isinstance(segment, Mapping):
        raise ContextBundleError("segment must be an object")
    module = _nonempty_text(module, "module")
    normalized_projection_modules = _normalize_projection_modules(
        module, projection_modules
    )
    view = normalize_module_view(state, module, registry, module_view)
    projection = _context_projection(
        segment,
        normalized_projection_modules,
        registry,
        view,
        formal_assets,
    )
    fact_ids, relation_ids = _select_entity_records(
        projection, formal_assets, view
    )
    example_ids, example_selection = _select_runtime_examples(
        segment, projection, formal_assets, view
    )
    constraints, _ = _select_constraints(segment, formal_assets, view)
    identity = _segment_identity(segment)
    segment_revision_digest = _digest(
        segment.get("segment_revision_digest"),
        "segment.segment_revision_digest",
    )
    input_status = segment.get("input_status", "ready")
    if input_status not in {"ready", "blocked"}:
        raise ContextBundleError("segment.input_status must be ready or blocked")
    protection_reason = segment.get("protected_reason")
    if protection_reason is not None and not isinstance(protection_reason, str):
        raise ContextBundleError("segment.protected_reason must be string or null")
    protected_texts = segment.get("protected_texts", [])
    if not isinstance(protected_texts, list) or any(
        not isinstance(item, str) for item in protected_texts
    ):
        raise ContextBundleError("segment.protected_texts must be a string array")
    neighbors, neighbor_selection = _select_neighbors(
        state,
        segment,
        normalized_projection_modules,
        registry,
        view,
        formal_assets,
    )
    bundle = {
        "schema": CONTEXT_BUNDLE_SCHEMA,
        "version": CONTEXT_BUNDLE_VERSION,
        "module": module,
        "projection_modules": normalized_projection_modules,
        "segment_revision_digest": segment_revision_digest,
        "project_asset_snapshot_digest": snapshot["digest"],
        "capability_resolution_digest": resolution["digest"],
        "module_view": view,
        "segment": {
            "identity": identity,
            "source": _source_text(segment),
            "current_target": _current_target(segment),
            "input_status": input_status,
            "input_block_reasons": deepcopy(
                segment.get("input_block_reasons", [])
            ),
            "input_warnings": deepcopy(segment.get("input_warnings", [])),
            "context": projection,
            "protection": {
                "protected": _protected(segment),
                "reason": protection_reason,
                "protected_texts": deepcopy(protected_texts),
            },
        },
        "entity_fact_ids": fact_ids,
        "relation_ids": relation_ids,
        "resolved_constraints": constraints,
        "neighbors": neighbors,
        "neighbor_selection": neighbor_selection,
        "runtime_example_ids": example_ids,
        "runtime_example_selection": example_selection,
        "term_evidence_ids": [],
        "context_status": _context_status(
            segment, projection, neighbor_selection
        ),
    }
    bundle["context_bundle_digest"] = canonical_digest(bundle)
    return validate_context_bundle(bundle)


def _referenced_rule_ids(bundle: Mapping) -> set[str]:
    output = set()
    for constraint in bundle["resolved_constraints"]:
        output.update(_constraint_rule_ids(constraint))
    return output


def validate_shared_context_assets(
    value: object,
    bundles: Sequence[Mapping],
) -> dict:
    if not isinstance(value, Mapping) or set(value) != {
        "entities",
        "relations",
        "review_examples",
        "constraints",
    }:
        raise ContextBundleError(
            "shared_context_assets must contain exactly entities, relations, "
            "review_examples, constraints"
        )
    normalized = {}
    for field in ("entities", "relations", "review_examples", "constraints"):
        raw = value[field]
        if not isinstance(raw, Mapping):
            raise ContextBundleError(f"shared_context_assets.{field} must be an object")
        normalized[field] = {key: deepcopy(raw[key]) for key in sorted(raw)}
    expected = {
        "entities": set(),
        "relations": set(),
        "review_examples": set(),
        "constraints": set(),
    }
    for raw_bundle in bundles:
        bundle = validate_context_bundle(raw_bundle)
        expected["entities"].update(bundle["entity_fact_ids"])
        expected["relations"].update(bundle["relation_ids"])
        expected["review_examples"].update(bundle["runtime_example_ids"])
        expected["constraints"].update(_referenced_rule_ids(bundle))
    for field, identifiers in expected.items():
        actual = set(normalized[field])
        if actual != identifiers:
            raise ContextBundleError(
                f"shared_context_assets.{field} differs from bundle references: "
                f"missing={sorted(identifiers - actual)}, extra={sorted(actual - identifiers)}"
            )
    bundle_entities: dict[str, set[str]] = {}
    bundle_examples: dict[
        str, tuple[str, str | None, set[str], set[str]]
    ] = {}
    for raw_bundle in bundles:
        bundle = validate_context_bundle(raw_bundle)
        bundle_key = bundle["context_bundle_digest"]
        projection = bundle["segment"]["context"]
        bundle_entities[bundle_key] = set(_dialogue_entity_ids(projection))
        bundle_examples[bundle_key] = (
            bundle["segment"]["identity"]["segment_key"],
            _content_type(projection),
            set(bundle["module_view"]["capabilities"]),
            set(bundle["module_view"]["dimensions"]),
        )
    for fact_id, record in normalized["entities"].items():
        if not isinstance(record, Mapping) or record.get("fact", {}).get("id") != fact_id:
            raise ContextBundleError(f"shared entity fact key mismatch: {fact_id}")
        if record["fact"].get("verification_status") not in VERIFIED_STATUSES:
            raise ContextBundleError(f"unverified entity fact was shared: {fact_id}")
        referring = [
            bundle
            for bundle in bundles
            if fact_id in bundle["entity_fact_ids"]
        ]
        if any(
            record.get("entity_id")
            not in bundle_entities[bundle["context_bundle_digest"]]
            for bundle in referring
        ):
            raise ContextBundleError(
                f"shared entity fact does not exactly match requested entity: {fact_id}"
            )
    for relation_id, relation in normalized["relations"].items():
        if not isinstance(relation, Mapping) or relation.get("id") != relation_id:
            raise ContextBundleError(f"shared relation key mismatch: {relation_id}")
        if relation.get("verification_status") not in VERIFIED_STATUSES:
            raise ContextBundleError(f"unverified relation was shared: {relation_id}")
        referring = [
            bundle for bundle in bundles if relation_id in bundle["relation_ids"]
        ]
        if any(
            relation.get("from")
            not in bundle_entities[bundle["context_bundle_digest"]]
            or relation.get("to")
            not in bundle_entities[bundle["context_bundle_digest"]]
            for bundle in referring
        ):
            raise ContextBundleError(
                f"shared relation does not exactly match requested entities: {relation_id}"
            )
    for example_id, example in normalized["review_examples"].items():
        if not isinstance(example, Mapping) or example.get("id") != example_id:
            raise ContextBundleError(f"shared review example key mismatch: {example_id}")
        if (
            example.get("review_status") != "reviewed"
            or "runtime_reference" not in example.get("uses", [])
        ):
            raise ContextBundleError(f"non-runtime reviewed example was shared: {example_id}")
        declared_types = set(example.get("content_types", []))
        declared_capabilities = set(example.get("capabilities", []))
        declared_dimensions = set(example.get("dimensions", []))
        if (
            not declared_capabilities
            or not declared_dimensions
            or (example.get("scope") != "segment" and not declared_types)
        ):
            raise ContextBundleError(
                f"runtime example lacks explicit matching metadata: {example_id}"
            )
        referring = [
            bundle
            for bundle in bundles
            if example_id in bundle["runtime_example_ids"]
        ]
        if any(
            (
                example.get("scope") == "segment"
                and example.get("segment_key") != segment_key
            )
            or (
                example.get("scope") != "segment"
                and content_type not in declared_types
            )
            or not capabilities.intersection(declared_capabilities)
            or not dimensions.intersection(declared_dimensions)
            for segment_key, content_type, capabilities, dimensions in (
                bundle_examples[bundle["context_bundle_digest"]]
                for bundle in referring
            )
        ):
            raise ContextBundleError(
                f"runtime example does not explicitly match its bundle: {example_id}"
            )
    for rule_id, rule in normalized["constraints"].items():
        if not isinstance(rule, Mapping) or rule.get("id") != rule_id:
            raise ContextBundleError(f"shared constraint key mismatch: {rule_id}")
        if rule.get("rule_status") != "confirmed":
            raise ContextBundleError(
                f"non-confirmed constraint was shared: {rule_id}"
            )
    return normalized


def _shared_context_assets(
    bundles: Sequence[Mapping], assets: Mapping
) -> dict:
    fact_ids = sorted(
        {item for bundle in bundles for item in bundle["entity_fact_ids"]}
    )
    relation_ids = sorted(
        {item for bundle in bundles for item in bundle["relation_ids"]}
    )
    example_ids = sorted(
        {item for bundle in bundles for item in bundle["runtime_example_ids"]}
    )
    rule_ids = sorted(
        {item for bundle in bundles for item in _referenced_rule_ids(bundle)}
    )
    missing = {
        "entities": [item for item in fact_ids if item not in assets["facts"]],
        "relations": [item for item in relation_ids if item not in assets["relations"]],
        "review_examples": [item for item in example_ids if item not in assets["review_examples"]],
        "constraints": [item for item in rule_ids if item not in assets["context_rules"]],
    }
    if any(missing.values()):
        raise ContextBundleError(f"bundle references unresolved shared assets: {missing}")
    shared = {
        "entities": {item: deepcopy(assets["facts"][item]) for item in fact_ids},
        "relations": {item: deepcopy(assets["relations"][item]) for item in relation_ids},
        "review_examples": {
            item: deepcopy(assets["review_examples"][item]) for item in example_ids
        },
        "constraints": {
            item: deepcopy(assets["context_rules"][item]) for item in rule_ids
        },
    }
    return validate_shared_context_assets(shared, bundles)


def validate_context_bundle_set(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise ContextBundleError("context bundle set must be an object")
    required = {
        "schema",
        "version",
        "module",
        "projection_modules",
        "capabilities",
        "shared_context_assets",
        "bundles",
        "context_bundle_set_digest",
    }
    if set(value) != required:
        raise ContextBundleError(
            f"context bundle set fields differ from contract: {sorted(set(value) ^ required)}"
        )
    if value["schema"] != "lqe.context-bundle-set" or value["version"] != 1:
        raise ContextBundleError("unsupported context bundle set schema/version")
    module = _nonempty_text(value["module"], "context bundle set module")
    projection_modules = _string_list(
        value["projection_modules"], "context bundle set projection_modules"
    )
    if value["projection_modules"] != projection_modules:
        raise ContextBundleError(
            "context bundle set projection_modules is not canonical"
        )
    if module not in projection_modules:
        raise ContextBundleError(
            "context bundle set projection_modules omits its module"
        )
    bundles = value["bundles"]
    if not isinstance(bundles, list):
        raise ContextBundleError("context bundle set bundles must be an array")
    seen_keys = set()
    bundle_order = []
    for raw_bundle in bundles:
        bundle = validate_context_bundle(raw_bundle)
        if bundle["module"] != module:
            raise ContextBundleError("context bundle set contains another module")
        if bundle["projection_modules"] != projection_modules:
            raise ContextBundleError(
                "context bundle set contains another projection module set"
            )
        key = bundle["segment"]["identity"]["segment_key"]
        if key in seen_keys:
            raise ContextBundleError(f"duplicate context bundle segment_key: {key}")
        seen_keys.add(key)
        bundle_order.append(
            (
                str(bundle["segment"]["identity"]["id"]),
                key,
                bundle["context_bundle_digest"],
            )
        )
    if bundle_order != sorted(bundle_order):
        raise ContextBundleError("context bundle set bundles are not canonical")
    validate_shared_context_assets(value["shared_context_assets"], bundles)
    capabilities = value["capabilities"]
    if not isinstance(capabilities, Mapping) or set(capabilities) != {
        "enabled",
        "disabled",
    }:
        raise ContextBundleError("context bundle set capability summary is invalid")
    for field in ("enabled", "disabled"):
        if capabilities[field] != _string_list(
            capabilities[field], f"capabilities.{field}"
        ):
            raise ContextBundleError(
                f"context bundle set capabilities.{field} is not canonical"
            )
    if value["context_bundle_set_digest"] != _digest_without(
        value, "context_bundle_set_digest"
    ):
        raise ContextBundleError("context bundle set digest mismatch")
    return deepcopy(dict(value))


def build_context_bundle_set(
    state: Mapping,
    segments: Sequence[Mapping],
    module: str,
    *,
    loaded_assets: Mapping | None = None,
    module_view: Mapping | None = None,
    projection_modules: Sequence[str] | None = None,
) -> dict:
    if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes)):
        raise ContextBundleError("segments must be an array")
    snapshot, resolution = _validate_state_bindings(state)
    assets = (
        load_project_context_assets(state)
        if loaded_assets is None
        else validate_loaded_project_context_assets(loaded_assets)
    )
    if assets["asset_snapshot_digest"] != snapshot["digest"]:
        raise ContextBundleError("loaded assets belong to another asset snapshot")
    if assets["capability_resolution_digest"] != resolution["digest"]:
        raise ContextBundleError(
            "loaded assets belong to another capability resolution"
        )
    module = _nonempty_text(module, "module")
    normalized_projection_modules = _normalize_projection_modules(
        module, projection_modules
    )
    bundles = [
        build_context_bundle(
            state,
            segment,
            module,
            loaded_assets=assets,
            module_view=module_view,
            projection_modules=normalized_projection_modules,
        )
        for segment in segments
    ]
    bundles.sort(
        key=lambda item: (
            str(item["segment"]["identity"]["id"]),
            item["segment"]["identity"]["segment_key"],
        )
    )
    output = {
        "schema": "lqe.context-bundle-set",
        "version": 1,
        "module": module,
        "projection_modules": normalized_projection_modules,
        "capabilities": _formal_capability_summary(resolution),
        "shared_context_assets": _shared_context_assets(bundles, assets),
        "bundles": bundles,
    }
    output["context_bundle_set_digest"] = canonical_digest(output)
    return validate_context_bundle_set(output)


def _selected_evidence_entry(bundle: Mapping) -> dict:
    identity = bundle["segment"]["identity"]
    neighbors = []
    for ordinal, neighbor in enumerate(bundle["neighbors"]):
        target = neighbor["target"]
        neighbors.append(
            {
                "ordinal": ordinal,
                "id": deepcopy(neighbor["id"]),
                "segment_key": neighbor["segment_key"],
                "source_digest": source_digest(neighbor["source"]),
                "context_digest": canonical_digest(neighbor["context"]),
                "target_digest": (
                    source_digest(target) if isinstance(target, str) else None
                ),
                "target_status": neighbor["target_status"],
            }
        )
    entry = {
        "module": bundle["module"],
        "segment_id": deepcopy(identity["id"]),
        "segment_key": identity["segment_key"],
        "source_digest": identity["source_digest"],
        "segment_revision_digest": bundle["segment_revision_digest"],
        "context_bundle_digest": bundle["context_bundle_digest"],
        "context_status": bundle["context_status"],
        "module_view_digest": canonical_digest(bundle["module_view"]),
        "context_projection_digest": canonical_digest(
            bundle["segment"]["context"]
        ),
        "entity_fact_ids": list(bundle["entity_fact_ids"]),
        "relation_ids": list(bundle["relation_ids"]),
        "runtime_example_ids": list(bundle["runtime_example_ids"]),
        "term_evidence_ids": list(bundle["term_evidence_ids"]),
        "resolved_constraint_digests": sorted(
            {canonical_digest(item) for item in bundle["resolved_constraints"]}
        ),
        "neighbors": neighbors,
        "neighbor_selection_digest": canonical_digest(
            bundle["neighbor_selection"]
        ),
        "runtime_example_selection_digest": canonical_digest(
            bundle["runtime_example_selection"]
        ),
    }
    entry["selection_digest"] = canonical_digest(entry)
    return entry


def validate_selected_context_evidence_index(value: object) -> dict:
    try:
        document = validate_json_schema(
            value,
            _load_schema(SELECTED_CONTEXT_EVIDENCE_INDEX_SCHEMA_PATH),
        )
    except ProfileIngestError as exc:
        raise ContextBundleError(str(exc)) from exc
    if document["index_digest"] != _digest_without(document, "index_digest"):
        raise ContextBundleError("selected evidence index digest mismatch")
    modules = _string_list(document["modules"], "selected evidence modules")
    if document["modules"] != modules:
        raise ContextBundleError("selected evidence modules are not canonical")
    previous = None
    seen = set()
    for entry in document["entries"]:
        order_key = (
            entry["module"],
            entry["segment_key"],
            _canonical_bytes(entry["segment_id"]),
        )
        if previous is not None and order_key <= previous:
            raise ContextBundleError("selected evidence entries are not canonical")
        previous = order_key
        identity = (entry["module"], entry["segment_key"])
        if identity in seen:
            raise ContextBundleError("selected evidence entry is duplicated")
        seen.add(identity)
        if entry["module"] not in modules:
            raise ContextBundleError("selected evidence entry has an unknown module")
        for field in (
            "entity_fact_ids",
            "relation_ids",
            "runtime_example_ids",
            "term_evidence_ids",
            "resolved_constraint_digests",
        ):
            if entry[field] != _string_list(entry[field], f"selected evidence {field}"):
                raise ContextBundleError(
                    f"selected evidence {field} is not canonical"
                )
        for ordinal, neighbor in enumerate(entry["neighbors"]):
            if neighbor["ordinal"] != ordinal:
                raise ContextBundleError(
                    "selected evidence neighbor ordinals are not canonical"
                )
        if entry["selection_digest"] != _digest_without(
            entry,
            "selection_digest",
        ):
            raise ContextBundleError("selected evidence entry digest mismatch")
    return document


def build_selected_context_evidence_index(
    state: Mapping,
    *,
    split_fingerprint: object,
    split_manifest_digest: object,
    modules: Sequence[str],
    context_bundle_sets: Sequence[Mapping],
) -> dict:
    snapshot, resolution = _validate_state_bindings(state)
    profile_digest = _digest(state.get("profile_digest"), "state.profile_digest")
    overlay = state.get("profile_overlay_digest")
    if overlay is not None:
        overlay = _digest(overlay, "state.profile_overlay_digest")
    normalized_modules = _string_list(list(modules), "selected evidence modules")
    entries = []
    for raw_bundle_set in context_bundle_sets:
        bundle_set = validate_context_bundle_set(raw_bundle_set)
        if bundle_set["module"] not in normalized_modules:
            raise ContextBundleError(
                "context bundle set module is absent from selected evidence modules"
            )
        for bundle in bundle_set["bundles"]:
            if bundle["project_asset_snapshot_digest"] != snapshot["digest"]:
                raise ContextBundleError(
                    "selected evidence bundle belongs to another asset snapshot"
                )
            if bundle["capability_resolution_digest"] != resolution["digest"]:
                raise ContextBundleError(
                    "selected evidence bundle belongs to another capability resolution"
                )
            entries.append(_selected_evidence_entry(bundle))
    entries.sort(
        key=lambda item: (
            item["module"],
            item["segment_key"],
            _canonical_bytes(item["segment_id"]),
        )
    )
    output = {
        "schema": SELECTED_CONTEXT_EVIDENCE_INDEX_SCHEMA,
        "version": SELECTED_CONTEXT_EVIDENCE_INDEX_VERSION,
        "split_fingerprint": _nonempty_text(
            split_fingerprint,
            "split_fingerprint",
        ),
        "split_manifest_digest": _digest(
            split_manifest_digest,
            "split_manifest_digest",
        ),
        "profile": {"digest": profile_digest, "overlay_digest": overlay},
        "project_asset_snapshot": {"digest": snapshot["digest"]},
        "capability_resolution": {"digest": resolution["digest"]},
        "modules": normalized_modules,
        "entries": entries,
    }
    output["index_digest"] = canonical_digest(output)
    return validate_selected_context_evidence_index(output)


def load_selected_context_evidence_index(path_value: object) -> dict:
    _, payload = _regular_file_bytes(
        path_value,
        label="selected context evidence index",
    )
    return validate_selected_context_evidence_index(
        _decode_json(payload, label="selected context evidence index")
    )


def _component_bytes(value: object) -> int:
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return len(_canonical_bytes(value, label="worker input component"))


def calculate_worker_input_bytes(components: Mapping | Sequence) -> int:
    """Measure complete UTF-8/canonical JSON components without truncation."""

    if isinstance(components, Mapping):
        values = components.values()
    elif isinstance(components, Sequence) and not isinstance(
        components, (str, bytes)
    ):
        values = components
    else:
        raise ContextBundleError("worker input components must be an object or array")
    return sum(_component_bytes(value) for value in values)


def enforce_worker_byte_budget(
    components: Mapping | Sequence, max_bytes: int | None = None
) -> int:
    """Compatibility shim that measures input; ``max_bytes`` is advisory only."""

    _ = max_bytes
    return calculate_worker_input_bytes(components)


def _document_summary(
    identifier: str,
    path_value: object,
    *,
    job_root: object | None = None,
) -> tuple[dict, bytes, Path]:
    path, payload = _regular_file_bytes(path_value, label=identifier)
    schema_name = None
    version = None
    if path.suffix.casefold() == ".json":
        value = _decode_json(payload, label=identifier)
        if not isinstance(value, Mapping):
            raise ContextBundleError(f"{identifier} JSON must be an object")
        schema_name = value.get("schema") if isinstance(value.get("schema"), str) else None
        version = value.get("version")
        if version is not None and type(version) not in (int, str):
            raise ContextBundleError(f"{identifier} version must be integer/string/null")
        if schema_name == "lqe.project-source-manifest":
            try:
                value = load_project_source_manifest(path)
            except ProfileIngestError as exc:
                raise ContextBundleError(str(exc)) from exc
    summary = {
        "id": identifier,
        "schema": schema_name,
        "version": version,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "locator": _path_locator(path, payload, job_root=job_root),
    }
    return summary, payload, path


def _source_manifest_summary(
    identifier: str,
    path_value: object,
) -> tuple[dict, Path]:
    path, payload = _regular_file_bytes(path_value, label=identifier)
    value = _decode_json(payload, label=identifier)
    if not isinstance(value, Mapping):
        raise ContextBundleError(f"{identifier} JSON must be an object")
    schema_name = value.get("schema") if isinstance(value.get("schema"), str) else None
    version = value.get("version")
    if version is not None and type(version) not in (int, str):
        raise ContextBundleError(f"{identifier} version must be integer/string/null")
    if schema_name == "lqe.project-source-manifest":
        try:
            document = load_project_source_manifest(path)
        except ProfileIngestError as exc:
            raise ContextBundleError(str(exc)) from exc
        coverage = document["coverage"]
        projection = {
            "kind": "project_source_manifest",
            "project": document["project"],
            "manifest_scope": document["manifest_scope"],
            "manifest_digest": document["manifest_digest"],
            "coverage": {
                field: coverage[field]
                for field in (
                    "total_nonempty",
                    "converted",
                    "normalized",
                    "ignored",
                    "unmapped",
                )
            },
            "sources": [
                {
                    "id": source["id"],
                    "kind": source["kind"],
                    "authority": {
                        "issuer": source["authority"]["issuer"],
                    },
                    "availability": source["availability"],
                }
                for source in sorted(document["sources"], key=lambda item: item["id"])
            ],
            "generated_assets": [
                {
                    "id": asset["asset_id"],
                    "kind": asset["kind"],
                    "distribution": asset["distribution"],
                    "generator": deepcopy(asset.get("generator")),
                }
                for asset in sorted(
                    document["generated_assets"],
                    key=lambda item: item["asset_id"],
                )
            ],
        }
    else:
        projection = {
            "kind": "bound_source_manifest",
            "schema": schema_name,
            "version": version,
        }
    summary = {
        "id": identifier,
        "schema": schema_name,
        "version": version,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "projection": projection,
        "projection_digest": canonical_digest(projection),
        "projection_bytes": _component_bytes(projection),
    }
    return summary, path


def _source_manifest_summaries(
    state: Mapping,
    explicit: Mapping[str, object] | None,
    snapshot: Mapping,
) -> list[dict]:
    paths = {}
    for field in _SOURCE_MANIFEST_FIELDS:
        value = state.get(field)
        if isinstance(value, str) and value.strip():
            paths[field] = value
    raw_asset_paths = state.get("project_asset_paths")
    if not isinstance(raw_asset_paths, Mapping):
        raise ContextBundleError("state.project_asset_paths must be an object")
    for asset_id, entry in sorted(snapshot["assets"].items()):
        if (
            entry.get("kind") != "project_source_manifest"
            or entry.get("status") != "present"
        ):
            continue
        path_value = raw_asset_paths.get(asset_id)
        if path_value is None:
            raise ContextBundleError(
                f"present project source manifest {asset_id!r} has no runtime path"
            )
        if path_value not in paths.values():
            paths[f"project_asset:{asset_id}"] = path_value
    raw_extra = state.get("source_manifest_paths", {})
    if raw_extra not in (None, {}):
        if not isinstance(raw_extra, Mapping):
            raise ContextBundleError("state.source_manifest_paths must be an object")
        for key, value in raw_extra.items():
            identifier = _nonempty_text(key, "source manifest id")
            if identifier in paths and paths[identifier] != value:
                raise ContextBundleError(
                    f"source manifest id {identifier!r} has conflicting paths"
                )
            paths[identifier] = value
    if explicit is not None:
        if not isinstance(explicit, Mapping):
            raise ContextBundleError("source_manifest_paths must be an object")
        for key, value in explicit.items():
            identifier = _nonempty_text(key, "source manifest id")
            if identifier in paths and paths[identifier] != value:
                raise ContextBundleError(
                    f"source manifest id {identifier!r} has conflicting paths"
                )
            paths[identifier] = value
    if not paths:
        raise ContextBundleError("worker context requires at least one source manifest")
    summaries = []
    real_paths = set()
    for identifier in sorted(paths):
        summary, path = _source_manifest_summary(
            identifier,
            paths[identifier],
        )
        real = path.resolve()
        if real in real_paths:
            raise ContextBundleError("source manifests alias the same file")
        real_paths.add(real)
        summaries.append(summary)
    return summaries


def _instruction_paths(
    module: str,
    common_path: object | None,
    module_path: object | None,
    suggestions_path: object | None,
) -> dict[str, object]:
    common = common_path or ROOT / "references" / "check_modules" / "common.md"
    if module == "suggestions" and suggestions_path is not None:
        module_path = suggestions_path
    if module_path is None:
        candidate = ROOT / "references" / "check_modules" / f"{module}.md"
        module_path = candidate if candidate.is_file() else ROOT / "references" / "suggestions.md"
    suggestions = None
    if module == "suggestions":
        suggestions = suggestions_path or ROOT / "references" / "suggestions.md"
    return {
        "common": str(common),
        "module": str(module_path),
        "suggestions": str(suggestions) if suggestions is not None else None,
    }


def _providers(resolution: Mapping) -> list[dict]:
    output = []
    for capability_id, item in sorted(resolution["enabled"].items()):
        if item.get("effect") not in FORMAL_CAPABILITY_EFFECTS:
            continue
        provider = item.get("provider")
        if not isinstance(provider, Mapping):
            continue
        output.append(
            {
                "capability_id": capability_id,
                "id": provider["id"],
                "api_version": provider["api_version"],
                "target_lang": provider["target_lang"],
                "descriptor_digest": provider["descriptor_digest"],
            }
        )
    return output


def _project_asset_summaries(
    snapshot: Mapping,
    resolution: Mapping,
    loaded_assets: Mapping,
) -> list[dict]:
    enabled_assets = _enabled_asset_bindings(
        snapshot,
        resolution,
        effects=FORMAL_CAPABILITY_EFFECTS,
    )
    visible_assets = set(enabled_assets)
    visible_assets.update(
        asset_id
        for asset_id, entry in snapshot["assets"].items()
        if entry.get("status") == "present"
        and entry.get("kind") in FOUNDATION_ASSET_KINDS
    )
    return [
        {
            "asset_id": asset_id,
            "kind": entry["kind"],
            "status": entry["status"],
            "sha256": entry.get("sha256"),
            "distribution": entry["distribution"],
            "availability": entry["availability"],
            "required": entry["required"],
            "authority": deepcopy(entry.get("authority")),
            "provenance": deepcopy(entry.get("provenance")),
            "media_type": entry.get("media_type"),
            "content_schema": entry.get("content_schema"),
            "capability_ids": enabled_assets.get(asset_id, []),
            "document_digest": (
                loaded_assets["asset_bindings"][asset_id]["document_digest"]
                if asset_id in loaded_assets["asset_bindings"]
                else None
            ),
        }
        for asset_id, entry in sorted(snapshot["assets"].items())
        if asset_id in visible_assets
    ]


def _readable_document_summaries(document: Mapping) -> list[Mapping]:
    summaries = []
    if document["language_notes"] is not None:
        summaries.append(document["language_notes"])
    summaries.extend(document["worker_documents"])
    summaries.extend(
        summary
        for summary in document["instructions"].values()
        if isinstance(summary, Mapping)
    )
    return summaries


def _source_manifest_projection_input(source_manifests: Sequence[Mapping]) -> list[dict]:
    return [
        {
            "id": summary["id"],
            "projection": deepcopy(summary["projection"]),
        }
        for summary in source_manifests
    ]


def verify_worker_context_manifest_resources(
    value: object,
    *,
    job_root: object | None = None,
    skill_root: object = ROOT,
) -> dict:
    document = validate_worker_context_manifest(value)
    delivered: dict[str, bytes] = {}
    for summary in _readable_document_summaries(document):
        locator = summary["locator"]
        payload = _locator_payload(
            locator,
            job_root=job_root,
            skill_root=skill_root,
            verify_paths=True,
            label=f"worker resource {summary['id']}",
        )
        if payload is None:  # pragma: no cover - verify_paths guarantees bytes
            raise ContextBundleError("worker resource locator was not resolved")
        if len(payload) != summary["bytes"]:
            raise ContextBundleError(
                f"worker resource {summary['id']} byte count mismatch"
            )
        if hashlib.sha256(payload).hexdigest() != summary["sha256"]:
            raise ContextBundleError(
                f"worker resource {summary['id']} digest mismatch"
            )
        delivered.setdefault(canonical_digest(locator), payload)
    resource_bytes = sum(len(payload) for payload in delivered.values())
    if resource_bytes != document["budget"]["components"]["readable_resources"]:
        raise ContextBundleError("worker readable-resource measurement mismatch")
    return document


def validate_worker_context_manifest(value: object) -> dict:
    try:
        document = validate_json_schema(
            value, _load_schema(WORKER_CONTEXT_MANIFEST_SCHEMA_PATH)
        )
    except ProfileIngestError as exc:
        raise ContextBundleError(str(exc)) from exc
    if document["worker_context_manifest_digest"] != _digest_without(
        document, "worker_context_manifest_digest"
    ):
        raise ContextBundleError("worker context manifest digest mismatch")
    components = document["budget"]["components"]
    if document["budget"]["measured_bytes"] != sum(components.values()):
        raise ContextBundleError("worker context manifest measurement components mismatch")
    if components["shared_context_assets"] != document["shared_context_assets"][
        "bytes"
    ]:
        raise ContextBundleError("worker shared-context measurement mismatch")
    if components["context_bundles"] != document["context_bundles"]["bytes"]:
        raise ContextBundleError("worker context-bundle measurement mismatch")
    if components["packet_payloads"] != document["packet_payloads"]["bytes"]:
        raise ContextBundleError("worker packet-payload measurement mismatch")
    for summary in document["source_manifests"]:
        if summary["projection_digest"] != canonical_digest(summary["projection"]):
            raise ContextBundleError("worker source-manifest projection digest mismatch")
        if summary["projection_bytes"] != _component_bytes(summary["projection"]):
            raise ContextBundleError("worker source-manifest projection byte mismatch")
        projection = summary["projection"]
        if projection["kind"] == "project_source_manifest":
            if summary["schema"] != "lqe.project-source-manifest" or summary[
                "version"
            ] != 1:
                raise ContextBundleError(
                    "worker project-source projection schema binding mismatch"
                )
            source_ids = [item["id"] for item in projection["sources"]]
            generated_ids = [item["id"] for item in projection["generated_assets"]]
            if source_ids != sorted(set(source_ids)) or generated_ids != sorted(
                set(generated_ids)
            ):
                raise ContextBundleError(
                    "worker source-manifest projection is not canonical"
                )
        elif (
            projection["schema"] != summary["schema"]
            or projection["version"] != summary["version"]
        ):
            raise ContextBundleError(
                "worker source-manifest projection schema binding mismatch"
            )
    if components["source_manifest_projections"] != _component_bytes(
        _source_manifest_projection_input(document["source_manifests"])
    ):
        raise ContextBundleError("worker source-manifest projection measurement mismatch")
    identifier_fields = {
        "project_assets": "asset_id",
        "language_providers": "capability_id",
        "source_manifests": "id",
        "worker_documents": "id",
    }
    for field, identifier_field in identifier_fields.items():
        identifiers = [
            item.get(identifier_field)
            for item in document[field]
        ]
        if len(identifiers) != len(set(identifiers)):
            raise ContextBundleError(f"worker context manifest has duplicate {field}")
        if identifiers != sorted(identifiers):
            raise ContextBundleError(
                f"worker context manifest {field} is not canonical"
            )
    for asset in document["project_assets"]:
        if asset["status"] != "present":
            raise ContextBundleError(
                "worker context manifest includes a non-present project asset"
            )
        if (
            asset["kind"] in LOADABLE_ASSET_SCHEMAS
            and asset["document_digest"] is None
        ):
            raise ContextBundleError(
                "worker context manifest lacks a canonical typed-asset digest"
            )
        if asset["capability_ids"] != _string_list(
            asset["capability_ids"], "project asset capability_ids"
        ):
            raise ContextBundleError(
                "worker context manifest project asset capabilities are not canonical"
            )
        if (
            asset["kind"] in LOADABLE_ASSET_SCHEMAS
            and not asset["capability_ids"]
        ):
            raise ContextBundleError(
                "worker context manifest includes an unenabled typed project asset"
            )
    for summary in _readable_document_summaries(document):
        payload = _locator_payload(
            summary["locator"],
            job_root=None,
            skill_root=ROOT,
            verify_paths=False,
            label=f"worker resource {summary['id']}",
        )
        if payload is not None:
            if len(payload) != summary["bytes"]:
                raise ContextBundleError(
                    f"worker resource {summary['id']} byte count mismatch"
                )
            if hashlib.sha256(payload).hexdigest() != summary["sha256"]:
                raise ContextBundleError(
                    f"worker resource {summary['id']} digest mismatch"
                )
    return document


def measure_complete_worker_input_bytes(
    worker_manifest: Mapping,
    context_bundle_set: Mapping,
    packet_payloads: Sequence[object],
    *,
    additional_inputs: Sequence[object] = (),
) -> int:
    """Measure the complete canonical input actually delivered to one worker."""

    manifest = validate_worker_context_manifest(worker_manifest)
    bundle_set = validate_context_bundle_set(context_bundle_set)
    if manifest["module"] != bundle_set["module"]:
        raise ContextBundleError("worker manifest module differs from bundle set")
    if (
        manifest["context_bundles"]["digest"]
        != bundle_set["context_bundle_set_digest"]
        or manifest["context_bundles"]["count"] != len(bundle_set["bundles"])
        or manifest["context_bundles"]["bytes"]
        != _component_bytes(bundle_set["bundles"])
    ):
        raise ContextBundleError("worker manifest bundle-set binding mismatch")
    if not isinstance(packet_payloads, Sequence) or isinstance(
        packet_payloads, (str, bytes)
    ):
        raise ContextBundleError("worker packet payloads must be an array")
    packets = list(packet_payloads)
    if manifest["packet_payloads"]["count"] != len(packets):
        raise ContextBundleError("worker packet payload count mismatch")
    if not isinstance(additional_inputs, Sequence) or isinstance(
        additional_inputs, (str, bytes)
    ):
        raise ContextBundleError("additional worker inputs must be an array")

    embedded_resource_bytes = 0
    seen_embedded_locators = set()
    for summary in _readable_document_summaries(manifest):
        locator = summary["locator"]
        if locator["kind"] != "embedded_text":
            continue
        locator_digest = canonical_digest(locator)
        if locator_digest in seen_embedded_locators:
            continue
        seen_embedded_locators.add(locator_digest)
        embedded_resource_bytes += summary["bytes"]
    external_resource_bytes = (
        manifest["budget"]["components"]["readable_resources"]
        - embedded_resource_bytes
    )
    if external_resource_bytes < 0:
        raise ContextBundleError("worker external-resource measurement is invalid")

    return sum(
        (
            _component_bytes(manifest),
            _component_bytes(bundle_set),
            sum(_component_bytes(packet) for packet in packets),
            external_resource_bytes,
            sum(_component_bytes(item) for item in additional_inputs),
        )
    )


def build_worker_context_manifest(
    state: Mapping,
    module: str,
    context_bundle_set: Mapping,
    *,
    max_worker_bytes: int | None = None,
    packet_payloads: Sequence[object] = (),
    source_manifest_paths: Mapping[str, object] | None = None,
    common_instructions_path: object | None = None,
    module_instructions_path: object | None = None,
    suggestion_instructions_path: object | None = None,
    job_root: object | None = None,
) -> dict:
    """Bind and measure worker input; ``max_worker_bytes`` is compatibility-only."""

    snapshot, resolution = _validate_state_bindings(state)
    bundle_set = validate_context_bundle_set(context_bundle_set)
    loaded_assets = load_project_context_assets(state)
    module = _nonempty_text(module, "module")
    if bundle_set["module"] != module:
        raise ContextBundleError("worker module differs from context bundle set")
    expected_capabilities = _formal_capability_summary(resolution)
    if bundle_set["capabilities"] != expected_capabilities:
        raise ContextBundleError(
            "context bundle set belongs to another capability resolution"
        )
    state_segments = state.get("segments")
    if not isinstance(state_segments, list):
        raise ContextBundleError("state.segments must be an array")
    by_key: dict[str, list[Mapping]] = {}
    for segment in state_segments:
        if not isinstance(segment, Mapping):
            raise ContextBundleError("state segment must be an object")
        key = segment.get("segment_key")
        if isinstance(key, str) and key:
            by_key.setdefault(key, []).append(segment)
    rebuilt_bundles = []
    for bundle in bundle_set["bundles"]:
        if bundle["project_asset_snapshot_digest"] != snapshot["digest"]:
            raise ContextBundleError(
                "context bundle set belongs to another project asset snapshot"
            )
        if bundle["capability_resolution_digest"] != resolution["digest"]:
            raise ContextBundleError(
                "context bundle set belongs to another capability resolution"
            )
        key = bundle["segment"]["identity"]["segment_key"]
        candidates = by_key.get(key, [])
        if len(candidates) != 1:
            raise ContextBundleError(
                f"context bundle segment_key {key!r} is not unique in state"
            )
        rebuilt = build_context_bundle(
            state,
            candidates[0],
            module,
            loaded_assets=loaded_assets,
            module_view=bundle["module_view"],
            projection_modules=bundle["projection_modules"],
        )
        if rebuilt != bundle:
            raise ContextBundleError(
                f"context bundle differs from canonical state projection: {key}"
            )
        rebuilt_bundles.append(rebuilt)
    rebuilt_shared = _shared_context_assets(rebuilt_bundles, loaded_assets)
    if rebuilt_shared != bundle_set["shared_context_assets"]:
        raise ContextBundleError(
            "shared context assets differ from canonical project assets"
        )
    profile_digest = _digest(state.get("profile_digest"), "state.profile_digest")
    overlay = state.get("profile_overlay_digest")
    if overlay is not None:
        overlay = _digest(overlay, "state.profile_overlay_digest")

    instruction_paths = _instruction_paths(
        module,
        common_instructions_path,
        module_instructions_path,
        suggestion_instructions_path,
    )
    instruction_summaries = {"suggestions": None}
    delivered_files: dict[Path, bytes] = {}
    for identifier, path_value in instruction_paths.items():
        if path_value is None:
            continue
        summary, payload, path = _document_summary(
            f"instructions.{identifier}",
            path_value,
            job_root=job_root,
        )
        instruction_summaries[identifier] = summary
        delivered_files.setdefault(path.resolve(), payload)

    worker_documents = []
    language_notes = None
    lang_path = state.get("lang_notes_path")
    if isinstance(lang_path, str) and lang_path.strip():
        language_notes, payload, path = _document_summary(
            "lang_notes_path",
            lang_path,
            job_root=job_root,
        )
        delivered_files.setdefault(path.resolve(), payload)
    for field in _WORKER_DOCUMENT_FIELDS:
        value = state.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        summary, payload, path = _document_summary(
            field,
            value,
            job_root=job_root,
        )
        worker_documents.append(summary)
        delivered_files.setdefault(path.resolve(), payload)
    worker_documents.sort(key=lambda item: item["id"])

    if not isinstance(packet_payloads, Sequence) or isinstance(
        packet_payloads, (str, bytes)
    ):
        raise ContextBundleError("packet_payloads must be an array")
    source_manifests = _source_manifest_summaries(
        state,
        source_manifest_paths,
        snapshot,
    )
    source_projection_input = _source_manifest_projection_input(source_manifests)
    packets = deepcopy(list(packet_payloads))
    shared = bundle_set["shared_context_assets"]
    bundles = bundle_set["bundles"]
    delivered_payloads = {
        canonical_digest(_path_locator(path, payload, job_root=job_root)): payload
        for path, payload in delivered_files.items()
    }
    budget_components = {
        "readable_resources": sum(
            len(payload) for payload in delivered_payloads.values()
        ),
        "source_manifest_projections": _component_bytes(
            source_projection_input
        ),
        "shared_context_assets": _component_bytes(shared),
        "context_bundles": _component_bytes(bundles),
        "packet_payloads": _component_bytes(packets),
    }
    measured = calculate_worker_input_bytes(
        list(delivered_payloads.values())
        + [source_projection_input, shared, bundles, packets]
    )
    if measured != sum(budget_components.values()):
        raise ContextBundleError("worker input measurement accounting mismatch")

    shared_counts = {
        field: len(shared[field])
        for field in ("entities", "relations", "review_examples", "constraints")
    }
    manifest = {
        "schema": WORKER_CONTEXT_MANIFEST_SCHEMA,
        "version": WORKER_CONTEXT_MANIFEST_VERSION,
        "module": module,
        "profile": {"digest": profile_digest, "overlay_digest": overlay},
        "capability_resolution": {"digest": resolution["digest"]},
        "project_asset_snapshot": {"digest": snapshot["digest"]},
        "project_assets": _project_asset_summaries(
            snapshot, resolution, loaded_assets
        ),
        "language_providers": _providers(resolution),
        "source_manifests": source_manifests,
        "language_notes": language_notes,
        "worker_documents": worker_documents,
        "instructions": instruction_summaries,
        "shared_context_assets": {
            "digest": canonical_digest(shared),
            "counts": shared_counts,
            "bytes": budget_components["shared_context_assets"],
        },
        "context_bundles": {
            "digest": bundle_set["context_bundle_set_digest"],
            "count": len(bundles),
            "bytes": _component_bytes(bundles),
        },
        "packet_payloads": {
            "digest": canonical_digest(packets),
            "count": len(packets),
            "bytes": _component_bytes(packets),
        },
        "budget": {
            "max_bytes": None,
            "measured_bytes": measured,
            "components": budget_components,
            "status": "advisory",
        },
    }
    manifest["worker_context_manifest_digest"] = canonical_digest(manifest)
    return verify_worker_context_manifest_resources(
        manifest,
        job_root=job_root,
    )


__all__ = [
    "CONTEXT_BUNDLE_SCHEMA",
    "CONTEXT_BUNDLE_VERSION",
    "SELECTED_CONTEXT_EVIDENCE_INDEX_SCHEMA",
    "SELECTED_CONTEXT_EVIDENCE_INDEX_VERSION",
    "ContextBundleError",
    "WorkerContextBudgetError",
    "build_context_bundle",
    "build_context_bundle_set",
    "build_selected_context_evidence_index",
    "build_worker_context_manifest",
    "calculate_worker_input_bytes",
    "canonical_digest",
    "enforce_worker_byte_budget",
    "load_project_context_assets",
    "load_selected_context_evidence_index",
    "measure_complete_worker_input_bytes",
    "normalize_module_view",
    "validate_context_bundle",
    "validate_context_bundle_set",
    "validate_selected_context_evidence_index",
    "validate_loaded_project_context_assets",
    "validate_shared_context_assets",
    "validate_worker_context_manifest",
    "verify_worker_context_manifest_resources",
]
