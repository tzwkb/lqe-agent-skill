from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lqe_capabilities import normalize_profile, resolve_capabilities
from lqe_context import (
    descriptor_registry,
    extract_segment_context,
    module_review_equivalence_key,
)
from lqe_engine import build_review_policy, required_modules
from lqe_input_guard import source_digest
from lqe_language_policies import trusted_provider_registry
from lqe_project_assets import asset_statuses, inspect_project_assets
from lqe_split_contract import canonical_digest


def bind_empty_runtime_context(state: dict) -> dict:
    source_lang = str(state.get("source_lang") or "und")
    target_lang = str(state.get("target_lang") or "und")
    normalized = normalize_profile(
        {
            "name": "runtime/test-fixture",
            "language_pair": f"{source_lang}-{target_lang}",
            "source_lang": source_lang,
            "target_lang": target_lang,
            "wordcount_basis": "source-chars",
        }
    )
    inspection = inspect_project_assets(
        normalized,
        profile_dir=ROOT,
        allow_outside_root=True,
        strict_required=True,
    )
    resolution = resolve_capabilities(
        normalized,
        asset_statuses=asset_statuses(inspection["snapshot"]),
        provider_registry=trusted_provider_registry(),
    )
    registry = descriptor_registry(
        normalized, capability_resolution=resolution
    )
    state.update(
        {
            "artifact_contract_version": 1,
            "job_runtime_contract_version": 2,
            "profile_contract_version": normalized["profile_contract_version"],
            "profile_digest": normalized["source_profile_digest"],
            "context_pipeline": deepcopy(normalized["context_pipeline"]),
            "normalized_capabilities": deepcopy(normalized["capabilities"]),
            "capability_descriptors": {},
            "capability_resolution": deepcopy(resolution),
            "capability_resolution_digest": resolution["digest"],
            "project_asset_snapshot": deepcopy(inspection["snapshot"]),
            "project_asset_snapshot_digest": inspection["snapshot"]["digest"],
            "project_asset_paths": {},
            "resolved_context_descriptors": deepcopy(registry),
            "review_policy": state.get("review_policy")
            or build_review_policy("optimized", "test-fixture"),
        }
    )
    state["source_manifest_path"] = str(
        ROOT / "tests" / "fixtures" / "runtime_source_manifest.json"
    )
    modules = required_modules(state)
    for index, segment in enumerate(state.get("segments", [])):
        segment.setdefault("segment_key", f"fixture-{segment['id']}")
        segment.setdefault("key_origin", "generated")
        segment.setdefault("input_status", "ready")
        segment.setdefault("input_block_reasons", [])
        segment.setdefault("input_warnings", [])
        segment.setdefault("protected_texts", [])
        segment.setdefault("resolved_constraints", [])
        segment["source_digest"] = source_digest(segment.get("source", ""))
        segment["context"] = extract_segment_context(
            [],
            {},
            registry,
            profile=normalized,
            source_provenance={
                "adapter": "test_fixture",
                "container": "fixture",
                "row_index": index,
            },
        )
        segment["segment_revision_digest"] = canonical_digest(
            {
                "segment_key": segment["segment_key"],
                "source": segment.get("source", ""),
                "target": segment.get("current_target", segment.get("target", "")),
                "protected": bool(segment.get("protected")),
                "context": segment["context"],
            }
        )
        segment["module_review_equivalence_keys"] = {
            module: module_review_equivalence_key(segment, module, registry)
            for module in modules
        }
    return state


def compact_draft(packet: dict, entries: list[dict]) -> dict:
    by_id = {entry["id"]: entry.get("issues", []) for entry in entries}
    findings = []
    for segment_id in packet["reviewed_ids"]:
        issues = deepcopy(by_id.get(segment_id, []))
        for issue in issues:
            issue.pop("review_provenance", None)
        if issues:
            findings.append({"id": segment_id, "issues": issues})
    return {
        "schema": "lqe.compact-module-draft",
        "version": 1,
        "module": packet["module"],
        "chunk_id": packet["chunk_id"],
        "packet_digest": packet["packet_digest"],
        "worker_batch_id": packet["worker_batch_id"],
        "worker_packet_basis_digest": packet["worker_packet_basis_digest"],
        "context_bundle_set_digest": packet["context_bundle_set_digest"],
        "worker_context_manifest_digest": packet[
            "worker_context_manifest_digest"
        ],
        "selected_evidence_index_path": packet[
            "selected_evidence_index_path"
        ],
        "selected_evidence_index_digest": packet[
            "selected_evidence_index_digest"
        ],
        "worker_receipt": {
            "worker_id": f"test-checker.{packet['module']}",
            "run_id": f"chunk-{packet['chunk_id']}",
        },
        "reviewed_ids": packet["reviewed_ids"],
        "findings": findings,
    }


def publish_compact_modules(job: Path, entries_by_module: dict[str, list[dict]]) -> None:
    def run(*arguments: object) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "lqe_review.py"), *map(str, arguments)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    prepared = run("prepare", "--job", job)
    if prepared.returncode != 0:
        raise AssertionError(prepared.stderr)
    auto = run("auto-publish", "--job", job)
    if auto.returncode != 0:
        raise AssertionError(auto.stderr)
    for module, entries in entries_by_module.items():
        packet_path = job / "review_packets" / module / "chunk_00.json"
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        if not packet["requires_ai"]:
            continue
        draft_path = job / f"{module}.compact.json"
        draft_path.write_text(
            json.dumps(compact_draft(packet, entries), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        published = run(
            "publish",
            "--job",
            job,
            "--chunk",
            packet["chunk_id"],
            "--module",
            module,
            "--input",
            draft_path,
        )
        if published.returncode != 0:
            raise AssertionError(published.stderr)
