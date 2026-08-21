# RAG Integration Notes for LQE

状态：暂不接入正式 LQE 评估流程。

## 当前状态

- AIPE 导出的 CSV 可能包含 `rag_references` 列。
- `lqe_io.py read --project ...` 会保留输入表原始列到 `state.rows_raw`。
- 当前不会把 `rag_references` 解析进 `segments`。
- 当前不会把 RAG 注入 pre-check、单 agent prompt、chunk、lens 或评分。
- `lqe_io.py ingest-corpus` 已实现 finalized 语料的版本化批量回流，但它不是评估时 RAG 接口；请求/回执合同见 `references/corpus_ingest.md`。

因此，RAG 现在只是随输入表留档，不参与判错、修正或计分。

## 暂不接入的原因

- LQE 当前权威上下文已经由项目档案提供：SG、TB、checks、adjudications、language notes、background。
- RAG 引用来自相似历史句段，不等同权威规范；直接参与判错容易把历史译文偏差放大。
- RAG 对术语和 TM 的边界容易混淆：RAG 不是术语锁定，也不是 100% TM match。
- 当前 CSV 中的 RAG 质量与检索阈值来自 AIPE 翻译阶段，不一定适合作为 LQE 评估证据。

## 若未来接入，建议定位

RAG 只做软上下文，不做硬规则。

优先级建议：

1. 原文与当前译文
2. TB / SG / adjudications / checks / language notes / background
3. TM 100% match 保护
4. RAG references

RAG 只能辅助判断历史译法、上下文、表达习惯；不能单独构成错误依据。

## 推荐接入点

### 1. `read` 阶段

在 `lqe_io.py read` 中识别 `rag_references` 列：

- 解析 JSON array。
- 写入 `segment["rag_references"]`。
- 保留原始 `rows_raw` 不变。
- 解析失败时不报错，中性记录为空数组或 `rag_parse_error`。

建议只保留每段前 3 条，避免 chunk 过大。

建议字段：

```json
{
  "rag_references": [
    {
      "source": "...",
      "target": "...",
      "score": 0.82
    }
  ]
}
```

### 2. chunk 阶段

在 `lqe_chunk.py split` 中把 `rag_references` 带入 chunk，但需要截断：

- 每段最多 3 条。
- 每条 source/target 最多 200-300 字符。
- 删除无关字段。

### 3. lens 注入

只建议给以下评估面使用：

- Lens A：准确机制。用于对照历史上下文、机制说明、相似句译法。
- Lens R：语域自然。用于判断项目内常见语气、风格和表达习惯。
- 小文件单 agent：可作为辅助上下文注入。

不建议给：

- pre-check：确定性检查不应依赖 RAG。
- Lens T：术语以 TB/adjudications 为准，RAG 只能造成噪音。
- Lens G：语法/拼写判断不需要 RAG。
- TM 保护：RAG 不是 100% match。

## Prompt 规则建议

给 AI 的规则应明确：

- RAG 是参考，不是规范。
- RAG 与 TB/SG/adjudications 冲突时，忽略 RAG。
- RAG target 不能作为术语锁定译法。
- 只有当 RAG 与原文语境高度相似，且能解释当前译文问题时，才可在 comment 中引用。
- 不因当前译文不同于 RAG 就直接报错。

可用说明：

```text
RAG references are historical similar translations. Use them only as soft context.
They are lower priority than source, current target, TB, SG, adjudications,
language notes, background, and TM locks. Do not report an error solely because
the current target differs from RAG.
```

## 风险与防护

- 噪音：RAG 命中相似但不等价文本。防护：只注入高分、限量、截断。
- 历史错误扩散：旧译文可能错。防护：RAG 低于 adjudications/SG/TB。
- token 膨胀：长剧情引用会撑爆 chunk。防护：每段最多 3 条，每条截断。
- 误当 TM：RAG 不是 exact match。防护：不得进入 TM locked ids。
- 术语误导：RAG 与 TB 不一致。防护：Lens T 不读 RAG。

## 最小实现清单

1. `lqe_io.py read`：解析 `rag_references` 到 segment。
2. `run_tests.py`：增加 CSV RAG 解析测试。
3. `lqe_chunk.py split`：带入截断后的 `rag_references`。
4. `docs/lenses/A.md` 与 `docs/lenses/R.md`：加入 RAG 软上下文规则。
5. 小文件单 agent prompt：加入同一段 RAG 优先级说明。
6. 验证：RAG 不影响 pre-check、T/G lens、TM locked、score calculation。

## 当前决策

评估时 RAG 注入仍不实施。finalized 语料回流是独立的显式外部写操作，不改变本节的上下文优先级或评分边界。
