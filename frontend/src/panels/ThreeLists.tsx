import {
  Button,
  Card,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
  type TableColumnsType,
} from "antd";
import { useMemo, useState, type ReactNode } from "react";
import {
  archiveThreeListIssue,
  archiveThreeListMethod,
  archiveThreeListTask,
  createThreeListIssue,
  createThreeListMethod,
  createThreeListTask,
  updateThreeListIssue,
  updateThreeListMethod,
  updateThreeListTask,
  type ThreeListPayload,
} from "../api/client";
import { ownerText, shortText, splitText, workItemStatusLabel } from "../lib/ui";
import type { LinkedIssueRow, LinkedMethodRow, LinkedTaskRow, Workspace } from "../types";
import { DateField, EditableCell, PanelEmpty } from "./panelKit";
import { useAsyncAction } from "./useAsyncAction";
import { useRowEditor, type RowEditor } from "./useRowEditor";

type ThreeListsPanelProps = {
  workspace: Workspace;
  onWorkspaceChange: (workspace: Workspace) => void;
};

type CreateKind = "issue" | "task" | "method";
type TextTarget = { id: string; field: "description"; value: string; setter: (value: string) => void };
type Option = { value: string; label: string };

const itemStatusOptions: Option[] = [
  { value: "candidate", label: "待确认" },
  { value: "confirmed", label: "已确认" },
  { value: "rejected", label: "取消" },
];

export function ThreeListsPanel({ workspace, onWorkspaceChange }: ThreeListsPanelProps) {
  const lists = workspace.three_lists;
  const peopleOptions = Array.from(new Set(workspace.people_workspace.people.map((person) => person.name)))
    .map((name) => ({ value: name, label: name }));
  const issueOptions = lists.issues.map((row) => ({ value: row.issue_id, label: row.issue_description }));
  const taskOptions = lists.tasks.map((row) => ({ value: row.task_id, label: row.task_description }));
  const [createKind, setCreateKind] = useState<CreateKind | null>(null);
  const [createDraft, setCreateDraft] = useState<ThreeListPayload>({});
  const [textTarget, setTextTarget] = useState<TextTarget | null>(null);

  const createAction = useAsyncAction(
    async (kind: CreateKind, draft: ThreeListPayload) => {
      const run = kind === "issue" ? createThreeListIssue : kind === "task" ? createThreeListTask : createThreeListMethod;
      onWorkspaceChange(await run({ ...draft, notes: "三清单人工新增" }));
    },
    { errorText: "新增失败", successText: "已新增" },
  );

  async function createRow() {
    if (!createKind) return;
    const created = await createAction.run(createKind, createDraft);
    if (!created) return;
    setCreateKind(null);
    setCreateDraft({});
  }

  return (
    <section className="panel-stack" aria-label="三清单">
      <Card size="small" title="三清单">
        <Space wrap>
          <Tag>问题清单 {lists.summary.issue_count}</Tag>
          <Tag>任务清单 {lists.summary.task_count}</Tag>
          <Tag>方法清单 {lists.summary.method_count}</Tag>
          <Tag color={lists.summary.unlinked_issue_count ? "warning" : undefined}>
            未关联问题 {lists.summary.unlinked_issue_count}
          </Tag>
          <Tag>长期记忆方法 {lists.summary.method_memory_count}</Tag>
          <Button onClick={() => exportCsv("问题清单", lists.issues)}>导出问题 CSV</Button>
          <Button onClick={() => exportCsv("任务清单", lists.tasks)}>导出任务 CSV</Button>
          <Button onClick={() => exportCsv("方法清单", lists.methods)}>导出方法 CSV</Button>
        </Space>
      </Card>
      <Tabs
        items={[
          {
            key: "issues",
            label: "问题清单",
            children: (
              <IssueTable
                rows={lists.issues}
                peopleOptions={peopleOptions}
                taskOptions={taskOptions}
                workspace={workspace}
                onWorkspaceChange={onWorkspaceChange}
                onTextEdit={setTextTarget}
                onCreate={() => setCreateKind("issue")}
              />
            ),
          },
          {
            key: "tasks",
            label: "任务清单",
            children: (
              <TaskTable
                rows={lists.tasks}
                peopleOptions={peopleOptions}
                issueOptions={issueOptions}
                workspace={workspace}
                onWorkspaceChange={onWorkspaceChange}
                onTextEdit={setTextTarget}
                onCreate={() => setCreateKind("task")}
              />
            ),
          },
          {
            key: "methods",
            label: "方法清单",
            children: (
              <MethodTable
                rows={lists.methods}
                issueOptions={issueOptions}
                taskOptions={taskOptions}
                onWorkspaceChange={onWorkspaceChange}
                onTextEdit={setTextTarget}
                onCreate={() => setCreateKind("method")}
              />
            ),
          },
        ]}
      />
      <Modal
        open={Boolean(createKind)}
        title={createTitle(createKind)}
        okText="确认"
        cancelText="取消"
        confirmLoading={createAction.pending}
        onOk={createRow}
        onCancel={() => setCreateKind(null)}
      >
        <CreateForm
          draft={createDraft}
          peopleOptions={peopleOptions}
          issueOptions={issueOptions}
          taskOptions={taskOptions}
          onChange={(patch) => setCreateDraft((draft) => ({ ...draft, ...patch }))}
        />
      </Modal>
      <Modal
        open={Boolean(textTarget)}
        title="编辑长文本"
        okText="确认"
        cancelText="取消"
        onOk={() => {
          if (textTarget) textTarget.setter(textTarget.value);
          setTextTarget(null);
        }}
        onCancel={() => setTextTarget(null)}
      >
        <Input.TextArea
          rows={8}
          value={textTarget?.value || ""}
          onChange={(event) => setTextTarget((target) => target ? { ...target, value: event.target.value } : target)}
        />
      </Modal>
    </section>
  );
}

function RowActions<Row>({
  editor,
  row,
  extra,
  onArchive,
  archiving,
}: {
  editor: RowEditor<Row>;
  row: Row;
  extra?: ReactNode;
  onArchive: () => void;
  archiving: boolean;
}) {
  if (editor.isEditing(row)) {
    return (
      <Space>
        <Button type="primary" loading={editor.isSaving(row)} onClick={() => editor.save(row)}>确认</Button>
        <Button onClick={editor.cancel}>取消</Button>
      </Space>
    );
  }
  return (
    <Space>
      <Button onClick={() => editor.begin(row)}>编辑</Button>
      {extra}
      <Button danger loading={archiving} onClick={onArchive}>归档</Button>
    </Space>
  );
}

function IssueTable(props: {
  rows: LinkedIssueRow[];
  peopleOptions: Option[];
  taskOptions: Option[];
  workspace: Workspace;
  onWorkspaceChange: (workspace: Workspace) => void;
  onTextEdit: (target: TextTarget) => void;
  onCreate: () => void;
}) {
  const { onWorkspaceChange, onTextEdit, peopleOptions, taskOptions } = props;
  const editor = useRowEditor<LinkedIssueRow>(
    "issue_id",
    async (id, payload) => {
      onWorkspaceChange(await updateThreeListIssue(id, { ...payload, notes: "三清单问题清单编辑" }));
    },
  );
  const archive = useAsyncAction(
    async (id: string) => {
      onWorkspaceChange(await archiveThreeListIssue(id));
    },
    { errorText: "归档失败", key: (id) => id },
  );

  const columns = useMemo<TableColumnsType<LinkedIssueRow>>(() => [
    {
      title: "问题",
      width: 220,
      ellipsis: true,
      render: (_, row) => (
        <EditableCell
          editing={editor.isEditing(row)}
          view={<strong>{row.issue_description}</strong>}
          edit={() => (
            <Input
              className="three-list-control"
              value={editor.draft(row).title ?? row.issue_description}
              onChange={(event) => editor.update(row, { title: event.target.value })}
            />
          )}
        />
      ),
    },
    {
      title: "状态",
      width: 130,
      render: (_, row) => (
        <EditableCell
          editing={editor.isEditing(row)}
          view={<Tag>{workItemStatusLabel(row.status)}</Tag>}
          edit={() => (
            <Select
              className="three-list-control"
              popupMatchSelectWidth={280}
              options={itemStatusOptions}
              value={editor.draft(row).status ?? row.status}
              onChange={(status) => editor.update(row, { status })}
            />
          )}
        />
      ),
    },
    {
      title: "责任人",
      width: 240,
      render: (_, row) => (
        <EditableCell
          editing={editor.isEditing(row)}
          view={ownerText(row.owner_text)}
          edit={() => (
            <Select
              className="three-list-control"
              popupMatchSelectWidth={280}
              mode="multiple"
              options={peopleOptions}
              value={editor.draft(row).owner_candidates ?? splitText(row.owner_text)}
              onChange={(owner_candidates) => editor.update(row, { owner_candidates })}
            />
          )}
        />
      ),
    },
    {
      title: "日期",
      width: 160,
      render: (_, row) => (
        <EditableCell
          editing={editor.isEditing(row)}
          view={row.planned_resolution_date || "待定"}
          edit={() => (
            <DateField
              placeholder="截止时间"
              value={editor.draft(row).due_date ?? row.planned_resolution_date}
              onChange={(due_date) => editor.update(row, { due_date })}
            />
          )}
        />
      ),
    },
    {
      title: "关联任务",
      width: 260,
      render: (_, row) => (
        <EditableCell
          editing={editor.isEditing(row)}
          view={shortText(row.linked_task_titles.join("、"), 80)
            || <Button type="link" onClick={() => editor.begin(row)}>待关联</Button>}
          edit={() => (
            <Select
              className="three-list-control"
              popupMatchSelectWidth={280}
              mode="multiple"
              options={taskOptions}
              value={editor.draft(row).linked_task_ids ?? row.linked_task_ids}
              onChange={(linked_task_ids) => editor.update(row, { linked_task_ids })}
            />
          )}
        />
      ),
    },
    {
      title: "描述",
      width: 120,
      render: (_, row) => (
        <Button
          type="link"
          onClick={() => onTextEdit({
            id: row.issue_id,
            field: "description",
            value: editor.draft(row).description ?? row.current_difficulty,
            setter: (value) => editor.update(row, { description: value }),
          })}
        >
          编辑详情
        </Button>
      ),
    },
    {
      title: "操作",
      width: 170,
      fixed: "right",
      render: (_, row) => (
        <RowActions
          editor={editor}
          row={row}
          archiving={archive.isPending(row.issue_id)}
          onArchive={() => void archive.run(row.issue_id)}
        />
      ),
    },
  ], [archive, editor, onTextEdit, peopleOptions, taskOptions]);

  return (
    <>
      <Space className="table-toolbar"><Button type="primary" onClick={props.onCreate}>新增问题</Button></Space>
      <Table<LinkedIssueRow>
        className="three-list-table"
        tableLayout="fixed"
        scroll={{ x: 1300 }}
        rowKey="issue_id"
        size="middle"
        pagination={{ pageSize: 8 }}
        dataSource={props.rows}
        columns={columns}
        locale={{
          emptyText: (
            <PanelEmpty
              title="问题清单还是空的"
              hint="上传会议纪要后，助理会把讨论中的卡点抽成候选问题；也可以直接新增一条问题并关联到任务。"
              action={<Button type="primary" onClick={props.onCreate}>新增问题</Button>}
            />
          ),
        }}
      />
    </>
  );
}

function TaskTable(props: {
  rows: LinkedTaskRow[];
  peopleOptions: Option[];
  issueOptions: Option[];
  workspace: Workspace;
  onWorkspaceChange: (workspace: Workspace) => void;
  onTextEdit: (target: TextTarget) => void;
  onCreate: () => void;
}) {
  const { onWorkspaceChange, onTextEdit, peopleOptions, issueOptions } = props;
  const editor = useRowEditor<LinkedTaskRow>(
    "task_id",
    async (id, payload) => {
      onWorkspaceChange(await updateThreeListTask(id, { ...payload, notes: "三清单任务清单编辑" }));
    },
  );
  const archive = useAsyncAction(
    async (id: string) => {
      onWorkspaceChange(await archiveThreeListTask(id));
    },
    { errorText: "归档失败", key: (id) => id },
  );

  const columns = useMemo<TableColumnsType<LinkedTaskRow>>(() => [
    {
      title: "任务",
      width: 240,
      ellipsis: true,
      render: (_, row) => (
        <EditableCell
          editing={editor.isEditing(row)}
          view={<strong>{row.task_description}</strong>}
          edit={() => (
            <Input
              className="three-list-control"
              value={editor.draft(row).title ?? row.task_description}
              onChange={(event) => editor.update(row, { title: event.target.value })}
            />
          )}
        />
      ),
    },
    {
      title: "状态",
      width: 130,
      render: (_, row) => <Tag>{workItemStatusLabel(row.task_status)}</Tag>,
    },
    {
      title: "责任人",
      width: 240,
      render: (_, row) => (
        <EditableCell
          editing={editor.isEditing(row)}
          view={ownerText(row.owner_text)}
          edit={() => (
            <Select
              className="three-list-control"
              popupMatchSelectWidth={280}
              mode="multiple"
              options={peopleOptions}
              value={editor.draft(row).owner_candidates ?? splitText(row.owner_text)}
              onChange={(owner_candidates) => editor.update(row, { owner_candidates })}
            />
          )}
        />
      ),
    },
    {
      title: "截止时间",
      width: 160,
      render: (_, row) => (
        <EditableCell
          editing={editor.isEditing(row)}
          view={row.due_date || "待定"}
          edit={() => (
            <DateField
              placeholder="截止时间"
              value={editor.draft(row).due_date ?? row.due_date}
              onChange={(due_date) => editor.update(row, { due_date })}
            />
          )}
        />
      ),
    },
    {
      title: "交付物",
      width: 220,
      ellipsis: true,
      render: (_, row) => (
        <EditableCell
          editing={editor.isEditing(row)}
          view={row.deliverable || "待补"}
          edit={() => (
            <Input
              className="three-list-control"
              value={editor.draft(row).deliverable ?? row.deliverable}
              onChange={(event) => editor.update(row, { deliverable: event.target.value })}
            />
          )}
        />
      ),
    },
    {
      title: "关联问题",
      width: 260,
      render: (_, row) => (
        <EditableCell
          editing={editor.isEditing(row)}
          view={shortText(row.linked_issue_titles.join("、"), 80)
            || <Button type="link" onClick={() => editor.begin(row)}>待关联</Button>}
          edit={() => (
            <Select
              className="three-list-control"
              popupMatchSelectWidth={280}
              mode="multiple"
              options={issueOptions}
              value={editor.draft(row).linked_issue_ids ?? row.linked_issue_ids}
              onChange={(linked_issue_ids) => editor.update(row, { linked_issue_ids })}
            />
          )}
        />
      ),
    },
    {
      title: "描述",
      width: 120,
      render: (_, row) => (
        <Button
          type="link"
          onClick={() => onTextEdit({
            id: row.task_id,
            field: "description",
            value: editor.draft(row).description ?? row.task_detail,
            setter: (value) => editor.update(row, { description: value }),
          })}
        >
          编辑详情
        </Button>
      ),
    },
    {
      title: "操作",
      width: 170,
      fixed: "right",
      render: (_, row) => (
        <RowActions
          editor={editor}
          row={row}
          archiving={archive.isPending(row.task_id)}
          onArchive={() => void archive.run(row.task_id)}
        />
      ),
    },
  ], [archive, editor, issueOptions, onTextEdit, peopleOptions]);

  return (
    <>
      <Space className="table-toolbar"><Button type="primary" onClick={props.onCreate}>新增任务</Button></Space>
      <Table<LinkedTaskRow>
        className="three-list-table"
        tableLayout="fixed"
        scroll={{ x: 1480 }}
        rowKey="task_id"
        size="middle"
        pagination={{ pageSize: 8 }}
        dataSource={props.rows}
        columns={columns}
        locale={{
          emptyText: (
            <PanelEmpty
              title="任务清单还是空的"
              hint="在对话里上传任务表或会议纪要，确认候选后任务会自动入库；也可以直接新增一条任务并指定责任人与交付物。"
              action={<Button type="primary" onClick={props.onCreate}>新增任务</Button>}
            />
          ),
        }}
      />
    </>
  );
}

function MethodTable(props: {
  rows: LinkedMethodRow[];
  issueOptions: Option[];
  taskOptions: Option[];
  onWorkspaceChange: (workspace: Workspace) => void;
  onTextEdit: (target: TextTarget) => void;
  onCreate: () => void;
}) {
  const { onWorkspaceChange, onTextEdit, issueOptions, taskOptions } = props;
  const editor = useRowEditor<LinkedMethodRow>(
    "method_id",
    async (id, payload) => {
      onWorkspaceChange(await updateThreeListMethod(id, { ...payload, notes: "三清单方法清单编辑" }));
    },
  );
  const archive = useAsyncAction(
    async (id: string) => {
      onWorkspaceChange(await archiveThreeListMethod(id));
    },
    { errorText: "归档失败", key: (id) => id },
  );
  const columns = useMemo<TableColumnsType<LinkedMethodRow>>(() => [
    {
      title: "方法",
      width: 240,
      ellipsis: true,
      render: (_, row) => (
        <EditableCell
          editing={editor.isEditing(row)}
          view={<strong>{row.overview}</strong>}
          edit={() => (
            <Input
              className="three-list-control"
              value={editor.draft(row).title ?? row.overview}
              onChange={(event) => editor.update(row, { title: event.target.value })}
            />
          )}
        />
      ),
    },
    {
      title: "状态",
      width: 130,
      render: (_, row) => (
        <EditableCell
          editing={editor.isEditing(row)}
          view={<Tag>{workItemStatusLabel(row.status)}</Tag>}
          edit={() => (
            <Select
              className="three-list-control"
              popupMatchSelectWidth={280}
              options={itemStatusOptions}
              value={editor.draft(row).status ?? row.status}
              onChange={(status) => editor.update(row, { status })}
            />
          )}
        />
      ),
    },
    {
      title: "Skill 成熟度",
      width: 160,
      render: (_, row) => (
        <Tooltip title={row.skill_gate_failures.length ? row.skill_gate_failures.join("；") : skillMaturityLabel(row.skill_maturity)}>
          <Tag color={skillMaturityColor(row.skill_maturity)}>
            {skillMaturityLabel(row.skill_maturity)}{row.skill_version ? ` v${row.skill_version}` : ""}
          </Tag>
        </Tooltip>
      ),
    },
    {
      title: "关联问题",
      width: 260,
      render: (_, row) => (
        <EditableCell
          editing={editor.isEditing(row)}
          view={shortText(row.linked_issue_titles.join("、"), 80) || "待关联"}
          edit={() => (
            <Select
              className="three-list-control"
              popupMatchSelectWidth={280}
              mode="multiple"
              options={issueOptions}
              value={editor.draft(row).linked_issue_ids ?? row.linked_issue_ids}
              onChange={(linked_issue_ids) => editor.update(row, { linked_issue_ids })}
            />
          )}
        />
      ),
    },
    {
      title: "关联任务",
      width: 260,
      render: (_, row) => (
        <EditableCell
          editing={editor.isEditing(row)}
          view={`${row.linked_task_count} 项`}
          edit={() => (
            <Select
              className="three-list-control"
              popupMatchSelectWidth={280}
              mode="multiple"
              options={taskOptions}
              value={editor.draft(row).linked_task_ids ?? row.linked_task_ids}
              onChange={(linked_task_ids) => editor.update(row, { linked_task_ids })}
            />
          )}
        />
      ),
    },
    {
      title: "详情",
      width: 120,
      render: (_, row) => (
        <Button
          type="link"
          onClick={() => onTextEdit({
            id: row.method_id,
            field: "description",
            value: editor.draft(row).description ?? row.detail,
            setter: (value) => editor.update(row, { description: value }),
          })}
        >
          编辑详情
        </Button>
      ),
    },
    {
      title: "操作",
      width: 120,
      fixed: "right",
      render: (_, row) => (
        <RowActions
          editor={editor}
          row={row}
          archiving={archive.isPending(row.method_id)}
          onArchive={() => void archive.run(row.method_id)}
        />
      ),
    },
  ], [archive, editor, issueOptions, onTextEdit, taskOptions]);

  return (
    <>
      <Space className="table-toolbar"><Button type="primary" onClick={props.onCreate}>新增方法</Button></Space>
      <Table<LinkedMethodRow>
        className="three-list-table"
        tableLayout="fixed"
        scroll={{ x: 1400 }}
        rowKey="method_id"
        size="middle"
        pagination={{ pageSize: 8 }}
        dataSource={props.rows}
        columns={columns}
        locale={{
          emptyText: (
            <PanelEmpty
              title="方法清单还是空的"
              hint="方法来自被反复验证的做法：在对话中确认「法」类候选，或直接新增一条方法，积累到两个来源的证据后即可晋级为 Skill。"
              action={<Button type="primary" onClick={props.onCreate}>新增方法</Button>}
            />
          ),
        }}
      />
    </>
  );
}

function skillMaturityLabel(value: string) {
  return ({ memory: "记忆卡", candidate: "Skill 候选", tested: "已测试", published: "已发布", revalidation_required: "待重新验证" } as Record<string, string>)[value] || value;
}

function skillMaturityColor(value: string) {
  return ({ memory: "default", candidate: "warning", tested: "processing", published: "success", revalidation_required: "error" } as Record<string, string>)[value] || "default";
}

function CreateForm(props: {
  draft: ThreeListPayload;
  peopleOptions: Option[];
  issueOptions: Option[];
  taskOptions: Option[];
  onChange: (patch: ThreeListPayload) => void;
}) {
  return (
    <Space direction="vertical" className="create-form">
      <Input placeholder="标题" value={props.draft.title} onChange={(event) => props.onChange({ title: event.target.value })} />
      <Input.TextArea rows={4} placeholder="说明" value={props.draft.description} onChange={(event) => props.onChange({ description: event.target.value })} />
      <Select mode="multiple" placeholder="责任人" options={props.peopleOptions} value={props.draft.owner_candidates} onChange={(owner_candidates) => props.onChange({ owner_candidates })} />
      <DateField placeholder="截止时间" value={props.draft.due_date || ""} onChange={(due_date) => props.onChange({ due_date })} />
      <Input placeholder="交付物" value={props.draft.deliverable} onChange={(event) => props.onChange({ deliverable: event.target.value })} />
      <Select mode="multiple" placeholder="关联问题" options={props.issueOptions} value={props.draft.linked_issue_ids} onChange={(linked_issue_ids) => props.onChange({ linked_issue_ids })} />
      <Select mode="multiple" placeholder="关联任务" options={props.taskOptions} value={props.draft.linked_task_ids} onChange={(linked_task_ids) => props.onChange({ linked_task_ids })} />
    </Space>
  );
}

function createTitle(kind: CreateKind | null) {
  if (kind === "issue") return "新增问题";
  if (kind === "task") return "新增任务";
  if (kind === "method") return "新增方法";
  return "新增";
}

function exportCsv(name: string, rows: Array<Record<string, unknown>>) {
  const keys = Array.from(new Set(rows.flatMap((row) => Object.keys(row))));
  const csv = [
    keys.join(","),
    ...rows.map((row) => keys.map((key) => csvValue(row[key])).join(",")),
  ].join("\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `${name}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

function csvValue(value: unknown) {
  const text = Array.isArray(value) ? value.join("、") : String(value ?? "");
  return `"${text.replace(/"/g, '""')}"`;
}
