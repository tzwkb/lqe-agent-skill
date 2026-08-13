# Suggestion 独立候选验收

该 worker 只验收 `suggestion_review.packet.json` 中已有候选。不得修改、重写或替换 `reference_target`。

开始前必须读取 `suggestion_context/worker_manifest.json`、其列出的全部资料和 `bundle_set.json`。verifier 使用与生成 worker 相同的资料快照；缺失或摘要不符时停止。

逐项检查：

1. 候选是否完整保留 source 的主体、动作、对象、否定、条件、情态和文本功能。
   必须以 source 为核对基准；当前 target 不是候选含义正确的证据。
2. 候选是否解决全部 `known_issues`，且没有引入新的 Addition、Omission、Mistranslation、语法或自然度问题。
3. 变量、标签、换行、保护文本和 confirmed constraints 是否全部满足。
4. 证据不足或存在多个需要业务判断的方案时返回 `human_required`，不能代替人工选择。

草稿格式：

```json
{
  "schema": "lqe.suggestion-review-draft",
  "version": 1,
  "review_packet_digest": "<packet.packet_digest>",
  "worker_context_manifest_digest": "<packet.worker_context_manifest_digest>",
  "reviewed_ids": [1, 2],
  "verdicts": [
    {
      "id": 1,
      "candidate_digest": "<packet entry candidate_digest>",
      "decision": "accept",
      "reason_codes": [],
      "evidence": "Source meaning, all known issues and constraints pass."
    },
    {
      "id": 2,
      "candidate_digest": "<packet entry candidate_digest>",
      "decision": "reject",
      "reason_codes": ["OMISSION_REMAINS"],
      "evidence": "The candidate still omits the source condition."
    }
  ]
}
```

合同：

- `reviewed_ids` 原样复制 packet；每个 ID 恰有一个 verdict。
- `worker_context_manifest_digest` 原样复制 packet；候选约束检查结果读取 `candidate_constraint_evaluations`，不得用原译的旧检查结果代替。
- `decision` 只能为 `accept | reject | human_required`。
- `candidate_digest` 必须原样复制；草稿不包含候选文本，因此不能暗改候选。
- `reject/human_required` 必须有非空 `reason_codes`；每项必须有简短非空 evidence。
- verifier 不新增建议、不修订候选、不改变风险 route。

发布与最终汇总：

```bash
python "$SCRIPTS/lqe_suggestion_review.py" publish-review --job "$JOB"
python "$SCRIPTS/lqe_suggestion_review.py" publish-final --job "$JOB"
python "$SCRIPTS/lqe_suggestions.py" validate --job "$JOB"
```

只有 `deterministic_accept` 或 verifier `accept` 进入 v5 final。最终建议仍只供报告展示，不进入 corrected、apply 或 export。
