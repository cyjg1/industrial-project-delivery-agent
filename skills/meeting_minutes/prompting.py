from __future__ import annotations

from dataclasses import dataclass

from skills.meeting_minutes.schemas import PromptBundle
from skills.meeting_minutes.store import MeetingMinutesSkillStore


SYSTEM_PROMPT = """你是面向大型工业项目的高级会议纪要助手。

你的任务不是把转写简单改写成文字，而是把会议转写整理成可被项目管理 Agent 继续使用的材料：
1. 提炼领导管理意图、决策、认知纠偏和后续工作调整。
2. 识别人名、公司名、系统名、字段名和专业术语的 ASR 识别错误，并按项目背景修正。
3. 归纳会议中稳定出现的方法论候选，例如管理原则、UAT 验收方法、数据治理方法或责任闭环方法。
4. 输出可执行待办，尽量包含责任人、截止时间、交付物和验收口径。

输出要求：
- 默认输出正式中文会议纪要，不写新闻稿，不写流水账。
- 管理意图和认知纠偏必须说明“为什么这样要求、后面怎么改”。
- 对不确定的人名、责任人、日期和结论标记“待确认”，不要编造确认态。
- 如果识别到可沉淀的方法论候选，请在正文后单独增加“方法论候选”小节，供后续结构化抽取。
- 技术对象、系统名和字段名保持原专业表达。
"""


TEMPLATE = """【第一部分：文稿性质】
{doc_nature}
【/第一部分】

【第二部分：项目固定背景】
{project_background}
【/第二部分】

【第三部分：上次会议延续信息】
{continuation_info}
【/第三部分】

【第四部分：本次会议转写】
{transcript}
【/第四部分】

【第五部分：参考风格样本】
{style_sample}
【/第五部分】

【第六部分：输出格式要求】
{output_format}
【/第六部分】

请根据以上六部分信息，生成本次会议纪要。"""


MAP_SYSTEM_PROMPT = """你是会议转写分块信息抽取器。

你只处理给定字符区间，不生成整篇会议纪要。完整保留本段中有证据的：
1. 决策与最终结论；
2. 行动项、责任人、期限、交付物和验收口径；
3. 风险、冲突与未决问题；
4. 可复用的方法、原则和业主要求；
5. 疑似人名、术语或字段名 ASR 错误及原句。

不得补全本段没有出现的人名、数字、日期或结论。没有某类内容就省略该类。输出紧凑中文摘要，并保留字符区间标签。"""


REDUCE_SYSTEM_PROMPT = f"""{SYSTEM_PROMPT}

你现在是会议纪要归并器。输入是按原始顺序排列、覆盖完整转写的分块摘要。
只依据这些摘要归并连续事项，合并重复表达，保留后出现的明确结论并把冲突标为待确认。
不得把分块摘要外的信息写成事实。"""


@dataclass(frozen=True)
class MeetingMinutesPromptInput:
    transcript: str
    doc_nature: str = "会议纪要"
    continuation_info: str = "（无）"
    output_format: str = "输出正式会议纪要、决策清单、待办事项和方法论候选。"
    project_background: str = "（暂无项目固定背景）"
    style_sample: str = "（未提供参考样本）"


@dataclass(frozen=True)
class TranscriptMapPromptInput:
    chunk_text: str
    chunk_index: int
    chunk_count: int
    start_char: int
    end_char: int


@dataclass(frozen=True)
class MeetingMinutesReducePromptInput:
    summaries: list[str]
    doc_nature: str = "会议纪要"
    continuation_info: str = "（无）"
    output_format: str = "输出正式会议纪要、决策清单、待办事项和方法论候选。"
    project_background: str = "（暂无项目固定背景）"
    style_sample: str = "（未提供参考样本）"


def build_meeting_minutes_prompt(
    prompt_input: MeetingMinutesPromptInput,
    *,
    store: MeetingMinutesSkillStore,
) -> PromptBundle:
    corrections = store.render_corrections_for_prompt()
    system_prompt = SYSTEM_PROMPT if not corrections else f"{SYSTEM_PROMPT}\n\n{corrections}"
    user_message = TEMPLATE.format(
        doc_nature=_fallback(prompt_input.doc_nature, "会议纪要"),
        project_background=_fallback(prompt_input.project_background, "（暂无项目固定背景）"),
        continuation_info=_fallback(prompt_input.continuation_info, "（无）"),
        transcript=prompt_input.transcript,
        style_sample=_fallback(prompt_input.style_sample, "（未提供参考样本）"),
        output_format=_fallback(prompt_input.output_format, "输出正式会议纪要、决策清单、待办事项和方法论候选。"),
    )
    return PromptBundle(system_prompt=system_prompt, user_message=user_message)


def build_transcript_map_prompt(prompt_input: TranscriptMapPromptInput) -> PromptBundle:
    user_message = (
        f"【分块 {prompt_input.chunk_index + 1}/{prompt_input.chunk_count}】\n"
        f"【原文字符区间 {prompt_input.start_char}:{prompt_input.end_char}】\n"
        f"{prompt_input.chunk_text}\n"
        "【/分块】"
    )
    return PromptBundle(system_prompt=MAP_SYSTEM_PROMPT, user_message=user_message)


def build_meeting_minutes_reduce_prompt(
    prompt_input: MeetingMinutesReducePromptInput,
    *,
    store: MeetingMinutesSkillStore,
) -> PromptBundle:
    corrections = store.render_corrections_for_prompt()
    system_prompt = REDUCE_SYSTEM_PROMPT if not corrections else f"{REDUCE_SYSTEM_PROMPT}\n\n{corrections}"
    summaries = "\n\n".join(
        f"【分块摘要 {index + 1}/{len(prompt_input.summaries)}】\n{summary}\n【/分块摘要】"
        for index, summary in enumerate(prompt_input.summaries)
    )
    user_message = (
        f"【文稿性质】\n{_fallback(prompt_input.doc_nature, '会议纪要')}\n【/文稿性质】\n\n"
        f"【项目固定背景】\n{_fallback(prompt_input.project_background, '（暂无项目固定背景）')}\n【/项目固定背景】\n\n"
        f"【上次会议延续信息】\n{_fallback(prompt_input.continuation_info, '（无）')}\n【/上次会议延续信息】\n\n"
        f"【按原文顺序的分块摘要】\n{summaries}\n【/按原文顺序的分块摘要】\n\n"
        f"【参考风格样本】\n{_fallback(prompt_input.style_sample, '（未提供参考样本）')}\n【/参考风格样本】\n\n"
        f"【输出格式要求】\n{_fallback(prompt_input.output_format, '输出正式会议纪要、决策清单、待办事项和方法论候选。')}\n【/输出格式要求】"
    )
    return PromptBundle(system_prompt=system_prompt, user_message=user_message)


def _fallback(value: str, default: str) -> str:
    return value if value and value.strip() else default
