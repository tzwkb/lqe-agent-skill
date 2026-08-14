# LQE Translator

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Agent Skill](https://img.shields.io/badge/Agent%20Skill-Codex-blue.svg)](SKILL.md)
[![Python](https://img.shields.io/badge/Python-3.x-blue.svg)](https://www.python.org/)

English | [中文](README_ZH.md)

Agent skill for game-localization LQE: deterministic pre-checks, focused AI check modules, validated local edits, scoring, and Excel deliverables.

> The PM guide is maintained outside the runtime Skill in the Langlobal development docs.

## What the workflow guarantees

- Project context is loaded from `profile.json`, `confirmed_rules.md`, the style guide, and target-language notes; standard mode also loads terminology.
- Standard mode requires terminology, accuracy, grammar, and naturalness. No-terminology mode requires `precheck_review`, accuracy, grammar, and naturalness; the optional proper-name module is standard-mode only.
- Models report `issues` and safe local `edit` operations. Python validates edits and builds the internal full-text result.
- `confirmed: true` authorizes a unique terminology edit; `protected: true` means the content must not be changed.
- Protected segments are neither changed nor scored.
- SDLXLIFF 1.2 can be read from one file or a recursively scanned directory without an intermediate workbook.
- Standard deliverables are `<job>_lqe.xlsx` plus a format-specific corrected file: CSV/TSV keeps the source extension; XLSX and SDLXLIFF use `.xlsx`.

## Directory structure

```text
lqe-translator/
├── scripts/
│   ├── lqe_io.py           # Read, pre-check, protect, report, and export
│   ├── lqe_chunk.py        # Split, validate, merge, and reconcile module results
│   ├── lqe_review.py       # Build compact review packets and publish sparse drafts
│   ├── lqe_suggestions.py  # Publish report-only full-sentence references
│   ├── lqe_corrections.py  # Validate local edits and build full-text results
│   ├── lqe_calc.py         # Calculate the LQE score
│   └── finalize_job.sh     # Validate through export in one command
├── references/
│   ├── suggestions.md
│   └── check_modules/
│       ├── common.md
│       ├── terminology.md
│       ├── precheck_review.md
│       ├── accuracy.md
│       ├── grammar.md
│       ├── naturalness.md
│       ├── proper_names.md
│       └── term_audit.md
├── target_languages/<code>/
│   ├── attributes.json
│   └── eval_notes.md
├── projects/<game>/<source>-<target>/
│   ├── profile.json
│   ├── checks.json
│   ├── confirmed_rules.md
│   ├── terms_*.json
│   └── sg*.md / sg*.txt
└── jobs/<job>/
    ├── state.json
    ├── scope.json
    ├── tabular_source_manifest.json # Tabular jobs
    ├── source_manifest.json         # SDLXLIFF jobs
    ├── tm_candidates.json         # SDLXLIFF jobs
    ├── capability_resolution.json
    ├── project_asset_snapshot.json
    ├── project_assets/
    ├── shadow_context/context.json # Shadow mode only
    ├── confirmed_rules.md
    ├── errors_precheck.json
    ├── errors.json
    ├── chunks/
    ├── review_packets/context/<module>/batch_NN/
    ├── suggestion_context/
    ├── reference_suggestions.packet.json
    ├── reference_suggestions.candidates.json
    ├── suggestion_review.packet.json
    ├── suggestion_review.json
    ├── reference_suggestions.json
    ├── <job>_lqe.xlsx
    └── <job>_corrected.<csv|tsv|xlsx>
```

## Setup

```bash
pip install "openpyxl>=3.1" "xlrd>=2.0" "jsonschema>=4.20" regex requests python-docx -q
SCRIPTS=~/.codex/skills/lqe-translator/scripts
```

Run the regression suite from the skill root:

```bash
python3 scripts/run_tests.py
```

## Workflow

### 1. Initialize a job

Project profiles are preferred because one option loads language settings, checks, confirmed rules, terminology, and the style guide.

Profile contract v2 can declare `module_context_views` for module-specific capabilities, dimensions, neighbor windows, and entity/relation/example limits. A zero runtime-example limit means no examples are delivered. `off` ignores optional views, `shadow` records them only in `state.shadow_module_context_views`, and `enforce` publishes them as formal worker input. Shadow-bound typed assets cannot enter formal bundles even if their records claim a core capability; relations marked `attributes.runtime_rule: false` are also excluded.

Before initializing a new job, the agent must ask the user to choose a review-output mode unless the current request already specifies it: `optimized` is the cost-saving mode and `full` restores complete suggestion behavior. `--review-mode` stores the choice in `state.review_policy`; an existing job keeps its stored mode.

```bash
JOB="jobs/<job>"
python3 "$SCRIPTS/lqe_io.py" read \
  --project "<game>/<source>-<target>" \
  --input "<file>.xlsx" \
  --source-col "<source column>" \
  --target-col "<target column>" \
  --review-mode "<optimized|full>" \
  --out "$JOB/state.json"
```

The profile must declare `language_pair`, `source_lang`, and `target_lang`. Run checks only after reading the project background, `confirmed_rules.md`, the style guide, and language notes.

Initialization stages and validates every asset before publication, rejects input/output/resource aliases (including symlinks and hardlinks), and publishes `state.json` last. Failure leaves no formal `state.json`, `scope.json`, `terms.json`, or partial SDL asset set.

When the request explicitly excludes terminology and proper-name checks, add `--no-terminology` to `read`. It overrides profile terminology and is mutually exclusive with an explicit `--terminology <file>`:

```bash
python3 "$SCRIPTS/lqe_io.py" read \
  --project "<game>/<source>-<target>" \
  --input "<file>.xlsx" \
  --source-col "<source column>" \
  --target-col "<target column>" \
  --no-terminology \
  --out "$JOB/state.json"
```

The resolved mode is stored in `state.check_scope` and copied to `$JOB/scope.json`. No-terminology mode disables terminology, proper-name, and term-audit work; it does not disable file-wide consistency, Markup, or numeric checks.

For SDLXLIFF, pass one `.sdlxliff` file or a directory. `--input-format` accepts `auto`, `tabular`, or `sdlxliff`; a single file and a directory containing only SDLXLIFF files are auto-detected, while a mixed directory requires the explicit format. SDLXLIFF input reads source and target segments directly, so it does not use `--source-col` or `--target-col`:

```bash
python3 "$SCRIPTS/lqe_io.py" read \
  --project "<game>/<source>-<target>" \
  --input "<file-or-directory>" \
  --input-format sdlxliff \
  --out "$JOB/state.json"
```

The first release supports XLIFF 1.2 with the SDL namespace. XLIFF 2.0 is rejected. Unknown vendor extensions are preserved and recorded when segment boundaries remain unambiguous; an extension that makes source, target, or `mid` pairing ambiguous causes the import to fail. Content type and exclusion behavior comes only from explicit profile rules, never from CC, FF, filenames, or directory names.

The following visible contract defines both resolved scopes:

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

### 2. Mark protected content

Terminology entries use explicit flags in standard mode; no-terminology mode ignores them. Explicit segment protection, including verified TM matches, remains available in both modes.

```json
{"source":"Source term","target":"Approved rendering","confirmed":true,"protected":false}
```

- Every new-job term or sense must contain boolean `confirmed` and `protected`; either missing field fails before formal job artifacts are published.
- A canonical CSV/XLSX/JSON record that already contains both boolean fields may retain `status` as audit metadata and does not require `profile.term_status_map`. If either boolean is missing, status values require an explicit mapping; do not infer confirmation.
- `protected_term_statuses` may only supplement protection and is not a confirmation decision. When present, it must be an array of non-empty strings.
- `Denied` is always excluded case-insensitively and must not be mapped.

When the input supplies exact TM-match evidence, the agent records explicit segment ids and then runs:

```bash
python3 "$SCRIPTS/lqe_io.py" protect-segments \
  --state "$JOB/state.json" \
  --protected-file "$JOB/tm_protected.agent_decision.json" \
  --reason TM_100_MATCH
```

The script never guesses match columns or values.

For SDLXLIFF, segments marked locked are always protected with reason `SOURCE_LOCKED`. By default, only segments satisfying all three conditions—`origin=tm`, `percent=100`, and `text-match=SourceAndTarget`—are written to `tm_candidates.json`; candidates are not protected automatically. Review them and pass that file to `protect-segments`, set profile policy `protect-exact-source-and-target`, or use `--protect-exact-tm` for an explicit strict decision. A plain 100% value is insufficient. If locked and exact-TM evidence coexist, `SOURCE_LOCKED` remains the effective reason and both evidence records are retained.

An optional profile section defines auditable SDLXLIFF rules:

```json
{
  "sdlxliff": {
    "tm_protection": "candidate-only",
    "content_type_rules": [
      {"id": "dialog", "glob": "**/dialog*.sdlxliff", "content_type": "Dialogue"}
    ],
    "exclude_rules": [
      {"id": "rejected", "field": "confirmation", "equals": "Rejected", "reason": "Client excluded"}
    ]
  }
}
```

### 3. Run the deterministic pre-check

```bash
python3 "$SCRIPTS/lqe_io.py" pre-check \
  --state "$JOB/state.json" \
  --out "$JOB/errors_precheck.json"
```

Checks include untranslated or empty targets, variables, tags, line breaks, numbers, length, whitespace, punctuation, repeated words, case, source-target consistency, and project-specific rules. Standard mode also runs terminology and terminology-dependent proper-name checks. No-terminology mode skips those checks while keeping all non-terminology pre-checks for contextual review.

### 4. Split and run check modules

```bash
python3 "$SCRIPTS/lqe_chunk.py" split \
  --state "$JOB/state.json" \
  --errors "$JOB/errors_precheck.json" \
  --outdir "$JOB/chunks" \
  --size 100

python3 "$SCRIPTS/lqe_review.py" prepare --job "$JOB"
python3 "$SCRIPTS/lqe_review.py" auto-publish --job "$JOB"
```

`split` reads terminology through the state in standard mode. `--terms <file>` is an optional standard-mode override and is rejected in no-terminology mode. Split inputs are fingerprinted; when the state, current target, scope, pre-check, terms, or split settings change, stale chunk artifacts are archived and old module outputs cannot be reused.

`prepare` creates generation-bound, module-specific `review_packets`, `batch_plan.json`, and `cost_report.json`. Non-terminology modules no longer receive terminology or pre-check fields they do not use. Protected segments and inapplicable `precheck_review` rows are deterministically filled with empty results. `auto-publish` handles only packets that require no AI review.

For every `chunk_NN.json`, produce the files selected by `state.check_scope`:

```text
# Standard mode
chunk_NN.terminology.json
chunk_NN.accuracy.json
chunk_NN.grammar.json
chunk_NN.naturalness.json

# No-terminology mode
chunk_NN.precheck_review.json
chunk_NN.accuracy.json
chunk_NN.grammar.json
chunk_NN.naturalness.json
```

Assign bounded workers from `batch_plan.json`. One worker handles at most four packets and no more than 25,000 source-plus-target characters or 100,000 total input bytes across instructions, project context, shared assets, bundles, manifests, and packets. A smallest indivisible unit that still exceeds the limit fails instead of being truncated. Every new batch starts a new worker and reloads the module specification and job context.

The model writes a compact draft: `reviewed_ids` exactly copies the packet, while `findings` contains only ids with issues. Publish it with `lqe_review.py publish --job "$JOB" --chunk <NN> --module <module> --input <draft.json>`. The publisher restores formal full-id coverage and validates ownership, pre-check references, and generation binding under the existing contract.

`precheck_review` confirms or removes non-terminology pre-check findings in the Markup, Length, Locale convention, Company style, Inconsistency, and Other categories. It must not create Terminology issues, `TERM REVIEW:` evidence, or `confirmed_term` edits.

The compact draft contract is:

```json
{
  "schema": "lqe.compact-module-draft",
  "version": 1,
  "module": "grammar",
  "chunk_id": 0,
  "packet_digest": "<packet.packet_digest>",
  "worker_batch_id": "<packet.worker_batch_id>",
  "worker_packet_basis_digest": "<packet.worker_packet_basis_digest>",
  "context_bundle_set_digest": "<packet.context_bundle_set_digest>",
  "worker_context_manifest_digest": "<packet.worker_context_manifest_digest>",
  "reviewed_ids": [0, 1, 2],
  "findings": [
    {
      "id": 1,
      "issues": [
        {
          "category": "Grammar",
          "severity": "Minor",
          "comment": "The verb form does not agree with the subject.",
          "needs_confirmation": true,
          "edit": null
        }
      ]
    }
  ]
}
```

In `optimized` mode, every `comment` must be non-empty English with 20–30 characters as a soft target; Minor issues use `needs_confirmation: true` and `edit: null` and never produce corrected text or automatic iteration changes. In `full` mode there is no character target, and the agent may provide a safe, unique local edit for any severity, including Minor.

Use `needs_confirmation: true` and `edit: null` for a new name, missing terminology, multiple reasonable options, or a rewrite. A terminology or proper-name edit also requires one unique `confirmed: true` candidate and `confirmed_term` evidence.

Every Terminology issue must also carry `term_source`, `expected_targets`, and `term_spans`:

```json
{
  "term_source": "督管案台",
  "expected_targets": ["Supervisor's Counter"],
  "term_spans": {
    "source": [{"start": 2, "end": 6, "text": "督管案台"}],
    "target": [{"start": 10, "end": 22, "text": "control desk"}]
  }
}
```

`term_spans` has exactly the `source` and `target` arrays. Each span object has exactly integer `start`, integer `end`, and non-empty `text`, using a non-empty zero-based, half-open range. Arrays are sorted by `(start,end,text)` and contain no duplicates or overlaps. `text` must exactly equal the corresponding source or current-target slice, and every source-span `text` must equal `term_source`. `source` is non-empty. `target` may be empty when no affected target token can be located safely, including omissions; do not guess or mark the whole sentence. When reviewing a machine-generated Terminology issue, inherit read-only `term_source`, `expected_targets`, and `term_spans.source` through `precheck_ref`, then locate the affected current-target token in `term_spans.target`; leave that array empty only when no token can be marked safely. Newly found Terminology issues must supply all three fields.

The tabular columns `content_type`, `text_type`, `文本类型`, and `文本类别` are passed into review packets as upstream text classifications and are never inferred. `optimized` mode uses row-level `content_type`, then `text_type_context`, to apply the matrix in `references/check_modules/common.md`; `full` keeps the classification as context without changing review intensity. Neither mode disables deterministic checks or required modules. Source text is never skipped because it resembles a section label; only marker rows explicitly declared in profile `tabular.text_type_marker_rules` are consumed and audited.

New jobs also bind stable segment identity, source provenance, and a project asset/capability snapshot. Use `--sheet`, `--key-col`, and repeatable `--context-col FIELD=COLUMN` when the input carries explicit context; aliases include `--content-type-col`, `--speaker-col`, and `--addressee-col`. Optional profile capabilities affect formal packets only in `enforce` mode; `shadow` writes `shadow_context/context.json` and never feeds formal deduplication, review, suggestions, or reports. Legacy `.xls` is read with `xlrd>=2.0` and corrected output is always `.xlsx`.

Profile v2 uses an asset registry to declare path, authority, provenance, and distribution, plus a capability registry to decide which modules may see each resource. Use canonical JSON assets for entities, relationships, review examples, and language policies. Raw XLSX/DOCX may remain as provenance, but merely placing files in a directory never injects them. Each batch's `worker_manifest.json` binds the actual instructions, style guide, language notes, shared assets, context bundles, and packets. Total worker input is capped at 100,000 bytes.

Cross-sheet or cross-version checks use `--pivot-sheet`, `--pivot-key-col`, repeatable `--pivot-compare`, and explicit `--pivot-authority`. Missing, duplicate, or authoritative mismatched keys block review. A historical job without `job_runtime_contract_version: 2` is validation-only; resume it by creating a new bound job:

```bash
python3 "$SCRIPTS/lqe_io.py" reread \
  --from-job <old-job> --input <original-input> --job <new-job>
```

### 5. Validate, merge, score, and export

```bash
python3 "$SCRIPTS/lqe_chunk.py" validate-checks --job "$JOB"
python3 "$SCRIPTS/lqe_chunk.py" merge-checks --job "$JOB"
python3 "$SCRIPTS/lqe_chunk.py" reconcile --job "$JOB"
python3 "$SCRIPTS/lqe_chunk.py" merge \
  --state "$JOB/state.json" \
  --errors "$JOB/errors_precheck.json" \
  --outdir "$JOB/chunks" \
  --out "$JOB/errors.json"

python3 "$SCRIPTS/lqe_calc.py" \
  --state "$JOB/state.json" \
  --errors "$JOB/errors.json"
```

`merge` re-derives the merged issues and provenance from the current bound module outputs, verifies both the content digest and an independent local publication receipt for formal module entries, rejects a forged intermediate merged file, and atomically publishes `errors.json` with `errors.contract.json`. Model drafts cannot self-declare AI review/edit status. Calc, write, apply, export, and aggregation hold a generation lease and reject missing provenance or a missing, tampered, or stale contract. Jobs created by the current reader also reject state-only verification when `chunks/` is missing.

When full-sentence reference suggestions are needed, run after merge and calc:

```bash
python3 "$SCRIPTS/lqe_suggestions.py" prepare \
  --job "$JOB" --severities "Major,Critical" --only-missing
python3 "$SCRIPTS/lqe_suggestions.py" publish-candidates \
  --job "$JOB" --input <reference-suggestion-draft.json>
python3 "$SCRIPTS/lqe_suggestion_review.py" prepare --job "$JOB"
python3 "$SCRIPTS/lqe_suggestion_review.py" publish-review \
  --job "$JOB" --input <suggestion-review-draft.json>
python3 "$SCRIPTS/lqe_suggestion_review.py" publish-final --job "$JOB"
```

`optimized` defaults candidates to Major/Critical; `full` defaults to every severity. Unresolved terminology decisions, blocked/protected segments, and constraint conflicts are rejected before generation. Resolved constraints are re-evaluated against the generated candidate: a definite mismatch is rejected, while an inconclusive result goes to the independent verifier. Only accepted candidates enter the v5 final artifact; stale candidate, review, or final digests fail closed.

For a first-round review, explicitly use `single`:

```bash
bash "$SCRIPTS/finalize_job.sh" "$JOB" <chunk-count> single
```

Choose `iterate` only when the user has explicitly requested automatic iteration. PASS alone creates `.finalized`; FAIL+single writes review artifacts without changing the current target or finalizing. FAIL+iterate advances `current_target`/iteration, sets `pending_recheck=true`, and returns `PENDING-RECHECK` only when at least one re-verified safe local edit was applied. With zero applicable edits, it writes the round report, exports the verified error overlay with `export --errors`, returns `REVIEW-REQUIRED`, removes `.iteration_pending`, and does not advance the iteration. The next round must rerun pre-check, split, and all modules.

### 6. Write the standard deliverables

```bash
python3 "$SCRIPTS/lqe_io.py" write \
  --state "$JOB/state.json" \
  --errors "$JOB/errors.json" \
  --score <score>

python3 "$SCRIPTS/lqe_io.py" export \
  --state "$JOB/state.json" \
  --errors "$JOB/errors.json"
```

Every input produces `<job>_lqe.xlsx` with the score, issues, suggested text, review handling, and history. The corrected output depends on the input format:

`write --score` is a consistency input. `write` recomputes the score from state policy and errors, warns on disagreement, and uses the recomputed result.

- CSV/TSV inputs produce `<job>_corrected.csv` or `<job>_corrected.tsv` and preserve rows, columns, and the source extension.
- XLSX input produces `<job>_corrected.xlsx` and preserves the workbook, worksheets, blank rows, column order, and formatting.
- SDLXLIFF produces a new fixed five-column `<job>_corrected.xlsx`.

Reports have three visible worksheets: `说明·导读`, `LQA Scorecard`, and `LQE Results`; `_LQE_CONTRACT` remains very hidden. The guide is first and is the default opening sheet, with a three-step reading flow, Scorecard guidance, definitions for all ten review columns, status and decision guidance, and a delivery checklist. The Scorecard shows the verdict, score, compact category summary, and all per-issue review rows without hidden rows or columns.

The Scorecard issue area and `LQE Results` share ten review columns: `Segment ID`, `原文`, `原译`, `AI/建议译文`, `建议状态`, `错误类别`, `严重度`, `问题说明`, `审校结论`, and `审校终稿或备注`. `原译` means the translation actually reviewed in that iteration: the imported target in iteration 0 and the previously applied `current_target` afterward. Term spans, diff rendering, and suggestions use that same baseline. The Scorecard combines parent and sub-category and omits file name, iteration, processing, and AI provenance columns; those audit fields remain in the hidden Results area. The Results reviewer view uses one visible row per segment; extra per-issue audit rows and clean segments are hidden. Status values are `可直接采用`, `建议待确认`, `部分修正，仍需确认`, `未生成建议，需人工处理`, and `已保护`.

Terminology detail exports and report highlighting read `term_source`, `expected_targets`, and `term_spans` directly from each issue. They never recover terms or offsets from `comment`. An old job with any Terminology issue missing these fields is incompatible and must rerun from `pre-check`, then rebuild all downstream artifacts.

Rich-text reports show affected source terms in red. Affected original-target terms are red; when the same text is removed or replaced, it is red with strikethrough. Inserted or replaced suggested text is red. When `term_spans.target` is empty, only the source term is highlighted. Results uses the union of all Terminology spans for a segment; each Scorecard row uses only that issue's spans. Historical entries containing Terminology or `term_spans` must carry `review_targets` as `{"<segment_id>": "reviewed target"}`; missing, damaged, or slice-mismatched snapshots fail closed. Safe local edits can enter the corrected workflow. Full-sentence entries in the separate `reference_suggestions.json` artifact are report-only and always marked `建议待确认`. Corrected files remain plain text and receive no diff or terminology styling.

In verified internal results, `corrected: ""` is a valid deletion of the whole target; only `corrected: null` means no suggested change. Write, apply, export, and aggregation preserve that distinction.

Tabular and SDLXLIFF reports use the same ten-column reviewer view. Source file, TU ID, SDL Segment ID, processing, per-issue provenance, protection evidence, and `LQE_Iter` remain in the hidden audit area; `LQE_Iter` is always last. `source_manifest.json` stores input SHA-256 hashes, declared languages, extension namespaces, rule matches, exclusions, and locked/TM evidence. The new corrected workbook uses five columns: `来源文件`, `TU ID`, `SDL Segment ID`, `原文`, `译文`.

The first release does not write back to SDLXLIFF XML. `export` creates `<job>_corrected.xlsx` and leaves every source XML file unchanged.

## Scoring

```text
K_per_category = Σ severity_points
L_per_category = weight × K
score = max((1 - ΣL / fixed_wordcount) × 100, 0)
```

`state.scoring_policy` is the default for calc, write, reports, iteration, and aggregation; CLI flags are explicit overrides only. The policy contains threshold, scorecard profile, LISA/MQM severity scale, Critical gate, and repeat dedup. Repeated annotations are cleared and rebuilt on every score. Default severity points are Neutral 0, Minor 1, Major 5, and Critical 10; the default threshold is 98. Protected segments are skipped.

## Multi-sheet workbooks

The default delivery is separate: create one child job and one standard
`<child>_lqe.xlsx` report per selected sheet, then stop after the child
deliverables are complete. Do not create a parent aggregate report or copy
multiple child Results/Scorecards into one workbook unless the user explicitly
asks for a combined report, cross-sheet summary, or restored source-workbook
delivery.

Only for an explicit aggregation request, run:

```bash
python3 "$SCRIPTS/aggregate_sheets.py" \
  --job <job> \
  --sheets <sheet-a>,<sheet-b>
```

For explicit aggregation, the parent job preserves worksheet order, blank rows, formulas, styles, and merged cells while replacing only validated/current targets. Aggregation revalidates every child against its current state, `errors.contract.json`, and verified chunk generation, including chunk terminology context. Each hidden `_LQE_CONTRACT` binds state/errors, visible `LQE Results`, and its per-issue provenance layout; the aggregate copies both child Results and Scorecard history. Before publication, aggregation reacquires every child lease in stable order. Missing, corrupt, stale, or unbound results/reports, stale chunk evidence, or source drift fail without replacing existing parent outputs. Child policies are inherited; incompatible non-threshold policies fail closed. An explicit `--threshold` overrides only the threshold, and any child FAIL makes the aggregate FAIL.

This aggregation command is for tabular workbooks only; an SDLXLIFF directory is one multi-file job, not a multi-sheet workbook.

## Verification

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 scripts/run_tests.py
```
