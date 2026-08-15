# 术语检查模块

公共协议见 `common.md`。本模块只处理 Terminology、Inconsistency、Company style，并复核机器预检结果。必须审阅 packet 的全部 `reviewed_ids`；紧凑草稿只在 `findings` 中写有问题的 id。每条问题使用 `"needs_confirmation"` 和 `"edit"`，不得输出 corrected。每个 Terminology issue 必须同时带 `term_source`、`expected_targets` 和 `term_spans`。

## 检查内容

1. 复核 `precheck`：保留确定的 Terminology issue 时带对应 `precheck_ref`，继承 `term_source`、`expected_targets` 和 `term_spans.source`，并精确补充 `term_spans.target`；数值、含义等准确性问题交给 `accuracy`。
2. 检查 `term_hits`：每个候选已展开成单独记录，包含 `source`、`target`、`confirmed`、`protected` 及可选的 `status/category/definition`。
3. 同一来源概念在全文使用不同译法时报告 Inconsistency；术语表内的冲突仍归 Terminology。
4. 只有违反明确风格指南的写法才报告 Company style；单纯不自然交给 `naturalness`。
5. Terminology issue 的 `term_spans.source` 精确定位受影响的原文词，`term_spans.target` 精确定位当前译文中的问题词。span 只含整数 `start`、整数 `end` 和非空 `text`，使用 0-based、左闭右开的非空区间；数组升序排列，不重复、不重叠。`text` 与切片完全一致，source span 的 `text` 还必须等于 `term_source`。`source` 非空；漏译或没有可安全定位的译文词时 `target` 写空数组，不猜测、不整句标记。

## 术语结论必须显式交接

术语表证据与当前句语境要分开判断。每条术语模块问题除通用字段外，还要写 `resolution_status` 和 `reason_codes`：

- `resolved`：当前句的术语选择已经确定；`needs_confirmation:false`，可按现有安全局部规则给 `edit`。
- `reference_allowed`：术语表没有权力强制本句，但后续可从原文独立生成参考译文；`needs_confirmation:true`、`edit:null`。近似词或不同源词证据还要写 `non_authorizing_evidence`，明确它不能授权替换。
- `human_choice_required`：确实需要人工选择译名或含义；`needs_confirmation:true`、`edit:null`。后续建议器必须跳过整句。
- `conflict`：已确认资料互相冲突；`needs_confirmation:true`、`edit:null`。后续建议器必须跳过整句。

新提交的术语模块 finding 不得只写 `needs_confirmation:true` 而省略为什么后续可继续或必须停止。历史产物继续兼容，但后续建议器按最保守方式停止。

## 是否可直接修改

- 当前译文偏离唯一且 `confirmed: true` 的候选时，可给局部 `edit`，并把证据写成 `{"type":"confirmed_term","source":"源词","target":"确认译法"}`。
- 候选未确认、存在多个含义、需要新译名、术语表缺词或术语表自身可能错误时，写 `"needs_confirmation": true`、`"edit": null`。
- “不同源词/近似词”不得继承 exact term 的权威。例如只有“红果→Red Fruit”时，“蓝果”不能据此强制改成 `Red Fruit`；若只是普通称谓且不需要客户决策，使用 `reference_allowed`，并把该近似证据标为 non-authorizing。
- `protected: true` 的词和受保护段不修改。
- 最长匹配优先；已被更长术语覆盖的子词不重复报告。

## 输出示例

```json
{
  "id": 8,
  "issues": [
    {
      "category": "Terminology",
      "severity": "Major",
      "comment": "The confirmed project term is not used.",
      "needs_confirmation": false,
      "edit": {
        "from": "Old Name",
        "to": "New Name",
        "start": 4,
        "end": 12,
        "evidence": {
          "type": "confirmed_term",
          "source": "新名",
          "target": "New Name"
        }
      },
      "term_source": "新名",
      "expected_targets": ["New Name"],
      "term_spans": {
        "source": [{"start": 2, "end": 4, "text": "新名"}],
        "target": [{"start": 4, "end": 12, "text": "Old Name"}]
      }
    }
  ]
}
```
