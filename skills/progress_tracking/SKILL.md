---
name: progress-tracking
description: Infer task progress from authorized task updates, meetings, material changes, defects, work items, and engineering evidence. Use during periodic scans, reporting, or when work has not been updated; always return evidence and confidence for human correction.
---

# 进展自动采集与状态推断

输出 `ProgressEvidence`。推断必须附证据和置信度，并允许责任人确认或修正；不得用发言频率评价产出。

正式任务状态只使用 `open/in_progress/pending_acceptance/done/canceled`，展示为“待开始、进行中、待验收、已完成、已取消”。不得推断或保存 `blocked` 状态；存在阻碍时保持最符合事实的任务状态，并将阻碍记录到 `blockers`、风险或依赖对象中。

状态不做顺序流转限制。只有当前负责人可以修改任务状态；非负责人只能查看。进度总览按负责人、状态、专业、板块和来源批次映射同一批正式任务，不另造状态口径。
