import { EditOutlined, RedoOutlined } from "@ant-design/icons";
import { Button, Card, DatePicker, Descriptions, Form, Input, List, Modal, Popconfirm, Progress, Select, Space, Tag } from "antd";
import dayjs from "dayjs";
import { useState } from "react";
import { archiveTaskCandidate, confirmItem, publishItemAsTask, rejectItem, retryIngestionJob, updateCandidateItem } from "../api/client";
import { shortText } from "../lib/ui";
import type { ConfirmationCard, IngestionJob, Workspace } from "../types";
import "./ReviewQueue.css";
import { PanelEmpty } from "./panelKit";
import { useAsyncAction } from "./useAsyncAction";

type ReviewQueuePanelProps = {
  workspace: Workspace;
  onWorkspaceChange: (workspace: Workspace) => void;
  onRefresh: () => Promise<void>;
};

export function ReviewQueuePanel({ workspace, onWorkspaceChange, onRefresh }: ReviewQueuePanelProps) {
  const cards = workspace.confirmation_cards;
  const canRetry = ["pm", "pmo"].includes(workspace.access.role);
  const ownerOptions = workspace.people_workspace.people
    .filter((person, index, people) => people.findIndex((candidate) => candidate.name === person.name) === index)
    .map((person) => ({
      value: person.name,
      label: [person.name, person.group, person.role].filter(Boolean).join(" · "),
    }));
  return (
    <section className="panel-stack" aria-label="审核队列">
      <IngestionStatusBand jobs={workspace.inputs.ingestion_jobs} canRetry={canRetry} onRefresh={onRefresh} />
      <Card size="small" title="审核队列">
        <Space wrap>
          <Tag color={cards.length ? "processing" : undefined}>待确认 {cards.length}</Tag>
          <Tag>候选 {workspace.memory.candidate_count}</Tag>
          <Tag>已确认 {workspace.memory.confirmed_count}</Tag>
          <Tag>已驳回 {workspace.memory.rejected_count}</Tag>
        </Space>
      </Card>
      {cards.length ? (
        <List
          className="review-list"
          dataSource={cards}
          renderItem={(card) => (
            <ReviewItem
              card={card}
              ownerOptions={ownerOptions}
              onWorkspaceChange={onWorkspaceChange}
              onRefresh={onRefresh}
            />
          )}
        />
      ) : (
        <Card size="small">
          <PanelEmpty
            title="没有待确认的候选"
            hint="在对话中上传会议纪要或原始转写，助理抽取出的人、事、法会先落在这里；确认之后才会写入三清单。"
          />
        </Card>
      )}
    </section>
  );
}

function IngestionStatusBand({
  jobs,
  canRetry,
  onRefresh,
}: {
  jobs: IngestionJob[];
  canRetry: boolean;
  onRefresh: () => Promise<void>;
}) {
  const visibleJobs = jobs.slice(0, 6);
  const retry = useAsyncAction(
    async (job: IngestionJob) => {
      await retryIngestionJob(job.id);
      await onRefresh();
    },
    { errorText: "入库作业重试失败", successText: "已重新排队", key: (job) => job.id },
  );
  if (!visibleJobs.length) return null;
  const activeCount = jobs.filter((job) => job.status === "queued" || job.status === "running").length;

  return (
    <section className="ingestion-status-band" aria-label="资料入库进度" aria-live="polite">
      <header>
        <strong>资料入库</strong>
        <span>{activeCount ? `${activeCount} 个处理中` : "最近作业"}</span>
      </header>
      <div className="ingestion-job-list">
        {visibleJobs.map((job) => (
          <div className="ingestion-job-row" key={job.id}>
            <div className="ingestion-job-main">
              <div className="ingestion-job-title">
                <strong>{job.source_title || job.source_id}</strong>
                <Tag color={jobStatusColor(job.status)}>{jobStatusLabel(job.status)}</Tag>
                <span>{inputKindLabel(job.input_kind)}</span>
              </div>
              <Progress
                percent={job.progress_percent}
                size="small"
                status={job.status === "failed" ? "exception" : job.status === "completed" ? "success" : "active"}
              />
              <div className="ingestion-job-meta">
                <span>{job.stage}</span>
                <span>{job.processed_chunks}/{job.total_chunks || "-"} 块</span>
                <span>
                  新增 {job.delta_new} · 更新 {job.delta_updated} · 冲突 {job.delta_conflict} ·
                  关闭 {job.delta_resolved} · 证据合并 {job.delta_auto_merged}
                </span>
              </div>
              {job.import_summary ? <div className="ingestion-job-summary">{job.import_summary}</div> : null}
              {job.error ? <div className="ingestion-error">{job.error}</div> : null}
            </div>
            {canRetry && (job.status === "failed" || job.status === "interrupted") && job.attempts < job.max_attempts ? (
              <Button
                type="text"
                icon={<RedoOutlined />}
                loading={retry.isPending(job.id)}
                onClick={() => void retry.run(job)}
                aria-label={`重试 ${job.source_id}`}
              >
                重试
              </Button>
            ) : null}
          </div>
        ))}
      </div>
    </section>
  );
}

function jobStatusLabel(status: string) {
  return ({ queued: "排队中", running: "处理中", completed: "已完成", failed: "失败", interrupted: "已中断" } as Record<string, string>)[status] || status;
}

function jobStatusColor(status: string) {
  return ({ queued: "default", running: "processing", completed: "success", failed: "error", interrupted: "warning" } as Record<string, string>)[status] || "default";
}

function inputKindLabel(kind: string) {
  return ({
    auto: "自动识别",
    minutes: "会议纪要",
    transcript: "原始转写",
    three_list_tasks: "任务清单",
    three_list_issues: "问题清单",
    work_logs: "工作日志",
  } as Record<string, string>)[kind] || kind;
}

function ReviewItem({
  card,
  ownerOptions,
  onWorkspaceChange,
  onRefresh,
}: {
  card: ConfirmationCard;
  ownerOptions: Array<{ value: string; label: string }>;
  onWorkspaceChange: (workspace: Workspace) => void;
  onRefresh: () => Promise<void>;
}) {
  const [notes, setNotes] = useState("");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(() => taskDraftFromCard(card));
  const isTaskCandidate = ["task", "tasks", "followup"].includes(card.category);

  const decide = useAsyncAction(
    async (action: "confirm" | "reject", note: string) => {
      if (action === "confirm") {
        await confirmItem(card.storage_item_id, note || "确认该候选");
      } else {
        await rejectItem(card.storage_item_id, note || "驳回该候选");
      }
      await onRefresh();
    },
    { errorText: "候选处理失败", key: (action) => action },
  );

  const publish = useAsyncAction(
    async () => {
      onWorkspaceChange(await publishItemAsTask(card.storage_item_id));
    },
    { errorText: "发布为任务失败", successText: "已发布为任务" },
  );

  const archive = useAsyncAction(
    async () => {
      onWorkspaceChange(await archiveTaskCandidate(card.storage_item_id));
    },
    { errorText: "删除任务失败", successText: "已删除候选任务" },
  );

  const editAndPublish = useAsyncAction(
    async () => {
      await updateCandidateItem(card.storage_item_id, {
        title: draft.title.trim(),
        description: draft.description.trim(),
        deliverable: draft.deliverable.trim(),
        acceptance_criteria: draft.acceptance_criteria.trim(),
        due_date: draft.due_date.trim(),
        owner_candidates: draft.owners,
        notes: "人工编辑待确认任务",
      });
      const published = await publishItemAsTask(card.storage_item_id);
      onWorkspaceChange(published);
      setEditing(false);
    },
    { errorText: "编辑并发布任务失败", successText: "任务已发布" },
  );

  const openEditor = () => {
    setDraft(taskDraftFromCard(card));
    setEditing(true);
  };

  const confirmationField = (label: string) => card.confirmation_fields.includes(label);
  const fieldLabel = (label: string) => (
    <span className="review-edit-field-label">
      {label}
      {confirmationField(label) ? <em>待确认</em> : null}
    </span>
  );
  const fieldClassName = (label: string) => confirmationField(label) ? "review-field-needs-confirmation" : undefined;

  return (
    <List.Item>
      <Card
        size="small"
        className={`review-card${isTaskCandidate ? " review-task-card" : ""}`}
        title={card.title}
        extra={<Tag color={isTaskCandidate ? "processing" : undefined}>{isTaskCandidate ? "待确认任务" : card.section}</Tag>}
      >
        {isTaskCandidate ? (
          <>
            <Descriptions className="review-task-details" bordered size="small" column={1}>
              <Descriptions.Item label="任务事项">{card.title || "待补充"}</Descriptions.Item>
              <Descriptions.Item label="任务说明">{card.agent_understanding || "待补充"}</Descriptions.Item>
              <Descriptions.Item label="交付物">{card.task_contract.deliverable || "待补充"}</Descriptions.Item>
              <Descriptions.Item label="验收口径">{card.task_contract.acceptance_criteria || "待补充"}</Descriptions.Item>
              <Descriptions.Item label="截止时间">{card.task_contract.due_date || "待补充"}</Descriptions.Item>
              <Descriptions.Item label="负责人">{card.owner_text || card.owner_candidates.join("、") || "待补充"}</Descriptions.Item>
            </Descriptions>
          </>
        ) : (
          <>
            <p className="review-understanding">{shortText(card.agent_understanding || card.inference_note, 180)}</p>
            <Space size={[6, 6]} wrap>
              <Tag>{card.section}</Tag>
              {card.matter_type ? <Tag>{card.matter_type}</Tag> : null}
              <Tag>责任人：{card.owner_text || card.owner_candidates.join("、") || "待确认"}</Tag>
            </Space>
          </>
        )}
        {!isTaskCandidate ? (
          <Input.TextArea
            rows={2}
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
            placeholder="补充确认或驳回原因（可选）"
          />
        ) : null}
        <Space className="review-actions" size={[12, 12]} wrap>
          {isTaskCandidate ? (
            <>
              <Button icon={<EditOutlined />} onClick={openEditor}>编辑任务</Button>
              <Button type="primary" loading={publish.pending} onClick={() => void publish.run()}>
                发布为任务
              </Button>
              <Popconfirm
                title="删除这条候选任务？"
                description="删除后不形成正式任务，并从待确认列表移除。"
                okText="删除任务"
                cancelText="取消"
                okButtonProps={{ danger: true }}
                onConfirm={() => void archive.run()}
              >
                <Button danger loading={archive.pending}>删除任务</Button>
              </Popconfirm>
            </>
          ) : (
            <>
              <Button type="primary" loading={decide.isPending("confirm")} onClick={() => void decide.run("confirm", notes)}>
                确认
              </Button>
              <Button loading={decide.isPending("reject")} onClick={() => void decide.run("reject", notes)}>
                驳回
              </Button>
            </>
          )}
        </Space>
        <Modal
          className="review-task-edit-modal"
          title="编辑并发布待确认任务"
          open={editing}
          width={720}
          style={{ top: 20 }}
          okText="发布任务"
          cancelText="取消"
          confirmLoading={editAndPublish.pending}
          okButtonProps={{ disabled: !draft.title.trim() }}
          onOk={() => void editAndPublish.run()}
          onCancel={() => setEditing(false)}
          destroyOnHidden
        >
          <Form className="review-task-edit-form" layout="vertical">
            <Form.Item className={fieldClassName("任务事项")} label={fieldLabel("任务事项")} required>
              <Input value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} />
            </Form.Item>
            <Form.Item className={fieldClassName("任务说明")} label={fieldLabel("任务说明")}>
              <Input.TextArea rows={2} value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} />
            </Form.Item>
            <Form.Item className={fieldClassName("交付物")} label={fieldLabel("交付物")}>
              <Input.TextArea rows={2} value={draft.deliverable} onChange={(event) => setDraft({ ...draft, deliverable: event.target.value })} />
            </Form.Item>
            <Form.Item className={fieldClassName("验收口径")} label={fieldLabel("验收口径")}>
              <Input.TextArea rows={2} value={draft.acceptance_criteria} onChange={(event) => setDraft({ ...draft, acceptance_criteria: event.target.value })} />
            </Form.Item>
            <Form.Item className={fieldClassName("截止时间")} label={fieldLabel("截止时间")}>
              <DatePicker
                className="review-due-date-picker"
                format="YYYY-MM-DD"
                placeholder="选择截止日期"
                value={draft.due_date && dayjs(draft.due_date).isValid() ? dayjs(draft.due_date) : null}
                onChange={(_, dateString) => setDraft({ ...draft, due_date: String(dateString) })}
              />
            </Form.Item>
            <Form.Item className={fieldClassName("负责人")} label={fieldLabel("负责人")} extra="支持搜索并选择多位项目成员">
              <Select
                className="review-owner-select"
                mode="multiple"
                showSearch
                optionFilterProp="label"
                placeholder="搜索并选择负责人"
                options={ownerOptions}
                value={draft.owners}
                onChange={(owners) => setDraft({ ...draft, owners })}
              />
            </Form.Item>
          </Form>
        </Modal>
      </Card>
    </List.Item>
  );
}

type TaskDraft = {
  title: string;
  description: string;
  deliverable: string;
  acceptance_criteria: string;
  due_date: string;
  owners: string[];
};

function taskDraftFromCard(card: ConfirmationCard): TaskDraft {
  return {
    title: card.title || "",
    description: card.agent_understanding || "",
    deliverable: card.task_contract.deliverable || "",
    acceptance_criteria: card.task_contract.acceptance_criteria || "",
    due_date: card.task_contract.due_date || "",
    owners: [...card.owner_candidates],
  };
}
