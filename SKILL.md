---
name: lqe-translator
description: "LQE scoring and review workflow for game-localization translations. Project profiles provide language settings, style guides, terminology, confirmed rules, deterministic checks, scorecards, and Excel reports. AI check modules report issues and safe local edits; Python validates edits, builds corrected text, and calculates scores. Triggers: LQE/LQA, 译文质检/评估/打分, translation QA."
---

# LQE Translator

每次会话先定义：

```bash
SCRIPTS=~/.codex/skills/lqe-translator/scripts
```

## 1. 收集输入

需要：

- Excel、CSV、TSV，或 SDLXLIFF 1.2 路径；SDLXLIFF 可传单文件或目录。
- 表格输入的原文列和译文列；SDLXLIFF 从句段结构读取，不需要列名。
- 项目语言轨，如 `nrc/zh-th`、`nrc/zh-en`、`wwm/zh-en`。
- 是否启用自动迭代；默认只跑首轮。
- 审校输出模式：降本模式（`optimized`）或完整模式（`full`）。

开始任何新任务前，如果用户尚未在当前请求中明确审校输出模式，Agent **必须先询问一次并等待回答**：

> 本次使用哪种审校输出模式：降本模式还是完整模式？

- 降本模式：Minor 只报告问题；comment 以 20–30 字符为软目标；完整参考建议默认只纳入 Major/Critical 候选；有上游文本分类时按分类矩阵调整重点。
- 完整模式：恢复降本改造前的行为；所有严重度都由 Agent 判断是否能给安全局部修改或完整参考建议；comment 无 20–30 字符目标；文本分类只作上下文，不改变检查强度或建议口径。

不得根据项目、文件、历史任务或成本偏好代替用户选择。用户已在当前请求中明确选择时不重复询问；继续已有 job 时读取 `state.review_policy`，不再询问。一个 job 内不得中途切换；需要换模式时重新执行 `read` 并重建后续 artifact。

优先使用 `read --project <game>/<source>-<target>`。用户只给游戏名时，列出该游戏已有语言轨；没有目标语言轨时，先建立 profile，不回退到另一套临时入口。

项目目录：

```text
projects/<game>/<source>-<target>/
├── profile.json
├── checks.json
├── confirmed_rules.md
├── terms_*.json
└── sg*.md / sg*.txt
```

`profile.json` 常用字段：

```json
{
  "name": "nrc/zh-th",
  "language_pair": "zh-th",
  "source_lang": "zh",
  "target_lang": "th",
  "background": "项目背景",
  "style_guide": "sg.md",
  "terminology": "terms.json",
  "checks": "checks.json",
  "confirmed_rules": "confirmed_rules.md",
  "wordcount_basis": "source-chars",
  "term_status_map": {"Approved": "confirmed", "Draft": "unconfirmed"},
  "protected_term_statuses": [],
  "scoring_policy": {
    "threshold": 98,
    "scorecard_profile": "legacy",
    "severity_scale": "lisa",
    "critical_gate": false,
    "repeat_dedup": true
  },
  "sdlxliff": {
    "tm_protection": "candidate-only",
    "content_type_rules": [
      {"id": "dialog", "glob": "**/dialog*.sdlxliff", "content_type": "剧情/对话"}
    ],
    "exclude_rules": [
      {"id": "rejected", "field": "confirmation", "equals": "Rejected", "reason": "Client excluded"}
    ]
  }
}
```

`language_pair`、`source_lang`、`target_lang` 必填。相对路径以 profile 所在目录为基准。`protected_term_statuses` 只能来自客户资料或用户明确确认；如提供，必须是元素均为非空字符串的数组，空数组表示没有受保护的术语状态。顶层 `threshold` 仅兼容旧 profile；新 profile 使用完整 `scoring_policy`。

新 profile 使用 `profile_contract_version: 2`，并显式声明 `assets`、`context_pipeline` 和 `capabilities`。资料必须登记路径、类型、权威级别、来源、分发级别和可用状态；程序不会扫描目录并自动启用文件。人物/实体、关系、审核示例、上下文规则、来源清单和逐句人工补充分别使用 `lqe.entities`、`lqe.review-examples`、`lqe.context-rules`、`lqe.project-source-manifest`、`lqe.segment-context-overrides` JSON。原始 xlsx/docx 可保留用于追溯，但不直接充当可执行人物关系或语域规则。

需要向模块提供人物事实、邻句或审核案例时，在 profile 的 `module_context_views` 中按模块声明 capability、dimension、邻句窗口和数量上限。`max_runtime_examples: 0` 表示不提供案例，不是“自动选择”。`off` 不启用 optional view；`shadow` 只把 view 写入 `state.shadow_module_context_views` 供审计；只有 `enforce` 才写入正式 `state.module_context_views`。shadow typed asset 即使声明 core capability，也不得进入正式 bundle、shared assets 或 worker manifest；关系若显式带 `attributes.runtime_rule: false`，不得作为运行上下文。

目标语言事实放在 `target_languages/<code>/attributes.json`，语言级检查说明放在 `eval_notes.md`。合并顺序为：内置默认 < 语言属性 < 项目 `checks.json` < CLI 参数。

项目规则顺序：实时要求 > `confirmed_rules.md` > 风格指南 > 通用检查方法。运行检查前必须读取项目背景、确认规则、风格指南和语言说明。

## 2. 初始化

```bash
pip install "openpyxl>=3.1" "xlrd>=2.0" "jsonschema>=4.20" regex requests python-docx -q

python "$SCRIPTS/lqe_io.py" read \
  --project "<game>/<source>-<target>" \
  --input "<输入文件>" \
  --source-col "<原文列>" \
  --target-col "<译文列>" \
  --review-mode "<optimized|full>" \
  --out "jobs/<文件名>/state.json"
```

客户没有逐句情境时，不得让模型补猜。先从已有 v2 job（包括 shadow job）生成缺口报告和人工待补模板：

```bash
python "$SCRIPTS/lqe_context_overrides.py" gaps \
  --state "$JOB/state.json" --out "$JOB/context_gap_report.json"
python "$SCRIPTS/lqe_context_overrides.py" scaffold \
  --state "$JOB/state.json" --out "$JOB/context_overrides.template.json"
```

模板中的未知值固定为 `null`、条目状态为 `pending`。由客户、PM 或其他明确授权来源填写后，删除不适用字段，将条目改为 `verified`，再用 `--context-overrides <已核实.json>` 新建 job。sidecar 只能写当前正式 `foundation`/`enforce` capability 字段，不能绕过 shadow 隔离。已有别名需要人工改为 canonical ID 时，模板在 `expected_context` 保存当前原值；程序只在原值仍精确一致时替换，否则视为漂移冲突。key 重复、源文摘要漂移、无授权/未核实、空值、未声明字段、未声明的已有值替换或补完后仍缺必填上下文时整批失败，原输入和原 job 不修改。成功时复制 sidecar，生成 `context_gap_report.json`，并把摘要绑定到 state、source manifest、segment revision 与 context runtime fingerprint。报告明确区分 `not_provided`、`unresolved_alias`、`ambiguous_alias` 和 `not_applicable`。

旧 `.xls` 通过 `xlrd>=2.0` 只读；corrected 固定输出 `.xlsx`。多工作表用 `--sheet` 明确主表。需要上下文时用 `--key-col` 和可重复的 `--context-col FIELD=COLUMN`；常用别名包括 `--content-type-col`、`--speaker-col`、`--addressee-col`、`--relationship-stage-col`、`--scene-id-col`、`--scene-tone-col`、`--context-note-col`。程序只采用 profile 已声明并成功协商的能力；`shadow` 仅留审计资料，`enforce` 才影响模块 packet、去重和建议。

SDLXLIFF 初始化：

```bash
python "$SCRIPTS/lqe_io.py" read \
  --project "<game>/<source>-<target>" \
  --input "<SDLXLIFF 文件或目录>" \
  --input-format sdlxliff \
  --review-mode "<optimized|full>" \
  --out "jobs/<文件名>/state.json"
```

`--input-format` 可取 `auto`、`tabular`、`sdlxliff`，默认 `auto`。单个 `.sdlxliff` 或只含 SDLXLIFF 的目录可自动识别；混有表格文件的目录必须显式使用 `--input-format sdlxliff`，未选中的受支持文件会记录在来源清单。目录会递归读取并按相对路径排序。表格目录仍不支持。

`--review-mode` 可取 `optimized`、`full`。Agent 正常运行必须传入用户选择；CLI 为兼容旧调用在省略时按 `optimized` 解析。初始化将解析后的完整策略写入 `state.review_policy`，split、review packet、suggestion packet 和报告都受该策略约束。

<pre data-lqe-review-mode-contract>
{
  "decision": "ask_before_new_job_unless_explicit",
  "flag": "--review-mode",
  "optimized": {
    "minor_edits_allowed": false,
    "comment_soft_target": "20-30 characters",
    "suggestion_candidate_severities": ["Critical", "Major"],
    "text_type_routing_enabled": true
  },
  "full": {
    "minor_edits_allowed": true,
    "comment_soft_target": null,
    "suggestion_candidate_severities": ["Neutral", "Minor", "Major", "Critical"],
    "text_type_routing_enabled": false
  },
  "state_field": "state.review_policy",
  "immutable_within_job": true,
  "legacy_cli_default": "optimized"
}
</pre>

第一版只接受带 SDL namespace 的 XLIFF 1.2/SDLXLIFF 1.2；XLIFF 2.0 明确失败。未知厂商扩展若不影响句段定位、文本边界和 `mid` 配对，会保留证据并记录 namespace；存在结构歧义时失败，不猜测。内容类型与排除只能来自 profile 的显式 `sdlxliff.content_type_rules` 和 `sdlxliff.exclude_rules`，不得根据 CC、FF、文件名或目录名推断。

任务明确要求“不检查术语”时，在同一条 `read` 命令加入 `--no-terminology`。该参数高于 profile 的术语配置，且不能与显式 `--terminology <file>` 同时使用。初始化会把解析后的模式写入 `state.check_scope`，并在 job 根目录生成内容相同的 `scope.json`。

`--no-terminology` 只关闭术语、专名和术语审计；不会关闭文件内一致性、Markup、数字等检查。初始化后报告段数、词数、语言、检查模式、启用模块、风格指南、确认规则、术语表和受保护内容的加载情况。

初始化先在 staging 中生成并校验全部资源，拒绝输入与输出/资源的同文件、软链、硬链或大小写路径别名；再以 `state.json` 最后发布。任一步失败都不得留下正式 `state.json`、`scope.json`、`terms.json` 或半套 SDL 资源。

以下可见合同精确定义两种模式，所有入口都以解析后的 scope 为准：

<pre data-lqe-scope-contract>
{
  "mode_flag": "--no-terminology",
  "standard": {
    "required": ["terminology", "accuracy", "grammar", "naturalness"],
    "optional": ["proper_names"]
  },
  "no-terminology": {
    "required": ["precheck_review", "accuracy", "grammar", "naturalness"],
    "optional": [],
    "disabled": ["terminology", "proper_names", "term_audit"]
  },
  "scope_artifact": {
    "path": "scope.json",
    "state_field": "state.check_scope",
    "relation": "same resolved scope"
  },
  "kept_checks": ["file-wide consistency", "Markup", "numeric checks"]
}
</pre>

## 3. 术语标记

本节只适用于标准模式；无术语模式不读取或使用术语条目。

需要转换的原始术语表如果缺少显式布尔 `confirmed`/`approved` 或 `protected`，则**必须在初始化前询问用户**如何处理；存在 `status` 等状态列时，必须提供“哪些状态值等于已确认/受保护”的映射。**仅仅存在 `status` 列不构成“已满足契约”——缺少显式布尔字段时，状态值必须被映射或经用户确认。** 不得根据文件名、工作表名或“通常如此”自行决定；**不得默认全部已确认，也不得默认全部未确认**。在获得映射或用户回答前，不生成正式 `terms_*.json`、不运行术语预检、不评分。

已经规范化的 canonical 术语或候选如果同时显式带布尔 `confirmed` 和 `protected`，即使保留 `status` 作为审计元数据，也不需要 `term_status_map`。若同时提供映射，显式字段与映射冲突时失败。

询问时明确给出三种处理口径：

1. 整份术语表视为已确认；唯一译法写 `confirmed: true`，多译词保留候选并按语境判断。
2. 整份术语表仅作未确认参考；写 `confirmed: false`。
3. 用户提供逐行规则或状态映射后再转换。

以下 converter 合同只适用于仍需从状态列推导显式布尔字段的原始术语表，不适用于已具备完整布尔字段的 canonical 数据：

<pre data-lqe-term-confirmation-contract>
{
  "trigger": "terminology has no explicit confirmation field (confirmed/approved) OR has a status column but no status->confirmed/protection mapping supplied",
  "required_action": "ask_user_or_supply_mapping_before_initialization",
  "status_column_detection": "RULE-BASED, not enumerative: a column is a status column if its header CONTAINS the token 'status' or '状态' (case-insensitive, any position, regardless of prefixes/suffixes/brackets). This catches future renames/relocations automatically. If MULTIPLE columns match, the converter MUST SystemExit and require --status-col to disambiguate (it never guesses).",
  "fail_closed": "converter MUST SystemExit in EITHER case below; it must NEVER emit terms with confirmed silently defaulted to false: (a) a status column is detected but no confirmation decision was supplied — a confirmation decision is --approved-statuses (values / '*' / '') and --protected-statuses ALONE is NOT enough; (b) NO status column is detected and --no-status was NOT passed (the column may have been renamed/relocated). It must print the distinct detected/unmapped status values for audit.",
  "mapping_channels": [
    "converter arg: --approved-statuses 'Approved,合规审核通过'  (status values are compared CASE-INSENSITIVELY — a rule, not per-value casing)",
    "converter arg: --approved-statuses '*' to treat the whole glossary as confirmed (choice 1)",
    "converter arg: --approved-statuses '' (empty) to explicitly treat all as unconfirmed (choice 2)",
    "converter arg: --protected-statuses '<status>' to mark those senses protected (NOT a confirmation decision on its own)",
    "converter arg: --exclude-statuses '<status>' to additionally drop rejected terms entirely (not checked, not flagged). NOTE: status 'Denied' is ALWAYS excluded by default (case-insensitive) — no flag needed (standing rule: client-rejected terms never enter the glossary)",
    "converter arg: --status-col '<header>' to disambiguate when multiple status-keyword columns exist",
    "converter arg: --no-status to assert the glossary has NO confirmation info at all (required when no status column is detected)",
    "profile.term_status_map: {\"Approved\": \"confirmed\"}  (use for status->confirmed/protection; 'Denied' must not be mapped since it is unconditionally excluded)"
  ],
  "forbidden_defaults": ["all_confirmed", "all_unconfirmed", "infer_from_unmapped_status", "silently_proceed_when_status_column_undetected"],
  "choices_when_asking": [
    "treat_entire_glossary_as_confirmed",
    "treat_as_unconfirmed_reference",
    "provide_row_or_status_mapping"
  ]
}
</pre>

术语条目或多义候选必须显式带：

```json
{"source":"源词","target":"译法","confirmed":false,"protected":false}
```

- `confirmed: true`：客户已经确认该译法；匹配证据完整时允许安全局部修改。
- `protected: true`：不可修改。
- 完成上述用户确认后，转换结果必须显式写出两个布尔字段；缺 `confirmed` 或缺 `protected` 都失败。不得依赖隐式默认值，也不要根据未映射的 `status` 自行推断。已有两个显式布尔字段时，`status` 仅作审计元数据。失败发生在正式 job 资源发布前。
- profile 的 `protected_term_statuses` 只负责把明确列出的状态映射为 `protected: true`；如提供，必须是元素均为非空字符串的数组。

## 4. TM 100% 匹配保护

输入自带 match 信息时，Agent 先检查列名、样例值和证据，再生成明确的 id 列表。脚本不猜列名或值。

```json
{"protected_ids":[0,12],"source":"agent_decision","evidence":"exact TM match"}
```

```bash
python "$SCRIPTS/lqe_io.py" protect-segments \
  --state "$JOB/state.json" \
  --protected-file "$JOB/tm_protected.agent_decision.json" \
  --reason TM_100_MATCH
```

项目配置了本地 TM 时：

```bash
python "$SCRIPTS/tm_index.py" build \
  --libraries <profile.tm.libraries...> \
  --out <profile.tm.index>

python "$SCRIPTS/tm_index.py" tm-match \
  --state "$JOB/state.json" \
  --index <profile.tm.index> \
  --out-protected "$JOB/tm_protected.json"
```

只有源文精确匹配且当前译文属于该源文的已收录译法时才保护。受保护段不修改、不扣分，报告和导出保留原译。

SDLXLIFF 的保护规则单独处理：

- SDL 明确 locked 的段初始化时自动保护为 `SOURCE_LOCKED`，不依赖 TM 策略。
- 默认 `candidate-only` 只把同时满足 `origin=tm`、`percent=100`、`text-match=SourceAndTarget` 的段写入 `tm_candidates.json`，不会自动保护。
- 确认候选后，可用上面的 `protect-segments` 命令把 `tm_candidates.json` 作为 `--protected-file`；或者在 profile 使用 `protect-exact-source-and-target`。
- CLI `--protect-exact-tm` 等同于当次显式启用严格自动保护，并优先于 profile。普通 100% 值或缺少任一条件都不保护。
- locked 与 TM 同时命中时，主原因仍为 `SOURCE_LOCKED`，两类证据分别保留。

## 5. 机器预检

首轮运行：

```bash
python "$SCRIPTS/lqe_io.py" pre-check \
  --state "$JOB/state.json" \
  --out "$JOB/errors_precheck.json"
```

预检覆盖：未翻译内容、空译文、变量、标签、换行、数字、长度、空格、全角标点、句尾标点、中文数字与量词、重复词、词内大小写、成对标点、拼音残留、文件内一致性和项目自定义规则。标准模式还运行术语命中、术语大小写和依赖术语表的专名检查；无术语模式从源头跳过这些术语检查。

`checks.json` 的 `builtin` 可关闭不适用项，`custom` 可增加 regex 或 `count_match` 检查。语言属性会自动关闭不适用于目标语言的检查。预检结果仍需按上下文复核。

## 6. 检查模块

大文件和小文件都按模块并行检查，并按 `review_packets/batch_plan.json` 分配有界 worker。每个 worker 最多处理 4 个 packet，同时不得超过 25,000 原译字符；instructions、项目资料、共享资产、bundle、manifest 和 packet 的总输入不得超过 100,000 字节。不可再拆的最小单元仍超限时失败，不截断。模块说明位于：

流程要求 subagent 并行检查时，如果因并发上限、权限、工具不可用或运行环境限制而无法启用，**必须主动询问用户**如何处理；**不得静默回退**为主 Agent 单跑、跳过模块、缩小覆盖范围或降低检查标准。用户明确同意替代方案后才能继续。

```text
references/check_modules/common.md
references/check_modules/terminology.md
references/check_modules/precheck_review.md
references/check_modules/accuracy.md
references/check_modules/grammar.md
references/check_modules/naturalness.md
references/check_modules/proper_names.md
references/check_modules/term_audit.md
references/suggestions.md
```

每个 worker 只处理 batch plan 中的一个批次，并在批次开始时读取 `common.md`、自己的模块文件和项目上下文。新批次必须新建 worker 并重新读取上下文；发生上下文压缩、异常重复判断或连续格式错误时也立即重开。模型只提交检查结果，不生成整段最终译文。

正式问题接口：

```json
[
  {
    "id": 0,
    "issues": [
      {
        "category": "Grammar",
        "severity": "Minor",
        "comment": "The verb form does not agree with the subject.",
        "needs_confirmation": true,
        "edit": null
      }
    ]
  }
]
```

固定接口为 `{id, issues:[{category,severity,comment,needs_confirmation,edit}]}`（模型输出）；每个 Terminology issue 必须再带 `term_source`、`expected_targets` 和 `term_spans`：

```json
{
  "category": "Terminology",
  "severity": "Major",
  "comment": "The confirmed project term is not used.",
  "needs_confirmation": true,
  "edit": null,
  "term_source": "督管案台",
  "expected_targets": ["Supervisor's Counter"],
  "term_spans": {
    "source": [{"start": 2, "end": 6, "text": "督管案台"}],
    "target": [{"start": 10, "end": 22, "text": "control desk"}]
  }
}
```

`term_spans` 必须恰有 `source` 和 `target` 两键。每个 span 对象恰有整数 `start`、整数 `end` 和非空 `text`，使用 0-based、左闭右开的非空区间；数组按 `(start,end,text)` 升序排列，不得重复或重叠。`text` 必须严格等于原文或当前译文的对应切片，且每个 source span 的 `text` 必须等于 `term_source`。`source` 必须非空；`target` 可为空，表示当前译文没有可安全定位的受影响词（如漏译），不得猜测或整句标记。复核机器预检 Terminology issue 时，`term_source`、`expected_targets` 和 `term_spans.source` 为只读并按 `precheck_ref` 继承；模型必须精确补充 `term_spans.target`，确实没有可标译文词时保留空数组。新发现的 Terminology issue 由模型完整提交这三个字段。

默认模型草稿使用第 7 节的紧凑包装，只列确有问题的接口项；publisher 补齐无问题 id。检查模块不得输出 corrected；`lqe_corrections.py` 验证局部修改后生成该内部字段。每个 review packet 都带 `review_policy`，worker 必须按其中的 `mode` 执行。

- 所有必需模块的正式产物覆盖全部 id；无问题项由 publisher 写成 `issues: []`。
- `optimized`：`comment` 统一用英文，以 20–30 个字符为软目标；完整、清楚优先，不机械补字或截断。Minor 只报告问题：必须写非空 `comment`、`needs_confirmation: true` 和 `edit: null`；非 Minor 的安全、唯一、局部改法才可写入 `edit`。
- `full`：`comment` 统一用英文并保持简明完整，不设 20–30 字符目标。包括 Minor 在内的任何严重度，只要 Agent 判断改法安全、唯一且局部，都可写 `needs_confirmation: false` 和 `edit`；无法安全修改时仍写 `needs_confirmation: true`、`edit: null`。
- 新译名、术语表错误或缺词、多个合理方案、整句重写写 `needs_confirmation: true` 和 `edit: null`。
- 术语或专名修改必须引用唯一的 `confirmed: true` 候选，证据格式为 `{"type":"confirmed_term","source":"...","target":"..."}`。
- 变量、标签、换行、受保护文本和受保护段不得被修改。

模块分工：

| 模块 | 类别 |
|---|---|
| `terminology` | Terminology、Inconsistency、Company style、预检复核 |
| `precheck_review` | 无术语模式下复核 Markup、Length、Locale convention、Company style、非术语 Inconsistency 和 Other 预检 |
| `accuracy` | Mistranslation、Omission、Addition、Untranslated |
| `grammar` | Grammar、Spelling、Punctuation |
| `naturalness` | Audience appropriateness、Culture specific reference、Unidiomatic |
| `proper_names` | 术语表自检中的专名音译 |

文本分类只使用上游提供的列或规则结果。表格输入已识别 `content_type`、`text_type`、`文本类型`、`文本类别`；不得根据文本、文件名、工作表名或目录名自行分类。`optimized` 优先使用非空 `content_type`，否则使用 `text_type_context`；两者都为空或分类未知时按标准强度处理。`full` 保留这些字段作为上下文，但不按下表改变检查强度。

分类只调整检查重点，不改变模块分工、严重度定义、机器预检、术语模式和保护规则：

| 上游文本类型 | `terminology` | `accuracy` | `grammar` | `naturalness` |
|---|---|---|---|---|
| UI/界面文本 | 重点 | 重点 | 重点 | 保底 |
| 技能/道具描述 | 重点 | 重点 | 标准 | 标准 |
| 主线剧情对话 | 重点 | 重点 | 重点 | 重点 |
| 支线/NPC对话 | 标准 | 重点 | 重点 | 重点 |
| 系统提示/教程 | 重点 | 重点 | 重点 | 标准 |
| 世界观/背景文本 | 重点 | 重点 | 重点 | 重点 |
| 战斗/操作提示 | 重点 | 重点 | 标准 | 标准 |

“重点”表示主动检查本模块全部类别；“标准”表示只报明确问题，不做偏好型润色；“保底”表示不主动寻找 Minor 润色，但仍报告影响功能、理解或达到 Major/Critical 的明显问题。无术语模式不得因分类重新启用 `terminology`。

## 7. 分块流程

```bash
python "$SCRIPTS/lqe_chunk.py" split \
  --state "$JOB/state.json" \
  --errors "$JOB/errors_precheck.json" \
  --outdir "$JOB/chunks"
```

`split` 会从 state 读取当前模式允许的术语，按相同源文和译文去重、过滤被更长术语覆盖的命中、保留术语候选标记，并为每段写 `kind`。标准模式可用 `--terms <file>` 显式覆盖术语源；无术语模式禁止该参数。密集内容可加 `--char-budget N`。

未显式传 `--size` 时，`context_pipeline.mode=enforce` 默认每块最多 5 段，`off`/`shadow` 保持 100 段；显式 `--size` 始终优先。该默认值为富上下文预留 worker 输入预算，`prepare` 仍按 100,000 字节硬上限校验；单段仍超限时失败，不截断资料。

默认紧接着生成低成本模块输入，并自动发布无需 AI 的确定性空结果：

```bash
python "$SCRIPTS/lqe_review.py" prepare --job "$JOB"
python "$SCRIPTS/lqe_review.py" auto-publish --job "$JOB"
```

`prepare` 在 `review_packets/<module>/` 生成与当前 split generation 绑定的模块专用 packet，并写出 `batch_plan.json`、`cost_report.json` 和 `selected_evidence_index.json`。该索引逐模块、逐句段记录 checker 实际选中的人物事实、关系、示例、约束和相邻句证据；后续建议阶段只能取这些实际选择的严格并集。非术语模块不携带术语和预检冗余；受保护段不进入任何 packet；`precheck_review` 只携带其负责类别的已有预检。正式 chunk 保持不变。packet 的 `requires_ai: false` 只能来自这些确定性空结果，`auto-publish` 不处理仍需判断的 packet。

当前 v2 job 还会为每个批次生成 `review_packets/context/<module>/batch_NN/bundle_set.json` 与 `worker_manifest.json`。worker 必须同时读取本批 packet、context bundle set、共享资料索引和 manifest 中列出的 SG/背景/确认规则/语言说明；每份可读资料必须使用可安全解析的 `job_relative`、`skill_relative` 或 `embedded_text` locator，并核对摘要、字节数及 100,000 字节总预算。checker 不交付 `references/suggestions.md`，其 `instructions.suggestions` 为 `null`；该文件只交付给 `module=suggestions`。完整 project source manifest 由 runtime 实时校验和绑定，checker 只接收已计入预算的 canonical compact projection，不读取审计 manifest 全文。只有 profile 已声明、能力协商已启用且摘要一致的资料可见。资料缺失、过期、越权、locator 不安全、超过字节预算或不能完整投影时失败，不截断后继续。

必需输出由 `state.check_scope` 决定：

```text
# 标准模式
chunk_NN.terminology.json
chunk_NN.accuracy.json
chunk_NN.grammar.json
chunk_NN.naturalness.json

# 无术语模式
chunk_NN.precheck_review.json
chunk_NN.accuracy.json
chunk_NN.grammar.json
chunk_NN.naturalness.json
```

按 `batch_plan.json` 的模块与批次启动 worker；同一 worker 不得跨批次。每个 worker 处理本批次全部 `requires_ai: true` 的 packet。草稿中的 `reviewed_ids` 必须完整复制 packet 的同名数组；`findings` 只写有问题的 id。`worker_receipt` 必须填写实际 checker worker 身份和本次唯一 run id，不得伪造或复用：

```json
{
  "schema": "lqe.compact-module-draft",
  "version": 1,
  "module": "grammar",
  "chunk_id": 0,
  "packet_digest": "<packet.packet_digest>",
  "worker_batch_id": "<packet.worker_batch_id>",
  "worker_packet_basis_digest": "<packet.worker_packet_basis_digest>",
  "context_bundle_set_digest": "<packet.context_bundle_set_digest>",
  "worker_context_manifest_digest": "<packet.worker_context_manifest_digest>",
  "selected_evidence_index_path": "<packet.selected_evidence_index_path>",
  "selected_evidence_index_digest": "<packet.selected_evidence_index_digest>",
  "worker_receipt": {"worker_id": "<actual-checker-worker>", "run_id": "<unique-run>"},
  "reviewed_ids": [0, 1, 2],
  "findings": [
    {
      "id": 1,
      "issues": [
        {
          "category": "Spelling",
          "severity": "Minor",
          "comment": "The word is misspelled.",
          "needs_confirmation": true,
          "edit": null
        }
      ]
    }
  ]
}
```

用紧凑 publisher 补齐空项并发布正式文件：

```bash
python "$SCRIPTS/lqe_review.py" publish \
  --job "$JOB" --chunk <NN> --module <module> \
  --input <紧凑草稿.json>
```

publisher 会从当前正式 chunk 重新派生 packet，拒绝错误 `packet_digest`、缺失审阅 id、缺失或非法 `worker_receipt`、空 finding、越权类别、错误预检引用和旧 generation，并把 packet、selected evidence index 与 checker receipt 绑定到正式 publication receipt。runtime v1 历史任务只读；`lqe_chunk.py publish-module` 完整数组入口仅保留给当前任务的可选 `proper_names`。packet 超过 30 段时按 `common.md` 每最多 20 个 id 原子更新紧凑草稿。

结构检查与合并：

```bash
python "$SCRIPTS/lqe_chunk.py" validate-checks --job "$JOB"
python "$SCRIPTS/lqe_chunk.py" merge-checks --job "$JOB"
python "$SCRIPTS/lqe_chunk.py" reconcile --job "$JOB"
python "$SCRIPTS/lqe_chunk.py" merge \
  --state "$JOB/state.json" \
  --errors "$JOB/errors_precheck.json" \
  --outdir "$JOB/chunks" \
  --out "$JOB/errors.json"
```

`validate-checks` 按 `state.check_scope` 要求当前四个必需模块结构正确且 id 完整。`merge-checks` 合并并去重问题；`reconcile` 确保准确性类别只由 `accuracy` 模块确认；`merge` 从当前绑定模块重新构建并核对 merged 内容，广播重复段、恢复必须保留的确定性问题，再由脚本生成最终内部结果。模型草稿不能自行声明 AI provenance。

`merge` 原子发布 `errors.json` 与 `errors.contract.json`，绑定当前 split generation。calc、write、apply、export 和聚合持有 generation lease，并拒绝缺 generation、契约缺失/篡改或旧证据。当前任务不得使用 `build-results` 或旧 `lqe_batch merge` 绕过模块发布流程。

需要为审校提供整句参考译文时，在 merge 和 calc 后运行独立建议流程：

```bash
python "$SCRIPTS/lqe_suggestions.py" prepare --job "$JOB" \
  [--categories "Company style,Unidiomatic"] \
  [--severities "Major,Critical"] [--only-missing]
# suggestion worker 按 references/suggestions.md 生成 generation draft
python "$SCRIPTS/lqe_suggestions.py" publish-candidates \
  --job "$JOB" --input <reference_suggestions.draft.json>
python "$SCRIPTS/lqe_suggestion_review.py" prepare --job "$JOB"
# 独立 verifier 按 references/suggestion_review.md 生成 review draft
python "$SCRIPTS/lqe_suggestion_review.py" publish-review \
  --job "$JOB" --input <suggestion_review.draft.json>
python "$SCRIPTS/lqe_suggestion_review.py" publish-final --job "$JOB"
```

参考建议允许整句改写。`optimized` 默认候选严重度为 Major/Critical，`full` 默认纳入全部严重度。术语审查留下未解决结论、输入 blocked、保护段或约束冲突时，程序直接拒绝候选；生成 worker 无权绕过。其余候选按风险进入确定性接收或独立 verifier，只有被接收的候选才能进入 v5 `reference_suggestions.json`。正式建议只供报告展示，不写入 corrected/export。任何 candidate、review 或 final 摘要过期都会 fail closed。

建议生成以源文为唯一语义依据，从源文重新建立完整命题；原译只用于保留变量、标签、换行、受保护文本和已验证局部修改。生成后再核对错译、漏译、增译和已解析约束。缺少说话人、受话人或关系字段本身不触发弃权：若源文已明确 speech act、强度或敌意，且候选无需选择未知关系、称谓、代词、礼貌等级或角色口吻即可忠实表达，应继续生成并引用源文证据；只有缺失信息会实质改变候选措辞时才弃权。`suggestion_context/` 保存同一批建议的资料包与 worker manifest，独立 verifier 读取相同资料证据，但不能修改候选文本，只能接受或拒绝。

一键收尾：

```bash
bash "$SCRIPTS/finalize_job.sh" "$JOB" <nchunks> [single|iterate]
```

`single` 只生成本轮报告和已验证局部建议；已有 `reference_suggestions.json` 时同时显示报告专用参考译文。`iterate` 在未达标时仅应用脚本重新验证通过的局部 edit。用户未明确选择时使用 `single`。状态机：PASS 才创建 `.finalized`；FAIL+single 不改当前译文且不完成；FAIL+iterate 只有至少应用一处安全局部修改时，才更新 `current_target`、`iteration + 1`、设置 `pending_recheck=true` 并返回 `PENDING-RECHECK`。如果没有可应用修改，则写出本轮报告，并通过 `export --errors` 写出已验证的错误覆盖，返回 `REVIEW-REQUIRED`，清除 `.iteration_pending`，不推进 iteration。下一轮必须重新预检、分块和检查；state/译文/scope/预检/术语指纹变化时旧 chunks 会归档，旧模块输出不可复用。

## 8. 评分

```bash
python "$SCRIPTS/lqe_calc.py" \
  --state "$JOB/state.json" \
  --errors "$JOB/errors.json"
```

```text
K_per_category = Σ severity_points
L_per_category = weight × K
score = max((1 - ΣL / 固定词数) × 100, 0)
```

默认策略来自 `state.scoring_policy`，CLI 只做显式覆盖。默认严重度点数：Neutral 0、Minor 1、Major 5、Critical 10；默认阈值 98。词数在初始化时固定。每次计分都先清除并重算 `repeated`；`repeat_dedup` 控制重复去重，`critical_gate` 控制 Critical 直接 FAIL。

脚本强制 Terminology、Untranslated、Markup、Length 为 Major。受保护段自动跳过计分。

## 9. 报告、迭代和导出

PASS 或单轮模式：

```bash
python "$SCRIPTS/lqe_io.py" write \
  --state "$JOB/state.json" \
  --errors "$JOB/errors.json" \
  --score <分数>
```

用户明确启用自动迭代且结果为 FAIL 时：

```bash
python "$SCRIPTS/lqe_io.py" apply-fixes \
  --state "$JOB/state.json" \
  --errors "$JOB/errors.json" \
  --score <分数>
```

导出建议译文：

```bash
python "$SCRIPTS/lqe_io.py" export \
  --state "$JOB/state.json" \
  --errors "$JOB/errors.json"
```

`write --score` 是一致性输入；脚本按 state policy 和 errors 重新计算，分数不一致时告警并采用重算值。报告中的建议状态固定为“可直接采用”“建议待确认”“部分修正，仍需确认”“未生成建议，需人工处理”“已保护”。`*_corrected.<ext>` 是标准交付文件名；其中 corrected 仅是内部机器字段和标准文件名的一部分。

内部结果中的 `corrected: ""` 是合法的整段删除；只有 `corrected: null` 表示没有建议修改。write、apply、export 和聚合不得把空字符串当成字段缺失。

LQE 报告保留三张可见工作表：`说明·导读`、`LQA Scorecard` 和 `LQE Results`；绑定 state/errors 和可见内容摘要的 `_LQE_CONTRACT` 必须保持 veryHidden。`说明·导读` 固定排在第一张并作为默认打开页，包含三步阅读路径、Scorecard 读法、10 列释义、建议状态、审校结论和交付提示。Scorecard 完整显示判定、分数、类别汇总和逐错误审校行，不隐藏行列；类别汇总将每个严重度的总数与重复数合并显示。

`LQA Scorecard` 的逐错误区域和 `LQE Results` 的一段一行审校区域共用 10 列：Segment ID、原文、原译、AI/建议译文、建议状态、错误类别、严重度、问题说明、审校结论、审校终稿或备注。这里的“原译”固定表示本轮实际送审译文：首轮为输入原译，后续轮为上一轮已应用的 `current_target`；术语跨度、差异比较和建议译文必须使用同一基线。Scorecard 合并父类别和子类别，不再显示文件名、迭代、处理方式和 AI provenance 等技术列；这些审计字段保留在 `LQE Results` 隐藏区。`LQE Results` 的多错误段在可见首行汇总；额外逐错误审计行、无问题段和原始来源字段保留但隐藏。`审校结论` 使用“接受、修改后接受、拒绝、待确认”下拉值。

术语不一致明细与报告标记必须直接读取 issue 的 `term_source`、`expected_targets` 和 `term_spans`。禁止从 `comment` / “问题说明”反解析术语或跨度。缺少任一字段的旧 Terminology issue 不兼容当前合同；旧任务必须从 `pre-check` 重新运行并重建后续 artifact。

报告使用富文本标记。原有差异图例保持：原译中删除或替换的内容显示为红色删除线，AI/建议译文中新增或替换的内容显示为红色字体。术语图例为：原文中受术语问题影响的词显示红字；原译中的术语问题词显示红字，若同时属于删除或替换差异则显示红色删除线。`term_spans.target` 为空时只标原文。Results 对同段全部 Terminology issue 的 span 取并集，Scorecard 每条问题只标对应 span。每个含 Terminology/`term_spans` 的历史迭代 entry 必须保存 `review_targets`，格式为 `{"<segment_id>": "该轮送审译文"}`；缺失、损坏或与 span 切片不一致时报告失败，不得回退到首次输入译文或静默跳过标记。安全局部 edit 生成的建议可进入 corrected 流程；`reference_suggestions.json` 的整句参考译文始终标为“建议待确认”，只用于审校。`LQE AI 复核状态`、`LQE AI 编辑状态`、`LQE 检查来源` 等逐错误 provenance 列保留在隐藏审计区。正式 module entries 同时受内容摘要和独立本地发布收据绑定；“AI 模块记录”是本地流程证据，不是 host/orchestrator 的外部身份签名。corrected 文件不添加差异样式或术语样式，保持纯文本。

表格与 SDLXLIFF 报告都采用同一 10 列审校视图，并把来源文件、TU ID、SDL Segment ID、处理方式、逐错误 provenance、保护证据和 `LQE_Iter` 保留在隐藏审计区；`LQE_Iter` 固定为最后一列。其余 SDL 私有字段、文件 SHA-256、语言、扩展 namespace、规则命中、排除和保护证据写入 `source_manifest.json`。SDL corrected Excel 固定为 5 列：来源文件、TU ID、SDL Segment ID、原文、译文。

第一版不回写 SDLXLIFF XML；`export` 只生成 `<任务名>_corrected.xlsx`，原始 XML 保持不变。

## 10. 多工作表

默认交付模式是分开报告：每个工作表单独建立子 job，并分别保留各自的
`<子任务名>_lqe.xlsx` 和 corrected 文件。未收到用户明确的“合并报告”“汇总报告”
或“恢复原工作簿结构”要求时，到子 job 交付为止，不运行聚合脚本，不生成父级
`<文件名>_lqe.xlsx`，也不把多个子任务的 Results/Scorecard 复制进同一个报告。

<pre data-lqe-multisheet-delivery-contract>
{
  "default_report_mode": "separate",
  "default_unit": "child_job",
  "default_outputs": ["&lt;child&gt;_lqe.xlsx", "&lt;child&gt;_corrected.&lt;ext&gt;"],
  "aggregate_requires": "explicit_user_request",
  "forbidden_without_request": [
    "&lt;parent&gt;_lqe.xlsx",
    "combined child Results/Scorecard workbook"
  ]
}
</pre>

只有用户明确要求跨工作表聚合时才运行：

```bash
python "$SCRIPTS/aggregate_sheets.py" \
  --job <文件名> \
  [--sheets 剧情,功能,社媒]
```

显式聚合结果放在父 job 目录，保留原工作簿工作表、空行、列顺序和格式，只替换目标列。聚合会按每个子 job 的当前 state、`errors.contract.json` 和已验证 chunk generation 重新校验结果，并复用 chunk 中的术语命中上下文；子报告隐藏的 `_LQE_CONTRACT` 同时绑定 state/errors、可见 `LQE Results` 内容及逐错误 provenance 行结构。聚合复制每个子任务的 Results 与 Scorecard（含合并行和历史 AI 状态）。发布前会按稳定顺序重新取得全部子任务 lease；缺失、损坏、过期或未绑定的结果/报告、过期 chunk 证据、输入文件漂移都会失败，原有父级产物保持不变。子任务默认各自继承 policy；除阈值外的策略不一致时失败。显式 `--threshold` 只覆盖阈值；任一子任务 FAIL 则汇总 FAIL。

缺少 `input_sha256` 的旧 state 仅兼容核对已选 source/target 单元格，不能证明其他公式或样式未漂移；正式交付前应重新 `read` 升级为当前 state contract。

该聚合命令只用于表格工作簿；SDLXLIFF 目录是一个多文件 job，不建立多工作表子 job。

## 11. 文件结构

```text
jobs/<文件名>/
├── state.json
├── scope.json
├── source_manifest.json       # SDLXLIFF job
├── tm_candidates.json         # SDLXLIFF job；默认只是候选
├── sg.txt
├── background.md
├── lang_notes.md
├── confirmed_rules.md
├── terms.json                 # 仅标准模式需要落盘转换时存在
├── errors_precheck.json
├── errors.json
├── chunks/
├── review_packets/
│   ├── selected_evidence_index.json
│   └── context/<module>/batch_NN/{bundle_set.json,worker_manifest.json}
├── suggestion_context/{bundle_set.json,worker_manifest.json}
├── reference_suggestions.packet.json
├── reference_suggestions.candidates.json
├── suggestion_review.packet.json
├── suggestion_review.json
├── reference_suggestions.json
├── <任务名>_lqe.xlsx
└── <任务名>_corrected.<ext>   # SDLXLIFF job 固定为 .xlsx
```

不要修改用户原始 Excel，也不要改写历史 `outputs`。任务产物只写入对应 job 目录。

缺少 `job_runtime_contract_version: 2` 的历史 job 只能查看或验证现有产物，不得继续重新分块、审校、计分、写报告或导出。需要续跑时使用 `lqe_io.py reread --from-job <旧任务> --input <原输入> --job <新任务>`；程序先核对输入摘要、列/格式和逐段覆盖，再原子建立 v2 job，旧任务保持不变。

## 12. 验证

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 scripts/run_tests.py
```
