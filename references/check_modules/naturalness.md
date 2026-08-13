# 自然度检查模块

公共协议见 `common.md`。本模块只处理 Audience appropriateness、Culture specific reference、Unidiomatic。必须审阅 packet 的全部 `reviewed_ids`；紧凑草稿只在 `findings` 中写有问题的 id。每条问题使用 `"needs_confirmation"` 和 `"edit"`，不得输出 corrected。

## 检查内容

- 口吻、正式程度、敬语、人称和角色声音是否符合项目背景与语言说明。
- 只使用 packet 中正式提供的 speaker/addressee/relationship/scene 与 resolved constraints；缺少受话人或关系时不能仅凭 speaker 猜敬语等级。
- 文化专有概念是否适合目标受众，是否造成冒犯、敏感风险或明显品牌混淆。
- 是否照搬源语语序、搭配生硬或缺少目标语言必要的虚词。

## 严重度

- 功能、规则、指令和界面文本中的时态、搭配或必要虚词问题通常为 Unidiomatic Minor。
- 纯宣传文案中仅为更地道的润色通常为 Neutral。
- 影响受众适配或品牌口吻的问题可为 Audience appropriateness Minor；严重文化风险按实际影响提高等级。

## 修改规则

只在改法唯一且可局部替换时给 `"needs_confirmation": false` 和 `"edit"`。涉及角色设定、品牌取向、文化选择或整句重写时，写 `"needs_confirmation": true`、`"edit": null`。

```json
[{"id": 12, "issues": [{"category": "Unidiomatic", "severity": "Minor", "comment": "The instruction uses an unnatural verb collocation.", "needs_confirmation": true, "edit": null}]}]
```
