# Suggestion generation v2

Generate complete replacement targets only for the IDs in one assigned packet. Read the packet, its bound manifest/bundle/content index, project rules, language notes, and `checker_selected_evidence.strict_union`; verify every locator digest. Do not read wider evidence or another batch.

## Output

Write one `generation.draft.json` matching schema v5. Copy packet, manifest, evidence and reviewed-ID bindings exactly. Use a fresh worker/run receipt. Cover every assigned ID exactly once in either `entries` or `abstained_ids`; the two sets must not overlap.

Each entry must preserve source meaning, variables, tags, line breaks, protected text, and required target forms. Record resolved source semantics and tone decisions; `tone_decision.uncertainties` is always empty. Current target text is diagnostic context, not authority.

## Decision rules

- Source meaning governs subjects, actions, objects, polarity, modality, speech act, intensity, and omissions.
- Use only confirmed terminology and strict-union context that applies to this occurrence. A near match, substring, different sense, or current target does not authorize a term.
- Preserve each confirmed exact target and required occurrence count unless the packet explicitly marks that occurrence non-authorizing.
- Apply every `suggestion_candidate_rules` item. The publisher rechecks deterministic rules.
- Missing dialogue metadata is not automatic abstention when source form is already clear and the candidate does not choose an unknown relationship, gender, or register. Abstain when the missing fact would materially change the candidate.
- If source identity/meaning conflicts with a confirmed project rule, abstain with both sides stated; never output a semantically wrong sentence merely to satisfy a mechanical term check.
- Use the packet's allowed abstention reason codes. Do not hide uncertainty in semantics, tone, or evidence fields.

## Stop conditions

One results basis gets one generation round. Existing formal candidates are immutable. Do not rewrite drafts to improve verifier acceptance. `reject` and `human_required` are final for this basis; only an exact, one-use user authorization may permit a revision or rebuild.
