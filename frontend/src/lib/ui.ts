import type { ConversationModelRound, ConversationToolStep, Workspace } from "../types";

export type ActiveView =
  | "chat"
  | "progress"
  | "calendar"
  | "threeLists"
  | "tasks"
  | "people"
  | "review"
  | "knowledge"
  | "files";

export type ChatMessage = {
  id: string;
  role: "agent" | "user";
  content: string;
  createdAt?: string;
  provider?: string;
  model?: string;
  contextBundle?: string[];
  modelInput?: string;
  modelOutput?: string;
  contextCharCount?: number;
  contextTokenCount?: number;
  contextBudget?: number;
  contextBudgetUnit?: string;
  contextTokenCounter?: string;
  contextRetrievalCount?: number;
  contextDegraded?: boolean;
  toolSteps?: ConversationToolStep[];
  modelRounds?: ConversationModelRound[];
  verification?: {
    checked: boolean;
    unsupported: string[];
    invalid_fact_ids: string[];
    cited_fact_ids: string[];
    citation_coverage: {
      claim_count: number;
      cited_claim_count: number;
      ratio: number;
    };
  };
  stopReason?: string;
};

export function openingMessage(workspace: Workspace): ChatMessage {
  const plan = workspace.milestone_control.plan;
  if (workspace.access.view_mode === "pmo" && workspace.daily_brief.content_markdown) {
    return {
      id: `opening-${workspace.daily_brief.brief_id || workspace.run_id || "local"}`,
      role: "agent",
      createdAt: workspace.daily_brief.generated_at || new Date().toISOString(),
      content: workspace.daily_brief.content_markdown,
    };
  }
  const report = workspace.daily_report_workspace;
  const priorityItems = report.items_requiring_update.slice(0, 5).map(
    (item) => `- ${item.title}（${item.owner_text}，${item.due_date || "截止时间待补"}）`,
  );
  const riskItems = report.risk_items.slice(0, 5).map(
    (item) => `- ${item.title}（${item.owner_text}${item.overdue ? "，已逾期" : ""}）`,
  );
  const actionItems = report.recommended_actions.map((action) => `- ${action}`);
  return {
    id: `opening-${workspace.run_id || "local"}`,
    role: "agent",
    createdAt: new Date().toISOString(),
    content: [
      `# ${report.title}`,
      `**当前角色** — ${workspace.access.role_profile?.label || workspace.access.role}`,
      `**当前项目** — ${plan.project || workspace.milestone.name} / ${plan.name} / ${formatDateRange(plan.date_start, plan.date_end)}。`,
      `**今天的关注重点** — ${report.focus}`,
      `**当前范围** — 进行中 ${report.summary.role_active_work_count} 项，待更新 ${report.summary.role_stale_report_count} 项，风险 ${report.summary.role_risk_count} 项。`,
      "## 今天优先处理",
      ...(actionItems.length ? actionItems : ["- 当前没有需要处理的角色事项。"]),
      "## 需要更新的事项",
      ...(priorityItems.length ? priorityItems : ["- 当前没有匹配到需要更新的事项。"]),
      "## 风险事项",
      ...(riskItems.length ? riskItems : ["- 当前角色范围内没有匹配到风险事项。"]),
    ].join("\n"),
  };
}

export function runStatusLabel(value: string) {
  const labels: Record<string, string> = {
    idle: "未运行",
    completed: "已完成",
    verification_failed: "验证失败",
    failed: "失败",
    pending: "等待中",
    running: "运行中",
    sdk_managed: "SDK 管理",
    legacy: "旧版记录",
    initialized: "已初始化",
  };
  return labels[value] || value || "未知";
}

export function workItemStatusLabel(value: string) {
  const labels: Record<string, string> = {
    todo: "待开始",
    doing: "进行中",
    done: "已完成",
    candidate: "待确认",
    confirmed: "已确认",
    rejected: "已驳回",
    archived: "已归档",
    open: "待开始",
    in_progress: "进行中",
    pending_acceptance: "待验收",
    canceled: "已取消",
  };
  return labels[value] || value || "未记录";
}

export function statusTone(value: string): "default" | "processing" | "success" | "warning" | "error" {
  if (["completed", "done", "confirmed"].includes(value)) return "success";
  if (["running", "doing", "in_progress", "sdk_managed"].includes(value)) return "processing";
  if (["verification_failed", "failed", "rejected"].includes(value)) return "error";
  if (["candidate", "pending", "pending_acceptance", "idle"].includes(value)) return "warning";
  return "default";
}

export function splitText(value: string | string[] | null | undefined): string[] {
  if (Array.isArray(value)) return value.filter(Boolean);
  return (value || "")
    .split(/[,，;；、\n]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export function ownerText(value: string | string[] | null | undefined) {
  const owners = splitText(value);
  return owners.length ? owners.join("、") : "责任人待确认";
}

export function todayBrief(workspace: Workspace) {
  const reviewCount = workspace.confirmation_cards.length;
  const todo = workspace.milestone_control.todo_backschedule;
  const suggestions = workspace.milestone_control.adjustment_suggestions;
  const people = workspace.people_workspace.summary;
  const issueCount = workspace.three_lists.summary.unlinked_issue_count;
  return [
    `待确认 ${reviewCount} 条，候选记忆 ${workspace.memory.candidate_count} 条。`,
    `今日任务 ${todo.total} 项，逾期 ${todo.overdue} 项，缺截止日期 ${todo.missing_due_date} 项。`,
    `未关联任务的问题 ${issueCount} 个；人员资产 ${people.people_count} 人，正在做 ${people.active_work_total} 项。`,
    suggestions[0]?.message ? `里程碑建议：${suggestions[0].message}` : "暂无新的里程碑调整建议。",
  ];
}

export function formatDateRange(start: string, end: string) {
  if (!start && !end) return "时间待补";
  if (start === end) return start;
  return `${start || "开始待补"} 至 ${end || "结束待补"}`;
}

export function shortText(value: string, max = 80) {
  if (!value) return "";
  return value.length > max ? `${value.slice(0, max)}...` : value;
}

export function taskTitle(value: string | null | undefined) {
  const title = String(value || "").trim();
  return title
    .replace(/^(?:20)?\d{2}[-./年]?\d{2}[-./月]?\d{2}日?\s*[-—_:：|]?\s*/, "")
    .trim() || title;
}

export function isMeetingTitle(value: string | null | undefined) {
  const title = taskTitle(value).replace(/[（(][^）)]*[）)]\s*$/, "").trim();
  return /(?:会议|例会|站会|沟通会|专题会|评审会|协调会|复盘会|宣贯会|启动会|碰头会)$/.test(title);
}
