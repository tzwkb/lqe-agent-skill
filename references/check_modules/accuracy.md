# 准确性检查模块

公共协议见 `common.md`。本模块只处理 Mistranslation、Omission、Addition、Untranslated。必须审阅 packet 的全部 `reviewed_ids`；紧凑草稿只在 `findings` 中写有问题的 id。每条问题使用 `"needs_confirmation"` 和 `"edit"`，不得输出 corrected。

## 逐段检查

- 占位符分别代表谁，施事、受事和容器关系是否颠倒。
- 机制动词、条件、范围、先后、因果和可逆状态是否准确。
- 每个数字、百分比、上限和次数是否完整且数值一致。
- “任意、仅、每、同时、最高、可、首次”等限定是否遗漏。
- 专属、限时、典藏等修饰语是否遗漏。
- 是否残留未翻译文本、拼音或误贴了其他条目的内容。

数值、机制、条件或整段含义错误通常为 Major；不要处理术语偏离、语法或口吻问题。

## 修改规则

- 先从 source 独立列出主体、动作、对象、否定、条件、情态和文本功能，再核对 target；不得把 target 当成 source 的替代文本或语义骨架。
- 遇到整句错位或 Major Accuracy 问题时，问题说明必须覆盖完整 source 命题；局部 edit 不足以恢复全部含义时必须标记需要完整重译。
- 只有能唯一定位、不会改变其他含义的局部替换才给 `"needs_confirmation": false` 和 `"edit"`。
- 需要重写句子、源文含义不清、多个改法均合理或涉及未确认名称时，写 `"needs_confirmation": true`、`"edit": null`。

```json
[{"id": 3, "issues": [{"category": "Omission", "severity": "Major", "comment": "The 50% trigger condition is missing.", "needs_confirmation": true, "edit": null}]}]
```
