# `projects/` 文件地图

项目按 `<game>/<source>-<target>/` 保存，游戏级共享资料放在 `<game>/common/`。使用 `read --project <game>/<source>-<target>` 加载；路径从 skill 根目录解析，不依赖当前工作目录。目标语言通用事实放在 `../target_languages/<code>/`。

## 语言轨结构

| 文件 | 用途 | 读取方 |
|---|---|---|
| `profile.json` | 语言、背景、完整评分策略及各资源路径 | `lqe_io.py read --project` |
| `checks.json` | 机器预检开关和项目自定义规则 | `lqe_io.py pre-check` |
| `confirmed_rules.md` | 客户或用户已经确认的项目规则 | Agent，检查前必读 |
| `terms_*.json` | 术语数据，可显式带 `confirmed` 和 `protected` | 标准模式引用或按需转换；无术语模式不读取 |
| `sg*.md` / `sg*.txt` | 风格指南文本 | 初始化时复制到任务目录 |
| `sources/` | 原始风格指南、术语表及其工作副本 | 项目维护与溯源 |
| `inputs/` | 待检查的原始交付文件 | 人工传给 `read --input` |

建议的 `profile.json`：

```json
{
  "name": "game/zh-en",
  "language_pair": "zh-en",
  "source_lang": "zh",
  "target_lang": "en",
  "background": "项目背景",
  "style_guide": "sg.md",
  "terminology": "terms.json",
  "checks": "checks.json",
  "confirmed_rules": "confirmed_rules.md",
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

`language_pair`、`source_lang`、`target_lang` 必填。相对路径以当前语言轨目录为基准。设置合并顺序为：内置默认 < 目标语言属性 < 项目 `checks.json` < 命令行参数。顶层 `threshold` 仅兼容旧 profile；新 profile 使用完整 `scoring_policy`。

`sdlxliff.tm_protection` 可取 `candidate-only` 或 `protect-exact-source-and-target`；CLI `--protect-exact-tm` 优先。`content_type_rules` 按顺序匹配大小写敏感的相对路径 glob，第一个命中生效。`exclude_rules` 必须有唯一 `id` 和非空 `reason`，可选 glob，并在 `relative_path`、`file_original`、`confirmation`、`origin`、`locked`、`source`、`target` 上使用且只使用一个 `equals` 或 `regex`。不得用不稳定的 job 段号，也不得根据 CC、FF、文件名或目录名推断内容类型。

## SDLXLIFF/XLIFF 输入

单个 SDLXLIFF 1.2 文件或目录可直接初始化：

```bash
python3 scripts/lqe_io.py read \
  --project <game>/<source>-<target> \
  --input <SDLXLIFF 文件或目录> --input-format sdlxliff \
  --out jobs/<任务名>/state.json
```

`--input-format` 可取 `auto`、`tabular`、`sdlxliff`、`xliff`。SDLXLIFF 1.2 使用 `sdlxliff`，XLIFF 2.0 使用 `xliff`；`.sdlxliff`、`.xliff`、`.xlf` 单文件和纯 XML 目录可自动识别。一个任务不得混用两个 XML 版本；表格目录不支持。

运行时支持带 SDL namespace 的 XLIFF 1.2 与 XLIFF 2.0。XLIFF 2.0 省略根节点 `trgLang` 时，必须由项目 profile 或 `--target-lang` 明确补齐。未知厂商扩展若不影响句段定位和文本边界，会保留并写入 state/manifest；SDL `mid` 或 XLIFF 2.0 unit/segment 定位出现歧义时失败，不猜测。

locked 段始终以 `SOURCE_LOCKED` 保护。默认 `candidate-only` 只把同时满足 `origin=tm`、`percent=100`、`text-match=SourceAndTarget` 的段写入 `tm_candidates.json`；候选只有在执行 `protect-segments` 后才成为当次任务的保护决定。profile 的 `protect-exact-source-and-target` 或 CLI `--protect-exact-tm` 是显式严格自动保护决定；单独的 100% 值不够。

## 运行时检查模式

profile 可以配置术语，但任务明确不检查术语时，在初始化命令加入 `--no-terminology`：

```bash
python3 scripts/lqe_io.py read \
  --project <game>/<source>-<target> \
  --input <输入文件> --source-col <原文列> --target-col <译文列> \
  --no-terminology --out jobs/<任务名>/state.json
```

该实时参数高于 profile 术语配置，且不能与显式 `--terminology <file>` 同时使用。解析结果写入 `state.check_scope` 和任务根目录的 `scope.json`。标准模式要求 `terminology`、`accuracy`、`grammar`、`naturalness`；无术语模式要求 `precheck_review`、`accuracy`、`grammar`、`naturalness`。无术语模式只关闭术语、专名和术语审计；不会关闭文件内一致性、Markup、数字等检查。

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

## 术语字段

```json
{"source":"源词","target":"译法","confirmed":false,"protected":false}
```

- `confirmed: true`：译法已经确认；匹配证据唯一时允许安全的局部修改。
- `protected: true`：不可修改。
- 新 job 的每个术语/候选必须显式带布尔 `confirmed` 和 `protected`；任一字段缺失都失败，不能根据状态值推断。
- 已具备两个显式布尔字段的 canonical CSV/XLSX/JSON 可保留 `status` 作为审计元数据，无需 `term_status_map`。缺少任一布尔字段时，状态值必须由 `term_status_map` 映射。
- `protected_term_statuses` 只补充保护，不构成确认决策；如提供，必须是元素均为非空字符串的数组。`Denied` 始终排除且不得映射。

## 检查结果接口

检查模块只提交 `{id, issues:[{category,severity,comment,needs_confirmation,edit}]}`。`edit` 只用于安全、唯一、局部的替换；需要新译名、存在多个合理方案或必须重写时，使用 `needs_confirmation: true` 和 `edit: null`。完整建议译文由脚本在校验后生成。

## 规则优先级

检查时按以下顺序执行：实时要求 > `confirmed_rules.md` > 风格指南 > 通用检查方法。新确认的规则应写入当前语言轨的 `confirmed_rules.md`，不要写进通用语言事实。

## 运行产物

任务产物写入 `../jobs/<任务名>/`，不写回项目资料：

```text
state.json
scope.json
source_manifest.json        # SDLXLIFF 任务
tm_candidates.json          # SDLXLIFF 任务
confirmed_rules.md
errors_precheck.json
errors.json
chunks/
<任务名>_lqe.xlsx
<任务名>_corrected.<csv|tsv|xlsx>
```

初始化在 staging 中完成校验，拒绝输入/输出/资源别名，并以 `state.json` 最后发布；失败不留下正式半套产物。原始输入保持不变。`<任务名>_lqe.xlsx` 用于检查和评分记录。corrected 输出按输入格式区分：

- CSV/TSV 输入输出 `<任务名>_corrected.csv` 或 `<任务名>_corrected.tsv`，保持原行列和输入扩展名。
- XLSX 输入输出 `<任务名>_corrected.xlsx`，保持工作簿、工作表、空行、列顺序和格式。
- SDLXLIFF/XLIFF 输出固定 5 列的 `<任务名>_corrected.xlsx` 配套表，并生成 corrected XML：单文件为 `<任务名>_corrected.<sdlxliff|xliff|xlf>`，目录为 `<任务名>_corrected_xliff/`。

经验证的内部结果中，`corrected: ""` 是合法的整段删除；只有 `corrected: null` 表示没有建议修改。write、apply、export 和多工作表聚合都必须保留这一区别。

SDLXLIFF 的 `source_manifest.json` 记录输入文件 SHA-256、语言、未知扩展 namespace、规则命中、纳入/排除原因和 locked/TM 证据；`tm_candidates.json` 单独记录严格候选。其 `LQE Results` 固定为来源文件、TU ID、SDL Segment ID、原文、原译、建议译文、处理方式、LQE Segment ID、LQE 错误序号、LQE AI 复核状态、LQE AI 编辑状态、LQE 检查来源、错误详情、Protected、Protection Evidence、LQE_Iter；同段多错误连续且每错误一行，`LQE_Iter` 固定在最后一列。新建的 corrected Excel 固定为来源文件、TU ID、SDL Segment ID、原文、译文。

XML 回写只生成新的 corrected 文件或目录，原始 XML 保持不变。写出前会重验源文件集合与 SHA-256；源漂移、格式错误或事务发布失败时，不留下正式 corrected 产物。

运行时默认继承 `state.scoring_policy`。仅 PASS 创建 `.finalized`；FAIL+single 不改当前译文、不完成；FAIL+iterate 只有至少应用一处重新验证通过的安全局部 edit 时，才更新 `current_target`/iteration、设置 `pending_recheck=true` 并返回 `PENDING-RECHECK`。零修改时写出本轮报告，并通过 `export --errors` 写出已验证的错误覆盖，返回 `REVIEW-REQUIRED`，清除 `.iteration_pending`，不推进 iteration。旧 iteration/target/scope/预检/术语指纹的 chunks 不可复用。

多工作表聚合按每个子任务的当前 state、`errors.contract.json` 和已验证 chunk generation 重新校验结果，并复用 chunk 术语命中上下文；隐藏 `_LQE_CONTRACT` 同时绑定 state/errors 与可见 `LQE Results` 内容。发布前会重新取得全部子任务 lease。结果/报告缺失、损坏、过期或未绑定、chunk 证据过期、输入文件漂移都会在父级产物发布前失败，原有父级产物保持不变。
