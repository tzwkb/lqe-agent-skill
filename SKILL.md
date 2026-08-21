---
name: lqe-translator
description: LQE scoring and review workflow for game-localization translations. Use for project-aware checks, safe corrections, scoring, reference suggestions, and tabular or XML delivery.
---

# LQE Translator

Run the native scripts. Read only the references required by the current phase. Never edit a formal artifact by hand or silently replace a failed native workflow. Before running any nontrivial CLI command, read [`references/cli.md`](references/cli.md); it is the complete command reference.

## Required decisions and inputs

Before a new job, confirm: input path, source/target columns or SDLXLIFF/XLIFF input, project/language pair, terminology source, and review mode. `optimized` is the cost-saving mode; `full` is complete review. If the request does not specify the mode, 必须先询问一次并等待回答；不得根据项目、文件、历史任务或成本偏好代选. Pass `--review-mode "<optimized|full>"`. Existing jobs keep `state.review_policy`.

<pre data-lqe-review-mode-contract>
{
  "decision": "ask_before_new_job_unless_explicit",
  "flag": "--review-mode",
  "optimized": {
    "minor_edits_allowed": false,
    "comment_soft_target": "20-30 characters",
    "suggestion_candidate_severities": ["Critical", "Major"],
    "text_type_routing_enabled": true
  },
  "full": {
    "minor_edits_allowed": true,
    "comment_soft_target": null,
    "suggestion_candidate_severities": ["Neutral", "Minor", "Major", "Critical"],
    "text_type_routing_enabled": false
  },
  "state_field": "state.review_policy",
  "immutable_within_job": true,
  "legacy_cli_default": "optimized"
}
</pre>

If terminology lacks explicit confirmation, 必须在初始化前询问用户. 不得默认全部已确认，也不得默认全部未确认.

<pre data-lqe-term-confirmation-contract>
{
  "trigger": "terminology has no explicit confirmation field (confirmed/approved) OR has a status column but no status->confirmed/protection mapping supplied",
  "required_action": "ask_user_or_supply_mapping_before_initialization",
  "status_column_detection": "RULE-BASED, not enumerative: a column is a status column if its header CONTAINS the token 'status' or '状态' (case-insensitive, any position, regardless of prefixes/suffixes/brackets). This catches future renames/relocations automatically. If MULTIPLE columns match, the converter MUST SystemExit and require --status-col to disambiguate (it never guesses).",
  "fail_closed": "converter MUST SystemExit in EITHER case below; it must NEVER emit terms with confirmed silently defaulted to false: (a) a status column is detected but no confirmation decision was supplied — a confirmation decision is --approved-statuses (values / '*' / '') and --protected-statuses ALONE is NOT enough; (b) NO status column is detected and --no-status was NOT passed (the column may have been renamed/relocated). It must print the distinct detected/unmapped status values for audit.",
  "mapping_channels": [
    "converter arg: --approved-statuses 'Approved,合规审核通过'  (status values are compared CASE-INSENSITIVELY — a rule, not per-value casing)",
    "converter arg: --approved-statuses '*' to treat the whole glossary as confirmed (choice 1)",
    "converter arg: --approved-statuses '' (empty) to explicitly treat all as unconfirmed (choice 2)",
    "converter arg: --protected-statuses '<status>' to mark those senses protected (NOT a confirmation decision on its own)",
    "converter arg: --exclude-statuses '<status>' to additionally drop rejected terms entirely (not checked, not flagged). NOTE: status 'Denied' is ALWAYS excluded by default (case-insensitive) — no flag needed (standing rule: client-rejected terms never enter the glossary)",
    "converter arg: --status-col '<header>' to disambiguate when multiple status-keyword columns exist",
    "converter arg: --no-status to assert the glossary has NO confirmation info at all (required when no status column is detected)",
    "profile.term_status_map: {\"Approved\": \"confirmed\"}  (use for status->confirmed/protection; 'Denied' must not be mapped since it is unconditionally excluded)"
  ],
  "forbidden_defaults": ["all_confirmed", "all_unconfirmed", "infer_from_unmapped_status", "silently_proceed_when_status_column_undetected"],
  "choices_when_asking": [
    "treat_entire_glossary_as_confirmed",
    "treat_as_unconfirmed_reference",
    "provide_row_or_status_mapping"
  ]
}
</pre>

## Scope

`--no-terminology` disables terminology, proper-name, and term-audit modules only. The resolved scope is stored in `state.check_scope` and `scope.json`.

<pre data-lqe-scope-contract>
{
  "mode_flag": "--no-terminology",
  "standard": {
    "required": ["terminology", "accuracy", "grammar", "naturalness"],
    "optional": ["proper_names"]
  },
  "no-terminology": {
    "required": ["precheck_review", "accuracy", "grammar", "naturalness"],
    "optional": [],
    "disabled": ["terminology", "proper_names", "term_audit"]
  },
  "scope_artifact": {
    "path": "scope.json",
    "state_field": "state.check_scope",
    "relation": "same resolved scope"
  },
  "kept_checks": ["file-wide consistency", "Markup", "numeric checks"]
}
</pre>

## Standard workflow

```bash
SKILL="${CODEX_HOME:-$HOME/.codex}/skills/lqe-translator"
SCRIPTS="$SKILL/scripts"
JOB="jobs/<job>"
```

1. Initialize. Profiles must declare `language_pair`, `source_lang`, and `target_lang`.

```bash
python3 "$SCRIPTS/lqe_io.py" read --project '<game>/<source>-<target>' \
  --input '<input.xlsx>' --source-col '<source>' --target-col '<target>' \
  --review-mode '<optimized|full>' --out "$JOB/state.json"
```

For SDLXLIFF 1.2 use `--input-format sdlxliff`; for XLIFF 2.0 use `--input-format xliff`. Single files and pure XML directories are supported. XLIFF 2.0 may omit `trgLang` only when the project profile or `--target-lang` supplies it explicitly. 未知厂商扩展 may be retained only when segment pairing remains unambiguous. `source_manifest.json` records the source and `tm_candidates.json` records strict SDL TM candidates. `SOURCE_LOCKED` segments are protected. `--protect-exact-tm` is explicit SDL opt-in. Export keeps the five-column XLSX companion and writes corrected XML without modifying the source XML.

Optional SDL profile keys are `"sdlxliff"`, `"tm_protection"`, `"content_type_rules"`, and `"exclude_rules"`; policies include `candidate-only` and `protect-exact-source-and-target`.

2. Run deterministic checks, split, and prepare compact work.

```bash
python3 "$SCRIPTS/lqe_io.py" pre-check --state "$JOB/state.json" --out "$JOB/errors_precheck.json"
python3 "$SCRIPTS/lqe_chunk.py" split --state "$JOB/state.json" --errors "$JOB/errors_precheck.json" --outdir "$JOB/chunks"
python3 "$SCRIPTS/lqe_review.py" prepare --job "$JOB"
```

For a new job, read `references/check_modules_v2/common.md`, then only its current v2 module file. Legacy jobs use the instruction locators already bound in their manifests. The 固定接口为 `{id, issues:[{category,severity,comment,needs_confirmation,edit}]}`. 检查模块不得输出 corrected; `lqe_corrections.py` 验证局部修改 and builds full text. Every compact draft uses:

```json
{"schema": "lqe.compact-module-draft", "reviewed_ids": [], "findings": [], "worker_receipt": {}}
```

The prepared `batch_plan.json`, packets, bundles, manifests, and `selected_evidence_index.json` are authoritative. Locator modes are `job_relative`, `skill_relative`, and `embedded_text`; resolve every locator and verify hashes before review. `instructions.suggestions` is the runtime instruction list and packet entries are the canonical compact projection. Each checker worker handles 每个 checker worker 最多处理 4 个 packet and 25,000 原译字符. There is 不设置字节硬上限: `budget.max_bytes: null`, `budget.status: "advisory"`; measured input goes to `cost_report.json`. Suggestion measurements are `suggestion_context/input_measurement.json` and `suggestion_review_context/input_measurement.json`. Use `--worker-batch-size N`; 不按 bytes 自动拆批或拒绝. 新批次必须新建 worker. If 流程要求 subagent but unavailable, 必须主动询问用户 and 不得静默回退.

```bash
python3 "$SCRIPTS/lqe_review.py" publish --job "$JOB" --chunk N --module <module> --input <draft.json>
python3 "$SCRIPTS/lqe_review.py" auto-publish --job "$JOB"
python3 "$SCRIPTS/lqe_chunk.py" validate-checks --job "$JOB"
python3 "$SCRIPTS/lqe_chunk.py" merge-checks --job "$JOB"
python3 "$SCRIPTS/lqe_chunk.py" reconcile --job "$JOB"
python3 "$SCRIPTS/lqe_chunk.py" merge --state "$JOB/state.json" --errors "$JOB/errors_precheck.json" --outdir "$JOB/chunks" --out "$JOB/errors.json"
```

In optimized mode, Minor 只报告问题 with `needs_confirmation: true` and `edit: null`; comments have a 20–30 character 软目标. Any uncertain correction also uses `needs_confirmation: true` 和 `edit: null`.

3. Score and create report-only reference suggestions.

```bash
python3 "$SCRIPTS/lqe_calc.py" --state "$JOB/state.json" --errors "$JOB/errors.json" --json
python3 "$SCRIPTS/lqe_suggestions.py" prepare --job "$JOB" --worker-batch-size 16
```

Read `suggestion_context/batch_plan.json` and run exactly one matching publication command:

```bash
# mode: single
python3 "$SCRIPTS/lqe_suggestions.py" publish-candidates --job "$JOB" --input "$JOB/reference_suggestions.draft.json"
# mode: batched
python3 "$SCRIPTS/lqe_suggestions.py" publish-candidates --job "$JOB" --input "$JOB/suggestion_context/batches"

python3 "$SCRIPTS/lqe_suggestion_review.py" prepare --job "$JOB"
```

Then read `suggestion_review_context/batch_plan.json` and run exactly one matching review command:

```bash
# mode: single
python3 "$SCRIPTS/lqe_suggestion_review.py" publish-review --job "$JOB" --input "$JOB/suggestion_review.draft.json"
# mode: batched
python3 "$SCRIPTS/lqe_suggestion_review.py" publish-review --job "$JOB" --input "$JOB/suggestion_review_context"

python3 "$SCRIPTS/lqe_suggestion_review.py" publish-final --job "$JOB"
python3 "$SCRIPTS/lqe_suggestions.py" validate --job "$JOB"
```

Guard v2 enforces `suggestion_candidate_rules`, confirmed-term 精确出现次数义务, known-issue assertions, and independent verification. Supported deterministic rules: `max_target_length`, `required_target_regex`, `forbidden_target_regex`, and `target_literal_count`; `reviewer_assertion` is verifier-only. Formal candidate/review/final artifacts are immutable.

一个 results basis 默认只允许一轮 generation. Never loop to improve acceptance: 不得为了提高接受数自动回到 generation or review. Rebuild/revision requires an exact user-created `--authorization-file`; the runtime copies it to `suggestion_context/rebuild_authorization.json` and consumes it once. The action must match the mutation. Never fabricate `authorized_by: "user"`. Rebuilds 绝不修改 `errors.json`.

4. Finalize only after checks, suggestions, and contracts pass.

```bash
bash "$SCRIPTS/finalize_job.sh" "$JOB" <chunk_count> single
```

Deliverables are `<job>_lqe.xlsx` and `<job>_corrected.<csv|tsv|xlsx>`. XML jobs additionally produce `<job>_corrected.<sdlxliff|xliff|xlf>` for a single file or `<job>_corrected_xliff/` for a directory. XML reports include: Segment ID、原文、原译、AI/建议译文、建议状态、错误类别、严重度、问题说明、审校结论、审校终稿或备注. The XLSX companion includes: 来源文件、TU/Unit ID、Segment ID、原文、译文. In the report, 原译中删除或替换的内容显示为红色删除线 and AI/建议译文中新增或替换的内容显示为红色字体; corrected 文件不添加差异样式.

## Multi-sheet default

Do not aggregate by default: 不运行聚合脚本. 只有用户明确要求跨工作表聚合时才运行 it.

<pre data-lqe-multisheet-delivery-contract>
{
  "default_report_mode": "separate",
  "default_unit": "child_job",
  "default_outputs": ["&lt;child&gt;_lqe.xlsx", "&lt;child&gt;_corrected.&lt;ext&gt;"],
  "aggregate_requires": "explicit_user_request",
  "forbidden_without_request": [
    "&lt;parent&gt;_lqe.xlsx",
    "combined child Results/Scorecard workbook"
  ]
}
</pre>

## Runtime routing and validation

- Commands, recovery, TM, terminology, context overrides, profile validation, exports, and tests: [`references/cli.md`](references/cli.md).
- Checker contract: [`references/check_modules_v2/common.md`](references/check_modules_v2/common.md), then only the selected v2 module. Unsuffixed checker files remain immutable for legacy jobs.
- Suggestion generation/review: [`references/suggestions_v2.md`](references/suggestions_v2.md) and [`references/suggestion_review_v2.md`](references/suggestion_review_v2.md) for new jobs. The unsuffixed files are immutable legacy runtime references.
- Feedback analysis and learning: [`references/feedback_learning.md`](references/feedback_learning.md).
- Finalized corpus publication: [`references/corpus_ingest.md`](references/corpus_ingest.md). Run it only after an explicit request to mutate the external corpus.
- Project profiles and assets: [`projects/README.md`](projects/README.md), then `projects/<game>/<pair>/profile.json`, `confirmed_rules.md`, style guide, and terminology.
- Target-language rules: [`target_languages/README.md`](target_languages/README.md), then only the selected language note.

Requires Python 3.12 or newer. Install: `python3 -m pip install -r requirements.txt`.

```bash
python3 scripts/run_tests.py
python3 /Users/spellbook/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
```
