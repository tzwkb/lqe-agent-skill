# LQE Translator

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Agent Skill](https://img.shields.io/badge/Agent%20Skill-Codex-blue.svg)](SKILL.md)
[![Python](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org/)

[English](README.md) | 中文

用于游戏本地化 LQE：先做机器预检，再由专项检查模块报告问题和安全的局部修改，最后由 Python 校验、评分并生成对应格式的交付文件。

> PM 的手动与 Agent 操作、验收和恢复流程见[项目经理操作手册](PM_GUIDE.html)。

## 核心约束

- 项目上下文来自 `profile.json`、`confirmed_rules.md`、风格指南和目标语言说明；标准模式还加载术语表。
- 标准模式必需模块为术语、准确性、语法和自然度；无术语模式必需模块为 `precheck_review`、准确性、语法和自然度，专名模块只在标准模式下可选。
- 模型只提交 `issues` 和安全的局部 `edit`；Python 校验后生成内部完整文本。
- `confirmed: true` 表示该译法已经确认，可在证据唯一时安全修改；`protected: true` 表示不可修改。
- 受保护段不修改、不计分。
- SDLXLIFF 1.2 与 XLIFF 2.0 可直接读取单文件或递归目录，不需要先转换为工作簿。
- 标准交付文件为 `<任务名>_lqe.xlsx` 和按输入格式确定扩展名的 corrected 文件；XML 任务保留 XLSX 配套表，并额外写出 corrected SDLXLIFF/XLIFF XML。

## 目录结构

```text
lqe-translator/
├── scripts/
│   ├── lqe_io.py           # 读取、预检、保护、报告和导出
│   ├── lqe_chunk.py        # 分块、校验、合并和归属处理
│   ├── lqe_review.py       # 生成低成本检查包并发布紧凑草稿
│   ├── lqe_suggestions.py  # 发布仅供报告使用的整句参考译文
│   ├── lqe_corrections.py  # 校验局部修改并生成完整文本
│   ├── lqe_calc.py         # LQE 评分
│   └── finalize_job.sh     # 从校验到导出的一键收尾
├── references/
│   ├── suggestions_v2.md
│   ├── suggestion_review_v2.md
│   ├── corpus_ingest.md
│   └── check_modules_v2/
│       ├── common.md
│       ├── terminology.md
│       ├── precheck_review.md
│       ├── accuracy.md
│       ├── grammar.md
│       ├── naturalness.md
│       ├── proper_names.md
│       └── term_audit.md
├── target_languages/<code>/
│   ├── attributes.json
│   └── eval_notes.md
├── projects/<game>/<source>-<target>/
│   ├── profile.json
│   ├── checks.json
│   ├── confirmed_rules.md
│   ├── terms_*.json
│   └── sg*.md / sg*.txt
└── jobs/<任务名>/
    ├── state.json
    ├── scope.json
    ├── tabular_source_manifest.json # 表格任务
    ├── source_manifest.json         # XML 任务
    ├── tm_candidates.json         # SDLXLIFF 任务
    ├── capability_resolution.json
    ├── project_asset_snapshot.json
    ├── project_assets/
    ├── shadow_context/context.json # 仅 shadow 模式
    ├── confirmed_rules.md
    ├── errors_precheck.json
    ├── errors.json
    ├── chunks/
    ├── review_packets/context/<module>/batch_NN/
    ├── suggestion_context/            # 含 advisory input_measurement.json
    ├── reference_suggestions.packet.json
    ├── reference_suggestions.candidates.json
    ├── suggestion_review_context/     # 含 advisory input_measurement.json
    ├── suggestion_review.packet.json
    ├── suggestion_review.json
    ├── reference_suggestions.json
    ├── <任务名>_lqe.xlsx
    ├── <任务名>_corrected.<csv|tsv|xlsx>
    └── <任务名>_corrected.<sdlxliff|xliff|xlf> 或 <任务名>_corrected_xliff/
```

## 安装与路径

需要 Python 3.12 或更高版本。

```bash
python3 -m pip install -r requirements.txt
SCRIPTS=~/.codex/skills/lqe-translator/scripts
```

在 skill 根目录运行回归测试：

```bash
python3 scripts/run_tests.py
```

## 标准流程

### 1. 初始化

优先使用项目档案；一个参数即可加载语言设置、检查项、确认规则、术语和风格指南。

Profile v2 可用 `module_context_views` 按模块声明 capability、dimension、邻句窗口，以及人物事实、关系和审核案例数量上限。`max_runtime_examples: 0` 表示不提供案例。`off` 不启用 optional view，`shadow` 只写入 `state.shadow_module_context_views` 供审计，`enforce` 才进入正式 worker 输入。shadow typed asset 即使自称 core capability 也不能进入正式 bundle；显式标记 `attributes.runtime_rule: false` 的关系同样会被排除。

新任务初始化前，Agent 必须先询问用户选择审校输出模式，除非当前请求已明确：`optimized` 为降本模式，`full` 为完整模式。选择通过 `--review-mode` 写入 `state.review_policy`；已有 job 直接沿用 state，不中途切换。

```bash
JOB="jobs/<任务名>"
python3 "$SCRIPTS/lqe_io.py" read \
  --project "<game>/<source>-<target>" \
  --input "<file>.xlsx" \
  --source-col "<原文列>" \
  --target-col "<译文列>" \
  --review-mode "<optimized|full>" \
  --out "$JOB/state.json"
```

项目档案必须声明 `language_pair`、`source_lang` 和 `target_lang`。运行检查前，必须读取项目背景、`confirmed_rules.md`、风格指南和语言说明。

初始化先在 staging 中生成并校验全部资源，拒绝输入/输出/资源别名（含软链和硬链），最后发布 `state.json`。失败时不留下正式 `state.json`、`scope.json`、`terms.json` 或半套 SDL 资源。

客户没有逐句情境时，先生成缺口报告和人工待补模板；程序不会自动猜说话人、受话人、场景、关系阶段、语气或文本类型：

```bash
python3 "$SCRIPTS/lqe_context_overrides.py" gaps \
  --state "$JOB/state.json" --out "$JOB/context_gap_report.json"
python3 "$SCRIPTS/lqe_context_overrides.py" scaffold \
  --state "$JOB/state.json" --out "$JOB/context_overrides.template.json"
```

人工或授权来源核实后，用 `read --context-overrides <已核实.json>` 新建 job。sidecar 不能绕过 shadow；key、源文摘要、授权、字段声明、冲突和必填上下文全部整批校验。歧义别名的模板会在 `expected_context` 记录当前原值，只有原值仍精确一致时才允许替换为人工确认的 canonical ID。通过后，sidecar、缺口报告及其 fingerprint 同时绑定进 state 和 source manifest；失败不修改原输入或原 job。运行顺序固定为项目 canonical segment override、本任务已核实 sidecar、语言/语域规则，因此规则只能依据当前 job 已绑定的情境作出结论。缺口状态区分 `not_provided`、`unresolved_alias`、`ambiguous_alias` 和 `not_applicable`。

任务明确不检查术语和专名时，在 `read` 中加入 `--no-terminology`。该参数覆盖 profile 术语配置，且不能与显式 `--terminology <file>` 同时使用：

```bash
python3 "$SCRIPTS/lqe_io.py" read \
  --project "<game>/<source>-<target>" \
  --input "<file>.xlsx" \
  --source-col "<原文列>" \
  --target-col "<译文列>" \
  --no-terminology \
  --out "$JOB/state.json"
```

解析后的模式写入 `state.check_scope`，并同步生成 `$JOB/scope.json`。无术语模式只关闭术语、专名和术语审计；不会关闭文件内一致性、Markup、数字等检查。

XML 本地化输入可传单个 `.sdlxliff`、`.xliff`、`.xlf` 文件，或只包含一种受支持 XML 家族的目录。`--input-format` 可取 `auto`、`tabular`、`sdlxliff`、`xliff`；受支持的单文件和纯 XML 目录可自动识别，混合目录必须显式指定。XML 直接读取句段，不使用 `--source-col` 或 `--target-col`：

```bash
python3 "$SCRIPTS/lqe_io.py" read \
  --project "<game>/<source>-<target>" \
  --input "<文件或目录>" \
  --input-format sdlxliff \
  --out "$JOB/state.json"
```

运行时支持 SDLXLIFF 1.2 与 XLIFF 2.0。XLIFF 2.0 省略根节点 `trgLang` 时，必须由项目 profile 或 `--target-lang` 明确提供目标语言；声明冲突会失败。未知厂商扩展若不影响句段边界会保留并记录，若造成 source、target 或 `mid` 配对歧义则失败。内容类型与排除只由 profile 显式规则决定，不根据 CC、FF、文件名或目录名推断。

以下可见合同精确定义两种解析后 scope：

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

### 2. 标记受保护内容

标准模式的术语条目使用明确字段；无术语模式忽略术语条目。两种模式都可使用经过证据确认的 TM 匹配等显式段保护。

```json
{"source":"源词","target":"确认译法","confirmed":true,"protected":false}
```

- 新 job 的每个术语/候选必须显式带布尔 `confirmed` 和 `protected`；任一字段缺失都在正式资源发布前失败。
- 已经同时显式带两个布尔字段的 canonical CSV/XLSX/JSON 可保留 `status` 作为审计元数据，无需 `term_status_map`；如果提供映射但与显式字段冲突，则失败。
- 以下状态列规则适用于缺少任一布尔字段、仍需转换的原始术语表。**状态列检测是「规则」而非「枚举」**：只要表头**包含** `status` 或 `状态`（大小写不敏感、任意位置、不论前后缀/括号）即视为状态列。未来主术语库改列名（如 `术语状态 Status(TH)`、`审核状态`）也能自动命中，不会因漏检而静默全 `confirmed:false`。若**同时命中多个**状态列，转换器报错退出并要求用 `--status-col '<表头>'` 指定，绝不猜测。
- 原始条目缺少任一布尔字段且**存在 `status` 列时，必须显式提供确认决策**，否则转换器 fail-closed 报错退出（并列出检测到的状态值），绝不静默产出全 `confirmed:false`：
  - 确认决策 = `--approved-statuses '<值>'` / `'*'`（整份确认） / `''`（显式整份未确认）；**仅传 `--protected-statuses` 不算确认决策**，仍会 fail-closed。
  - 转换器参数 `--approved-statuses 'Approved,合规审核通过'`（按需）；
  - 或 `--exclude-statuses '<status>'` 把额外驳回状态的术语整条剔除（不参与检查、不判术语问题）；
  - **`Denied` 状态术语默认整条排除，无需任何 flag**（既定规则：客户驳回的术语永不进入术语表；大小写不敏感，`denied`/`DENIED` 同样排除）；`--exclude-statuses` 仅用于追加其它需排除的状态；
  - 或 profile 的 `term_status_map`。
- **状态值比较大小写不敏感（规则）**：`Approved`/`approved`、`Denied`/`denied` 一视同仁，不存在逐值特判。
- 需要转换但**未检测到任何状态列时，转换器同样 fail-closed**，除非显式传 `--no-status`（声明该术语表确实无任何确认信息）。这彻底堵死「列被改名/移位 → 静默全未确认」的口子。
- **不要凭「未映射」的 `status` 自行推断 `confirmed`**；但一旦用户提供了映射，转换器应据映射显式写出 `confirmed`/`protected`，这属于契约授权的落地方式，而非猜测。
- 缺少显式布尔字段的 CSV/XLSX/JSON 状态值必须由 `profile.term_status_map` 明确映射。`protected_term_statuses` 只能补充保护，不构成确认决策；如提供，必须是元素均为非空字符串的数组。`Denied` 始终大小写不敏感地排除且不得映射。

输入文件带 TM 精确匹配证据时，Agent 先确认列名、样例值和证据，写出明确的段 id，再运行：

```bash
python3 "$SCRIPTS/lqe_io.py" protect-segments \
  --state "$JOB/state.json" \
  --protected-file "$JOB/tm_protected.agent_decision.json" \
  --reason TM_100_MATCH
```

脚本不会猜测匹配列或匹配值。

SDLXLIFF 中明确 locked 的段始终以 `SOURCE_LOCKED` 保护。默认策略 `candidate-only` 只把同时满足 `origin=tm`、`percent=100`、`text-match=SourceAndTarget` 的段写入 `tm_candidates.json`，不会自动保护。确认后可将该文件交给 `protect-segments`，也可在 profile 使用 `protect-exact-source-and-target`，或用 CLI `--protect-exact-tm` 显式启用严格自动保护；只有 100% 数值不够。locked 与严格 TM 同时命中时，主原因仍为 `SOURCE_LOCKED`，两类证据分别保留。

profile 可增加可审计的 SDLXLIFF 规则：

```json
{
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

### 3. 机器预检

```bash
python3 "$SCRIPTS/lqe_io.py" pre-check \
  --state "$JOB/state.json" \
  --out "$JOB/errors_precheck.json"
```

预检覆盖未翻译或空译文、变量、标签、换行、数字、长度、空格、标点、重复词、大小写、文件内一致性和项目自定义规则。标准模式还运行术语及依赖术语表的专名检查；无术语模式跳过这些术语检查，其余预检仍需结合上下文复核。

### 4. 分块并运行检查模块

```bash
python3 "$SCRIPTS/lqe_chunk.py" split \
  --state "$JOB/state.json" \
  --errors "$JOB/errors_precheck.json" \
  --outdir "$JOB/chunks"

python3 "$SCRIPTS/lqe_review.py" prepare --job "$JOB"
python3 "$SCRIPTS/lqe_review.py" auto-publish --job "$JOB"
```

标准模式下，`split` 通过 state 读取术语；`--terms <file>` 只是可选覆盖，无术语模式会拒绝该参数。未显式传 `--size` 时，enforce 上下文任务默认每块最多 5 段，off/shadow 保持原来的 100 段；显式 `--size` 始终优先。该默认值用于控制富上下文任务的单批资料密度；`prepare` 完整计量 worker 输入，但不设置字节硬上限，也不截断任何资料。分块输入带指纹；state、当前译文、scope、预检、术语或最终生效的分块参数变化时，旧 chunks 会归档，旧模块输出不可复用。每个 `chunk_NN.json` 按 `state.check_scope` 生成：

`prepare` 生成与当前 chunk 绑定的模块专用 `review_packets`、`batch_plan.json`、`cost_report.json` 和 `selected_evidence_index.json`。该索引记录各 checker 模块对每个句段实际选中的证据，而不是更宽泛的配置视图。非术语模块不再重复读取术语与预检字段；受保护段和 `precheck_review` 的不适用段由脚本补空。`auto-publish` 只发布完全不需要 AI 的 packet。

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

按 `batch_plan.json` 为每个模块分配有界 worker：每个 checker 批次最多 4 个 packet，同时不超过 25,000 原译字符。instructions、项目资料、共享资产、bundle、manifest 和 packet 的完整输入字节继续计量，但不设程序硬上限。manifest 的 `budget.measured_bytes` 是组件基线，包含 manifest/bundle 外壳和实际 packet 的完整批次值由 batch plan/cost report 或 suggestion `input_measurement.json` 记录。主 Agent在派发 checker 前读取 `review_packets` 的 batch plan、cost report 和批次 manifest；派发建议生成或复核前分别读取对应 `input_measurement.json`、存在时的 batch plan，以及 packet 引用的 worker manifest，再结合当次模型与任务自行决定继续还是重新规划。新 manifest 使用 `budget.max_bytes: null`、`budget.status: "advisory"`。每份可读资料都必须带安全的 `job_relative`、`skill_relative` 或 `embedded_text` locator，并绑定摘要和字节数；不得通过截断降低计量。每个新批次重新读取模块说明和项目上下文。

模型写紧凑草稿：`reviewed_ids` 完整复制 packet，`findings` 只保留有问题的 id；同时复制 selected evidence 绑定，并填写实际 checker 的 `worker_id` 与本次唯一 `run_id`。用 `lqe_review.py publish --job "$JOB" --chunk <NN> --module <module> --input <草稿.json>` 发布。publisher 会补齐正式全 ID 数组，并把 packet、checker receipt 与 `selected_evidence_index.json` 绑定到正式 module publication receipt。

`precheck_review` 只确认或删除 Markup、Length、Locale convention、Company style、Inconsistency、Other 类别的非术语预检，不得创建 Terminology、`TERM REVIEW:` 或 `confirmed_term` 证据。

紧凑草稿协议：

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
          "category": "Grammar",
          "severity": "Minor",
          "comment": "The verb form does not agree with the subject.",
          "needs_confirmation": true,
          "edit": null
        }
      ]
    }
  ]
}
```

`optimized` 中，所有 `comment` 必须为非空英文，以 20–30 个字符为软目标；Minor 固定使用 `needs_confirmation: true`、`edit: null`，不生成 corrected 或自动迭代修改。`full` 中 comment 无字符目标，包括 Minor 在内的所有严重度均由 Agent 判断是否存在安全、唯一的局部 edit。

新译名、术语表缺词、多个合理方案或整句重写，使用 `needs_confirmation: true` 和 `edit: null`。术语或专名修改还必须有唯一的 `confirmed: true` 候选和 `confirmed_term` 证据。

每个 Terminology issue 必须额外带 `term_source`、`expected_targets` 和 `term_spans`：

```json
{
  "term_source": "督管案台",
  "expected_targets": ["Supervisor's Counter"],
  "term_spans": {
    "source": [{"start": 2, "end": 6, "text": "督管案台"}],
    "target": [{"start": 10, "end": 22, "text": "control desk"}]
  }
}
```

`term_spans` 必须恰有 `source` 和 `target` 两个数组。每个 span 对象恰有整数 `start`、整数 `end` 和非空 `text`，使用 0-based、左闭右开的非空区间；数组按 `(start,end,text)` 升序排列，不得重复或重叠。`text` 必须严格等于原文或当前译文的对应切片，且每个 source span 的 `text` 必须等于 `term_source`。`source` 非空；漏译或没有可安全定位的译文问题词时，`target` 可为空，不得猜测或整句标记。复核机器预检 Terminology issue 时，`term_source`、`expected_targets` 和 `term_spans.source` 为只读并按 `precheck_ref` 继承；模型必须精确补充 `term_spans.target`，确实没有可标译文词时保留空数组。新发现的 Terminology issue 必须完整提交三个结构化字段。

表格中的 `content_type`、`text_type`、`文本类型`、`文本类别` 会作为上游文本分类传入 review packet，不自行分类。`optimized` 优先使用行级 `content_type`，否则使用 `text_type_context`，并按 `references/check_modules_v2/common.md` 的矩阵调整重点；`full` 只把分类作为上下文，不改变检查强度。两种模式都不关闭机器预检或必需模块。普通源文永远不会因为内容像“类型标题”而被跳过；只有 profile 的 `tabular.text_type_marker_rules` 显式声明的标记行才会被识别并审计。

新任务同时绑定稳定句段身份、来源证据以及项目资产/能力快照。输入含显式上下文时使用 `--sheet`、`--key-col` 和可重复的 `--context-col FIELD=COLUMN`；常用别名包括 `--content-type-col`、`--speaker-col`、`--addressee-col`。可选能力只有在 `enforce` 模式才进入正式 packet；`shadow` 写入独立 `shadow_context/context.json`，不进入正式去重、审校、建议或报告。旧 `.xls` 由 `xlrd>=2.0` 只读，corrected 固定输出 `.xlsx`。

profile v2 用 asset registry 声明资料的路径、权威级别、来源和分发范围，并用 capability registry 决定各模块能读取什么。人物、关系、剧情示例和语言规则应使用 canonical JSON 资产；模块启用 `language_policy.*` 并消费其结论时，必须同时声明对应 `constraint_kinds`，缺失会直接判 profile 无效，不再静默丢掉已解析规则。原始 XLSX/DOCX 可保留作溯源，但不会因放在目录里而自动注入。`worker_manifest.json` 通过安全、可核验的 locator 绑定每批 worker 实际读取的 instructions、SG、语言说明、共享资产、context bundle 和 packet。checker 的 `instructions.suggestions` 为 null，只有建议生成 worker 接收该说明；完整 project source manifest 保持 runtime 实时校验，worker 只接收已计量的 canonical compact projection，不读取原始审计全文。`review_packets/selected_evidence_index.json` 逐模块、逐句段记录实际选中的证据，供后续建议阶段取严格并集。worker 输入字节只作主 Agent本轮判断的审计证据，不是程序接受阈值。

跨 sheet/版本核对使用 `--pivot-sheet`、`--pivot-key-col`、可重复的 `--pivot-compare` 和显式 `--pivot-authority`；同 key 不完整、重复或 authoritative 值冲突会在审校前阻断。历史任务缺 `job_runtime_contract_version: 2` 时只能验证既有产物；续跑需用：

```bash
python3 "$SCRIPTS/lqe_io.py" reread \
  --from-job <旧任务> --input <原输入> --job <新任务>
```

### 5. 校验、合并和评分

```bash
python3 "$SCRIPTS/lqe_chunk.py" validate-checks --job "$JOB"
python3 "$SCRIPTS/lqe_chunk.py" merge-checks --job "$JOB"
python3 "$SCRIPTS/lqe_chunk.py" reconcile --job "$JOB"
python3 "$SCRIPTS/lqe_chunk.py" merge \
  --state "$JOB/state.json" \
  --errors "$JOB/errors_precheck.json" \
  --outdir "$JOB/chunks" \
  --out "$JOB/errors.json"

python3 "$SCRIPTS/lqe_calc.py" \
  --state "$JOB/state.json" \
  --errors "$JOB/errors.json"
```

`merge` 会从当前绑定模块重新推导 merged 问题与 provenance，拒绝伪造的中间 merged 文件，再原子发布 `errors.json` 与 `errors.contract.json`。正式 module entries 的内容摘要与独立本地发布收据都会被校验；模型草稿不能自报 AI 复核/编辑状态。calc、write、apply、export 和聚合持有 generation lease，并拒绝 provenance 缺失以及契约缺失、篡改或过期。当前 reader 创建的任务在 `chunks/` 缺失时也不会退回 state-only 校验。

需要整句参考建议时，在 merge 和 calc 后运行：

```bash
python3 "$SCRIPTS/lqe_suggestions.py" prepare \
  --job "$JOB" --severities "Major,Critical" --only-missing \
  [--worker-batch-size N]
python3 "$SCRIPTS/lqe_suggestions.py" publish-candidates \
  --job "$JOB" --input <参考建议草稿.json>
python3 "$SCRIPTS/lqe_suggestion_review.py" prepare --job "$JOB"
python3 "$SCRIPTS/lqe_suggestion_review.py" publish-review \
  --job "$JOB" --input <建议复核草稿.json>
python3 "$SCRIPTS/lqe_suggestion_review.py" publish-final --job "$JOB"
```

`optimized` 默认只将 Major/Critical 放入候选；`full` 默认纳入全部严重度。未解决术语结论、blocked/保护段和约束冲突在生成前直接拒绝；生成后再按候选文本重评已解析约束。明确不匹配的候选硬拒绝，无法确定的候选交给独立 verifier。只有验收通过的候选才能进入 v5 正式建议；candidate、review 或 final 摘要过期均 fail closed。

不传 `--worker-batch-size` 时 generation 默认单批。主 Agent读取 `suggestion_context/input_measurement.json` 的 advisory 实测后，可按本轮判断重新 prepare 并显式指定每批候选数；verifier 沿用 generation 批次，并在 `suggestion_review_context/input_measurement.json` 记录自己的完整实测。两阶段都不按 bytes 自动拆批、拒绝或截断。

缺少说话人、受话人或关系字段不等于自动弃权。若源文已经明确 speech act、强度或敌意，且候选不需要选择未知关系、称谓、代词、礼貌等级或角色口吻即可保持这些信息，生成 worker 应引用源文证据继续生成。正式候选的 `tone_decision.uncertainties` 必须为空：已解决或通过中性表达规避的缺口写入 evidence；仍会改变译文的未知信息必须弃权，不能带着不确定性进入发布链。

首轮检查应明确使用 `single`：

```bash
bash "$SCRIPTS/finalize_job.sh" "$JOB" <分块数> single
```

只有用户明确要求自动迭代时才使用 `iterate`。仅 PASS 创建 `.finalized`；FAIL+single 只写审阅产物，不改当前译文、不完成。FAIL+iterate 只有至少应用一处重新验证通过的安全局部 edit 时，才更新 `current_target`、iteration、设置 `pending_recheck=true` 并返回 `PENDING-RECHECK`；零修改时写出本轮报告，并通过 `export --errors` 写出已验证的错误覆盖，返回 `REVIEW-REQUIRED`，清除 `.iteration_pending`，不推进 iteration。下一轮必须重新预检、分块并运行全部模块。

### 6. 生成标准交付文件

```bash
python3 "$SCRIPTS/lqe_io.py" write \
  --state "$JOB/state.json" \
  --errors "$JOB/errors.json" \
  --score <分数>

python3 "$SCRIPTS/lqe_io.py" export \
  --state "$JOB/state.json" \
  --errors "$JOB/errors.json"
```

所有输入都生成 `<任务名>_lqe.xlsx`，记录分数、问题、建议译文、处理方式和历史记录。corrected 输出按输入格式区分：

`write --score` 是一致性输入；脚本按 state policy 与 errors 重算，分数不一致时告警并采用重算值。

- CSV/TSV 输入输出 `<任务名>_corrected.csv` 或 `<任务名>_corrected.tsv`，保持原行列和输入扩展名。
- XLSX 输入输出 `<任务名>_corrected.xlsx`，保持工作簿、工作表、空行、列顺序和格式。
- SDLXLIFF/XLIFF 输出固定 5 列的 `<任务名>_corrected.xlsx` 配套表，并生成 corrected XML：单文件为 `<任务名>_corrected.<sdlxliff|xliff|xlf>`，目录为 `<任务名>_corrected_xliff/`。

报告保留 `说明·导读`、`LQA Scorecard` 和 `LQE Results` 三张可见工作表；`_LQE_CONTRACT` 保持 veryHidden。导读固定排在第一张并作为默认打开页，面向新人解释阅读流程、Scorecard、10 个审校列、建议状态、审校结论和交付检查。Scorecard 完整显示判定、分数、精简类别汇总和逐错误审校行，不隐藏行列。

Scorecard 的逐错误区域和 `LQE Results` 共用 10 列：Segment ID、原文、原译、AI/建议译文、建议状态、错误类别、严重度、问题说明、审校结论、审校终稿或备注。“原译”固定表示本轮实际送审译文：首轮为输入原译，后续轮为上一轮已应用的 `current_target`；术语跨度、差异比较和建议译文使用同一基线。Scorecard 合并父类别和子类别，并移除文件名、迭代、处理方式和 AI provenance 等技术列；这些审计字段保留在 Results 隐藏区。Results 可见区一段一行；多错误在首行汇总，额外逐错误审计行与无问题段隐藏。建议状态固定为“可直接采用”“建议待确认”“部分修正，仍需确认”“未生成建议，需人工处理”“已保护”。

术语不一致明细与报告标记直接读取 issue 的 `term_source`、`expected_targets` 和 `term_spans`，不得从 `comment` / “问题说明”反解析术语或跨度。只要旧任务中任一 Terminology issue 缺少这些字段，就不兼容当前合同，必须从 `pre-check` 重跑并重建全部后续 artifact。

报告富文本图例：原文中受术语问题影响的词显示红字；原译中的术语问题词显示红字，若同时属于删除或替换差异则显示红色删除线；AI/建议译文中新增或替换的内容显示红字。`term_spans.target` 为空时只标原文。Results 对同段全部 Terminology span 取并集，Scorecard 每条问题只标对应 span。每个含 Terminology/`term_spans` 的历史迭代 entry 必须保存 `review_targets`，格式为 `{"<segment_id>": "该轮送审译文"}`；缺失、损坏或与 span 切片不一致时失败，不得回退或静默跳过。安全局部 edit 可进入 corrected 流程；独立 `reference_suggestions.json` 中的整句建议只供审校，并固定标为“建议待确认”。corrected 文件保持纯文本，不添加差异或术语样式。

经验证的内部结果中，`corrected: ""` 是合法的整段删除；只有 `corrected: null` 表示没有建议修改。write、apply、export 和聚合都必须保留这一区别。

表格与 XML 报告使用相同的 10 列审校视图。来源文件、TU/Unit ID、Segment ID、处理方式、逐错误 provenance、保护证据和 `LQE_Iter` 保留在隐藏审计区，`LQE_Iter` 固定为最后一列。`source_manifest.json` 保存输入 SHA-256、声明语言、扩展 namespace、规则命中、排除和 locked/TM 证据；`tm_candidates.json` 把 SDL 严格候选与保护决定分开。corrected Excel 配套表固定为 5 列：来源文件、TU/Unit ID、Segment ID、原文、译文。

`export` 以事务方式写出 corrected XML，所有原始 XML 保持不变。corrected 混合内容格式错误、源文件漂移或发布失败时，不留下正式 corrected 产物。

### Finalized 语料回流

`ingest-corpus` 是显式外部写操作，只接受带 `.finalized` 标记的当前运行时任务。它按版本化 JSON 合同进行有界异步批量发布，可从环境变量读取 bearer 认证，并写入幂等回执。新端点应先用 `--dry-run --payload-out <路径>` 检查；完整合同见 [`references/corpus_ingest.md`](references/corpus_ingest.md)。

## 评分

```text
K_per_category = Σ severity_points
L_per_category = weight × K
score = max((1 - ΣL / 固定词数) × 100, 0)
```

`state.scoring_policy` 是 calc、write、报告、迭代和聚合的默认策略，CLI 只做显式覆盖。策略包含阈值、评分卡、LISA/MQM 严重度、Critical gate 和重复去重；每次计分都会清除并重建 repeated。默认严重度点数为 Neutral 0、Minor 1、Major 5、Critical 10；默认阈值为 98。受保护段不计分。

## 多工作表

默认分开交付：每个工作表单独建立子任务，并分别保留各自标准的
`<子任务名>_lqe.xlsx` 报告和 corrected 文件。用户没有明确要求“合并报告”、
“跨工作表汇总”或“恢复原工作簿结构”时，到子任务交付为止，不生成父级聚合报告，
也不把多个子任务的 Results/Scorecard 复制进同一个工作簿。

只有用户明确要求聚合时才运行：

```bash
python3 "$SCRIPTS/aggregate_sheets.py" \
  --job <任务名> \
  --sheets <工作表一>,<工作表二>
```

显式聚合时，父任务保留工作表顺序、空行、公式、样式和合并单元格，只替换已校验/当前译文。聚合会按每个子任务的当前 state、`errors.contract.json` 和已验证 chunk generation 重新校验结果，并复用 chunk 术语上下文；隐藏 `_LQE_CONTRACT` 同时绑定 state/errors、可见 `LQE Results` 及逐错误 provenance 行结构，汇总报告复制各子任务 Results 与 Scorecard 历史。发布前会按稳定顺序重新取得全部子任务 lease。结果/报告缺失、损坏、过期或未绑定、chunk 证据过期、输入漂移都会失败且不替换原有父级产物。子任务默认继承各自 policy；除阈值外策略不一致时失败。显式 `--threshold` 只覆盖阈值；任一子任务 FAIL 则汇总 FAIL。

该聚合命令只用于表格工作簿；SDLXLIFF 目录属于一个多文件任务，不是多工作表任务。

## 验证

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 scripts/run_tests.py
```
