# 报告专用参考建议候选

该 worker 只生成候选完整译文，不修改问题清单，也不生成或覆盖 `corrected`。候选不会直接进入报告；模型生成的整句建议必须通过独立 verifier。

输入为 `reference_suggestions.packet.json`，schema 固定为 `lqe.reference-suggestion-generation-packet` v5。先读取 `review_policy`，审阅 `segments` 中的全部 ID。`excluded_segments` 已由 publisher 硬拒绝，不得提交这些 ID。

草稿格式：

```json
{
  "schema": "lqe.reference-suggestion-generation-draft",
  "version": 5,
  "packet_digest": "<packet.packet_digest>",
  "worker_context_manifest_digest": "<packet.worker_context_manifest_digest>",
  "selection": {
    "categories": [],
    "severities": ["Critical", "Major"],
    "only_missing": false
  },
  "reviewed_ids": [0, 1, 2],
  "entries": [
    {"id": 1, "reference_target": "完整的参考译文"}
  ],
  "abstained_ids": [0, 2]
}
```

规则：

- `reviewed_ids` 和 `selection` 必须原样复制 packet。
- 开始前读取 `suggestion_context/worker_manifest.json`、其列出的全部资料和 `bundle_set.json`；草稿必须原样复制 `worker_context_manifest_digest`。
- `entries.id ∪ abstained_ids` 必须恰好覆盖 `packet.segments.id`，互斥、无重复。
- `reference_target` 是完整译文，不是说明、选项或局部片段。
- 从 source 重新提取主体、动作、对象、否定、条件、情态和文本功能后再生成完整译文；不得沿用当前 target 作为语义骨架。当前 target 只用于保留变量、标签、换行、保护文本和已验证的局部修改。
- 生成后必须对 source/candidate 再做一次 Mistranslation、Omission、Addition 与已解析约束核对。
- 必须处理 `known_issues` 的完整并集，不得只处理 `trigger_issue_ids`。
- 如果存在 `validated_target`，以它为已验证局部修改基础，避免恢复已修正问题。
- 变量、标签、换行及 `generation_constraints.protected_texts` 的数量和顺序必须保留。
- 上下文不足、多种合理方案或无法可靠改写时写入 `abstained_ids`。
- 已有 `validated_target` 的安全局部修改可写入 `abstained_ids`；publisher 会从 canonical 值机械构造 `deterministic_accept`，不要求 worker 重复生成。
- 不得添加 route、verdict、status、comment、corrected 或审校结论。
- 术语模块留下 `needs_confirmation: true` 的 Terminology、Inconsistency 或 Company style 已被 publisher 标成 `hard_reject`；不得通过其他类别、近似术语或自选译名绕过。
- 例外只有术语模块明确写成 `resolution_status: reference_allowed`：这表示术语表不能强制当前句，但不阻止按 source 正常翻译；候选仍必须经过独立 verifier。`non_authorizing_evidence` 只禁止把近似/不同源词当成强制替换证据。

`optimized` 只用上游文本类型路由：优先读取 `content_type`，否则读取 `text_type_context`；缺失或未知时按标准强度，不自行分类。`full` 只把文本类型作为上下文，不据此降低检查。

发布候选：

```bash
python "$SCRIPTS/lqe_suggestions.py" publish-candidates \
  --job "$JOB" --input "$JOB/reference_suggestions.draft.json"
```

publisher 重新派生实时 packet、校验完整 ID 覆盖、资料摘要和保护签名，并按候选文本重新评估已解析约束。约束明确不匹配时 `hard_reject`；约束通过或无法确定时，模型候选仍进入 `independent_verifier`；只有不受未验证约束影响的 canonical local edit 同值候选才可 `deterministic_accept`。

旧 `publish` 不再发布最终建议，会明确失败并提示新流程。
