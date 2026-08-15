from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RoleViewProfile:
    key: str
    label: str
    principle: str
    l1_focus: str
    l2_objects: str
    l3_evidence: str
    l4_actions: str
    prompt_rules: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "principle": self.principle,
            "layers": {
                "l1_focus": self.l1_focus,
                "l2_objects": self.l2_objects,
                "l3_evidence": self.l3_evidence,
                "l4_actions": self.l4_actions,
            },
            "prompt_rules": list(self.prompt_rules),
        }


ROLE_VIEW_PROFILES: dict[str, RoleViewProfile] = {
    "pmo": RoleViewProfile(
        key="pmo",
        label="项目经理 / PMO / 总体组",
        principle="项目全景、交付优先。聚焦进度、人力与资源调配、关键节点、关键路径、跨专题依赖、项目风险、待决策与向上汇报。",
        l1_focus="当前最需要项目管理角色关注的进度偏差、人员负荷、资源冲突、关键节点、关键路径、跨专题风险和待决策事项。",
        l2_objects="里程碑、专题、关键任务、风险、决策、交付物、责任人、人员负荷、资源安排、依赖链和升级项。",
        l3_evidence="来源材料、会议日期、历史结论、版本变化、影响链路、置信度和待核实点。",
        l4_actions="指定责任人、发起补全、升级决策、调整节点、生成汇报或催办材料。",
        prompt_rules=(
            "优先输出项目全景、进度与人力资源例外和跨专题影响，不展开与当前问题无关的执行明细。",
            "把无人认领、超期、责任冲突、关键路径风险和需决策事项前置。",
            "建议动作必须说明影响对象、建议责任人、截止时间或下一步确认方式。",
        ),
    ),
    "professional_lead": RoleViewProfile(
        key="professional_lead",
        label="专业统筹负责人",
        principle="专业域级、质量优先。聚焦标准口径、跨专题一致性、评审把关、专业风险、变更影响与意见闭环。",
        l1_focus="本专业口径变化、专业风险、跨专题不一致、需评审或复核的事项。",
        l2_objects="专业标准、方案、评审、交付物、专业问题、变更影响、复核人和涉及专题。",
        l3_evidence="专业依据、适用范围、版本差异、冲突证据、历史决策和材料引用位置。",
        l4_actions="确认专业口径、发起评审、指定复核人、要求整改、标记禁止自行判断项。",
        prompt_rules=(
            "优先判断标准口径、一致性、质量门和评审闭环，而不是项目经营汇总。",
            "遇到专业结论不明确时，明确列出待确认口径、确认人和可继续推进的临时边界。",
            "变更和风险要说明会影响哪些方案、接口、数据、测试、交付物或专题。",
        ),
    ),
    "topic_lead": RoleViewProfile(
        key="topic_lead",
        label="专题负责人",
        principle="专题级、协同优先。聚焦本专题任务、责任边界、输入输出、内外部依赖、待确认事项与需升级问题。",
        l1_focus="本专题当前任务、输入输出缺口、外部依赖、责任边界、需本人介入或升级的事项。",
        l2_objects="本专题任务树、成员分工、会议、交付物、外部输入、对外输出、问题和截止时间。",
        l3_evidence="任务来源、关联会议、上下游专题、有效版本、历史承接事项和原文依据。",
        l4_actions="认领或转派任务、协调输入、确认边界、补充负责人、向 PMO 或专业统筹升级。",
        prompt_rules=(
            "优先回答本专题能否推进、谁等我、我等谁、哪些边界需要确认。",
            "输出任务时必须包含主责、协同、输入、输出、截止时间和阻塞点。",
            "需要外部专题或上级介入时，给出升级对象和要带过去的证据。",
        ),
    ),
    "exec": RoleViewProfile(
        key="exec",
        label="实施人员",
        principle="个人任务级、行动优先。聚焦具体任务、背景依据、输入输出、关系人、完成标准、变更提醒与阻塞上报。",
        l1_focus="我现在要做什么、优先级、截止时间、卡点、变更提醒和需要找谁确认。",
        l2_objects="我的任务、步骤、输入材料、输出物、关系人、完成标准、预计工作量和依赖。",
        l3_evidence="任务来源片段、最新确认结论、材料版本、历史变化、模板示例和引用位置。",
        l4_actions="确认接收、提出澄清、反馈阻塞、生成初稿、引用材料、提交状态更新。",
        prompt_rules=(
            "优先给出可执行下一步，避免项目管理层面的泛化汇总。",
            "每个任务都要说明输入、输出、完成标准、关系人和卡住时找谁。",
            "不能确定的要求要标记待确认，并说明确认前哪些工作可以继续。",
        ),
    ),
    "viewer": RoleViewProfile(
        key="viewer",
        label="只读观察者",
        principle="只读、最小可见。仅展示当前权限允许查看的摘要。",
        l1_focus="当前可见范围内的摘要和待核实事项。",
        l2_objects="可见任务、材料和状态。",
        l3_evidence="可见来源和引用位置。",
        l4_actions="提出问题或请求授权人员确认。",
        prompt_rules=("严格限制在当前可见数据内回答。",),
    ),
}


def role_view_key(role: str) -> str:
    if role in {"pm", "pmo"}:
        return "pmo"
    if role == "professional_lead":
        return "professional_lead"
    if role == "topic_lead":
        return "topic_lead"
    if role == "exec":
        return "exec"
    return "viewer"


def role_view_profile(role: str) -> RoleViewProfile:
    return ROLE_VIEW_PROFILES[role_view_key(role)]


def role_view_prompt(role: str) -> str:
    profile = role_view_profile(role)
    lines = [
        f"当前角色视角：{profile.label}。",
        f"角色原则：{profile.principle}",
        f"L1 关注摘要层：{profile.l1_focus}",
        f"L2 对象清单层：{profile.l2_objects}",
        f"L3 详情证据层：{profile.l3_evidence}",
        f"L4 行动交互层：{profile.l4_actions}",
        "回答规则：",
    ]
    lines.extend(f"- {rule}" for rule in profile.prompt_rules)
    lines.append("所有结论必须区分已确认事实、模型推断、建议和待确认；引用事实时保留来源、版本或证据位置。")
    return "\n".join(lines)
