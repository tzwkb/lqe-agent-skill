import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Iterable, Mapping


def paths_alias(first: Path, second: Path) -> bool:
    first = Path(first)
    second = Path(second)
    try:
        if os.path.samefile(first, second):
            return True
    except (FileNotFoundError, OSError):
        pass
    if first.resolve() == second.resolve():
        return True
    return (
        first.parent.resolve() == second.parent.resolve()
        and first.name.casefold() == second.name.casefold()
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def state_reference_paths(state: dict) -> dict[str, Path]:
    references: dict[str, Path] = {}
    single_fields = (
        "input_path",
        "sg_path",
        "terms_path",
        "lang_notes_path",
        "background_path",
        "checks_path",
        "confirmed_rules_path",
        "source_manifest_path",
        "tabular_source_manifest_path",
        "tm_candidates_path",
        "capability_resolution_path",
        "project_asset_snapshot_path",
        "context_overrides_path",
        "context_gap_report_path",
    )
    for field in single_fields:
        value = state.get(field)
        if isinstance(value, str) and value.strip():
            references[field] = Path(value)
    values = state.get("input_paths")
    if isinstance(values, list):
        for index, value in enumerate(values):
            if isinstance(value, str) and value.strip():
                references[f"input_paths[{index}]"] = Path(value)
    asset_paths = state.get("project_asset_paths")
    if isinstance(asset_paths, dict):
        for asset_id, value in asset_paths.items():
            if isinstance(value, str) and value.strip():
                references[f"project_asset_paths.{asset_id}"] = Path(value)
    return references


def validate_artifact_paths(
    outputs: Mapping[str, Path] | Iterable[tuple[str, Path]],
    protected_inputs: Mapping[str, Path] | Iterable[tuple[str, Path]],
    *,
    context: str,
) -> None:
    output_items = [
        (str(label), Path(path))
        for label, path in (
            outputs.items() if isinstance(outputs, Mapping) else outputs
        )
    ]
    input_items = [
        (str(label), Path(path))
        for label, path in (
            protected_inputs.items()
            if isinstance(protected_inputs, Mapping)
            else protected_inputs
        )
    ]
    for index, (label, path) in enumerate(output_items):
        for previous_label, previous_path in output_items[:index]:
            if paths_alias(path, previous_path):
                raise ValueError(
                    f"{context}: output {label} conflicts with output "
                    f"{previous_label}: {path}"
                )
        for input_label, input_path in input_items:
            if paths_alias(path, input_path):
                raise ValueError(
                    f"{context}: output {label} conflicts with input "
                    f"{input_label}: {path}"
                )


def _file_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino


def _unlink_if_owned(path: Path, identity: tuple[int, int]) -> None:
    if _file_identity(path) == identity:
        Path(path).unlink()


def _replace_staged(source: Path, destination: Path) -> None:
    source = Path(source)
    destination = Path(destination)
    identity = _file_identity(source)
    if identity is None:
        raise FileNotFoundError(f"staged artifact is missing: {source}")
    if os.path.lexists(destination):
        os.replace(source, destination)
        return
    try:
        os.link(source, destination, follow_symlinks=False)
        source.unlink()
    except BaseException:
        _unlink_if_owned(destination, identity)
        raise


def _publish_new_staged(source: Path, destination: Path) -> None:
    source = Path(source)
    destination = Path(destination)
    identity = _file_identity(source)
    if identity is None:
        raise FileNotFoundError(f"staged artifact is missing: {source}")
    try:
        os.link(source, destination, follow_symlinks=False)
        source.unlink()
    except BaseException:
        _unlink_if_owned(destination, identity)
        raise


def _backup_existing(destination: Path) -> tuple[Path, tuple[int, int]]:
    identity = _file_identity(destination)
    if identity is None:
        raise FileNotFoundError(f"artifact disappeared before backup: {destination}")
    with tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=f".{destination.name}.rollback.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        backup = Path(handle.name)
    backup.unlink()
    os.link(destination, backup, follow_symlinks=False)
    if _file_identity(backup) != identity:
        backup.unlink(missing_ok=True)
        raise RuntimeError(f"artifact changed while backing up: {destination}")
    return backup, identity


def publish_replacement_transaction(
    replacements: list[tuple[Path, Path]],
    *,
    overwrite: bool = True,
) -> None:
    normalized = [(Path(source), Path(destination)) for source, destination in replacements]
    destinations: list[Path] = []
    for source, destination in normalized:
        if not source.exists() or source.is_symlink() or not source.is_file():
            raise FileNotFoundError(f"staged artifact is not a regular file: {source}")
        if paths_alias(source, destination):
            raise ValueError(f"staged artifact aliases destination: {source}")
        for previous in destinations:
            if paths_alias(previous, destination):
                raise ValueError(
                    f"job artifacts resolve to the same path: {previous}, {destination}"
                )
        destinations.append(destination)
        if os.path.lexists(destination):
            if not overwrite:
                raise FileExistsError(
                    f"artifact destination already exists: {destination}"
                )
            info = destination.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ValueError(
                    f"job artifact destination is not a regular file: {destination}"
                )

    backups: dict[Path, Path] = {}
    published: dict[Path, tuple[int, int]] = {}
    try:
        for _, destination in normalized:
            if overwrite and os.path.lexists(destination):
                backup, _ = _backup_existing(destination)
                backups[destination] = backup
        for source, destination in normalized:
            identity = _file_identity(source)
            if identity is None:
                raise FileNotFoundError(f"staged artifact is missing: {source}")
            published[destination] = identity
            if overwrite:
                _replace_staged(source, destination)
            else:
                _publish_new_staged(source, destination)
            if _file_identity(destination) != identity:
                raise RuntimeError(f"published artifact identity mismatch: {destination}")
    except BaseException:
        for destination, identity in reversed(list(published.items())):
            if _file_identity(destination) != identity:
                continue
            backup = backups.pop(destination, None)
            if backup is None:
                destination.unlink()
            else:
                os.replace(backup, destination)
        raise
    finally:
        for backup in backups.values():
            backup.unlink(missing_ok=True)


def write_json_atomic(path: Path, value: object) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            staged = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
        publish_replacement_transaction([(staged, path)])
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
