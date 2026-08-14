# 报告专用参考建议候选

该 worker 只生成完整参考译文，不修改问题清单，不写 `corrected`。模型生成的整句建议必须通过独立 verifier；发布后的 route 固定为 `independent_verifier`。

输入是 `lqe.reference-suggestion-generation-packet` v5。单批时读取 `reference_suggestions.packet.json`；若根 packet 带 `batch_plan`，根 packet 只供本地汇总，每个新 worker 只读取计划列出的一个 `packet.json`，并把草稿写到该批 `draft_path`。不得合并、遗漏或截断批次。

开始前必须读取 packet 中 `instructions.worker_context` 指向的：

- `worker_manifest.json`；
- `bundle_set.json`；
- `content_index.json` 中每个 `job_relative_path`，以及每个 `embedded_text`；
- manifest 列出的语言说明、SG、背景、确认规则，以及仅供输入完整性审计的 compact source-manifest projection。

对于带 `selected_evidence_index` 的正式任务，每个 `segments[].checker_selected_evidence.strict_union` 是该句所有实际 checker 已选择证据的唯一正式并集，包含逐模块的句段上下文、人物事实、关系、运行时示例、已解析约束和邻句。SG、背景、确认规则和语言说明仍须完整读取，但不能据此为当前句重新检索、扩大或替换正式证据；完整 source manifest 不向语义 worker 暴露。`context_view_basis.merged_view` 只保留审计依据，suggestion bundle 本身是零动态证据的 projection。索引 path/digest 用于发布器审计，worker 读取 packet 内嵌的逐句并集，不读取完整索引。缺少 checker 已选证据的句子由 publisher 硬拒绝，worker 不得自行补齐。

草稿格式：

```json
{
  "schema": "lqe.reference-suggestion-generation-draft",
  "version": 5,
  "packet_digest": "<packet.packet_digest>",
  "worker_context_manifest_digest": "<packet.worker_context_manifest_digest>",
  "selected_evidence_index_digest": "<packet.selected_evidence_index.digest>",
  "worker_receipt": {
    "worker_id": "<本生成 worker 的稳定非空 ID>",
    "run_id": "<本次运行的稳定非空 ID>"
  },
  "selection": {
    "categories": [],
    "severities": ["Critical", "Major"],
    "only_missing": false
  },
  "reviewed_ids": [0, 1],
  "entries": [
    {
      "id": 0,
      "reference_target": "完整参考译文",
      "source_semantics": {
        "subjects": ["speaker"],
        "actions": ["orders addressee to follow"],
        "objects": ["addressee", "home"],
        "negation": {"present": false, "scope": null},
        "polarity": "affirmative",
        "modality": ["imperative"],
        "speech_act": "command",
        "text_function": "direct dialogue",
        "intensity": "strong",
        "omitted_source_elements": [],
        "unsupported_additions": []
      },
      "tone_decision": {
        "register": "source-explicit forceful command",
        "politeness": "no relationship-specific choice added",
        "depends_on_dialogue_context": false,
        "evidence": [
          {"type": "source_form", "value": "imperative and exclamation directly establish a strong command"}
        ],
        "uncertainties": []
      }
    }
  ],
  "abstained_ids": [1],
  "abstention_reasons": [
    {
      "id": 1,
      "reason_codes": ["SOURCE_INTENT_UNCERTAIN"],
      "evidence": "The source/context evidence does not establish one reliable intent."
    }
  ]
}
```

规则：

- `reviewed_ids`、`selection`、`packet_digest`、`worker_context_manifest_digest` 和 `selected_evidence_index_digest` 原样复制；`entries.id ∪ abstained_ids` 必须完整覆盖 `packet.segments.id`。
- `abstention_reasons` 必须与 `abstained_ids` 同序、一一对应；每项写非空 reason code 和证据。源意无法确定时必须 abstain，并明确记录原因。
- `worker_receipt` 必填。`worker_id` 和 `run_id` 各自都不得与 `packet.checker_worker_receipts` 中任何 checker 值重复；只更换其中一个字段仍不合格。多批任务每批也必须使用新的 worker ID 和 run ID。
- 逐句语气、人物、关系、示例、约束和邻句判断只可引用 `checker_selected_evidence.strict_union` 中的实际记录；不得根据完整 profile 或 merged max 配置自行再选一次。
- 从 source 重新建立主体、动作、对象、极性/否定、情态、speech act、文本功能和强度；当前 target 只用于保留变量、标签、换行、保护文本和已验证局部修改。
- `optimized` 优先读取 `content_type`，否则读取 `text_type_context`；缺失或未知时按标准强度。文本功能和语气只能结合正式上下文证据判断，不能从项目或目标语言的词面硬猜。`full` 不因文本类型降低检查。
- `source_semantics` 同时记录 source→candidate 的漏译检查和 candidate→source 的增译检查。仍有漏项或无来源信息时，不得提交候选。
- `tone_decision` 必须给出源文或正式上下文证据。只有会实质影响候选措辞且无法规避的不确定性，才写入 `abstained_ids`。
- `depends_on_dialogue_context` 表示“候选措辞是否依赖缺失或冲突的对话信息”，不是“该句是否属于对话”或“对话字段是否齐全”。
- `speaker_id`、`addressee_ids` 或 `relationship_stage` 缺失本身不是自动弃权条件。若源文形式已经明确 speech act、intensity 或敌意/辱骂强度，且候选不需要选择未知关系、称谓/代词、礼貌等级或 character voice 即可保持这些信息，设 `depends_on_dialogue_context: false`，并引用 `source_form` 证据。
- 只有缺失或冲突的信息会实质改变候选的 register、politeness、称谓/代词或 character voice 时，才设 `depends_on_dialogue_context: true`；此时不得猜测，必须弃权。publisher 会拒绝在 `dialogue_context_readiness` 非 ready 时提交的此类候选。
- 必须处理全部 `known_issues`，不是只处理 `trigger_issue_ids`；生成后再检查 Mistranslation、Omission、Addition、语法、自然度和全部 resolved constraints。
- 变量、标签、换行和保护文本的数量与顺序必须保留。
- `excluded_segments` 不得重新引入。未解决术语、保护段、输入阻断和约束冲突均不能被其他模块意见绕过。
- 术语模块留下 `needs_confirmation: true` 的问题会由 publisher 硬拒绝；建议 worker 不得通过近似词、其他类别或自选译名绕过。
- `resolution_status: reference_allowed` 只表示术语表不能强制当前句，不阻止按 source 正常翻译；此类候选仍须独立 verifier。`non_authorizing_evidence` 不能作为强制替换依据。
- `validated_target` 只可作为已验证局部修改依据，不得被当作 source 语义证据，也不会产生模型候选的 `deterministic_accept`。
- 不得输出 route、verdict、status、comment、corrected 或审校结论。

单批发布：

```bash
python "$SCRIPTS/lqe_suggestions.py" publish-candidates \
  --job "$JOB" --input "$JOB/reference_suggestions.draft.json"
```

多批发布：

```bash
python "$SCRIPTS/lqe_suggestions.py" publish-candidates \
  --job "$JOB" --input "$JOB/suggestion_context/batches"
```

publisher 重新派生实时 packet/批次计划，验证完整覆盖、资料绑定、worker receipts、保护签名和结构化语义收据。它只能验证结构与确定性约束，不能宣称候选语义正确；所有成功生成的候选均进入独立 verifier。旧 `publish` 会明确失败。
