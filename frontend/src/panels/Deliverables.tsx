import { DownloadOutlined, PlusOutlined } from "@ant-design/icons";
import { App as AntdApp, Alert, Button, Checkbox, Form, Input, Modal, Select, Space, Table, Tag } from "antd";
import { useEffect, useState } from "react";
import {
  createDeliverable,
  downloadDeliverableFile,
  loadDeliverables,
  updateDeliverable,
  type DeliverablePayload,
} from "../api/client";
import type { Deliverable, Workspace } from "../types";
import { DateField, EditableCell, PanelEmpty } from "./panelKit";
import "./Deliverables.css";


const STATUS_LABELS: Record<string, string> = {
  expected: "待提交",
  draft: "草稿",
  submitted: "已提交",
  accepted: "已验收",
  rejected: "已退回",
};

// 绿=已完成 / 蓝=进行中 / 红=被退回 / 灰=尚未开始，与全站四组语义色一致。
const STATUS_TONES: Record<string, string | undefined> = {
  expected: undefined,
  draft: undefined,
  submitted: "processing",
  accepted: "success",
  rejected: "error",
  archived: undefined,
};

const ROLE_LABELS: Record<string, string> = {
  formal: "正式成果",
  process: "过程文档",
  evidence: "验收证据",
  reference: "参考资料",
};

const EMPTY_DRAFT: DeliverablePayload = {
  title: "",
  type_label: "",
  acceptance_criteria: "",
  due_date: "",
  required: true,
  sensitivity: "l1",
};

export function DeliverablesPanel({ workspace }: { workspace: Workspace }) {
  const { message, modal } = AntdApp.useApp();
  const [rows, setRows] = useState<Deliverable[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState("");
  const [error, setError] = useState("");
  const [editing, setEditing] = useState("");
  const [draft, setDraft] = useState<DeliverablePayload>(EMPTY_DRAFT);
  const [createOpen, setCreateOpen] = useState(false);
  const canManage = ["pm", "pmo"].includes(workspace.access.role);

  async function reload() {
    setLoading(true);
    try {
      setRows(await loadDeliverables(workspace.milestone_workspace.summary.milestone_id));
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "交付物加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void reload();
  }, [workspace.access.actor.id, workspace.milestone_workspace.summary.milestone_id]);

  function beginEdit(row: Deliverable) {
    setEditing(row.deliverable_id);
    setDraft({
      title: row.title,
      type_label: row.type_label,
      acceptance_criteria: row.acceptance_criteria,
      due_date: row.due_date,
      required: row.required,
      sensitivity: row.sensitivity,
      status: row.status,
    });
  }

  async function saveEdit(row: Deliverable) {
    const previous = rows;
    setRows((current) => current.map((item) => item.deliverable_id === row.deliverable_id ? { ...item, ...draft } as Deliverable : item));
    setSaving(row.deliverable_id);
    try {
      await updateDeliverable(row.deliverable_id, draft);
      setEditing("");
      await reload();
    } catch (reason) {
      setRows(previous);
      const detail = reason instanceof Error ? reason.message : "交付物保存失败";
      setError(detail);
      message.error(detail);
    } finally {
      setSaving("");
    }
  }

  async function createRow() {
    setSaving("create");
    try {
      await createDeliverable(draft);
      setCreateOpen(false);
      setDraft(EMPTY_DRAFT);
      await reload();
    } catch (reason) {
      const detail = reason instanceof Error ? reason.message : "交付物创建失败";
      setError(detail);
      message.error(detail);
    } finally {
      setSaving("");
    }
  }

  function archive(row: Deliverable) {
    modal.confirm({
      title: `归档“${row.title}”？`,
      content: "历史提交版本仍保留。",
      okText: "归档",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        try {
          await updateDeliverable(row.deliverable_id, { status: "archived" });
          await reload();
        } catch (reason) {
          message.error(reason instanceof Error ? reason.message : "归档失败");
        }
      },
    });
  }

  return (
    <section className="deliverables-panel" aria-label="里程碑交付物">
      {error ? <Alert type="error" showIcon message={error} /> : null}
      <div className="deliverables-toolbar">
        <div>
          <strong>交付物</strong>
          <span>当前里程碑 {workspace.milestone_workspace.summary.name}</span>
        </div>
        {canManage ? (
          <Button type="primary" icon={<PlusOutlined />} onClick={() => { setDraft(EMPTY_DRAFT); setCreateOpen(true); }}>
            新增交付物
          </Button>
        ) : null}
      </div>
      <Table
        rowKey="deliverable_id"
        size="middle"
        loading={loading}
        tableLayout="fixed"
        scroll={{ x: 1160 }}
        pagination={{ pageSize: 10 }}
        dataSource={rows}
        expandable={{ expandedRowRender: (row) => <VersionList row={row} /> }}
        locale={{
          emptyText: (
            <PanelEmpty
              title="当前里程碑还没有登记交付物"
              hint="先把要交的东西登记下来：名称、验收口径和截止日期。登记之后，成员提交的文件会按版本挂在这一行下面。"
              action={canManage ? (
                <Button type="primary" icon={<PlusOutlined />} onClick={() => { setDraft(EMPTY_DRAFT); setCreateOpen(true); }}>
                  新增交付物
                </Button>
              ) : null}
            />
          ),
        }}
        columns={[
          {
            title: "交付物",
            width: 230,
            render: (_, row) => (
              <EditableCell
                editing={editing === row.deliverable_id}
                view={<strong>{row.title}</strong>}
                edit={() => <Input value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} />}
              />
            ),
          },
          {
            title: "类型",
            width: 150,
            render: (_, row) => (
              <EditableCell
                editing={editing === row.deliverable_id}
                view={row.type_label || "未分类"}
                edit={() => <Input value={draft.type_label} onChange={(event) => setDraft({ ...draft, type_label: event.target.value })} />}
              />
            ),
          },
          {
            title: "截止日期",
            width: 150,
            render: (_, row) => (
              <EditableCell
                editing={editing === row.deliverable_id}
                view={row.due_date || "待定"}
                edit={() => (
                  <DateField
                    allowClear={false}
                    placeholder="截止日期"
                    value={draft.due_date || ""}
                    onChange={(due_date) => setDraft({ ...draft, due_date })}
                  />
                )}
              />
            ),
          },
          {
            title: "验收口径",
            width: 300,
            ellipsis: true,
            render: (_, row) => (
              <EditableCell
                editing={editing === row.deliverable_id}
                view={row.acceptance_criteria || "待补"}
                edit={() => (
                  <Input.TextArea
                    rows={2}
                    value={draft.acceptance_criteria}
                    onChange={(event) => setDraft({ ...draft, acceptance_criteria: event.target.value })}
                  />
                )}
              />
            ),
          },
          { title: "状态", width: 110, render: (_, row) => <Tag color={STATUS_TONES[row.status]}>{STATUS_LABELS[row.status] || row.status}</Tag> },
          { title: "当前版本", width: 100, render: (_, row) => row.current_version ? `v${row.current_version}` : "未提交" },
          {
            title: "操作",
            width: 190,
            fixed: "right" as const,
            render: (_, row) => editing === row.deliverable_id ? (
              <Space>
                <Button type="primary" loading={saving === row.deliverable_id} onClick={() => saveEdit(row)}>确认</Button>
                <Button onClick={() => setEditing("")}>取消</Button>
              </Space>
            ) : row.can_manage ? (
              <Space>
                <Button onClick={() => beginEdit(row)}>编辑</Button>
                <Button danger onClick={() => archive(row)}>归档</Button>
              </Space>
            ) : <span className="deliverables-muted">只读</span>,
          },
        ]}
      />
      <Modal
        title="新增交付物"
        open={createOpen}
        okText="创建"
        cancelText="取消"
        confirmLoading={saving === "create"}
        okButtonProps={{ disabled: !draft.title?.trim() }}
        onOk={createRow}
        onCancel={() => setCreateOpen(false)}
      >
        <RequirementForm draft={draft} onChange={(patch) => setDraft({ ...draft, ...patch })} />
      </Modal>
    </section>
  );
}

function RequirementForm({ draft, onChange }: { draft: DeliverablePayload; onChange: (patch: DeliverablePayload) => void }) {
  return (
    <Form layout="vertical">
      <Form.Item label="名称" required><Input value={draft.title} onChange={(event) => onChange({ title: event.target.value })} /></Form.Item>
      <Form.Item label="类型"><Input value={draft.type_label} onChange={(event) => onChange({ type_label: event.target.value })} /></Form.Item>
      <Form.Item label="截止日期">
        <DateField placeholder="截止日期" value={draft.due_date || ""} onChange={(due_date) => onChange({ due_date })} />
      </Form.Item>
      <Form.Item label="验收口径"><Input.TextArea rows={3} value={draft.acceptance_criteria} onChange={(event) => onChange({ acceptance_criteria: event.target.value })} /></Form.Item>
      <Form.Item label="密级"><Select value={draft.sensitivity} options={["l1", "l2", "l3", "l4"].map((value) => ({ value, label: value.toUpperCase() }))} onChange={(sensitivity) => onChange({ sensitivity })} /></Form.Item>
      <Checkbox checked={draft.required} onChange={(event) => onChange({ required: event.target.checked })}>必需交付</Checkbox>
    </Form>
  );
}

function VersionList({ row }: { row: Deliverable }) {
  const { message } = AntdApp.useApp();
  if (!row.versions.length) return <span className="deliverables-muted">尚无已提交文件</span>;
  return (
    <div className="deliverable-versions">
      <strong>已提交文件</strong>
      {row.versions.map((version) => (
        <div className="deliverable-version" key={version.version_id}>
          <span>v{version.version} · {version.submitted_by} · {version.created_at.slice(0, 16).replace("T", " ")}</span>
          <Space wrap>
            {version.files.map((file) => (
              <Button
                size="small"
                icon={<DownloadOutlined />}
                key={file.file_id}
                onClick={() => downloadDeliverableFile(file.file_id, file.original_name).catch((reason) => message.error(reason instanceof Error ? reason.message : "下载失败"))}
              >
                {file.original_name} · {ROLE_LABELS[file.artifact_role] || file.artifact_role} · 下载
              </Button>
            ))}
          </Space>
        </div>
      ))}
    </div>
  );
}
