"""读取 state.json 和 errors.json，计算分数并输出 PASS/FAIL。"""
import argparse
import json
import tempfile
from pathlib import Path

from lqe_chunk import verification_generation_lease
from lqe_corrections import CheckFormatError, verify_results
from lqe_engine import (
    get_review_policy,
    read_json,
    require_current_job_runtime,
    requires_bound_artifacts,
    validate_scope_entries,
)
from lqe_paths import publish_replacement_transaction, write_json_atomic
from lqe_result_contract import (
    build_result_contract,
    result_contract_path,
    validate_result_contract,
)
from lqe_scoring import (
    resolve_scoring_policy,
    score_errors,
    scoring_policy_overrides,
)


def _parse_protected_ids(text: str | None) -> set[int]:
    return {int(x.strip()) for x in (text or "").split(",") if x.strip()}


def _load_protected_file(path: str | None) -> set[int]:
    ids: set[int] = set()
    if not path:
        return ids
    data = read_json(path)
    if isinstance(data, dict):
        if "candidate_ids" in data:
            raise ValueError(
                "candidate_ids are not a protection decision; "
                "confirm them with lqe_io.py protect-segments first"
            )
        data = data.get("protected_ids") or data.get("segments") or []
    for item in data:
        if isinstance(item, int):
            ids.add(item)
        elif isinstance(item, str) and item.strip():
            ids.add(int(item.strip()))
        elif isinstance(item, dict):
            sid = item.get("id", item.get("seg_id", item.get("segment_id")))
            if sid is not None:
                ids.add(int(sid))
    return ids


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--state",  required=True)
    p.add_argument("--errors", required=True)
    p.add_argument("--threshold", type=float, default=None)
    critical = p.add_mutually_exclusive_group()
    critical.add_argument(
        "--critical-gate",
        action="store_true",
        dest="critical_gate",
        help="任一 Critical 错误直接 FAIL",
    )
    critical.add_argument(
        "--no-critical-gate",
        action="store_false",
        dest="critical_gate",
        help="显式关闭 Critical gate",
    )
    p.set_defaults(critical_gate=None)
    p.add_argument("--severity-scale", choices=["lisa", "mqm"], default=None,
                   help="严重度乘数档；省略时继承 state.scoring_policy")
    p.add_argument("--scorecard-profile", default=None, dest="scorecard_profile",
                   help="评分卡 profile；省略时继承 state.scoring_policy")
    p.add_argument("--protected-ids", default=None, dest="protected_ids",
                   help="逗号分隔的已保护段 id；这些段不触发 Critical 直接失败规则")
    p.add_argument("--protected-file", default=None, dest="protected_file",
                   help="已保护段 id JSON 文件（如 {\"protected_ids\":[...]}）；这些段不计分")
    repeat = p.add_mutually_exclusive_group()
    repeat.add_argument(
        "--repeat-dedup",
        action="store_true",
        dest="repeat_dedup",
        help="显式启用重复错误去重",
    )
    repeat.add_argument(
        "--no-repeat-dedup",
        action="store_false",
        dest="repeat_dedup",
        help="显式关闭重复错误去重",
    )
    p.set_defaults(repeat_dedup=None)
    p.add_argument("--json", action="store_true",
                   help="只输出 JSON（score/status/…）供其他脚本读取")
    args = p.parse_args()

    state_path = Path(args.state)
    errors_path = Path(args.errors)
    protected = _parse_protected_ids(args.protected_ids)
    protected.update(_load_protected_file(args.protected_file))
    initial_state = read_json(state_path)
    require_current_job_runtime(initial_state, "calc")
    if not requires_bound_artifacts(initial_state):
        state = initial_state
        errors = read_json(errors_path)
        validate_scope_entries(
            state, errors, issues_key="errors", label=errors_path.name
        )
        policy = resolve_scoring_policy(state, scoring_policy_overrides(args))
        computation = score_errors(
            state, errors, policy, protected_ids=protected
        )
        result = computation["output"]
        if computation["annotations_changed"]:
            write_json_atomic(errors_path, computation["annotated_errors"])
    else:
        with verification_generation_lease(
            state_path,
            exclusive=True,
        ) as (state, segments, manifest, revalidate):
            errors = read_json(errors_path)
            original_errors = errors
            validate_scope_entries(
                state, errors, issues_key="errors", label=errors_path.name
            )
            contract_path = result_contract_path(errors_path)
            if manifest is None or not contract_path.is_file():
                raise ValueError(
                    f"{errors_path.name}: bound result contract is required"
                )
            original_contract = read_json(contract_path)
            validate_result_contract(
                original_contract,
                manifest,
                errors,
                label=errors_path.name,
            )
            errors = verify_results(
                segments,
                errors,
                str(errors_path),
                allow_internal_provenance=True,
                require_internal_provenance=True,
                review_policy=get_review_policy(state),
            )
            policy = resolve_scoring_policy(
                state,
                scoring_policy_overrides(args),
            )
            computation = score_errors(
                state, errors, policy, protected_ids=protected
            )
            result = computation["output"]
            if computation["annotations_changed"]:
                annotated = computation["annotated_errors"]
                revalidate()
                if read_json(errors_path) != original_errors:
                    raise ValueError("errors input changed during scoring")
                if read_json(contract_path) != original_contract:
                    raise ValueError("result contract changed during scoring")
                with tempfile.TemporaryDirectory(
                    dir=errors_path.parent,
                    prefix=f".{errors_path.name}.score.",
                ) as staging_name:
                    staging = Path(staging_name)
                    staged_errors = staging / errors_path.name
                    staged_contract = staging / contract_path.name
                    write_json_atomic(staged_errors, annotated)
                    write_json_atomic(
                        staged_contract,
                        build_result_contract(manifest, annotated),
                    )
                    publish_replacement_transaction(
                        [
                            (staged_errors, errors_path),
                            (staged_contract, contract_path),
                        ]
                    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return
    if result["score"] is None:
        print(
            f"SCORE=N/A STATUS={result['status']} "
            f"REVIEW_STATUS={result.get('review_status', 'INPUT_BLOCKED')} "
            f"ERRORS={result['errors']} WORDCOUNT={result['wordcount']} "
            f"CRITICAL={result['critical']} REPEATED={result['repeated']} "
            "NPT/1000=N/A"
        )
        return
    gate_note = " (CRITICAL_GATE)" if result["critical_gate"] else ""
    print(
        f"SCORE={result['score']:.2f} STATUS={result['status']}{gate_note} "
        f"ERRORS={result['errors']} WORDCOUNT={result['wordcount']} "
        f"CRITICAL={result['critical']} REPEATED={result['repeated']} "
        f"NPT/1000={result['npt']:.2f}"
    )
    for k, v in sorted(
        computation["distribution"].items(), key=lambda x: -x[1]
    ):
        print(f"  {v:>4}x  {k}")


if __name__ == "__main__":
    try:
        main()
    except (CheckFormatError, OSError, ValueError) as exc:
        raise SystemExit(f"[calc] {exc}") from exc
