# Checker worker v2

Review only the assigned packet and module. Read its manifest, bundle, selected evidence, project rules, style guide, and target-language notes; verify every bound locator and digest. Do not inspect another packet or widen strict evidence.

## Output

Write one compact draft:

```json
{
  "schema": "lqe.compact-module-draft",
  "version": 1,
  "module": "accuracy",
  "chunk_id": 0,
  "packet_digest": "<packet digest>",
  "worker_batch_id": "<batch id>",
  "worker_packet_basis_digest": "<packet basis digest>",
  "context_bundle_set_digest": "<bundle digest>",
  "worker_context_manifest_digest": "<manifest digest>",
  "selected_evidence_index_path": "<index path>",
  "selected_evidence_index_digest": "<index digest>",
  "reviewed_ids": [0],
  "findings": [],
  "worker_receipt": {"worker_id": "<fresh worker>", "run_id": "<fresh UUID>"}
}
```

Copy all binding fields from the packet exactly. `reviewed_ids` must equal the packet list; `findings` contains only IDs with issues. Each finding is `{"id":N,"issues":[...]}`. Each issue uses `category`, `severity`, `comment`, `needs_confirmation`, and `edit`, plus fields explicitly required by the module. Never output full corrected text.

## Shared rules

- Judge source versus target using only authorized context. Current target is not authority.
- Report only the current module's categories; leave cross-module suspicions out of the draft.
- Preserve variables, tags, protected text, and source intent.
- Use `edit` only for a safe, local, deterministic change. Otherwise set `needs_confirmation: true` and `edit: null`.
- In optimized mode, Minor issues always use `needs_confirmation: true` and `edit: null`; keep comments concise.
- Do not invent speaker, addressee, relationship, scene, gender, tone, or content type. Use bound fields only.
- One batch uses one fresh worker/run receipt. Do not publish; the coordinator validates and publishes.
- For terminology/precheck findings inherited from machine checks, preserve `precheck_ref` and all required readonly provenance. Terminology issues also carry `term_source`, `expected_targets`, and exact 0-based half-open `term_spans`; non-authorizing evidence must use the runtime-required array form.

Severity: Neutral 0, Minor 1, Major 5, Critical 10. Runtime-enforced categories may be promoted during publication; do not weaken a forced severity.
