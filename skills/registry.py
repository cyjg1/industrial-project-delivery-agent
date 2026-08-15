from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent.access_policy import ROLE_RANK
from skills.contracts import ConfirmationPolicy, SkillContext, SkillResult, SkillSpec
from skills.change_impact.runner import ChangeImpactSkillRunner
from skills.capability_assets.runner import CapabilityAssetsSkillRunner
from skills.decision_management.runner import DecisionManagementSkillRunner
from skills.dependency_analysis.runner import DependencyAnalysisSkillRunner
from skills.knowledge_trace.runner import KnowledgeTraceSkillRunner
from skills.gate_assessment.runner import GateAssessmentSkillRunner
from skills.meeting_preparation.runner import MeetingPreparationSkillRunner
from skills.meeting_minutes.llm import (
    MeetingMinutesModelClient,
    MeetingMinutesModelConfig,
    create_meeting_minutes_client,
    load_meeting_minutes_model_config,
)
from skills.meeting_minutes.runner import MeetingMinutesSkillRunner
from skills.meeting_minutes.store import DEFAULT_MEETING_MINUTES_STORE_DIR, MeetingMinutesSkillStore
from skills.project_knowledge.runner import ProjectKnowledgeSkillRunner
from skills.progress_tracking.runner import ProgressTrackingSkillRunner
from skills.quality_check.runner import QualityCheckSkillRunner
from skills.risk_analysis.runner import RiskAnalysisSkillRunner
from skills.role_reporting.runner import RoleReportingSkillRunner
from skills.review_support.runner import ReviewSupportSkillRunner
from skills.resource_commercial.runner import ResourceCommercialSkillRunner
from skills.task_context.runner import TaskContextSkillRunner
from skills.task_planning.runner import TaskPlanningSkillRunner


ResultAdapter = Callable[[dict[str, Any]], SkillResult]


@dataclass
class ProjectSkill:
    name: str
    description: str
    spec: SkillSpec
    runner: Any
    model_config: MeetingMinutesModelConfig | None = None
    context_aware: bool = True
    result_adapter: ResultAdapter | None = None

    def run(self, payload: dict[str, Any]) -> dict[str, Any] | SkillResult:
        """Compatibility entry point for existing callers."""
        if self.context_aware:
            raise RuntimeError("run() 仅保留给 meeting_minutes 兼容调用；其他 Skill 请使用 execute() 并传入权限上下文")
        return self.runner.run(payload)

    def execute(self, payload: dict[str, Any], context: SkillContext | None = None) -> SkillResult:
        if self.spec.runtime_status != "active":
            raise RuntimeError(
                f"{self.spec.capability_id} 当前为 contract_only，仅定义契约和原型 runner，不可进入运行时执行"
            )
        self._enforce_role(context)
        raw = self.runner.run(payload, context=context) if self.context_aware else self.runner.run(payload)
        if isinstance(raw, SkillResult):
            return raw
        if self.result_adapter is not None:
            return self.result_adapter(dict(raw))
        return SkillResult(
            capability_id=self.spec.capability_id,
            skill_name=self.name,
            artifact_type=self.spec.output_type,
            artifact=dict(raw),
            requires_confirmation=self.spec.confirmation_policy != ConfirmationPolicy.NONE,
        )

    def _enforce_role(self, context: SkillContext | None) -> None:
        required_rank = ROLE_RANK.get(self.spec.required_role, -1)
        if context is None or context.actor is None or context.access_context is None:
            raise PermissionError(f"{self.spec.capability_id} 需要带 actor 和 access_context 的 SkillContext")
        role = context.access_context.role_of(context.actor, context.project_id)
        if ROLE_RANK.get(role, -1) < required_rank:
            raise PermissionError(f"当前角色无权执行 {self.spec.capability_id}")


PROJECT_KNOWLEDGE_SPEC = SkillSpec(
    capability_id="C01",
    name="project_knowledge",
    description="归集项目对象、关系、来源和版本，形成统一且可追溯的项目事实图。",
    when_to_use="项目初始化、新材料接入、材料版本更新或需要重建项目事实关系时。",
    output_type="ProjectGraph",
    required_inputs=("project_id",),
    optional_inputs=("facts", "objects", "relationships", "source_ids"),
    dependencies=(),
    tool_names=("SourceManifestTool", "ExtractionTool", "EvidenceVerifierTool"),
    required_role="exec",
    confirmation_policy=ConfirmationPolicy.CANDIDATE,
    system_hint="区分事实与推断，保留来源、版本、状态和置信度；重要事实必须进入人工确认。",
)


MEETING_MINUTES_SPEC = SkillSpec(
    capability_id="C02",
    name="meeting_minutes",
    description="把会议转写加工为纪要、行动候选、术语纠错候选和方法候选。",
    when_to_use="会议结束、转写完成、纪要上传或会议结论修订时。",
    output_type="MeetingEvent",
    optional_inputs=(
        "transcript_text",
        "transcript_path",
        "meeting_id",
        "meeting_date",
        "project_background",
        "continuation_info",
    ),
    dependencies=("C01",),
    tool_names=("ingest_file", "ExtractionTool", "propose_candidates"),
    required_role="exec",
    confirmation_policy=ConfirmationPolicy.CANDIDATE,
    system_hint="会议纪要要抽取议题、结论、任务、问题、风险、决策、变更和责任关系；不明确内容标记待确认并引用原文。",
    runtime_status="active",
)


KNOWLEDGE_TRACE_SPEC = SkillSpec(
    capability_id="C15",
    name="knowledge_trace",
    description="基于当前有效项目事实回答问题，并返回来源、版本和确认状态。",
    when_to_use="询问当前结论、决策依据、确认人、影响范围或历史变化时。",
    output_type="EvidenceBackedAnswer",
    required_inputs=("question",),
    optional_inputs=("ranked_facts",),
    dependencies=("C01",),
    tool_names=("search_memory",),
    required_role="exec",
    confirmation_policy=ConfirmationPolicy.NONE,
    system_hint="优先当前有效且已确认的事实；无依据时明确不知道，不能把历史讨论或候选结论冒充当前事实。",
)


TASK_PLANNING_SPEC = SkillSpec(
    capability_id="C04",
    name="task_planning",
    description="把目标、里程碑和会议纪要拆解为实施人员可直接执行的叶子任务及项目日历事项；过滤已完成项，把重复和一次性会议本体写入日历，把管理层汇总事项下钻为各板块或专业的具体交付，并统一责任、依赖、状态和来源覆盖口径。",
    when_to_use="项目启动、阶段计划制定、会议产生新任务、会议安排或范围变更，尤其是需要把会议纪要区分为任务清单和项目日历时。",
    output_type="TaskGraph",
    required_inputs=("project_id", "goal"),
    optional_inputs=("tasks", "meeting_markdown", "source_text", "source_id", "people_structure", "fallback_owner", "critical_path_task_ids"),
    dependencies=("C01", "C15"),
    tool_names=("get_milestone_status", "get_person", "propose_candidates"),
    required_role="topic_lead",
    confirmation_policy=ConfirmationPolicy.HUMAN_CONFIRM,
    system_hint="必须先按原文证据区分任务与会议，再改写标题：每日/每周例会及一次性专题会、评审会、协调会、对碰会等会议本体均生成日历事项，不进入任务；只有原文明示的会前准备或会后落实交付动作才单独建任务，不得因会议携带已有材料而臆造准备任务。任务必须继续拆到实施人员无需二次分工即可直接执行；管理者汇总、审核或提交事项若依赖多个板块输入，必须先生成板块或专业子任务。来源逐项核数，正式分工必须确认。",
)


TASK_CONTEXT_SPEC = SkillSpec(
    capability_id="C05",
    name="task_context",
    description="围绕具体任务组装最新结论、材料、关系人、输入输出、模板、风险和完成标准。",
    when_to_use="任务创建、分派、接手、恢复或上下文更新时。",
    output_type="TaskContextPackage",
    required_inputs=("task",),
    dependencies=("C01", "C04", "C09", "C15"),
    tool_names=("search_memory", "get_tasks", "get_person", "search_methods"),
    required_role="exec",
    confirmation_policy=ConfirmationPolicy.CANDIDATE,
    system_hint="明确我要做什么、为什么做、输入输出、关系人、完成标准和缺失信息；不明确内容必须待确认。",
)


DECISION_MANAGEMENT_SPEC = SkillSpec(
    capability_id="C09",
    name="decision_management",
    description="把未决事项整理为包含决策人、选项、依据、期限和影响范围的决策对象。",
    when_to_use="会议或评审出现未决事项、责任冲突、任务缺少口径或重大风险需要决策时。",
    output_type="DecisionItem",
    required_inputs=("question",),
    optional_inputs=("decision_id", "decision_level", "decision_makers", "participants", "options", "due_date", "affected_object_ids", "evidence_refs"),
    dependencies=("C01", "C08", "C15"),
    tool_names=("search_memory", "get_person", "propose_candidates"),
    required_role="exec",
    confirmation_policy=ConfirmationPolicy.HUMAN_CONFIRM,
    system_hint="区分建议、推断和已确认结论；Agent 不替代授权人决策，缺少决策人、期限或依据时明确待确认。",
)

PROGRESS_TRACKING_SPEC = SkillSpec("C06", "progress_tracking", "从多类证据推断任务进展并生成可确认状态。", "定时扫描、会议结束、材料更新或汇报前。", "ProgressEvidence", ("task_id",), dependencies=("C01", "C04"), tool_names=("get_tasks", "search_memory", "propose_candidates"), confirmation_policy=ConfirmationPolicy.HUMAN_CONFIRM, system_hint="进展推断必须显示证据与置信度并允许责任人修正；不使用阻塞状态，阻碍单独记录为 blocker、风险或依赖。")
DEPENDENCY_ANALYSIS_SPEC = SkillSpec("C08", "dependency_analysis", "识别跨任务、专题和交付物的供需依赖与协同断点。", "任务规划、材料更新、任务阻塞或跨专题会议前。", "DependencyGraph", ("project_id",), dependencies=("C01", "C04"), tool_names=("get_tasks", "search_memory", "get_person"), confirmation_policy=ConfirmationPolicy.CANDIDATE, system_hint="明确谁等谁、输入输出、节点、状态和责任，冲突必须待确认。")
ROLE_REPORTING_SPEC = SkillSpec("C14", "role_reporting", "按受众角色和周期生成可追溯项目汇报。", "日报周报、例会、领导或业主询问、阶段汇报时。", "ReportPackage", ("audience_role",), dependencies=("C01", "C06", "C07", "C09"), tool_names=("get_tasks", "get_milestone_status", "search_memory"), confirmation_policy=ConfirmationPolicy.CANDIDATE, system_hint="按角色输出不同粒度，事实可追溯，对外口径需负责人确认。")
RISK_ANALYSIS_SPEC = SkillSpec("C07", "risk_analysis", "从进度、依赖、变更、质量和决策中识别有证据的项目风险。", "定时分析、关键节点临近、状态异常或评审发现问题时。", "RiskObject", ("title",), dependencies=("C04", "C06", "C08", "C09", "C10", "C12"), tool_names=("get_tasks", "get_milestone_status", "search_memory", "propose_candidates"), confirmation_policy=ConfirmationPolicy.HUMAN_CONFIRM, system_hint="说明概率、影响、责任、措施和升级条件；重大风险必须确认。")
CHANGE_IMPACT_SPEC = SkillSpec("C10", "change_impact", "识别相对基线的变化并传播分析受影响对象。", "新结论、新要求、版本替换或范围变化时。", "ChangeEvent", ("change", "baseline"), dependencies=("C01", "C04", "C08", "C09", "C15"), tool_names=("search_memory", "get_tasks", "propose_candidates"), confirmation_policy=ConfirmationPolicy.HUMAN_CONFIRM, system_hint="区分变化类型，列出影响链；确认前不得自动修改任务。")
QUALITY_CHECK_SPEC = SkillSpec("C12", "quality_check", "检查交付物完整性、正确性、一致性、版本和可追溯性。", "交付物上传更新、评审前或验收前。", "QualityFindings", ("deliverable_id",), dependencies=("C01", "C15"), tool_names=("search_memory", "draft", "propose_candidates"), confirmation_policy=ConfirmationPolicy.CANDIDATE, system_hint="区分确定性问题和语义推断，语义问题必须证据和专业确认。")
MEETING_PREPARATION_SPEC = SkillSpec("C03", "meeting_preparation", "围绕待解决问题推荐参会人、决策人、材料和会前任务。", "创建周会、专题会、评审会或问题升级拉会时。", "MeetingPlan", ("objective",), dependencies=("C01", "C08", "C09", "C15"), tool_names=("search_memory", "get_person", "get_tasks", "draft"), confirmation_policy=ConfirmationPolicy.HUMAN_CONFIRM, system_hint="参会和决策角色只推荐，信息包继承权限，不扩大可见范围。")
REVIEW_SUPPORT_SPEC = SkillSpec("C11", "review_support", "准备评审材料并把专业意见转为可跟踪整改项。", "需求、方案、接口、数据、测试或上线评审前后。", "ReviewPackage", ("review_object_id",), dependencies=("C01", "C09", "C10", "C12", "C15"), tool_names=("search_memory", "draft", "propose_candidates"), confirmation_policy=ConfirmationPolicy.HUMAN_CONFIRM, system_hint="不替代正式评审；整改关闭必须经指定复核人确认。")
GATE_ASSESSMENT_SPEC = SkillSpec("C13", "gate_assessment", "依据阶段门槛评估进入下一阶段或上线的条件。", "需求冻结、开发开工、联调、测试、上线或验收前。", "GateAssessment", ("gate_name",), dependencies=("C06", "C07", "C09", "C11", "C12"), tool_names=("get_milestone_status", "get_tasks", "search_memory", "draft"), confirmation_policy=ConfirmationPolicy.HUMAN_CONFIRM, system_hint="明确证据缺失和例外，最终准入由授权人或评审组织决定。")
RESOURCE_COMMERCIAL_SPEC = SkillSpec("C16", "resource_commercial", "分析资源负荷、技能瓶颈、成本趋势和验收收款节点。", "资源计划、阶段复盘、人员冲突或经营节点临近时。", "ResourceCommercialModel", ("project_id",), dependencies=("C04", "C06", "C12", "C13"), tool_names=("get_tasks", "get_person", "get_milestone_status", "search_memory"), required_role="pmo", confirmation_policy=ConfirmationPolicy.HUMAN_CONFIRM, system_hint="只基于可核验任务、产出和负荷，保护个人与财务权限。")
CAPABILITY_ASSETS_SPEC = SkillSpec("C17", "capability_assets", "识别可复用能力、模板、规则和方法并划分复用边界。", "功能方案盘点、复盘、验收、产品化分析或新项目启动时。", "CapabilityAsset", ("capability_name",), dependencies=("C01", "C12", "C15"), tool_names=("search_memory", "search_methods", "draft", "propose_candidates"), required_role="professional_lead", confirmation_policy=ConfirmationPolicy.HUMAN_CONFIRM, system_hint="基于跨专题项目证据区分通用、配置、映射、适配和定制，分类须确认。")


_SPECS = {
    spec.name: spec
    for spec in (
        PROJECT_KNOWLEDGE_SPEC,
        MEETING_MINUTES_SPEC,
        MEETING_PREPARATION_SPEC,
        TASK_PLANNING_SPEC,
        TASK_CONTEXT_SPEC,
        PROGRESS_TRACKING_SPEC,
        RISK_ANALYSIS_SPEC,
        DEPENDENCY_ANALYSIS_SPEC,
        DECISION_MANAGEMENT_SPEC,
        CHANGE_IMPACT_SPEC,
        REVIEW_SUPPORT_SPEC,
        QUALITY_CHECK_SPEC,
        GATE_ASSESSMENT_SPEC,
        ROLE_REPORTING_SPEC,
        KNOWLEDGE_TRACE_SPEC,
        RESOURCE_COMMERCIAL_SPEC,
        CAPABILITY_ASSETS_SPEC,
    )
}

_ALIASES = {
    "c01": "project_knowledge",
    "project-knowledge": "project_knowledge",
    "c02": "meeting_minutes",
    "meeting-minutes": "meeting_minutes",
    "c03": "meeting_preparation",
    "meeting-preparation": "meeting_preparation",
    "c04": "task_planning",
    "task-planning": "task_planning",
    "c05": "task_context",
    "task-context": "task_context",
    "c06": "progress_tracking",
    "progress-tracking": "progress_tracking",
    "c07": "risk_analysis",
    "risk-analysis": "risk_analysis",
    "c08": "dependency_analysis",
    "dependency-analysis": "dependency_analysis",
    "c09": "decision_management",
    "decision-management": "decision_management",
    "c10": "change_impact",
    "change-impact": "change_impact",
    "c11": "review_support",
    "review-support": "review_support",
    "c12": "quality_check",
    "quality-check": "quality_check",
    "c13": "gate_assessment",
    "gate-assessment": "gate_assessment",
    "c14": "role_reporting",
    "role-reporting": "role_reporting",
    "c15": "knowledge_trace",
    "knowledge-trace": "knowledge_trace",
    "c16": "resource_commercial",
    "resource-commercial": "resource_commercial",
    "c17": "capability_assets",
    "capability-assets": "capability_assets",
}


def list_project_skills() -> list[str]:
    return list(_SPECS)


def list_project_skill_specs() -> list[SkillSpec]:
    return list(_SPECS.values())


def get_project_skill_spec(name: str) -> SkillSpec:
    return _SPECS[_canonical_name(name)]


def get_project_skill(
    name: str,
    *,
    store_dir: str | Path | None = None,
    model_client: MeetingMinutesModelClient | None = None,
) -> ProjectSkill:
    canonical = _canonical_name(name)
    if canonical == "project_knowledge":
        return ProjectSkill(
            name=canonical,
            description=PROJECT_KNOWLEDGE_SPEC.description,
            spec=PROJECT_KNOWLEDGE_SPEC,
            runner=ProjectKnowledgeSkillRunner(),
        )
    if canonical == "knowledge_trace":
        return ProjectSkill(
            name=canonical,
            description=KNOWLEDGE_TRACE_SPEC.description,
            spec=KNOWLEDGE_TRACE_SPEC,
            runner=KnowledgeTraceSkillRunner(),
        )
    if canonical == "task_planning":
        return ProjectSkill(
            name=canonical,
            description=TASK_PLANNING_SPEC.description,
            spec=TASK_PLANNING_SPEC,
            runner=TaskPlanningSkillRunner(),
        )
    if canonical == "task_context":
        return ProjectSkill(
            name=canonical,
            description=TASK_CONTEXT_SPEC.description,
            spec=TASK_CONTEXT_SPEC,
            runner=TaskContextSkillRunner(),
        )
    if canonical == "decision_management":
        return ProjectSkill(
            name=canonical,
            description=DECISION_MANAGEMENT_SPEC.description,
            spec=DECISION_MANAGEMENT_SPEC,
            runner=DecisionManagementSkillRunner(),
        )
    if canonical == "progress_tracking":
        return ProjectSkill(canonical, PROGRESS_TRACKING_SPEC.description, PROGRESS_TRACKING_SPEC, ProgressTrackingSkillRunner())
    if canonical == "dependency_analysis":
        return ProjectSkill(canonical, DEPENDENCY_ANALYSIS_SPEC.description, DEPENDENCY_ANALYSIS_SPEC, DependencyAnalysisSkillRunner())
    if canonical == "role_reporting":
        return ProjectSkill(canonical, ROLE_REPORTING_SPEC.description, ROLE_REPORTING_SPEC, RoleReportingSkillRunner())
    if canonical == "risk_analysis":
        return ProjectSkill(canonical, RISK_ANALYSIS_SPEC.description, RISK_ANALYSIS_SPEC, RiskAnalysisSkillRunner())
    if canonical == "change_impact":
        return ProjectSkill(canonical, CHANGE_IMPACT_SPEC.description, CHANGE_IMPACT_SPEC, ChangeImpactSkillRunner())
    if canonical == "quality_check":
        return ProjectSkill(canonical, QUALITY_CHECK_SPEC.description, QUALITY_CHECK_SPEC, QualityCheckSkillRunner())
    if canonical == "meeting_preparation":
        return ProjectSkill(canonical, MEETING_PREPARATION_SPEC.description, MEETING_PREPARATION_SPEC, MeetingPreparationSkillRunner())
    if canonical == "review_support":
        return ProjectSkill(canonical, REVIEW_SUPPORT_SPEC.description, REVIEW_SUPPORT_SPEC, ReviewSupportSkillRunner())
    if canonical == "gate_assessment":
        return ProjectSkill(canonical, GATE_ASSESSMENT_SPEC.description, GATE_ASSESSMENT_SPEC, GateAssessmentSkillRunner())
    if canonical == "resource_commercial":
        return ProjectSkill(canonical, RESOURCE_COMMERCIAL_SPEC.description, RESOURCE_COMMERCIAL_SPEC, ResourceCommercialSkillRunner())
    if canonical == "capability_assets":
        return ProjectSkill(canonical, CAPABILITY_ASSETS_SPEC.description, CAPABILITY_ASSETS_SPEC, CapabilityAssetsSkillRunner())

    config = load_meeting_minutes_model_config()
    store = MeetingMinutesSkillStore(Path(store_dir) if store_dir else DEFAULT_MEETING_MINUTES_STORE_DIR)
    client = model_client or create_meeting_minutes_client(config)
    return ProjectSkill(
        name="meeting_minutes",
        description="会议转写转纪要 skill：沉淀人名/术语纠错候选、议题和方法论候选。",
        spec=MEETING_MINUTES_SPEC,
        runner=MeetingMinutesSkillRunner(store=store, model_client=client),
        model_config=config,
        context_aware=False,
        result_adapter=_meeting_minutes_result,
    )


def _canonical_name(name: str) -> str:
    normalized = name.strip().lower()
    normalized = _ALIASES.get(normalized, normalized.replace("-", "_"))
    if normalized not in _SPECS:
        raise KeyError(f"未知项目 skill：{name}")
    return normalized


def _meeting_minutes_result(raw: dict[str, Any]) -> SkillResult:
    correction_candidates = list(raw.get("asr_correction_candidates") or [])
    methodology_candidates = list(raw.get("methodology_candidates") or [])
    candidates = [
        {"candidate_type": "asr_correction", **item}
        for item in correction_candidates
    ] + [
        {"candidate_type": "methodology", **item}
        for item in methodology_candidates
    ]
    evidence_refs = []
    for item in methodology_candidates:
        for reference in item.get("evidence_refs") or []:
            evidence_refs.append({"source_id": str(raw.get("meeting_id") or ""), "locator": str(reference)})
    artifact = {
        "meeting_id": raw.get("meeting_id"),
        "meeting_date": raw.get("meeting_date"),
        "minutes_markdown": raw.get("minutes_markdown", ""),
        "model_used": raw.get("model_used", ""),
        "tokens_estimated": raw.get("tokens_estimated", 0),
        "correction_candidates": correction_candidates,
        "methodology_candidates": methodology_candidates,
    }
    return SkillResult(
        capability_id="C02",
        skill_name="meeting_minutes",
        artifact_type="MeetingEvent",
        artifact=artifact,
        candidates=candidates,
        evidence_refs=evidence_refs,
        requires_confirmation=bool(candidates),
        audit=dict(raw.get("prompt_audit") or {}),
    )
