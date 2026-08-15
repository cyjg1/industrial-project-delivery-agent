import { Button, Card, DatePicker, Form, Input, InputNumber, Progress, Select, Space, Table, Tag, Tooltip, type TableColumnsType } from "antd";
import { useMemo, useState, type CSSProperties } from "react";
import { updateMilestonePlan, updateWorkItem } from "../api/client";
import { statusTone, workItemStatusLabel } from "../lib/ui";
import { BOARD_OPTIONS, PROFESSION_OPTIONS } from "../lib/progressTaxonomy";
import type { MilestoneWorkspaceRow, Workspace } from "../types";
import { DateField, PanelEmpty, toDayjs } from "./panelKit";
import { useAsyncAction } from "./useAsyncAction";

type MilestonePanelProps = {
  workspace: Workspace;
  onWorkspaceChange: (workspace: Workspace) => void;
};

export function MilestonePanel({ workspace, onWorkspaceChange }: MilestonePanelProps) {
  const plan = workspace.milestone_control.plan;
  const [draftPlan, setDraftPlan] = useState({
    name: plan.name,
    date_start: plan.date_start,
    date_end: plan.date_end,
    scenario_id: plan.scenario,
    chain_name: plan.chain,
    acceptance_criteria: plan.acceptance_criteria.join("\n"),
  });
  const [rowDrafts, setRowDrafts] = useState<Record<string, Partial<MilestoneWorkspaceRow>>>({});
  const rows = workspace.milestone_workspace.rows;
  const gantt = buildGantt(rows, plan.date_start, plan.date_end, workspace.milestone_control.time_progress.today);

  const savePlan = useAsyncAction(
    async () => {
      const next = await updateMilestonePlan({
        ...draftPlan,
        acceptance_criteria: draftPlan.acceptance_criteria.split(/\n+/).map((item) => item.trim()).filter(Boolean),
      });
      onWorkspaceChange(next);
    },
    { errorText: "里程碑保存失败", successText: "里程碑已保存" },
  );

  const saveTask = useAsyncAction(
    async (row: MilestoneWorkspaceRow) => {
      const draft = rowDrafts[row.row_id] || {};
      const progressWasEdited = Object.prototype.hasOwnProperty.call(draft, "progress_percent");
      const progressPercent = progressWasEdited ? draft.progress_percent ?? null : row.progress_percent;
      const next = await updateWorkItem(row.row_id, {
        title: draft.title ?? row.title,
        due_date: draft.due_date ?? row.due_date,
        deliverable: draft.deliverable ?? row.deliverable,
        acceptance_criteria: draft.acceptance_criteria ?? row.acceptance_criteria,
        professional_id: draft.professional_id ?? row.professional_id,
        board_id: draft.board_id ?? row.board_id,
        planned_start: draft.planned_start ?? row.planned_start,
        ...(progressWasEdited ? { progress_percent: progressPercent } : {}),
        owner_candidates: (draft.owner_text ?? row.owner_text).split(/[,，、]/).map((item) => item.trim()).filter(Boolean),
        notes: "从里程碑表格保存任务",
      });
      onWorkspaceChange(next);
    },
    { errorText: "任务保存失败", successText: "任务已保存", key: (row) => row.row_id },
  );

  function updateRow(row: MilestoneWorkspaceRow, patch: Partial<MilestoneWorkspaceRow>) {
    setRowDrafts((drafts) => ({ ...drafts, [row.row_id]: { ...drafts[row.row_id], ...patch } }));
  }

  const columns = useMemo<TableColumnsType<MilestoneWorkspaceRow>>(() => [
    {
      title: "任务",
      dataIndex: "title",
      width: 230,
      ellipsis: true,
      render: (value: string, row) => (
        <Input
          value={rowDrafts[row.row_id]?.title ?? value}
          onChange={(event) => updateRow(row, { title: event.target.value })}
        />
      ),
    },
    {
      title: "责任人",
      dataIndex: "owner_text",
      width: 150,
      ellipsis: true,
      render: (value: string, row) => (
        <Input
          value={rowDrafts[row.row_id]?.owner_text ?? value}
          onChange={(event) => updateRow(row, { owner_text: event.target.value })}
        />
      ),
    },
    {
      title: "专业 / 板块",
      width: 210,
      render: (_, row) => (
        <Space direction="vertical" size={4} className="milestone-classification">
          <Select
            placeholder="选择专业"
            allowClear
            options={PROFESSION_OPTIONS}
            value={(rowDrafts[row.row_id]?.professional_id ?? row.professional_id) || undefined}
            onChange={(value) => updateRow(row, { professional_id: value || "" })}
          />
          <Select
            placeholder="选择板块"
            allowClear
            options={BOARD_OPTIONS}
            value={(rowDrafts[row.row_id]?.board_id ?? row.board_id) || undefined}
            onChange={(value) => updateRow(row, { board_id: value || "" })}
          />
        </Space>
      ),
    },
    {
      title: "计划时间",
      width: 230,
      render: (_, row) => (
        <Space direction="vertical" size={4}>
          <DateField
            placeholder="开始日期"
            value={rowDrafts[row.row_id]?.planned_start ?? row.planned_start}
            onChange={(planned_start) => updateRow(row, { planned_start })}
          />
          <DateField
            placeholder="截止日期"
            value={rowDrafts[row.row_id]?.due_date ?? row.due_date}
            onChange={(due_date) => updateRow(row, { due_date })}
          />
        </Space>
      ),
    },
    {
      title: "进度",
      width: 125,
      render: (_, row) => {
        const draft = rowDrafts[row.row_id] || {};
        const value = Object.prototype.hasOwnProperty.call(draft, "progress_percent")
          ? draft.progress_percent
          : row.progress_percent;
        return (
          <InputNumber
            min={0}
            max={100}
            suffix="%"
            style={{ width: "100%" }}
            placeholder="待更新"
            value={value}
            onChange={(nextValue) => updateRow(row, { progress_percent: nextValue })}
          />
        );
      },
    },
    {
      title: "交付物",
      dataIndex: "deliverable",
      width: 230,
      ellipsis: true,
      render: (value: string, row) => (
        <Input
          value={rowDrafts[row.row_id]?.deliverable ?? value}
          onChange={(event) => updateRow(row, { deliverable: event.target.value })}
        />
      ),
    },
    {
      title: "操作",
      width: 105,
      render: (_, row) => (
        <Button
          disabled={!row.editable || row.row_type !== "work_item"}
          loading={saveTask.isPending(row.row_id)}
          onClick={() => void saveTask.run(row)}
        >
          保存任务
        </Button>
      ),
    },
  ], [rowDrafts, saveTask]);

  return (
    <section className="panel-stack" aria-label="里程碑面板">
      <Card size="small" title="里程碑计划">
        <Form layout="vertical" className="compact-form">
          <div className="form-grid">
            <Form.Item label="名称">
              <Input value={draftPlan.name} onChange={(event) => setDraftPlan({ ...draftPlan, name: event.target.value })} />
            </Form.Item>
            <Form.Item label="计划起止">
              <DatePicker.RangePicker
                style={{ width: "100%" }}
                allowEmpty={[true, true]}
                placeholder={["开始日期", "结束日期"]}
                value={[toDayjs(draftPlan.date_start), toDayjs(draftPlan.date_end)]}
                onChange={(_, texts) => setDraftPlan({ ...draftPlan, date_start: texts[0] || "", date_end: texts[1] || "" })}
              />
            </Form.Item>
            <Form.Item label="场景">
              <Input value={draftPlan.scenario_id} onChange={(event) => setDraftPlan({ ...draftPlan, scenario_id: event.target.value })} />
            </Form.Item>
            <Form.Item label="链路">
              <Input value={draftPlan.chain_name} onChange={(event) => setDraftPlan({ ...draftPlan, chain_name: event.target.value })} />
            </Form.Item>
          </div>
          <Form.Item label="交付物与验收口径">
            <Input.TextArea
              rows={3}
              value={draftPlan.acceptance_criteria}
              onChange={(event) => setDraftPlan({ ...draftPlan, acceptance_criteria: event.target.value })}
            />
          </Form.Item>
          <Button type="primary" loading={savePlan.pending} onClick={() => void savePlan.run()}>
            保存里程碑
          </Button>
        </Form>
      </Card>

      <Card size="small" title="甘特视图">
        {rows.length ? (
          <div className="gantt-shell">
            <div
              className="gantt-axis"
              style={{ gridTemplateColumns: `minmax(96px, 140px) repeat(${gantt.weeks.length}, minmax(0, 1fr))` }}
            >
              <span />
              {gantt.weeks.map((week) => <time key={week.key}>{week.label}</time>)}
            </div>
            {rows.slice(0, 12).map((row) => (
              <div key={row.row_id} className="gantt-row">
                <span>{row.title}</span>
                <div className="gantt-track">
                  <i className="today-line" style={{ left: `${gantt.todayPercent}%` }} />
                  {gantt.rowMap[row.row_id]?.pending ? (
                    <b className="gantt-pending">缺截止日期</b>
                  ) : (
                    <Tooltip title={ganttTooltip(row)}>
                      <b className="gantt-bar" style={gantt.rowMap[row.row_id]?.base}>
                        {gantt.rowMap[row.row_id]?.overdue ? (
                          <i className="gantt-overdue overdue" style={gantt.rowMap[row.row_id]?.overdue} />
                        ) : null}
                      </b>
                    </Tooltip>
                  )}
                </div>
                <Tag color={statusTone(row.status)}>{workItemStatusLabel(row.status)}</Tag>
              </div>
            ))}
          </div>
        ) : (
          <PanelEmpty
            title="还没有排进里程碑的任务"
            hint="先在上面填好里程碑的起止日期，再从「任务清单」确认任务并补上计划开始与截止日期，甘特图会自动画出条形和逾期区间。"
          />
        )}
      </Card>

      <Card size="small" title={workspace.daily_report_workspace.title}>
        <Space className="milestone-summary" wrap>
          <Tag>项目进行中 {workspace.daily_report_workspace.summary.project_active_work_count}</Tag>
          <Tag>我的范围 {workspace.daily_report_workspace.summary.role_active_work_count}</Tag>
          <Tag color={workspace.daily_report_workspace.summary.role_stale_report_count ? "warning" : "default"}>
            待补日报 {workspace.daily_report_workspace.summary.role_stale_report_count}
          </Tag>
          <Tag color={workspace.daily_report_workspace.summary.role_risk_count ? "error" : "default"}>
            风险事项 {workspace.daily_report_workspace.summary.role_risk_count}
          </Tag>
        </Space>
        <p className="role-workbench-focus">{workspace.daily_report_workspace.focus}</p>
        {workspace.daily_report_workspace.recommended_actions.length ? (
          <ul className="role-action-list">
            {workspace.daily_report_workspace.recommended_actions.map((action) => <li key={action}>{action}</li>)}
          </ul>
        ) : null}
        {workspace.daily_report_workspace.owner_rollup.length ? (
          <Table
            size="middle"
            rowKey="owner"
            pagination={false}
            dataSource={workspace.daily_report_workspace.owner_rollup.slice(0, 6)}
            columns={[
              { title: "人员", dataIndex: "owner" },
              { title: "进行中", dataIndex: "active_count" },
              { title: "待补日报", dataIndex: "stale_count" },
              { title: "风险", dataIndex: "risk_count" },
            ]}
          />
        ) : (
          <PanelEmpty
            compact
            title="还没有可汇总的人员工作量"
            hint="任务补上责任人之后，这里会按人汇总进行中、待补日报和风险数量。"
          />
        )}
      </Card>

      <Card size="small" title="里程碑表格">
        <Space className="milestone-summary" wrap>
          <Tag>时间进度 {workspace.milestone_control.time_progress.percent}%</Tag>
          <Tag>材料进度 {workspace.milestone_control.material_progress.raw_coverage_percent}%</Tag>
          <Tag>任务倒排 {workspace.milestone_control.todo_backschedule.total}</Tag>
          <Tag>调整建议 {workspace.milestone_control.adjustment_suggestions.length}</Tag>
        </Space>
        <Progress percent={workspace.milestone_control.time_progress.percent} showInfo={false} />
        <Table<MilestoneWorkspaceRow>
          className="milestone-table"
          size="middle"
          tableLayout="fixed"
          rowKey="row_id"
          pagination={{ pageSize: 8 }}
          scroll={{ x: 1340 }}
          dataSource={rows}
          columns={columns}
          locale={{
            emptyText: (
              <PanelEmpty
                title="里程碑下还没有任务"
                hint="在「三清单 · 任务清单」新增任务，或在对话中上传任务表；确认后的任务会挂到当前里程碑，在这里补专业、板块、计划时间和进度。"
              />
            ),
          }}
        />
      </Card>
    </section>
  );
}

function buildGantt(rows: MilestoneWorkspaceRow[], planStart: string, planEnd: string, todayText: string) {
  const rowMap: Record<string, { pending: boolean; base: CSSProperties; overdue?: CSSProperties }> = {};
  for (const row of rows) {
    rowMap[row.row_id] = ganttBarStyle(row, planStart, planEnd, todayText);
  }
  return {
    rowMap,
    weeks: weekTicks(planStart, planEnd),
    todayPercent: percentForDate(todayText, planStart, planEnd),
  };
}

function ganttBarStyle(row: MilestoneWorkspaceRow, planStart: string, planEnd: string, todayText: string) {
  const rangeStart = parseDate(planStart);
  const rangeEnd = parseDate(planEnd);
  const today = parseDate(todayText);
  if (row.row_type !== "milestone" && !row.due_date) {
    return { pending: true, base: { left: "0%", width: "0%" } };
  }
  const rowStart = parseDate(row.row_type === "milestone" ? planStart : row.planned_start || row.date_start || planStart) || rangeStart;
  const nominalEnd = parseDate(row.row_type === "milestone" ? planEnd : row.due_date || row.date_end || planEnd);
  const isOverdue = Boolean(
    row.row_type !== "milestone" &&
    nominalEnd &&
    today &&
    nominalEnd < today &&
    !["done", "canceled", "archived"].includes(row.status),
  );
  const rowEnd = (isOverdue ? today : nominalEnd) || rowStart || rangeEnd;
  if (!rangeStart || !rangeEnd || !rowStart || !rowEnd) return { pending: false, base: { left: "0%", width: "8px" } };
  const totalDays = Math.max(1, daysBetween(rangeStart, rangeEnd) + 1);
  const left = clamp((daysBetween(rangeStart, rowStart) / totalDays) * 100, 0, 100);
  const right = clamp(((daysBetween(rangeStart, rowEnd) + 1) / totalDays) * 100, left, 100);
  const base: CSSProperties = {
    left: `${left}%`,
    width: `${Math.max(4, right - left)}%`,
  };
  if (!isOverdue || !nominalEnd) return { pending: false, base };
  const duePercent = clamp(((daysBetween(rangeStart, nominalEnd) + 1) / totalDays) * 100, left, 100);
  const todayPercent = clamp(((daysBetween(rangeStart, today as Date) + 1) / totalDays) * 100, duePercent, 100);
  return {
    pending: false,
    base,
    overdue: {
      left: `${Math.max(0, duePercent - left)}%`,
      width: `${Math.max(2, todayPercent - duePercent)}%`,
    },
  };
}

function weekTicks(planStart: string, planEnd: string) {
  const start = parseDate(planStart);
  const end = parseDate(planEnd);
  if (!start || !end) return [{ key: "unknown", label: "时间待补" }];
  const ticks = [];
  const cursor = new Date(start);
  cursor.setDate(cursor.getDate() - cursor.getDay() + 1);
  while (cursor <= end || ticks.length === 0) {
    ticks.push({
      key: cursor.toISOString(),
      label: `${cursor.getMonth() + 1}/${cursor.getDate()}`,
    });
    cursor.setDate(cursor.getDate() + 7);
  }
  return ticks;
}

function percentForDate(value: string, planStart: string, planEnd: string) {
  const rangeStart = parseDate(planStart);
  const rangeEnd = parseDate(planEnd);
  const target = parseDate(value);
  if (!rangeStart || !rangeEnd || !target) return 0;
  const totalDays = Math.max(1, daysBetween(rangeStart, rangeEnd) + 1);
  return clamp(((daysBetween(rangeStart, target) + 1) / totalDays) * 100, 0, 100);
}

function ganttTooltip(row: MilestoneWorkspaceRow) {
  return [
    `责任人：${row.owner_text || "待确认"}`,
    `交付物：${row.deliverable || "待补"}`,
    `验收口径：${row.acceptance_criteria || "待补"}`,
    `状态：${workItemStatusLabel(row.status)}`,
  ].join("\n");
}

function parseDate(value: string) {
  const time = Date.parse(value);
  return Number.isFinite(time) ? new Date(time) : null;
}

function daysBetween(start: Date, end: Date) {
  return Math.floor((end.getTime() - start.getTime()) / 86400000);
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}
