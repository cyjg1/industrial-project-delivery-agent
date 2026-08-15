---
name: project-knowledge
description: Build a traceable project fact graph from authorized project materials. Use for project initialization, new or updated materials, entity and relationship extraction, version tracking, and identifying facts that still require human confirmation.
---

# 项目知识归集与关联建模

把已授权材料整理为统一的 `ProjectGraph`，保留对象、关系、来源、版本、状态和置信度。

## 工作流

1. 只处理核心层已经完成权限过滤的输入。
2. 区分 `candidate`、`confirmed`、`rejected` 和 `archived`。
3. 为事实和关系保留来源定位；缺少证据时输出警告，不升级为确认事实。
4. 输出候选和确认需求，由核心层决定是否写入 SQLite。

## 边界

- 不直接修改项目数据库。
- 不决定材料可见范围。
- 不把模型推断标记为已确认事实。
