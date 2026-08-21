# Suggestion 独立候选验收 v2

该 worker 只验收 packet 中已有候选，不得修改、重写或替换 `reference_target`，也不得要求 generation 为提高接受数重做候选。

单批读取根 packet；存在 `batch_plan` 时，每个新 verifier 只读取计划列出的一个 packet。必须读取 packet 绑定的 generation manifest、bundle、content index、项目资料及 verifier instructions，并核对摘要。只允许使用逐句 `checker_selected_evidence.strict_union` 中的正式上下文；不得从更宽资料重新选择有利证据。

逐项以 source 为准检查并为每项写 `pass | fail | uncertain` 与非空证据：subjects、actions、objects、polarity/negation、modality、speech act、text function、intensity、omissions、unsupported additions、tone。还必须确认全部 known issues、变量、标签、换行、保护文本和 confirmed constraints。当前 target 不是候选正确的证据。

对话字段缺失本身不是失败；只有缺失信息会改变称谓、代词、礼貌等级、语域或角色口吻时才 `human_required`。正式 candidate 的 `tone_decision.uncertainties` 必须为空；仍依赖未知信息时不得用笼统 tone pass 放行。

每个 packet entry 带 `applicable_rule_assertions`。verdict 必须按原顺序逐条提交 `rule_verifications`，不得遗漏、增补或改写 rule id：

```json
{
  "rule_verifications": [
    {
      "rule_id": "project-rule-id",
      "status": "pass",
      "evidence": "The candidate satisfies the stated project rule."
    }
  ]
}
```

没有适用 assertion 时也必须写空数组。`accept` 要求十一项 semantic verification 和全部 rule verification 都为 `pass`；任一 `fail` 只能 `reject`，任一无法裁决的规则只能 `human_required`。规则失败时使用 assertion 提供的 `reason_code`。

草稿其余字段沿用 v1 合同：复制 packet、manifest、selected evidence 摘要和 reviewed ids；每个 ID 恰有一个 verdict；candidate digest 原样复制；草稿不得包含候选文本；worker/run 必须同时与 checker、generation 及其他 review 批次不同。`reject`/`human_required` 必须给出非空 reason codes 和证据。

只有 verifier `accept` 进入 final。`reject`/`human_required` 是当前 results basis 的终态，不得触发自动回到 generation。
