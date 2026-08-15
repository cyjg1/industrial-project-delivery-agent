import { CheckOutlined, StopOutlined } from "@ant-design/icons";
import {
  App as AntdApp,
  Button,
  Popconfirm,
  Space,
  Table,
  Tag,
  Tooltip,
} from "antd";
import { useState } from "react";
import {
  approveEnvironmentCognition,
  approveExecutionPolicy,
  retireEnvironmentCognition,
  retireExecutionPolicy,
} from "../api/client";
import type {
  EnvironmentCognition,
  ExecutionPolicy,
  MemorySkillEpisode,
  Workspace,
} from "../types";
import "./KnowledgePipeline.css";

type GovernanceKind = "policy" | "cognition";
type GovernanceAction = "approve" | "retire";

type Props = {
  workspace: Workspace;
  onRefresh: () => Promise<void>;
};

export function MemorySkillEvolutionPanel({ workspace, onRefresh }: Props) {
  const { message } = AntdApp.useApp();
  const [actionKey, setActionKey] = useState("");
  const evolution = workspace.memory_skill_evolution;
  const summary = evolution.summary;
  const canManage = ["pm", "pmo"].includes(workspace.access.role);
  const governanceRows: Array<
    | ({ row_type: "policy"; row_id: string } & ExecutionPolicy)
    | ({ row_type: "cognition"; row_id: string } & EnvironmentCognition)
  > = [
    ...evolution.policies.map((row) => ({
      ...row,
      row_type: "policy" as const,
      row_id: row.policy_id,
    })),
    ...evolution.cognitions.map((row) => ({
      ...row,
      row_type: "cognition" as const,
      row_id: row.cognition_id,
    })),
  ];
  const metrics = [
    {
      key: "episode",
      label: "L1 运行经验",
      value: summary.episode_count,
      detail: `${summary.trace_count} 条可审计轨迹`,
    },
    {
      key: "policy",
      label: "L2 执行策略",
      value: summary.policy_candidate_count,
      detail: `${summary.policy_approved_count} 条已批准`,
    },
    {
      key: "cognition",
      label: "L3 环境认知",
      value: summary.cognition_candidate_count,
      detail: `${summary.cognition_approved_count} 条已批准 / ${summary.cognition_revalidation_count} 条待复核`,
    },
    {
      key: "active",
      label: "方法技能可靠性",
      value: summary.active_skill_count,
      detail: `${summary.probationary_skill_count} 个观察期`,
    },
  ];

  async function runAction(
    kind: GovernanceKind,
    id: string,
    action: GovernanceAction,
  ) {
    const key = `${kind}:${id}:${action}`;
    setActionKey(key);
    try {
      if (kind === "policy") {
        await (action === "approve"
          ? approveExecutionPolicy(id)
          : retireExecutionPolicy(id));
      } else {
        await (action === "approve"
          ? approveEnvironmentCognition(id)
          : retireEnvironmentCognition(id));
      }
      await onRefresh();
      message.success(action === "approve" ? "治理候选已批准" : "治理资产已停用");
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "治理操作失败");
    } finally {
      setActionKey("");
    }
  }

  return (
    <section className="knowledge-evolution" aria-label="智能体经验治理">
      <div className="knowledge-evolution-head">
        <div>
          <h3>智能体经验治理</h3>
          <p>运行经验与甲方业务方法分账管理；候选在人工批准前不会进入执行检索。</p>
        </div>
        <Tag color="processing">三阶段治理</Tag>
      </div>

      <div className="knowledge-evolution-metrics">
        {metrics.map((metric) => (
          <div key={metric.key} className="knowledge-evolution-metric">
            <span>{metric.label}</span>
            <strong>{metric.value}</strong>
            <small>{metric.detail}</small>
          </div>
        ))}
      </div>

      <div className="knowledge-evolution-grid">
        <div>
          <h4>最近运行记录</h4>
          <Table<MemorySkillEpisode>
            size="small"
            rowKey="episode_id"
            dataSource={evolution.recent_episodes.slice(0, 8)}
            pagination={false}
            locale={{ emptyText: "尚无运行经验" }}
            columns={[
              {
                title: "边界",
                dataIndex: "relation",
                width: 92,
                render: (value: string) => <Tag>{episodeRelationLabel(value)}</Tag>,
              },
              {
                title: "输入目标",
                key: "task",
                ellipsis: true,
                render: (_: unknown, row) => row.payload?.task_summary || row.run_id,
              },
              {
                title: "回报",
                dataIndex: "reward_value",
                width: 82,
                render: (value: number | null, row) => (
                  <Tooltip title={rewardSourceLabel(row.reward_source)}>
                    <span
                      className={
                        (value ?? 0) >= 0
                          ? "knowledge-value-positive"
                          : "knowledge-value-negative"
                      }
                    >
                      {value == null ? "待定" : value.toFixed(2)}
                    </span>
                  </Tooltip>
                ),
              },
              {
                title: "结果",
                dataIndex: "feedback_status",
                width: 92,
                render: (value: string) => (
                  <Tag
                    color={
                      value === "pending"
                        ? undefined
                        : value === "accepted"
                          ? "success"
                          : "warning"
                    }
                  >
                    {feedbackStatusLabel(value)}
                  </Tag>
                ),
              },
            ]}
          />
        </div>

        <div>
          <h4>L2/L3 治理候选</h4>
          <Table
            size="small"
            rowKey="row_id"
            dataSource={governanceRows}
            scroll={{ x: 680 }}
            pagination={
              governanceRows.length > 6
                ? { pageSize: 6, size: "small" }
                : false
            }
            locale={{ emptyText: "尚未达到跨运行记录归纳门槛" }}
            columns={[
              {
                title: "层级",
                dataIndex: "row_type",
                width: 76,
                render: (value: string) => (
                  <Tag color={value === "policy" ? "processing" : "purple"}>
                    {value === "policy" ? "L2" : "L3"}
                  </Tag>
                ),
              },
              {
                title: "归纳结果",
                key: "result",
                ellipsis: true,
                render: (_: unknown, row: (typeof governanceRows)[number]) =>
                  row.row_type === "policy"
                    ? row.trigger_text
                    : row.regularities[0] || row.entities.join("、"),
              },
              {
                title: "证据",
                key: "support",
                width: 92,
                render: (_: unknown, row: (typeof governanceRows)[number]) =>
                  row.row_type === "policy"
                    ? `${row.support_episode_ids.length} 次运行`
                    : `${row.support_policy_ids.length} 条策略`,
              },
              {
                title: "稳定度",
                key: "quality",
                width: 84,
                render: (_: unknown, row: (typeof governanceRows)[number]) =>
                  `${Math.round(
                    (row.row_type === "policy"
                      ? row.stability
                      : row.confidence) * 100,
                  )}%`,
              },
              {
                title: "状态",
                dataIndex: "status",
                width: 96,
                render: (value: string) => (
                  <Tag
                    color={
                      value === "approved"
                        ? "success"
                        : value === "retired"
                          ? "default"
                          : "warning"
                    }
                  >
                    {governanceStatusLabel(value)}
                  </Tag>
                ),
              },
              {
                title: "操作",
                key: "action",
                width: 144,
                render: (_: unknown, row: (typeof governanceRows)[number]) => (
                  <GovernanceActions
                    row={row}
                    canManage={canManage}
                    actionKey={actionKey}
                    onAction={(action) =>
                      void runAction(row.row_type, row.row_id, action)
                    }
                  />
                ),
              },
            ]}
          />
        </div>
      </div>

      {evolution.skill_reliability.length ? (
        <div className="knowledge-reliability">
          <h4>已发布项目方法技能的使用可靠性</h4>
          {evolution.skill_reliability.map((row) => (
            <div key={`${row.skill_id}:${row.version_id}`}>
              <strong>{row.name}</strong>
              <Tag
                color={
                  row.lifecycle === "active"
                    ? "success"
                    : row.lifecycle === "revalidation_required"
                      ? "warning"
                      : "default"
                }
              >
                {row.lifecycle === "active"
                  ? "活跃"
                  : row.lifecycle === "revalidation_required"
                    ? "待重验"
                    : "观察期"}
              </Tag>
              <span>{Math.round(row.reliability * 100)}%</span>
              <small>{row.episode_count} 个不同运行记录</small>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function GovernanceActions({
  row,
  canManage,
  actionKey,
  onAction,
}: {
  row:
    | ({ row_type: "policy"; row_id: string } & ExecutionPolicy)
    | ({ row_type: "cognition"; row_id: string } & EnvironmentCognition);
  canManage: boolean;
  actionKey: string;
  onAction: (action: GovernanceAction) => void;
}) {
  if (!canManage || row.status === "retired") {
    return <span className="muted-text">-</span>;
  }
  if (row.status === "approved") {
    return (
      <Popconfirm
        title="停用后不再进入检索，是否继续？"
        okText="停用"
        cancelText="取消"
        onConfirm={() => onAction("retire")}
      >
        <Button
          size="small"
          icon={<StopOutlined />}
          loading={actionKey === `${row.row_type}:${row.row_id}:retire`}
        >
          停用
        </Button>
      </Popconfirm>
    );
  }
  return (
    <Space size={4}>
      <Popconfirm
        title="批准后允许进入分层检索，是否继续？"
        okText="批准"
        cancelText="取消"
        onConfirm={() => onAction("approve")}
      >
        <Button
          size="small"
          type="primary"
          icon={<CheckOutlined />}
          loading={actionKey === `${row.row_type}:${row.row_id}:approve`}
        >
          批准
        </Button>
      </Popconfirm>
      <Popconfirm
        title="停用后保留审计记录，但不进入检索，是否继续？"
        okText="停用"
        cancelText="取消"
        onConfirm={() => onAction("retire")}
      >
        <Button
          size="small"
          icon={<StopOutlined />}
          loading={actionKey === `${row.row_type}:${row.row_id}:retire`}
        >
          停用
        </Button>
      </Popconfirm>
    </Space>
  );
}

function episodeRelationLabel(value: string) {
  return {
    new_task: "新任务",
    follow_up: "追问",
    correction: "纠正",
  }[value] || value;
}

function governanceStatusLabel(value: string) {
  return {
    candidate: "待批准",
    approved: "已批准",
    revalidation_required: "待重验",
    retired: "已停用",
  }[value] || value;
}

function feedbackStatusLabel(value: string) {
  return {
    pending: "待反馈",
    accepted: "已接受",
    corrected: "已纠正",
    rejected: "已驳回",
  }[value] || value;
}

function rewardSourceLabel(value: string) {
  return {
    pending: "待评估",
    human: "人工反馈",
    deterministic_verifier: "确定性校验",
    runtime_status: "运行状态",
  }[value] || value;
}
