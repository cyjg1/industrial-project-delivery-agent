---
name: meeting-minutes
description: Turn an authorized raw meeting transcript into formal minutes and candidate actions, terminology corrections, and reusable methods. Use after a meeting, when a transcript or minutes file is uploaded, or when revised meeting conclusions must be processed for human confirmation.
---

# 会议理解与行动化

## 定位

`meeting_minutes` 是 C02 的项目内可独立运行技能，不包含前端和后端页面。它负责把原始会议转写转成正式纪要，并从结果中沉淀三类辅助资产：

- 人名、公司名、系统名、专业术语的 ASR 纠错候选。
- 可复用的方法论候选，例如 UAT 倒逼闭环、字段来源-去向-样例验证、场景-脚本-责任人-证据绑定。
- 每次运行的 prompt 审计摘要和本地运行日志。

## 边界

- 该 skill 不直接写 `data/store/candidate_items.json`，也不把会议结论升级为 confirmed。
- 该 skill 的运行数据保存在 `data/skills/meeting_minutes`，默认不提交到 GitHub。
- 主项目巡检 Agent 仍负责读取会议纪要、人员资产、项目记忆，并通过证据门禁产出候选卡片。

## 运行方式

```bash
python3 -m skills.meeting_minutes.cli \
  --transcript /path/to/transcript.txt \
  --doc-nature 专题讨论会 \
  --meeting-date 2026-06-23 \
  --project-background "项目人员：负责人A 是 S3 主导人员。" \
  --output /tmp/meeting_minutes.md \
  --print-audit
```

默认读取主项目 `.env` 中的真实模型配置。`LLM_PROVIDER=openai_compatible`、`OPENAI_COMPAT_MODEL=glm-5.2`、`OPENAI_COMPAT_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1` 时走统一的 OpenAI 兼容 Chat Completions 配置；未配置真实模型时直接报错，不生成替代结果。
