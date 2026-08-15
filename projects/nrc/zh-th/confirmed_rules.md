# NRC 确认规则（TH 轨）— 评估前必读

共通（源文侧/跨语言/在线主表）确认规则见 `../common/confirmed_rules_common.md`（read 自动并入）；**本文件只放 TH 专有**。
效力顺序：CN-EN 更新 > Query > TH-SG。
> 只放规则/口径与「勿判」项；逐条术语译法进 `terms_th.json`（status 标记），不在此重复——每轮评估都注入，越短越好。

## 术语状态语义（terms_th.json 的 status）
- **New**（主库 TH 列）：待审核——偏离时记录 Terminology，但需结合语境
- **NeedReview**：待复核——保留为未确认候选，偏离时结合语境判断
- **空状态**：按未确认候选处理
- **合规审核通过**：当前项目导入口径为 `confirmed: true`、`protected: false`
- **Denied**：客户拒绝项，导入时直接排除，不进入术语检查
- **WorkingTB**（译员工作 TB）：项目流通译法——偏离时记录 Terminology Major
- **Approved**：PM/客户定稿，导入口径为 `confirmed: true`、`protected: false`，严格执行
- 精灵名一律 ≤12 字符（仅精灵名段；无 key 列时凭 TB 命中判断）

## TH SG 关键硬规则（评估时执行）
- 泰国皇家学会标准泰语；轻奇幻语气，禁古泰语（ข้าน้อย/สู/เอ็ง）与粗俗词（มึง/กู）
- 规避泰服宝可梦官方译名（玩法/精灵名/独有道具），但不强行区分（火系/水系可平译）
- 变量/标签原样；省略号=`...`；对话半角双引号；千分位英文逗号；数字+单位空格（紧凑 UI 可省）
- 道具名 = [核心词]+类型名定式，确认后全文统一
- 公制单位；禁自加粗体/斜体

## 受保护内容（0611 确认）
- profile `protected_term_statuses=[]`（TB 无明确保护标记，因此术语仍可结合语境检查和修改）；客户 memoQ 保护清单到货后写入 terms 并更新 protected_term_statuses。对照 `protected_terms_batch4.json`
- 0611 夏老师 Batch4：97 段专名标为受保护，仅表示本批不可修改；TB 未更新，仍记录 Terminology，并注明「0611 标为受保护，待客户澄清」，不直接修改。`?!`/`！？` 混合不算连续标点（checks.json）；复合术语最长匹配优先（lqe_io.py）

## PM 确认 2026-06-15（XLY；逐条术语定稿见 `terms_th.json` status=Approved，原件见对应 PM 文件）
- **命名规则**：专名**音译优先**、规避泰服宝可梦官方译名（星光→`สตาร์ไลท์`，含星光专名优先音译；星光狮/对决/回顾等旧意译待逐条补音译形）
- **考核对手**：按语境逐段判（NPC 名 `คู่ต่อสู้ประเมินผล` / 动作 `การประเมินคู่ต่อสู้`），并列出
- **海盔虫保留 `เจ้า`**（表「可爱生物」，去掉成「帽子」）——此前认为「多加前缀」属于误报，不再记录

## TB 更新 2026-06-30（Master TB 0630 导入，脚本 `mastertb_to_terms.py`）
- 源文件 `../common/ROCO_MasterTB_0630.xlsx`（表头改中文双语列名，与 0629 版 Subject/ZH-CN/Domain/Note 不同；脚本按列名定位，不受影响）。旧库快照 `terms_th_pre0630.bak.json`
- 162 条译法随新表更新，含修复 65 条历史遗留 `target="0"` 占位符。838 条主表待填充，3 条回填自旧库，其余尚无泰语译法（不入 TB）
- **同名多义**：伊贝儿/里奥/裘卡/黄蜂后/呱呱/克制 在「物种 Species」与「个体/NPC」两分类下译法被 0630 故意拆开（0629 两分类译法一致，无冲突）。terms_th.json 现已支持一个 source 挂多个 `senses`，两个语义都已收进 TB，不再只保留一个——评估时 AI 结合段落语境（个体/角色 vs 物种）判断该用哪个，都不匹配才报 Terminology
  - 全部多义分组清单见 `terms_th.multisense.json`（每次重新导入自动重新生成，供人工核对是否确实有意区分）

## TB 更新 2026-07-16（Master TB 0716 TH 导入）
- 源文件 `../common/ROCO_MasterTB_0716_TH.xlsx`；旧库快照 `terms_th_pre0716.bak.json`，旧多义清单快照 `terms_th_pre0716.multisense.bak.json`
- 导入 5,539 个来源、5,548 个义项；新增 1,236 个来源，移除主表已不再收录的 11 个来源，299 个共有来源的泰语译法发生变化
- 43 行 TH 为空且无可回填旧译，未写入术语库；状态分布：合规审核通过 4,301、New 1,239、Denied 4、Approved 4
- 多义来源共 9 个：伊贝儿、翠顶夫人、里奥、黄蜂后、帕帕斯卡、呱呱、古路尼、裘卡、克制；以 `terms_th.multisense.json` 为准
- 所有 `confirmed` / `protected` 均按技能合同保持显式 `false`；不根据 status 自动推断

## TB 更新 2026-07-28（Master TB 0728 TH 导入）
- 源文件 `../common/ROCO_MasterTB_0728_TH.xlsx`；旧库快照 `terms_th_pre0728.bak.json`，旧多义清单快照 `terms_th_pre0728.multisense.bak.json`
- 导入 5,695 个来源、5,709 个义项；新增 167 个来源，移除 7 个来源，87 个共有来源的泰语译法发生变化
- 44 行 TH 为空：1 个待填充缺口沿用旧库译法，2 个来源另有已填充行，41 个来源未写入术语库；Denied 6 行已排除
- 导入后状态分布：合规审核通过 4,229、New 1,412、NeedReview 64、Approved 4
- 多义来源共 14 个：翼龙、伊兰、伊贝儿、书卷守护、咔咔羽毛、翠顶夫人、里奥、黄蜂后、帕帕斯卡、呱呱、格斗小六、古路尼、裘卡、克制；以 `terms_th.multisense.json` 为准
- 状态映射沿用当前项目口径：Approved、合规审核通过为 `confirmed: true`；New、NeedReview、空状态为 `confirmed: false`；全部为 `protected: false`

## TB 更新 2026-08-11（Master TB 0807 MMQ 导入）
- 源文件 `../common/ROCO_MasterTB_0807_MMQ.xlsx`；旧库快照 `terms_th_pre0807.bak.json`，旧多义清单快照 `terms_th_pre0807.multisense.bak.json`
- 源表无状态列；用户于 2026-08-11 明确选择“整表确认”。所有 5,761 个非空 TH 义项均写为 `confirmed: true`、`protected: false`
- 导入 5,745 个来源、5,761 个义项；相较 0728 新增 50 个来源，无来源移除，171 个共有来源的泰语译法发生变化
- 378 行 TH 为空，本次未从旧库回填；非空译文中无 `target="0"` 占位符
- 多义来源共 16 个：翼龙、伊兰、伊贝儿、书卷守护、咔咔羽毛、翠顶夫人、里奥、黄蜂后、吞噬、帕帕斯卡、呱呱、格斗小六、古路尼、裘卡、洛克王国、克制；以 `terms_th.multisense.json` 为准
- 0728 曾标记 Denied 的水枝枝、海珊瑚、礼拜堂、礼堂、豆包鱼、豆豆鱼均在 0807 源表中重现；按本次“整表确认”决定视为重新批准，已纳入术语库

（范围口径「都列出」、severity 口径见 SKILL.md 标准流程，全语言通用，不在此重复）
