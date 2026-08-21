# LQE Runtime Command Reference

Read this file before running a nontrivial LQE command. Commands below reflect the
native argparse interfaces; do not invent aliases or replace a failed command with
a custom pipeline.

## Setup

```bash
SKILL="${CODEX_HOME:-$HOME/.codex}/skills/lqe-translator"
SCRIPTS="$SKILL/scripts"
JOB="jobs/<job>"
```

Use Python 3.12 or newer from the active Codex environment. Install runtime
dependencies only when missing:

```bash
python3 -m pip install -r requirements.txt
```

## Standard end-to-end workflow

### 1. Initialize

Tabular input:

```bash
python3 "$SCRIPTS/lqe_io.py" read \
  --project '<game>/<source>-<target>' \
  --input '<input.xlsx>' \
  --source-col '<source>' \
  --target-col '<target>' \
  --review-mode '<optimized|full>' \
  --out "$JOB/state.json"
```

SDLXLIFF 1.2 input:

```bash
python3 "$SCRIPTS/lqe_io.py" read \
  --project '<game>/<source>-<target>' \
  --input '<file-or-directory>' \
  --input-format sdlxliff \
  --review-mode '<optimized|full>' \
  --out "$JOB/state.json"
```

XLIFF 2.0 input (`.xliff` or `.xlf`):

```bash
python3 "$SCRIPTS/lqe_io.py" read \
  --project '<game>/<source>-<target>' \
  --input '<file-or-directory>' \
  --input-format xliff \
  --review-mode '<optimized|full>' \
  --out "$JOB/state.json"
```

When XLIFF 2.0 omits root `trgLang`, the project profile or `--target-lang`
must supply the target language. Conflicting declarations fail closed.

Useful read switches include `--sheet`, `--key-col`, repeatable `--context-col`,
`--no-header`, `--group-col`, `--style-guide`, `--source-lang`, `--target-lang`,
`--profile-overlay`, `--context-overrides`, `--protect-exact-tm`, and the mutually
exclusive `--terminology` / `--no-terminology` flags. Context aliases are
`--content-type-col`, `--speaker-col`, `--addressee-col`,
`--relationship-stage-col`, `--scene-id-col`, `--scene-tone-col`, and
`--context-note-col`.

### 2. Deterministic checks and checker review

```bash
python3 "$SCRIPTS/lqe_io.py" pre-check \
  --state "$JOB/state.json" \
  --out "$JOB/errors_precheck.json"

python3 "$SCRIPTS/lqe_chunk.py" split \
  --state "$JOB/state.json" \
  --errors "$JOB/errors_precheck.json" \
  --outdir "$JOB/chunks"

python3 "$SCRIPTS/lqe_review.py" prepare --job "$JOB"
python3 "$SCRIPTS/lqe_review.py" publish \
  --job "$JOB" --chunk N --module '<module>' --input '<draft.json>'
python3 "$SCRIPTS/lqe_review.py" auto-publish --job "$JOB"
python3 "$SCRIPTS/lqe_chunk.py" validate-checks --job "$JOB"
python3 "$SCRIPTS/lqe_chunk.py" merge-checks --job "$JOB"
python3 "$SCRIPTS/lqe_chunk.py" reconcile --job "$JOB"
python3 "$SCRIPTS/lqe_chunk.py" merge \
  --state "$JOB/state.json" \
  --errors "$JOB/errors_precheck.json" \
  --outdir "$JOB/chunks" \
  --out "$JOB/errors.json"
```

`auto-publish` publishes already valid drafts; it does not authorize inventing
review output. Publish a directory of checker drafts with:

```bash
python3 "$SCRIPTS/lqe_review.py" publish-directory \
  --job "$JOB" --input-dir '<draft-dir>'
```

Recovery from an explicitly selected source job uses:

```bash
python3 "$SCRIPTS/lqe_review.py" reuse-drafts \
  --source-job '<source-job>' --job "$JOB" --drafts '<draft-dir>' --out '<out-dir>'
```

### 3. Score and suggestion chain

```bash
python3 "$SCRIPTS/lqe_calc.py" \
  --state "$JOB/state.json" --errors "$JOB/errors.json" --json

python3 "$SCRIPTS/lqe_suggestions.py" prepare \
  --job "$JOB" --worker-batch-size 16
```

Read `$JOB/suggestion_context/batch_plan.json`. Run exactly one command matching
its `mode`:

```bash
# mode: single
python3 "$SCRIPTS/lqe_suggestions.py" publish-candidates \
  --job "$JOB" --input "$JOB/reference_suggestions.draft.json"
# mode: batched
python3 "$SCRIPTS/lqe_suggestions.py" publish-candidates \
  --job "$JOB" --input "$JOB/suggestion_context/batches"

python3 "$SCRIPTS/lqe_suggestion_review.py" prepare --job "$JOB"
```

Read `$JOB/suggestion_review_context/batch_plan.json`. Run exactly one command
matching its `mode`:

```bash
# mode: single
python3 "$SCRIPTS/lqe_suggestion_review.py" publish-review \
  --job "$JOB" --input "$JOB/suggestion_review.draft.json"
# mode: batched
python3 "$SCRIPTS/lqe_suggestion_review.py" publish-review \
  --job "$JOB" --input "$JOB/suggestion_review_context"
python3 "$SCRIPTS/lqe_suggestion_review.py" publish-final --job "$JOB"
python3 "$SCRIPTS/lqe_suggestions.py" validate --job "$JOB"
```

For batched generation, the input directory contains
`batch_XXXX/generation.draft.json`; for batched review, it contains
`batch_XXXX/review.draft.json`. Any authorized rebuild/revision must pass
the exact user-created `--authorization-file` to the relevant prepare or publish
command. Authorization is action-specific and single-use.

Do not use `lqe_suggestions.py publish`: it is a disabled legacy entry point and
intentionally fails. Use `publish-candidates`.

### 4. Finalize

```bash
bash "$SCRIPTS/finalize_job.sh" "$JOB" '<chunk-count>' single
```

Use `iterate` only for a workflow that explicitly requires iterative finalization.
Suggestion generation/review must never be looped merely to increase acceptance.

## Input, protection, and terminology commands

Re-read a changed source into a new job while preserving compatible lineage:

```bash
python3 "$SCRIPTS/lqe_io.py" reread \
  --from-job '<old-job>' --input '<input>' --job "$JOB" \
  --profile-overlay '<overlay.json>' --context-overrides '<overrides.json>'
```

Protect selected segments:

```bash
python3 "$SCRIPTS/lqe_io.py" protect-segments \
  --state "$JOB/state.json" --protected-file '<ids.txt>' \
  --reason '<reason>' --out "$JOB/state.json"
```

`--protected-ids '<id,id,...>'` may replace `--protected-file`.

Inspect terminology hits for all or selected IDs:

```bash
python3 "$SCRIPTS/lqe_io.py" lookup-terms \
  --state "$JOB/state.json" --ids '<id,id,...>'
```

Convert a master terminology workbook. Explicit confirmation handling is
mandatory; see the contract in `SKILL.md`.

```bash
python3 "$SCRIPTS/mastertb_to_terms.py" \
  --input '<master.xlsx>' --out '<terms.json>' \
  --status-col '<header>' \
  --approved-statuses 'Approved,合规审核通过' \
  --protected-statuses '<protected-statuses>' \
  --exclude-statuses '<extra-rejected-statuses>'
```

Use `--approved-statuses '*'` for fully confirmed terminology,
`--approved-statuses ''` for explicitly unconfirmed terminology, or `--no-status`
when the workbook truly contains no status field. Other switches include
`--target-col`, `--source-hdr`, and `--backfill`.

Prepare and inspect master terminology:

```bash
python3 "$SCRIPTS/mastertb_prep.py" prep --input '<master.xlsx>' --job-dir '<term-job>'
python3 "$SCRIPTS/mastertb_prep.py" chunks --job-dir '<term-job>' --size 300
python3 "$SCRIPTS/mastertb_prep.py" merge --job-dir '<term-job>'
python3 "$SCRIPTS/mastertb_prep.py" report --job-dir '<term-job>' --label '<label>'
python3 "$SCRIPTS/mastertb_prep.py" view --job-dir '<term-job>' --size 300
```

`mastertb_prep.py merge --no-consistency` disables its consistency pass only when
the user explicitly wants that behavior.

Build and query strict TM protection:

```bash
python3 "$SCRIPTS/tm_index.py" build \
  --libraries '<tm-a>' '<tm-b>' --out '<tm-index.json>'
python3 "$SCRIPTS/tm_index.py" tm-match \
  --state "$JOB/state.json" --index '<tm-index.json>' \
  --out-protected '<protected.json>'
```

Search a term list interactively:

```bash
python3 "$SCRIPTS/term_suggest.py" \
  --terms '<terms.json>' --query '<text>' -k 10 --threshold 0.6
```

## Context overrides and project profiles

Find missing context and scaffold an override file:

```bash
python3 "$SCRIPTS/lqe_context_overrides.py" gaps \
  --state "$JOB/state.json" --out "$JOB/context_gaps.json"
python3 "$SCRIPTS/lqe_context_overrides.py" scaffold \
  --state "$JOB/state.json" --out "$JOB/context_overrides.json" \
  --source-id '<source-id>' --issuer '<issuer>'
```

Validate a project profile or canonical asset, then validate overlays:

```bash
python3 "$SCRIPTS/lqe_profile_ingest.py" validate '<profile.json>' \
  --target-lang '<lang>'
python3 "$SCRIPTS/lqe_profile_ingest.py" validate '<canonical-asset.json>' \
  --schema '<canonical-schema-name>' --target-lang '<lang>'
python3 "$SCRIPTS/lqe_profile_ingest.py" validate-overrides \
  --input '<overrides.json>' --segments '<segments.json>' \
  --declared-extensions '<extensions.json>' --source-ids '<ids.json>'
python3 "$SCRIPTS/lqe_profile_ingest.py" manifest \
  --project '<project>' --manifest-scope '<internal|public|redacted>' \
  --sources '<sources.json>' --generated-assets '<assets.json>' \
  --coverage '<coverage.json>' --coverage-details '<details.json>' \
  --output '<manifest.json>'
```

Read `../projects/README.md` before creating or changing a project profile. Read
`../target_languages/README.md` and the selected language note before applying
target-language rules.

## Lower-level result and export commands

Apply validated fixes to state:

```bash
python3 "$SCRIPTS/lqe_io.py" apply-fixes \
  --state "$JOB/state.json" --errors "$JOB/errors.json" --score \
  --protected-file '<protected.json>'
```

Build result rows, write the report, or export corrected source separately:

```bash
python3 "$SCRIPTS/lqe_io.py" build-results \
  --state "$JOB/state.json" --checks "$JOB/errors.json" \
  --out "$JOB/results.json"
python3 "$SCRIPTS/lqe_io.py" write \
  --state "$JOB/state.json" --errors "$JOB/errors.json" --score "$JOB/score.json"
python3 "$SCRIPTS/lqe_io.py" export \
  --state "$JOB/state.json" --errors "$JOB/errors.json"
```

Scoring switches shared by `lqe_calc.py`, `apply-fixes`, and `write` include
`--threshold`, `--scorecard-profile`, `--severity-scale lisa|mqm`,
`--critical-gate` / `--no-critical-gate`, and
`--repeat-dedup` / `--no-repeat-dedup`. Protection can use `--protected-ids` or
`--protected-file`.

XML export keeps the five-column corrected XLSX companion and also writes corrected
XML. A single file produces `<job>_corrected.<sdlxliff|xliff|xlf>`; a directory
produces `<job>_corrected_xliff/` with the original relative paths. Source XML is
never modified.

Publish a finalized job to a corpus only after explicit external-mutation
authorization. Use `--dry-run` first when the endpoint contract is new:

```bash
python3 "$SCRIPTS/lqe_io.py" ingest-corpus \
  --state "$JOB/state.json" --aipe-url '<https-url>' \
  --auth-env '<token-env-name>' --batch-size 500 --concurrency 4 \
  --timeout 30 --retries 2
```

Optional switches are `--changed-only`, `--dry-run`, `--payload-out`, and
`--receipt`. The versioned request/response contract and fail-closed rules are in
[`corpus_ingest.md`](corpus_ingest.md).

## Chunk recovery and checkpoint commands

```bash
python3 "$SCRIPTS/lqe_chunk.py" split-half --job "$JOB" --chunk N --module '<module>'
python3 "$SCRIPTS/lqe_chunk.py" join-parts \
  --parts '<part-a.json>' '<part-b.json>' --out '<joined.json>' \
  --review-mode '<optimized|full>'
python3 "$SCRIPTS/lqe_chunk.py" ckpt-append \
  --file '<checkpoint.jsonl>' --entry '<entry.json>' \
  --review-mode '<optimized|full>'
python3 "$SCRIPTS/lqe_chunk.py" ckpt-finalize \
  --jsonl '<checkpoint.jsonl>' --out '<draft.json>' \
  --review-mode '<optimized|full>'
```

The legacy optional proper-name path is available only when its manifest requires
it:

```bash
python3 "$SCRIPTS/lqe_chunk.py" publish-module \
  --job "$JOB" --chunk N --module proper_names --input '<draft.json>' \
  --split-fingerprint '<fingerprint>' --chunk-payload-digest '<digest>'
```

Batch planning and merge helpers:

```bash
python3 "$SCRIPTS/lqe_batch.py" plan --job "$JOB" --output-budget 24000
python3 "$SCRIPTS/lqe_batch.py" merge --job "$JOB"
```

## Multi-sheet aggregation

Only run this when the user explicitly requests cross-sheet aggregation:

```bash
python3 "$SCRIPTS/aggregate_sheets.py" \
  --job '<parent-job>' --sheets '<sheet-a,sheet-b>' --threshold '<score-threshold>'
```

## Verification

```bash
cd "$SKILL"
python3 scripts/run_tests.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 /Users/spellbook/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
```
