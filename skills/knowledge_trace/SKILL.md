---
name: knowledge-trace
description: Answer project questions from current authorized facts with evidence and decision history. Use when a user asks what the current conclusion is, why it was decided, who confirmed it, what it affects, or where the supporting source is.
---

# 项目知识检索与决策追溯

基于核心层提供的可见事实生成 `EvidenceBackedAnswer`。

## 工作流

1. 优先使用当前有效且已确认的事实。
2. 区分已确认结论、候选结论和无依据三种状态。
3. 返回来源定位、版本、关联对象和置信度。
4. 没有充分证据时明确回答不知道，不补造结论。

## 边界

- 不自行扩大检索范围或绕过权限过滤。
- 不把历史讨论当作当前有效结论。
- 不替代授权人的正式决策。
