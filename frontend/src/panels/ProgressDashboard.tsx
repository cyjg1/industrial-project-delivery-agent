import { Card, Progress, Segmented, Space, Tag, Tooltip } from "antd";
import { useState } from "react";
import type { ProgressHealth, ProgressSegment, Workspace } from "../types";
import { DailyWorkRecordsPanel } from "./DailyWorkRecords";
import { DeliverablesPanel } from "./Deliverables";
import { MilestonePanel } from "./Milestone";
import { PanelEmpty } from "./panelKit";
import "./ProgressDashboard.css";

type DashboardView = "profession" | "board" | "owner" | "status" | "source" | "plan" | "records" | "deliverables";

type ProgressDashboardProps = {
  workspace: Workspace;
  onWorkspaceChange: (workspace: Workspace) => void;
  showManagementViews: boolean;
};

const HEALTH_LABELS: Record<ProgressHealth, string> = {
  green: "正常",
  yellow: "关注",
  red: "风险",
  gray: "无数据",
};

const MANAGEMENT_VIEW_OPTIONS = [
  { label: "按专业", value: "profession" },
  { label: "按板块", value: "board" },
  { label: "按责任人", value: "owner" },
  { label: "按状态", value: "status" },
  { label: "按来源", value: "source" },
  { label: "计划明细", value: "plan" },
  { label: "工作日志", value: "records" },
  { label: "交付物", value: "deliverables" },
];

const MEMBER_VIEW_OPTIONS = [
  { label: "工作日志", value: "records" },
  { label: "交付物", value: "deliverables" },
];

type SegmentView = "profession" | "board" | "owner" | "status" | "source";

const EMPTY_HINTS: Record<SegmentView, { title: string; hint: string }> = {
  profession: {
    title: "还没有可按专业统计的任务",
    hint: "进度按任务的「专业」字段汇总。到「计划明细」的里程碑表格里给任务选上专业，这里就会出现每个专业的实际进度与计划进度对比。",
  },
  board: {
    title: "还没有可按板块统计的任务",
    hint: "进度按任务的「板块」字段汇总。到「计划明细」的里程碑表格里给任务选上板块，这里会同时展示板块下的专业进度和三清单进度。",
  },
  owner: {
    title: "任务还没有责任人",
    hint: "在「三清单 · 任务清单」或「任务」面板给任务指定责任人，这里会按人汇总进度、逾期和缺计划的数量。",
  },
  status: {
    title: "还没有可统计的任务状态",
    hint: "确认候选任务后，任务会带着状态进入统计；也可以先在「三清单 · 任务清单」新增一条任务。",
  },
  source: {
    title: "还没有来源批次",
    hint: "每次上传的会议纪要或任务表都会形成一个来源批次，用来追溯任务是从哪份材料里来的。先上传一份材料试试。",
  },
};

export function ProgressDashboardPanel({ workspace, onWorkspaceChange, showManagementViews }: ProgressDashboardProps) {
  const [view, setView] = useState<DashboardView>("profession");
  const dashboard = workspace.progress_dashboard;
  const effectiveView = showManagementViews || view === "records" || view === "deliverables"
    ? view
    : "records";

  if (effectiveView === "plan") {
    return (
      <section className="progress-dashboard-plan">
        <DashboardSwitcher value={effectiveView} onChange={setView} showManagementViews={showManagementViews} />
        <MilestonePanel workspace={workspace} onWorkspaceChange={onWorkspaceChange} />
      </section>
    );
  }

  if (effectiveView === "records") {
    return (
      <section className="progress-dashboard-plan">
        <DashboardSwitcher value={effectiveView} onChange={setView} showManagementViews={showManagementViews} />
        <DailyWorkRecordsPanel workspace={workspace} />
      </section>
    );
  }

  if (effectiveView === "deliverables") {
    return (
      <section className="progress-dashboard-plan">
        <DashboardSwitcher value={effectiveView} onChange={setView} showManagementViews={showManagementViews} />
        <DeliverablesPanel workspace={workspace} />
      </section>
    );
  }

  const segments = effectiveView === "profession"
    ? dashboard.professions
    : effectiveView === "board"
      ? dashboard.boards
      : dashboardSegments(dashboard, effectiveView);

  return (
    <section className="panel-stack progress-dashboard" aria-label="项目进度总览">
      <DashboardSwitcher value={effectiveView} onChange={setView} showManagementViews={showManagementViews} />
      <Card size="small" className="progress-dashboard-summary">
        <Space wrap>
          <Tag>任务 {dashboard.summary.task_count}</Tag>
          <Tag>专业 {dashboard.summary.profession_count}</Tag>
          <Tag>板块 {dashboard.summary.board_count}</Tag>
          <Tag>责任人 {dashboard.summary.owner_count}</Tag>
          <Tag>来源批次 {dashboard.summary.source_batch_count}</Tag>
          <Tag color={dashboard.summary.unclassified_profession_count ? "warning" : "default"}>
            待分专业 {dashboard.summary.unclassified_profession_count}
          </Tag>
          <Tag color={dashboard.summary.unclassified_board_count ? "warning" : "default"}>
            待分板块 {dashboard.summary.unclassified_board_count}
          </Tag>
          <Tag color={dashboard.summary.missing_progress_count ? "warning" : "default"}>
            进度待更新 {dashboard.summary.missing_progress_count}
          </Tag>
        </Space>
        <div className="progress-legend">
          {(Object.entries(dashboard.legend) as Array<[ProgressHealth, string]>).map(([health, label]) => (
            <span key={health}><i className={`health-dot health-${health}`} />{label}</span>
          ))}
        </div>
      </Card>

      {segments.length ? (
        <div className="progress-card-grid">
        {effectiveView === "profession"
          ? dashboard.professions.map((profession) => (
              <ProgressCard key={profession.id} segment={profession}>
                <h4>各板块进度</h4>
                <div className="progress-segment-list">
                  {profession.boards.length
                    ? profession.boards.map((board) => <SegmentRow key={board.id} segment={board} />)
                    : <p className="progress-empty">当前没有已映射任务</p>}
                </div>
                <TimeNodes nodes={profession.time_nodes} />
              </ProgressCard>
            ))
          : effectiveView === "board" ? dashboard.boards.map((board) => (
              <ProgressCard key={board.id} segment={board}>
                <h4>专业与三清单进度</h4>
                <div className="progress-segment-list">
                  {board.professions.length
                    ? board.professions.map((profession) => <SegmentRow key={profession.id} segment={profession} />)
                    : <p className="progress-empty">该板块下的任务还没有标注专业</p>}
                  <div className="progress-segment-row">
                    <span><i className={`health-dot health-${board.three_lists.health}`} />三清单</span>
                    <Progress percent={board.three_lists.progress} size="small" showInfo={false} />
                    <strong>{board.three_lists.progress}%</strong>
                  </div>
                  <small className="three-list-detail">
                    问题关闭 {board.three_lists.issue_progress}% · 任务完成 {board.three_lists.task_progress}% · 方法确认 {board.three_lists.method_progress}%
                  </small>
                  <small className="three-list-detail">
                    问题 {board.three_lists.issue_count} · 任务 {board.three_lists.task_count} · 方法 {board.three_lists.method_count} · 未关联问题 {board.three_lists.unlinked_issue_count}
                  </small>
                </div>
                <TimeNodes nodes={board.time_nodes} />
              </ProgressCard>
            ))
          : dashboardSegments(dashboard, effectiveView).map((segment) => (
              <ProgressCard key={segment.id} segment={segment}>{null}</ProgressCard>
            ))}
        </div>
      ) : (
        <Card size="small">
          <PanelEmpty title={EMPTY_HINTS[effectiveView].title} hint={EMPTY_HINTS[effectiveView].hint} />
        </Card>
      )}
    </section>
  );
}

function DashboardSwitcher({
  value,
  onChange,
  showManagementViews,
}: {
  value: DashboardView;
  onChange: (value: DashboardView) => void;
  showManagementViews: boolean;
}) {
  return (
    <Card size="small" className="progress-dashboard-switcher" title="项目进度总览">
      <Segmented
        block
        value={value}
        onChange={(next) => onChange(next as DashboardView)}
        options={showManagementViews ? MANAGEMENT_VIEW_OPTIONS : MEMBER_VIEW_OPTIONS}
      />
    </Card>
  );
}

function dashboardSegments(
  dashboard: Workspace["progress_dashboard"],
  view: Exclude<DashboardView, "profession" | "board" | "plan" | "records" | "deliverables">,
) {
  if (view === "owner") return dashboard.owners;
  if (view === "status") return dashboard.statuses;
  return dashboard.source_batches;
}

function ProgressCard({ segment, children }: { segment: ProgressSegment; children: React.ReactNode }) {
  return (
    <article className={`progress-overview-card health-border-${segment.health}`}>
      <header>
        <div>
          <span className={`health-badge health-${segment.health}`}>{HEALTH_LABELS[segment.health]}</span>
          <h3>{segment.name}</h3>
        </div>
        <strong>{segment.progress}%</strong>
      </header>
      <div className="progress-comparison">
        <span>实际 {segment.progress}%</span>
        <Progress percent={segment.progress} status={segment.health === "red" ? "exception" : "normal"} showInfo={false} />
        <span>计划 {segment.planned_progress}%</span>
      </div>
      <Space wrap size={[6, 6]}>
        <Tag>任务 {segment.task_count}</Tag>
        <Tag color={segment.overdue_count ? "error" : "default"}>逾期 {segment.overdue_count}</Tag>
        <Tag color={segment.missing_plan_count ? "warning" : "default"}>缺计划 {segment.missing_plan_count}</Tag>
        <Tag color={segment.missing_progress_count ? "warning" : "default"}>进度待更新 {segment.missing_progress_count}</Tag>
      </Space>
      {children}
      {segment.top_risks.length ? (
        <div className="progress-risk-list">
          <h4>优先风险</h4>
          {segment.top_risks.map((risk) => (
            <Tooltip key={risk.task_id} title={`${risk.owner_text} · ${risk.due_date || "截止时间待补"}`}>
              <p><i className={`health-dot health-${risk.health}`} />{risk.title}{risk.missing_progress ? " · 进度待更新" : ""}</p>
            </Tooltip>
          ))}
        </div>
      ) : null}
    </article>
  );
}

function SegmentRow({ segment }: { segment: ProgressSegment }) {
  return (
    <div className="progress-segment-row">
      <span><i className={`health-dot health-${segment.health}`} />{segment.name}</span>
      <Progress percent={segment.progress} size="small" showInfo={false} />
      <strong>{segment.progress}%</strong>
    </div>
  );
}

function TimeNodes({ nodes }: { nodes: Array<Omit<ProgressSegment, "id" | "name"> & { date: string }> }) {
  if (!nodes.length) return null;
  return (
    <div className="progress-time-nodes">
      <h4>时间节点</h4>
      <div>
        {nodes.slice(0, 5).map((node) => (
          <Tooltip key={node.date} title={`${node.task_count} 项任务，完成 ${node.progress}%`}>
            <span className={`time-node health-border-${node.health}`}>{node.date.slice(5)} · {node.progress}%</span>
          </Tooltip>
        ))}
      </div>
    </div>
  );
}
