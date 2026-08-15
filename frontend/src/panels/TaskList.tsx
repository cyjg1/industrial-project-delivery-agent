import { App as AntdApp, Button, Card, Input, Modal, Progress, Segmented, Select, Space, Table, Tag, Tooltip } from "antd";
import { useEffect, useMemo, useState } from "react";
import { updateThreeListTask } from "../api/client";
import { isMeetingTitle, ownerText, shortText, splitText, statusTone, taskTitle, workItemStatusLabel } from "../lib/ui";
import type { LinkedTaskRow, SwitchableUser, Workspace } from "../types";
import { PanelEmpty } from "./panelKit";

type TaskListPanelProps = {
  workspace: Workspace;
  switchableUsers: SwitchableUser[];
  onWorkspaceChange: (workspace: Workspace) => void;
};

const activeStatuses = new Set(["open", "in_progress", "pending_acceptance"]);
const keyRoles = new Set(["topic_lead", "professional_lead", "pmo", "pm"]);
const lifecycleStatusValues = ["candidate", "open", "in_progress", "pending_acceptance", "done", "canceled"];
const taskStatusOptions = lifecycleStatusValues.map((value) => ({
  value,
  label: workItemStatusLabel(value),
}));
const statusOptions = [
  { value: "all", label: "全部状态" },
  { value: "active", label: "未完成" },
  ...taskStatusOptions,
];

export function TaskListPanel({ workspace, switchableUsers, onWorkspaceChange }: TaskListPanelProps) {
  const { message } = AntdApp.useApp();
  const canDelegate = keyRoles.has(workspace.access.role);
  const actorName = workspace.access.actor.name;
  const [scope, setScope] = useState<"mine" | "team">(canDelegate ? "team" : "mine");
  const [status, setStatus] = useState("all");
  const [query, setQuery] = useState("");
  const [detailTask, setDetailTask] = useState<LinkedTaskRow | null>(null);
  const [transferTask, setTransferTask] = useState<LinkedTaskRow | null>(null);
  const [nextOwners, setNextOwners] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [statusSaving, setStatusSaving] = useState("");

  const allRows = useMemo(
    () => workspace.three_lists.tasks.filter((row) => !isMeetingTitle(row.task_description)),
    [workspace.three_lists.tasks],
  );
  const assigneeOptions = useMemo(
    () => Array.from(
      new Map(
        switchableUsers
          .filter((user) => user.role !== "viewer")
          .map((user) => [
            user.name,
            {
              value: user.name,
              label: `${user.name} · ${roleLabel(user.role)}${user.topics.length ? ` · ${user.topics.map((topic) => topic.name).join("、")}` : ""}`,
            },
          ]),
      ).values(),
    ),
    [switchableUsers],
  );
  const myRows = useMemo(
    () => allRows.filter((row) => splitText(row.owner_text).includes(actorName)),
    [actorName, allRows],
  );
  const rows = useMemo(() => {
    const base = scope === "mine" ? myRows : allRows;
    const normalizedQuery = query.trim().toLowerCase();
    return base.filter((row) => {
      const statusMatched = status === "all"
        || (status === "active" ? activeStatuses.has(row.task_status) : row.task_status === status);
      const textMatched = !normalizedQuery || [
        row.task_description,
        row.task_detail,
        row.owner_text,
        row.deliverable,
        row.acceptance_criteria,
      ].join(" ").toLowerCase().includes(normalizedQuery);
      return statusMatched && textMatched;
    });
  }, [allRows, myRows, query, scope, status]);

  const scopedRows = scope === "mine" ? myRows : allRows;
  const unfinishedCount = scopedRows.filter((row) => activeStatuses.has(row.task_status)).length;
  const overdueCount = scopedRows.filter(isOverdue).length;
  const unownedCount = scopedRows.filter((row) => splitText(row.owner_text).length === 0).length;

  useEffect(() => {
    setScope(canDelegate ? "team" : "mine");
  }, [actorName, canDelegate]);

  function resetFilters() {
    setScope(canDelegate ? "team" : "mine");
    setStatus("all");
    setQuery("");
  }

  async function transfer() {
    if (!transferTask || !nextOwners.length) return;
    const previousOwners = splitText(transferTask.owner_text);
    setSaving(true);
    try {
      const next = await updateThreeListTask(transferTask.task_id, {
        owner_candidates: nextOwners,
        notes: `${actorName}将任务责任人从“${previousOwners.join("、") || "待确认"}”转派为“${nextOwners.join("、")}”。`,
      });
      onWorkspaceChange(next);
      setTransferTask(null);
      setNextOwners([]);
      message.success("任务已流转到新责任人的任务看板");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "任务分发失败");
    } finally {
      setSaving(false);
    }
  }

  async function changeStatus(task: LinkedTaskRow, nextStatus: string) {
    setStatusSaving(task.task_id);
    try {
      const next = await updateThreeListTask(task.task_id, {
        status: nextStatus,
        notes: `${actorName}在任务清单将状态修改为“${workItemStatusLabel(nextStatus)}”。`,
      });
      onWorkspaceChange(next);
      message.success(`任务状态已更新为“${workItemStatusLabel(nextStatus)}”`);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "任务状态更新失败");
    } finally {
      setStatusSaving("");
    }
  }

  return (
    <section className="panel-stack task-board" aria-label="任务清单">
      <Card size="small" title="任务清单" extra={<Tag>{workspace.access.role_profile?.label || workspace.access.role}</Tag>}>
        <div className="task-summary-grid">
          <SummaryMetric label={scope === "mine" ? "我的未完成任务" : "团队未完成任务"} value={unfinishedCount} tone="processing" />
          <SummaryMetric label="已逾期" value={overdueCount} tone={overdueCount ? "error" : "success"} />
          <SummaryMetric label="责任人待补" value={unownedCount} tone={unownedCount ? "warning" : "success"} />
          <SummaryMetric label="当前范围任务" value={scopedRows.length} tone="default" />
        </div>
        <Space className="task-board-toolbar" wrap>
          {canDelegate ? (
            <Segmented
              value={scope}
              options={[
                { value: "mine", label: "我的任务" },
                { value: "team", label: "团队任务" },
              ]}
              onChange={(value) => setScope(value as "mine" | "team")}
            />
          ) : <Tag color="blue">我的任务</Tag>}
          <Select value={status} options={statusOptions} onChange={setStatus} />
          <Input.Search
            allowClear
            placeholder="搜索事项、负责人、交付物或验收口径"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <Tag color="blue">当前用户：{actorName}</Tag>
        </Space>
      </Card>

      {rows.length ? (
        <Table
          className="task-list-table"
          rowKey="task_id"
          size="middle"
          tableLayout="fixed"
          scroll={{ x: 1715, y: "max(240px, calc(100dvh - 440px))" }}
          pagination={{ pageSize: 12, showSizeChanger: true }}
          dataSource={rows}
          columns={[
            {
              title: "任务事项",
              width: 230,
              fixed: "left",
              align: "left",
              render: (_, row) => (
                <Button className="task-title-button" type="link" onClick={() => setDetailTask(row)}>
                  {taskTitle(row.task_description)}
                </Button>
              ),
            },
            {
              title: "任务说明",
              width: 300,
              render: (_, row) => (
                <Tooltip title={row.task_detail || row.task_description}>
                  <div className="task-description-cell">
                    {row.task_detail || row.task_description || "待补"}
                  </div>
                </Tooltip>
              ),
            },
            {
              title: "交付物",
              width: 250,
              ellipsis: true,
              render: (_, row) => <Tooltip title={row.deliverable}>{row.deliverable || "待补"}</Tooltip>,
            },
            {
              title: "验收口径",
              width: 300,
              render: (_, row) => <Tooltip title={row.acceptance_criteria}>{shortText(row.acceptance_criteria, 58) || "待补"}</Tooltip>,
            },
            {
              title: "截止时间",
              width: 125,
              render: (_, row) => <Tag color={isOverdue(row) ? "error" : "default"}>{row.due_date || "待补"}</Tag>,
            },
            {
              title: "状态",
              width: 110,
              className: "task-status-column",
              render: (_, row) => splitText(row.owner_text).includes(actorName) ? (
                <Select
                  className="task-status-select"
                  value={row.task_status}
                  options={statusChangeOptions(row.task_status)}
                  loading={statusSaving === row.task_id}
                  onChange={(nextStatus) => changeStatus(row, nextStatus)}
                />
              ) : <Tag color={statusTone(row.task_status)}>{workItemStatusLabel(row.task_status)}</Tag>,
            },
            {
              title: "负责人",
              width: 140,
              ellipsis: true,
              render: (_, row) => <Tooltip title={ownerText(row.owner_text)}>{ownerText(row.owner_text)}</Tooltip>,
            },
            {
              title: "进度",
              width: 110,
              render: (_, row) => (
                <Progress
                  percent={row.progress ?? (row.task_status === "done" ? 100 : 0)}
                  size="small"
                />
              ),
            },
            {
              title: "操作",
              width: 125,
              fixed: "right",
              className: "task-action-column",
              render: (_, row) => (
                (canDelegate || splitText(row.owner_text).includes(actorName)) && activeStatuses.has(row.task_status) ? (
                  <Button
                    type="primary"
                    onClick={() => {
                      setTransferTask(row);
                      setNextOwners(splitText(row.owner_text));
                    }}
                  >
                    转派/下发
                  </Button>
                ) : <span className="muted-text">—</span>
              ),
            },
          ]}
        />
      ) : (
        <Card size="small">
          {allRows.length ? (
            <PanelEmpty
              title="当前筛选条件下没有任务"
              hint={scope === "mine"
                ? "当前没有分配给你的任务。可以切到「团队任务」查看全部，或把状态改成「全部状态」。"
                : "换一个状态或清空搜索关键词试试；也可以在「三清单 · 任务清单」里新增任务。"}
              action={<Button onClick={resetFilters}>清空筛选条件</Button>}
            />
          ) : (
            <PanelEmpty
              title="项目还没有任务"
              hint="在对话中上传任务表或会议纪要，确认助理抽取的候选后任务会进入这里；也可以在「三清单 · 任务清单」直接新增。"
            />
          )}
        </Card>
      )}

      <Modal
        open={Boolean(detailTask)}
        title={taskTitle(detailTask?.task_description) || "任务详情"}
        footer={<Button onClick={() => setDetailTask(null)}>关闭</Button>}
        onCancel={() => setDetailTask(null)}
        width={760}
      >
        {detailTask ? <TaskDetail task={detailTask} /> : null}
      </Modal>

      <Modal
        open={Boolean(transferTask)}
        title={`转派/向下分发：${taskTitle(transferTask?.task_description)}`}
        okText="确认流转"
        onOk={transfer}
        okButtonProps={{ disabled: !nextOwners.length, loading: saving }}
        onCancel={() => {
          setTransferTask(null);
          setNextOwners([]);
        }}
      >
        <Space direction="vertical" className="task-delegate-form">
          <div>当前负责人：{ownerText(transferTask?.owner_text)}</div>
          <div className="task-delegate-tip">编辑框已保留当前负责人。你可以直接增加、删除或替换人员；保存后，任务会按新的负责人清单流转到对应个人看板。</div>
          <Select
            mode="multiple"
            showSearch
            optionFilterProp="label"
            placeholder="选择具体实施人员或其他责任人"
            options={assigneeOptions}
            value={nextOwners}
            onChange={setNextOwners}
          />
        </Space>
      </Modal>
    </section>
  );
}

function SummaryMetric(props: { label: string; value: number; tone: string }) {
  return (
    <div className={`task-summary-metric task-summary-${props.tone}`}>
      <strong>{props.value}</strong>
      <span>{props.label}</span>
    </div>
  );
}

function TaskDetail({ task }: { task: LinkedTaskRow }) {
  return (
    <div className="task-detail-grid">
      <span>状态</span><strong>{workItemStatusLabel(task.task_status)}</strong>
      <span>负责人/执行人</span><strong>{ownerText(task.owner_text)}</strong>
      <span>截止时间</span><strong>{task.due_date || "待补"}</strong>
      <span>交付物</span><div>{task.deliverable || "待补"}</div>
      <span>验收口径</span><div>{task.acceptance_criteria || "待补"}</div>
      <span>任务说明</span><div className="task-detail-long">{task.task_detail || task.task_description}</div>
      <span>来源批次</span><div>{task.source_batch_id || "未标记"}</div>
      <span>关联问题</span><div>{task.linked_issue_titles.join("、") || "暂未关联"}</div>
    </div>
  );
}

function roleLabel(role: string) {
  return ({
    pm: "项目经理",
    pmo: "PMO / 总体组",
    professional_lead: "专业统筹",
    topic_lead: "专题负责人",
    exec: "实施人员",
  } as Record<string, string>)[role] || role;
}

function statusChangeOptions(currentStatus: string) {
  if (currentStatus === "candidate") return taskStatusOptions;
  return taskStatusOptions.filter((option) => option.value !== "candidate");
}

function isOverdue(task: LinkedTaskRow) {
  if (!activeStatuses.has(task.task_status) || !task.due_date) return false;
  const timestamp = Date.parse(`${task.due_date.slice(0, 10)}T23:59:59`);
  return Number.isFinite(timestamp) && timestamp < Date.now();
}
