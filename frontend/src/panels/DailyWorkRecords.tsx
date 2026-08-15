import { App as AntdApp, Alert, Button, Card, Input, Select, Space, Table, Tag } from "antd";
import { useEffect, useState } from "react";
import { loadDailyWorkRecords, updateDailyWorkRecord } from "../api/client";
import type { DailyWorkRecord, DailyWorkRecordKind, Workspace } from "../types";
import { DateField, EditableCell, PanelEmpty } from "./panelKit";
import "./DailyWorkRecords.css";


const KIND_LABELS: Record<DailyWorkRecordKind, string> = {
  work: "做了什么",
  conclusion: "结论",
  problem: "问题",
  output: "产出",
};

const KIND_OPTIONS = Object.entries(KIND_LABELS).map(([value, label]) => ({ value, label }));

type Draft = {
  record_date: string;
  kind: DailyWorkRecordKind;
  text: string;
};

export function DailyWorkRecordsPanel({ workspace }: { workspace: Workspace }) {
  const { message } = AntdApp.useApp();
  const [rows, setRows] = useState<DailyWorkRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState("");
  const [editing, setEditing] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    loadDailyWorkRecords()
      .then((records) => {
        if (!cancelled) setRows(records);
      })
      .catch((reason) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "工作日志加载失败");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [workspace.access.actor.id]);

  function beginEdit(row: DailyWorkRecord) {
    setEditing(row.record_id);
    setDraft({
      record_date: row.record_date,
      kind: row.kind,
      text: row.text,
    });
  }

  function cancelEdit() {
    setEditing("");
    setDraft(null);
  }

  async function save(row: DailyWorkRecord) {
    if (!draft) return;
    const previous = rows;
    const optimistic: DailyWorkRecord = {
      ...row,
      record_date: draft.record_date,
      kind: draft.kind,
      text: draft.text,
      edited_by_human: true,
    };
    setRows((current) => current.map((item) => item.record_id === row.record_id ? optimistic : item));
    setSaving(row.record_id);
    setError("");
    try {
      const updated = await updateDailyWorkRecord(row.record_id, {
        record_date: optimistic.record_date,
        kind: optimistic.kind,
        text: optimistic.text,
      });
      setRows((current) => current.map((item) => item.record_id === row.record_id ? updated : item));
      cancelEdit();
    } catch (reason) {
      setRows(previous);
      const detail = reason instanceof Error ? reason.message : "工作日志保存失败";
      setError(detail);
      message.error(detail);
    } finally {
      setSaving("");
    }
  }

  return (
    <section className="daily-work-records" aria-label="工作日志数据库">
      {error ? <Alert type="error" showIcon message={error} /> : null}
      <Card
        size="small"
        title="工作日志"
        extra={<span className="daily-work-records-scope">当前范围：{workspace.access.actor.name} · {workspace.access.role}</span>}
      >
        <Table
          rowKey="record_id"
          size="middle"
          loading={loading}
          tableLayout="fixed"
          scroll={{ x: 1160 }}
          pagination={{ pageSize: 12, showSizeChanger: true }}
          dataSource={rows}
          locale={{
            emptyText: (
              <PanelEmpty
                title="还没有工作日志"
                hint="在对话里说清楚“今天做了什么、结论是什么、遇到什么问题、产出了什么”，助理会自动拆成四类记录写到这里；历史 Excel 也可以直接上传回填。"
              />
            ),
          }}
          columns={[
            {
              title: "日期",
              width: 150,
              render: (_, row) => (
                <EditableCell
                  editing={editing === row.record_id}
                  view={row.record_date}
                  edit={() => (
                    <DateField
                      allowClear={false}
                      placeholder="记录日期"
                      value={draft?.record_date || row.record_date}
                      onChange={(record_date) => setDraft((current) => current ? { ...current, record_date: record_date || current.record_date } : current)}
                    />
                  )}
                />
              ),
            },
            { title: "人员", dataIndex: "subject_name", width: 130, ellipsis: true },
            {
              title: "类型",
              width: 130,
              render: (_, row) => (
                <EditableCell
                  editing={editing === row.record_id}
                  view={<Tag>{KIND_LABELS[row.kind] || row.kind}</Tag>}
                  edit={() => (
                    <Select
                      value={draft?.kind}
                      options={KIND_OPTIONS}
                      onChange={(kind) => setDraft((current) => current ? { ...current, kind } : current)}
                    />
                  )}
                />
              ),
            },
            {
              title: "内容",
              width: 360,
              render: (_, row) => (
                <EditableCell
                  editing={editing === row.record_id}
                  view={<span className="daily-work-record-text">{row.text}</span>}
                  edit={() => (
                    <Input.TextArea
                      rows={3}
                      value={draft?.text}
                      onChange={(event) => setDraft((current) => current ? { ...current, text: event.target.value } : current)}
                    />
                  )}
                />
              ),
            },
            {
              title: "解决选项",
              width: 300,
              render: (_, row) => row.solution_options.length
                ? <ul className="daily-work-record-options">{row.solution_options.map((option, index) => <li key={`${row.record_id}-${index}`}>{option.text}</li>)}</ul>
                : <span className="daily-work-record-empty">无</span>,
            },
            {
              title: "来源",
              width: 120,
              render: (_, row) => row.source_origin === "historical_excel_backfill"
                ? <Tag color="default">历史回填</Tag>
                : <Tag color="processing">对话日报</Tag>,
            },
            {
              title: "操作",
              width: 150,
              fixed: "right" as const,
              render: (_, row) => editing === row.record_id ? (
                <Space>
                  <Button type="primary" loading={saving === row.record_id} onClick={() => save(row)}>确认</Button>
                  <Button disabled={saving === row.record_id} onClick={cancelEdit}>取消</Button>
                </Space>
              ) : row.can_edit ? <Button onClick={() => beginEdit(row)}>编辑</Button> : <span className="daily-work-record-empty">只读</span>,
            },
          ]}
        />
      </Card>
    </section>
  );
}
