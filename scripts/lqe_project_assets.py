"""Project asset registry validation, inspection, and staging helpers.

The loader is intentionally declaration-driven: files that merely exist beside a
profile are never discovered or activated.  This module does not publish job
state; callers are expected to run it inside their existing staging transaction.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePath
import re
import shutil
import stat
from typing import Mapping

from lqe_profile_ingest import ProfileIngestError, load_project_source_manifest


ASSET_SNAPSHOT_SCHEMA = "lqe.project-asset-snapshot"
ASSET_SNAPSHOT_VERSION = 1

ASSET_DISTRIBUTIONS = frozenset(
    {"public_allowed", "internal_only", "redacted"}
)
ASSET_AVAILABILITY = frozenset({"included", "external"})

LEGACY_ASSET_FIELDS = {
    "style_guide": "style_guide",
    "terminology": "terminology",
    "checks": "checks",
    "confirmed_rules": "confirmed_rules",
}

_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ASSET_KIND_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,127}$")
_ALLOWED_DECLARATION_KEYS = frozenset(
    {
        "kind",
        "path",
        "required",
        "authority",
        "provenance",
        "distribution",
        "availability",
        "description",
        "media_type",
        "content_schema",
    }
)


class ProjectAssetError(ValueError):
    """Raised when an asset declaration or snapshot is unsafe or invalid."""


def _canonical_json(value: object, *, context: str) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProjectAssetError(f"{context} is not canonical JSON: {exc}") from exc


def canonical_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value, context="asset value")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ProjectAssetError(f"cannot read project asset {path}: {exc}") from exc
    return digest.hexdigest()


def _require_nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectAssetError(f"{field} must be a non-empty string")
    if "\x00" in value:
        raise ProjectAssetError(f"{field} must not contain NUL")
    return value.strip()


def _validate_json_object(value: object, field: str) -> dict:
    if not isinstance(value, dict):
        raise ProjectAssetError(f"{field} must be an object")
    _canonical_json(value, context=field)
    return deepcopy(value)


def _validate_declared_path(raw: object, field: str) -> str:
    value = _require_nonempty_string(raw, field)
    path = PurePath(value)
    if any(part == ".." for part in path.parts):
        raise ProjectAssetError(f"{field} must not contain parent traversal")
    if value in {".", "./"}:
        raise ProjectAssetError(f"{field} must identify a file")
    return value


def validate_asset_declaration(
    asset_id: str,
    declaration: object,
    *,
    legacy: bool = False,
) -> dict:
    """Validate one v2 asset declaration and return a defensive copy."""

    if not isinstance(asset_id, str) or not _ASSET_ID_RE.fullmatch(asset_id):
        raise ProjectAssetError(
            "asset id must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}"
        )
    if not isinstance(declaration, dict):
        raise ProjectAssetError(f"asset {asset_id!r} must be an object")
    unknown = sorted(set(declaration) - _ALLOWED_DECLARATION_KEYS)
    if unknown:
        raise ProjectAssetError(
            f"asset {asset_id!r} has unknown fields: {', '.join(unknown)}"
        )

    missing = [
        key
        for key in (
            "kind",
            "path",
            "required",
            "authority",
            "provenance",
            "distribution",
            "availability",
        )
        if key not in declaration
    ]
    if missing:
        raise ProjectAssetError(
            f"asset {asset_id!r} is missing fields: {', '.join(missing)}"
        )

    kind = _require_nonempty_string(
        declaration.get("kind"), f"asset {asset_id}.kind"
    )
    if not _ASSET_KIND_RE.fullmatch(kind):
        raise ProjectAssetError(f"asset {asset_id}.kind is invalid")
    path = _validate_declared_path(
        declaration.get("path"), f"asset {asset_id}.path"
    )
    required = declaration.get("required")
    if type(required) is not bool:
        raise ProjectAssetError(f"asset {asset_id}.required must be boolean")

    authority = _validate_json_object(
        declaration.get("authority"), f"asset {asset_id}.authority"
    )
    _require_nonempty_string(
        authority.get("issuer"), f"asset {asset_id}.authority.issuer"
    )
    if "level" in authority:
        _require_nonempty_string(
            authority.get("level"), f"asset {asset_id}.authority.level"
        )

    provenance = _validate_json_object(
        declaration.get("provenance"), f"asset {asset_id}.provenance"
    )
    _require_nonempty_string(
        provenance.get("kind"), f"asset {asset_id}.provenance.kind"
    )

    distribution = declaration.get("distribution")
    if distribution not in ASSET_DISTRIBUTIONS:
        raise ProjectAssetError(
            f"asset {asset_id}.distribution must be one of "
            f"{sorted(ASSET_DISTRIBUTIONS)}"
        )
    availability = declaration.get("availability")
    if availability not in ASSET_AVAILABILITY:
        raise ProjectAssetError(
            f"asset {asset_id}.availability must be one of "
            f"{sorted(ASSET_AVAILABILITY)}"
        )

    output = {
        "kind": kind,
        "path": path,
        "required": required,
        "authority": authority,
        "provenance": provenance,
        "distribution": distribution,
        "availability": availability,
    }
    for key in ("description", "media_type", "content_schema"):
        if key not in declaration:
            continue
        output[key] = _require_nonempty_string(
            declaration[key], f"asset {asset_id}.{key}"
        )
    if legacy:
        output["legacy_inferred_metadata"] = True
    return output


def _legacy_asset_declaration(field: str, path: str) -> dict:
    return {
        "kind": LEGACY_ASSET_FIELDS[field],
        "path": path,
        "required": True,
        "authority": {"issuer": "legacy_profile", "level": "unspecified"},
        "provenance": {"kind": "legacy_profile_field", "field": field},
        "distribution": "internal_only",
        "availability": "included",
    }


def normalize_asset_registry(
    profile: Mapping[str, object],
    *,
    legacy: bool | None = None,
) -> dict[str, dict]:
    """Normalize a profile's declared assets without scanning its directory."""

    if not isinstance(profile, Mapping):
        raise ProjectAssetError("project profile must be an object")
    if legacy is None:
        legacy = profile.get("profile_contract_version") in (None, 1)

    if legacy:
        registry = {}
        for field, kind in LEGACY_ASSET_FIELDS.items():
            raw_path = profile.get(field)
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            declaration = _legacy_asset_declaration(field, raw_path.strip())
            registry[kind] = validate_asset_declaration(
                kind, declaration, legacy=True
            )
        return registry

    raw_registry = profile.get("assets")
    if not isinstance(raw_registry, dict):
        raise ProjectAssetError("v2 profile assets must be an object")
    output = {}
    for asset_id in sorted(raw_registry):
        output[asset_id] = validate_asset_declaration(
            asset_id, raw_registry[asset_id]
        )
    return output


def resolve_asset_path(
    profile_dir: Path,
    declared_path: str,
    *,
    allow_outside_root: bool = False,
) -> Path:
    """Resolve an included asset path and enforce its profile-root boundary."""

    raw = _validate_declared_path(declared_path, "asset.path")
    root = Path(profile_dir).resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = Path(os.path.abspath(candidate))
    resolved = candidate.resolve(strict=False)
    if not allow_outside_root:
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ProjectAssetError(
                f"project asset escapes profile directory: {declared_path}"
            ) from exc
    return candidate


def inspect_asset_registry(
    registry: Mapping[str, object],
    *,
    profile_dir: Path,
    allow_outside_root: bool = False,
    strict_required: bool = True,
) -> dict:
    """Inspect only declared assets and return snapshot plus local source paths."""

    if not isinstance(registry, Mapping):
        raise ProjectAssetError("asset registry must be an object")
    normalized = {
        asset_id: validate_asset_declaration(
            asset_id,
            {
                key: value
                for key, value in declaration.items()
                if key != "legacy_inferred_metadata"
            }
            if isinstance(declaration, dict)
            else declaration,
            legacy=bool(
                isinstance(declaration, dict)
                and declaration.get("legacy_inferred_metadata") is True
            ),
        )
        for asset_id, declaration in registry.items()
    }

    entries = {}
    resolved_paths: dict[str, Path] = {}
    identities: dict[tuple[int, int], str] = {}
    for asset_id in sorted(normalized):
        declaration = normalized[asset_id]
        entry = {
            key: deepcopy(value)
            for key, value in declaration.items()
            if key != "legacy_inferred_metadata"
        }
        if declaration.get("legacy_inferred_metadata") is True:
            entry["metadata_status"] = "legacy_inferred"

        if declaration["availability"] == "external":
            entry.update({"status": "external", "sha256": None, "size": None})
            entries[asset_id] = entry
            if declaration["required"] and strict_required:
                raise ProjectAssetError(
                    f"required project asset {asset_id!r} is external"
                )
            continue

        path = resolve_asset_path(
            Path(profile_dir),
            declaration["path"],
            allow_outside_root=allow_outside_root,
        )
        try:
            info = path.lstat()
        except FileNotFoundError:
            entry.update({"status": "missing", "sha256": None, "size": None})
            entries[asset_id] = entry
            if declaration["required"] and strict_required:
                raise ProjectAssetError(
                    f"required project asset {asset_id!r} is missing: {path}"
                )
            continue
        except OSError as exc:
            raise ProjectAssetError(
                f"cannot inspect project asset {asset_id!r}: {path}: {exc}"
            ) from exc

        if stat.S_ISLNK(info.st_mode):
            raise ProjectAssetError(
                f"project asset {asset_id!r} must not be a symbolic link: {path}"
            )
        if not stat.S_ISREG(info.st_mode):
            raise ProjectAssetError(
                f"project asset {asset_id!r} is not a regular file: {path}"
            )
        try:
            resolved_real_path = path.resolve(strict=True)
            if not allow_outside_root:
                resolved_real_path.relative_to(Path(profile_dir).resolve())
        except ValueError as exc:
            raise ProjectAssetError(
                f"project asset resolves outside profile directory: {path}"
            ) from exc
        except OSError as exc:
            raise ProjectAssetError(
                f"cannot resolve project asset {asset_id!r}: {path}: {exc}"
            ) from exc
        identity = (info.st_dev, info.st_ino)
        previous = identities.get(identity)
        if previous is not None:
            raise ProjectAssetError(
                f"project assets {previous!r} and {asset_id!r} alias the same file"
            )
        identities[identity] = asset_id
        digest = file_sha256(path)
        entry.update({"status": "present", "sha256": digest, "size": info.st_size})
        entries[asset_id] = entry
        resolved_paths[asset_id] = path

    snapshot = {
        "schema": ASSET_SNAPSHOT_SCHEMA,
        "version": ASSET_SNAPSHOT_VERSION,
        "assets": entries,
    }
    snapshot["digest"] = canonical_digest(snapshot)
    return {
        "registry": normalized,
        "snapshot": snapshot,
        "resolved_paths": resolved_paths,
    }


def inspect_project_assets(
    profile: Mapping[str, object],
    *,
    profile_dir: Path,
    allow_outside_root: bool | None = None,
    strict_required: bool = True,
) -> dict:
    legacy = profile.get("profile_contract_version") in (None, 1)
    if allow_outside_root is None:
        allow_outside_root = legacy
    registry = normalize_asset_registry(profile, legacy=legacy)
    return inspect_asset_registry(
        registry,
        profile_dir=profile_dir,
        allow_outside_root=allow_outside_root,
        strict_required=strict_required,
    )


def build_project_asset_snapshot(
    profile: Mapping[str, object],
    *,
    profile_dir: Path,
    allow_outside_root: bool | None = None,
    strict_required: bool = True,
) -> dict:
    return inspect_project_assets(
        profile,
        profile_dir=profile_dir,
        allow_outside_root=allow_outside_root,
        strict_required=strict_required,
    )["snapshot"]


def validate_project_asset_snapshot(snapshot: object) -> dict:
    if not isinstance(snapshot, dict):
        raise ProjectAssetError("project asset snapshot must be an object")
    if snapshot.get("schema") != ASSET_SNAPSHOT_SCHEMA:
        raise ProjectAssetError("unknown project asset snapshot schema")
    if snapshot.get("version") != ASSET_SNAPSHOT_VERSION:
        raise ProjectAssetError("unsupported project asset snapshot version")
    assets = snapshot.get("assets")
    if not isinstance(assets, dict):
        raise ProjectAssetError("project asset snapshot assets must be an object")
    digest = snapshot.get("digest")
    if not isinstance(digest, str) or not digest:
        raise ProjectAssetError("project asset snapshot digest is missing")
    unsigned = {key: deepcopy(value) for key, value in snapshot.items() if key != "digest"}
    if digest != canonical_digest(unsigned):
        raise ProjectAssetError("project asset snapshot digest mismatch")
    return deepcopy(snapshot)


def asset_statuses(snapshot: Mapping[str, object]) -> dict[str, dict]:
    validated = validate_project_asset_snapshot(snapshot)
    return deepcopy(validated["assets"])


def copy_project_assets(inspection: Mapping[str, object], destination: Path) -> dict[str, Path]:
    """Copy present assets into a caller-owned staging directory.

    The destination must not already contain an asset's target file.  This helper
    never publishes or replaces a formal job artifact.
    """

    if not isinstance(inspection, Mapping):
        raise ProjectAssetError("asset inspection must be an object")
    snapshot = validate_project_asset_snapshot(inspection.get("snapshot"))
    resolved_paths = inspection.get("resolved_paths")
    if not isinstance(resolved_paths, Mapping):
        raise ProjectAssetError("asset inspection resolved_paths must be an object")

    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    copied = {}
    created = []
    created_dirs = []
    try:
        for asset_id in sorted(snapshot["assets"]):
            entry = snapshot["assets"][asset_id]
            if entry.get("status") != "present":
                continue
            source = resolved_paths.get(asset_id)
            if not isinstance(source, Path):
                source = Path(source) if isinstance(source, str) else None
            if source is None:
                raise ProjectAssetError(
                    f"asset inspection lacks resolved path for {asset_id!r}"
                )
            try:
                source_info = source.lstat()
            except OSError as exc:
                raise ProjectAssetError(
                    f"cannot re-inspect project asset {asset_id!r}: {exc}"
                ) from exc
            if stat.S_ISLNK(source_info.st_mode) or not stat.S_ISREG(
                source_info.st_mode
            ):
                raise ProjectAssetError(
                    f"project asset changed type before staging: {asset_id}"
                )
            if file_sha256(source) != entry["sha256"]:
                raise ProjectAssetError(
                    f"project asset changed before staging: {asset_id}"
                )
            target_dir = root / asset_id
            if not target_dir.exists():
                target_dir.mkdir(parents=True)
                created_dirs.append(target_dir)
            target = target_dir / source.name
            if os.path.lexists(target):
                raise ProjectAssetError(f"staged asset already exists: {target}")

            manifest = None
            details_source = None
            details_target = None
            if entry.get("kind") == "project_source_manifest":
                try:
                    manifest = load_project_source_manifest(source)
                except ProfileIngestError as exc:
                    raise ProjectAssetError(
                        f"invalid project source manifest {asset_id!r}: {exc}"
                    ) from exc
                details_relative = Path(manifest["coverage"]["details_path"])
                details_source = source.parent / details_relative
                try:
                    details_info = details_source.lstat()
                except OSError as exc:
                    raise ProjectAssetError(
                        f"cannot re-inspect project source coverage details: {exc}"
                    ) from exc
                if stat.S_ISLNK(details_info.st_mode) or not stat.S_ISREG(
                    details_info.st_mode
                ):
                    raise ProjectAssetError(
                        "project source coverage details must remain a regular "
                        "non-symlink file"
                    )
                details_target = target_dir / details_relative
                if os.path.lexists(details_target):
                    raise ProjectAssetError(
                        f"staged coverage details already exist: {details_target}"
                    )
                missing_dirs = []
                cursor = details_target.parent
                while cursor != target_dir and not cursor.exists():
                    missing_dirs.append(cursor)
                    cursor = cursor.parent
                details_target.parent.mkdir(parents=True, exist_ok=True)
                created_dirs.extend(reversed(missing_dirs))
                shutil.copyfile(details_source, details_target)
                created.append(details_target)

            shutil.copyfile(source, target)
            created.append(target)
            if file_sha256(target) != entry["sha256"]:
                raise ProjectAssetError(f"copied asset digest mismatch: {asset_id}")
            if manifest is not None:
                try:
                    load_project_source_manifest(target)
                except ProfileIngestError as exc:
                    raise ProjectAssetError(
                        f"copied project source manifest is incomplete: {exc}"
                    ) from exc
            copied[asset_id] = target
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        for path in reversed(created_dirs):
            try:
                path.rmdir()
            except OSError:
                pass
        raise
    return copied
