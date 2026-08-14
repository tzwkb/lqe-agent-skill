# Suggestion 独立候选验收

该 worker 只验收 `suggestion_review.packet.json` 中已有候选，不得修改、重写或替换 `reference_target`。

单批时读取根 packet；若根 packet 带 `batch_plan`，根 packet 只供本地汇总，每个新 verifier 只读取计划列出的一个 `packet.json`，并把草稿写到该批 `draft_path`。同时读取 packet 指向的 generation `worker_manifest.json`、`bundle_set.json` 和 `content_index.json` 全部可读资料。verifier 与 generation 使用同一份逐句 `checker_selected_evidence.strict_union`：它是实际 checker 已选择的句段上下文、人物事实、关系、运行时示例、约束和邻句的唯一正式并集。SG、背景、确认规则和语言说明仍完整可读，但不能用于替当前句重新选取或扩大正式证据；完整 source manifest 只做 runtime live validation，不作为 worker 语义全文。索引仅用于 path/digest 审计，不由 worker 整体读取。

逐项以 source 为准检查：

1. subjects；
2. actions；
3. objects；
4. polarity/negation；
5. modality；
6. speech act；
7. text function；
8. intensity；
9. source→candidate 是否漏掉条件、范围或其他信息；
10. candidate→source 是否增添无来源信息；
11. tone、politeness、register 和 character voice 是否有 source/正式上下文证据。对话字段缺失本身不是失败；若 source 已明确 speech act、intensity 或敌意，且候选没有加入依赖未知关系的称谓、代词、礼貌等级或 character voice，可按 source 证据通过。

还要确认全部 `known_issues` 已解决，变量、标签、换行、保护文本和 confirmed constraints 均满足。当前 target 不是候选含义正确的证据。证据不足或需要业务选择时返回 `human_required`。

草稿格式：

```json
{
  "schema": "lqe.suggestion-review-draft",
  "version": 1,
  "review_packet_digest": "<packet.packet_digest>",
  "worker_context_manifest_digest": "<packet.worker_context_manifest_digest>",
  "selected_evidence_index_digest": "<packet.selected_evidence_index.digest>",
  "worker_receipt": {
    "worker_id": "<与所有 checker 和 generation worker 不同的稳定 ID>",
    "run_id": "<与所有 checker 和 generation run 不同的稳定 ID>"
  },
  "reviewed_ids": [0],
  "verdicts": [
    {
      "id": 0,
      "candidate_digest": "<packet entry candidate_digest>",
      "decision": "accept",
      "reason_codes": [],
      "evidence": "Checked every source proposition, tone decision and constraint.",
      "semantic_verification": {
        "subjects": {"status": "pass", "evidence": "Subject retained."},
        "actions": {"status": "pass", "evidence": "Action retained."},
        "objects": {"status": "pass", "evidence": "Objects retained."},
        "polarity_negation": {"status": "pass", "evidence": "Polarity retained."},
        "modality": {"status": "pass", "evidence": "Imperative retained."},
        "speech_act": {"status": "pass", "evidence": "Command retained."},
        "text_function": {"status": "pass", "evidence": "Dialogue function retained."},
        "intensity": {"status": "pass", "evidence": "Force retained."},
        "omissions": {"status": "pass", "evidence": "No source element omitted."},
        "unsupported_additions": {"status": "pass", "evidence": "No unsupported proposition."},
        "tone": {"status": "pass", "evidence": "Tone follows cited evidence."}
      }
    }
  ]
}
```

合同：

- `reviewed_ids`、`review_packet_digest`、`worker_context_manifest_digest` 和 `selected_evidence_index_digest` 原样复制；每个 ID 恰有一个 verdict。
- `worker_receipt` 必填；`worker_id` 和 `run_id` 各自都不得与任一 checker 或 generation receipt 的对应值重复。只更换其中一个字段仍不合格；多批 review 每批也必须使用新的 ID。
- 语义、语气和上下文复核只能使用 packet 内嵌的 checker 严格证据并集；不得从更宽资料重新选取有利证据来放行候选。
- `candidate_digest` 原样复制；草稿不得包含候选文本。
- 每个 `semantic_verification` 字段必须逐项给出 `pass | fail | uncertain` 和非空证据。
- `accept` 要求十一项全部为 `pass`；任一 `fail/uncertain` 都只能 `reject` 或 `human_required`。
- `reject/human_required` 必须给出非空 `reason_codes`。
- verifier 不新增建议、不修订候选、不改变风险 route。

单批发布使用默认草稿路径。多批把草稿写入计划的 `draft_path`，再执行同一命令；脚本会自动合并全部批次且验证完整覆盖：

```bash
python "$SCRIPTS/lqe_suggestion_review.py" publish-review --job "$JOB"
python "$SCRIPTS/lqe_suggestion_review.py" publish-final --job "$JOB"
python "$SCRIPTS/lqe_suggestions.py" validate --job "$JOB"
```

只有 verifier `accept` 进入 v5 final。最终建议仅供报告展示，不进入 corrected、apply 或 export。
