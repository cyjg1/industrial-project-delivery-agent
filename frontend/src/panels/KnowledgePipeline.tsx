import {
  BookOutlined,
  BulbOutlined,
  CheckCircleOutlined,
  CheckOutlined,
  ExperimentOutlined,
  FileTextOutlined,
  RocketOutlined,
  ThunderboltOutlined,
  ToolOutlined,
} from "@ant-design/icons";
import { Alert, App as AntdApp, Button, Card, Descriptions, Empty, Skeleton, Table, Tag, Tooltip, Typography } from "antd";
import { useEffect, useMemo, useState } from "react";
import {
  compileProjectSkill,
  confirmItem,
  loadSkillPackages,
  publishProjectSkill,
  testProjectSkill,
  type ContractSkill,
  type SkillPackageSummary,
} from "../api/client";
import type { LinkedMethodRow, Workspace } from "../types";
import "./KnowledgePipeline.css";
import { MemorySkillEvolutionPanel } from "./MemorySkillEvolutionPanel";

const MATURITY_META: Record<string, { label: string; tone: "default" | "processing" | "warning" | "success" }> = {
  memory: { label: "记忆卡", tone: "default" },
  candidate: { label: "技能候选", tone: "processing" },
  tested: { label: "已测试", tone: "warning" },
  published: { label: "已发布", tone: "success" },
  revalidation_required: { label: "待重新验证", tone: "warning" },
};

const MATURITY_ORDER = ["memory", "candidate", "tested", "published", "revalidation_required"];

const GATE_LABELS: Record<string, string> = {
  confirmed_method: "方法尚未经过人工确认",
  independent_evidence: "至少需要两份相互独立的来源证据",
  raw_evidence: "至少两份独立来源必须可追溯到原始转写",
  business_goal: "缺少明确的业务目标",
  principles: "至少需要两条判断原则",
  reasoning_chain: "至少需要两步推理链",
  applicable_scope: "缺少适用范围",
  package_name: "编译后的技能缺少名称",
  package_description: "编译后的技能缺少说明",
  package_when_to_use: "编译后的技能缺少触发条件",
  when_not_to_use: "至少需要一条不应触发的边界",
  execution_steps: "至少需要两步带可验证完成条件的执行步骤",
  stop_conditions: "至少需要一条明确停止条件",
  boundaries: "至少需要一条权限或适用边界",
  anti_patterns: "至少需要一条应避免的空泛或不安全做法",
  registered_tools: "技能引用了统一工具注册表之外的工具",
  pressure_tests: "需要有效的正向和反向压力测试",
};

function formatGateFailure(value: string) {
  const code = value.split(":", 1)[0]?.trim() || "";
  return GATE_LABELS[code] || "存在尚未满足的技能发布条件";
}

function packageStatusMeta(status: string) {
  if (status === "loaded") return { label: "已加载", color: "success" as const };
  if (status === "active") return { label: "已启用", color: "success" as const };
  if (status === "published") return { label: "已发布", color: "success" as const };
  if (status === "error") return { label: "加载失败", color: "error" as const };
  return { label: status || "待处理", color: "processing" as const };
}

type KnowledgePipelinePanelProps = {
  workspace: Workspace;
  onWorkspaceChange: (workspace: Workspace) => void;
  onRefresh: () => Promise<void>;
};

export function KnowledgePipelinePanel({
  workspace,
  onWorkspaceChange,
  onRefresh,
}: KnowledgePipelinePanelProps) {
  const { message } = AntdApp.useApp();
  const [packages, setPackages] = useState<SkillPackageSummary[]>([]);
  const [contractSkills, setContractSkills] = useState<ContractSkill[]>([]);
  const [skillsDir, setSkillsDir] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionKey, setActionKey] = useState("");
  const [packageRevision, setPackageRevision] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    loadSkillPackages()
      .then((payload) => {
        if (cancelled) return;
        setPackages(payload.packages);
        setContractSkills(payload.contract_skills);
        setSkillsDir(payload.external_skills_dir);
        setError("");
      })
      .catch((reason) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "技能库加载失败");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [packageRevision]);

  const inputs = workspace.inputs;
  const memory = workspace.memory;
  const threeLists = workspace.three_lists;
  const methods = threeLists?.methods ?? [];

  const maturityGroups = useMemo(() => {
    const groups = new Map<string, LinkedMethodRow[]>();
    methods.forEach((row) => {
      const key = MATURITY_ORDER.includes(row.skill_maturity) ? row.skill_maturity : "memory";
      const bucket = groups.get(key);
      if (bucket) bucket.push(row);
      else groups.set(key, [row]);
    });
    return groups;
  }, [methods]);

  const publishedCount = maturityGroups.get("published")?.length ?? 0;
  const activeContractCount = contractSkills.filter((skill) => skill.status === "active").length;
  const externalPackageCount = packages.filter((item) => item.origin === "external").length;
  const projectPackageCount = packages.filter((item) => item.origin === "project").length;

  async function runMethodAction(
    row: LinkedMethodRow,
    action: "confirm" | "compile" | "test" | "publish",
  ) {
    const key = `${row.method_id}:${action}`;
    setActionKey(key);
    try {
      if (action === "confirm") {
        await confirmItem(row.method_id, "在知识沉淀工作台确认方法候选");
        await onRefresh();
      } else {
        const next = action === "compile"
          ? await compileProjectSkill(row.method_id)
          : action === "test"
            ? await testProjectSkill(row.skill_id)
            : await publishProjectSkill(row.skill_id);
        onWorkspaceChange(next);
      }
      setPackageRevision((value) => value + 1);
      message.success({
        confirm: "方法已确认，现已进入证据门禁",
        compile: "技能候选已编译",
        test: "压力测试已完成",
        publish: "技能已发布",
      }[action]);
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "方法操作失败");
    } finally {
      setActionKey("");
    }
  }

  const stages = [
    {
      key: "input",
      icon: <FileTextOutlined />,
      title: "输入",
      caption: "会议纪要 · 原始转写 · 讨论",
      value: inputs?.source_counts?.total ?? 0,
      unit: "份材料",
      detail: `已入库 ${inputs?.source_counts?.curated_ready ?? 0} · 待处理 ${inputs?.source_counts?.raw_pending ?? 0}`,
    },
    {
      key: "candidate",
      icon: <BulbOutlined />,
      title: "理解",
      caption: "模型抽取为候选，等待人工确认",
      value: memory?.candidate_count ?? 0,
      unit: "条候选",
      detail: `待确认卡片 ${workspace.confirmation_cards?.length ?? 0} 条`,
      tone: "processing" as const,
    },
    {
      key: "confirmed",
      icon: <CheckCircleOutlined />,
      title: "沉淀",
      caption: "人工确认后成为项目事实",
      value: memory?.confirmed_count ?? 0,
      unit: "条已确认",
      detail: `问题 ${threeLists?.summary?.issue_count ?? 0} · 任务 ${threeLists?.summary?.task_count ?? 0} · 方法 ${threeLists?.summary?.method_count ?? 0}`,
      tone: "success" as const,
    },
    {
      key: "skill",
      icon: <ThunderboltOutlined />,
      title: "复用",
      caption: "已验证的方法晋级为可执行项目技能",
      value: publishedCount + packages.length + activeContractCount,
      unit: "个可用能力",
      detail: `方法晋级 ${publishedCount} · 项目技能 ${projectPackageCount} · 外部导入 ${externalPackageCount} · 内置 ${activeContractCount}`,
      tone: "success" as const,
    },
  ];

  const methodColumns = [
    {
      title: "方法",
      dataIndex: "overview",
      key: "overview",
      render: (value: string, row: LinkedMethodRow) => (
        <div className="knowledge-method-cell">
          <strong>{value || row.method_id}</strong>
          {row.applicable_scope ? <small>适用范围：{row.applicable_scope}</small> : null}
        </div>
      ),
    },
    {
      title: "成熟度",
      dataIndex: "skill_maturity",
      key: "skill_maturity",
      width: 132,
      render: (value: string) => {
        const meta = MATURITY_META[value] || MATURITY_META.memory;
        return <Tag color={meta.tone === "default" ? undefined : meta.tone}>{meta.label}</Tag>;
      },
    },
    {
      title: "证据",
      dataIndex: "evidence_count",
      key: "evidence_count",
      width: 96,
      render: (value: number) => (
        <Tooltip title="晋级为项目技能至少需要两个不同来源的证据">
          <span className={value >= 2 ? "knowledge-evidence-ok" : "knowledge-evidence-weak"}>{value} 条</span>
        </Tooltip>
      ),
    },
    {
      title: "未通过的门禁",
      dataIndex: "skill_gate_failures",
      key: "skill_gate_failures",
      render: (value: string[]) =>
        value?.length ? (
          <span className="knowledge-gate-failures">{value.map(formatGateFailure).join("；")}</span>
        ) : (
          <span className="muted-text">—</span>
        ),
    },
    {
      title: "下一步",
      key: "action",
      width: 148,
      render: (_: unknown, row: LinkedMethodRow) => (
        <MethodAction
          row={row}
          loading={actionKey.startsWith(`${row.method_id}:`)}
          canManage={["pm", "pmo"].includes(workspace.access.role)}
          onRun={(action) => void runMethodAction(row, action)}
        />
      ),
    },
  ];

  return (
    <section className="panel-stack" aria-label="知识沉淀">
      <section className="knowledge-flow-section" aria-label="知识链路">
        <div className="knowledge-flow-intro">
          <h3>知识链路</h3>
        </div>
        <ol className="knowledge-flow">
          {stages.map((stage, index) => (
            <li key={stage.key} className={`knowledge-stage knowledge-stage-${stage.tone || "default"}`}>
              <span className="knowledge-stage-index">{index + 1}</span>
              <div className="knowledge-stage-head">
                {stage.icon}
                <strong>{stage.title}</strong>
              </div>
              <div className="knowledge-stage-value">
                <b>{stage.value}</b>
                <span>{stage.unit}</span>
              </div>
              <p className="knowledge-stage-caption">{stage.caption}</p>
              <p className="knowledge-stage-detail">{stage.detail}</p>
            </li>
          ))}
        </ol>
      </section>

      <MemorySkillEvolutionPanel
        workspace={workspace}
        onRefresh={onRefresh}
      />

      <Card
        size="small"
        title="方法资产"
        extra={<span className="muted-text">共 {methods.length} 条 · 已发布 {publishedCount} 个技能</span>}
      >
        {methods.length ? (
          <Table<LinkedMethodRow>
            className="knowledge-method-table"
            size="middle"
            rowKey={(row) => row.method_id}
            dataSource={methods}
            columns={methodColumns}
            pagination={methods.length > 8 ? { pageSize: 8, size: "small" } : false}
            expandable={{
              expandedRowRender: (row) => <MethodEvidenceDetail row={row} />,
              rowExpandable: () => true,
            }}
          />
        ) : (
          <Empty
            description={
              <span>
                还没有沉淀方法。
                <br />
                在对话中上传会议纪要，确认抽取出的「法」，它们会出现在这里。
              </span>
            }
          />
        )}
      </Card>

      <Card
        size="small"
        title={
          <span>
            <BookOutlined /> 技能库
          </span>
        }
        extra={skillsDir ? <Tooltip title={skillsDir}><span className="muted-text">外部技能目录</span></Tooltip> : null}
      >
        {loading ? (
          <Skeleton active paragraph={{ rows: 3 }} />
        ) : error ? (
          <Alert type="error" showIcon message="技能库加载失败" description={error} />
        ) : (
          <div className="knowledge-skill-groups">
            <div>
              <h4>项目与外部技能</h4>
              <p className="muted-text">
                项目 {projectPackageCount} 个 · 外部 {externalPackageCount} 个，格式错误会单独列出。
              </p>
              {packages.length ? (
                <ul className="knowledge-skill-list">
                  {packages.map((item) => {
                    const statusMeta = packageStatusMeta(item.status);
                    return (
                      <li key={`${item.origin}:${item.name}`} className="knowledge-skill-item">
                        <div className="knowledge-skill-head">
                          <strong>{item.name}</strong>
                          <Tag>{item.origin === "project" ? "项目" : "外部"}</Tag>
                          <Tag color={statusMeta.color}>{statusMeta.label}</Tag>
                          <span className="muted-text">v{item.version}</span>
                        </div>
                        <p>{item.description}</p>
                        {item.error ? <Alert type="warning" showIcon message={item.error} /> : null}
                        {item.allowed_tools.length ? (
                          <div className="knowledge-skill-tools">
                            {item.allowed_tools.map((tool) => (
                              <Tag key={tool}>{tool}</Tag>
                            ))}
                          </div>
                        ) : null}
                      </li>
                    );
                  })}
                </ul>
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚无项目或外部技能" />
              )}
            </div>

            <div>
              <h4>内置能力</h4>
              <p className="muted-text">已接入 {activeContractCount} / {contractSkills.length}</p>
              <ul className="knowledge-contract-list">
                {contractSkills.map((skill) => (
                  <li key={skill.capability_id}>
                    <Tooltip title={skill.when_to_use}>
                      <span className="knowledge-contract-name">
                        <code>{skill.capability_id}</code> {skill.description}
                      </span>
                    </Tooltip>
                    <Tag color={skill.status === "active" ? "success" : undefined}>
                      {skill.status === "active" ? "已接入" : "契约骨架"}
                    </Tag>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}
      </Card>
    </section>
  );
}

export { MATURITY_META };

function MethodAction({
  row,
  loading,
  canManage,
  onRun,
}: {
  row: LinkedMethodRow;
  loading: boolean;
  canManage: boolean;
  onRun: (action: "confirm" | "compile" | "test" | "publish") => void;
}) {
  if (!canManage) {
    return <span className="muted-text">仅项目管理角色可操作</span>;
  }
  if (row.status !== "confirmed") {
    return (
      <Tooltip title="人工确认后才会成为项目方法事实">
        <Button
          size="small"
          type="primary"
          icon={<CheckOutlined />}
          loading={loading}
          onClick={() => onRun("confirm")}
        >
          确认方法
        </Button>
      </Tooltip>
    );
  }
  if (!row.skill_id) {
    const blocked = row.skill_gate_failures.length > 0;
    return (
      <Tooltip title={blocked ? row.skill_gate_failures.map(formatGateFailure).join("；") : "将已确认方法编译为可测试技能"}>
        <span>
          <Button
            size="small"
            icon={<ToolOutlined />}
            disabled={blocked}
            loading={loading}
            onClick={() => onRun("compile")}
          >
            编译
          </Button>
        </span>
      </Tooltip>
    );
  }
  if (["candidate", "revalidation_required", "memory"].includes(row.skill_maturity)) {
    return (
      <Tooltip title="用正例和负例检查触发条件、步骤与停止边界">
        <Button
          size="small"
          icon={<ExperimentOutlined />}
          loading={loading}
          onClick={() => onRun("test")}
        >
          压力测试
        </Button>
      </Tooltip>
    );
  }
  if (row.skill_maturity === "tested") {
    return (
      <Tooltip title="发布后才允许 Agent 在项目内复用">
        <Button
          size="small"
          type="primary"
          icon={<RocketOutlined />}
          loading={loading}
          onClick={() => onRun("publish")}
        >
          发布
        </Button>
      </Tooltip>
    );
  }
  return <Tag color="success">已发布</Tag>;
}

function MethodEvidenceDetail({ row }: { row: LinkedMethodRow }) {
  return (
    <div className="knowledge-method-detail">
      <Descriptions column={1} size="small" bordered>
        <Descriptions.Item label="业务目标">{row.business_goal || "待补充"}</Descriptions.Item>
        <Descriptions.Item label="原则">
          {row.principles.length ? row.principles.join("；") : "待补充"}
        </Descriptions.Item>
        <Descriptions.Item label="推理链">
          {row.reasoning_chain.length
            ? row.reasoning_chain.map((value, index) => `${index + 1}. ${value}`).join("\n")
            : "待补充"}
        </Descriptions.Item>
        <Descriptions.Item label="适用范围">{row.applicable_scope || "待补充"}</Descriptions.Item>
        <Descriptions.Item label="会议直接观察">
          {row.observed_fields.length ? row.observed_fields.join("、") : "未标注"}
        </Descriptions.Item>
        <Descriptions.Item label="模型推断建议">
          {row.proposed_fields.length ? row.proposed_fields.join("、") : "无"}
        </Descriptions.Item>
        <Descriptions.Item label="推断依据">
          {row.inference_basis.length ? row.inference_basis.join("；") : "无"}
        </Descriptions.Item>
        <Descriptions.Item label="推断置信度">
          {inferenceConfidenceLabel(row.inference_confidence)}
        </Descriptions.Item>
      </Descriptions>
      <div className="knowledge-evidence-list">
        <Typography.Title level={5}>证据链</Typography.Title>
        {row.evidence_refs.length ? row.evidence_refs.map((evidence) => (
          <section className="knowledge-evidence-row" key={`${row.method_id}:${evidence.source_doc_id}:${evidence.locator}`}>
            <div className="knowledge-evidence-heading">
              <strong>{evidence.meeting_title || evidence.source_doc_id}</strong>
              {evidence.meeting_date ? <span>{evidence.meeting_date}</span> : null}
              <Tag color={evidence.evidence_level === "raw_traceable" ? "success" : "warning"}>
                {evidence.evidence_level === "raw_traceable" ? "原始转写可追溯" : "待补原始转写"}
              </Tag>
            </div>
            <blockquote>{evidence.quote || "未记录证据摘录"}</blockquote>
            <p>
              纪要定位：{evidence.locator || "未记录"} · 原始定位：{evidence.raw_locator || "未记录"}
            </p>
            <p className="muted-text">
              原始来源：{evidence.raw_source || "未登记"}
            </p>
          </section>
        )) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有证据引用" />}
      </div>
    </div>
  );
}

function inferenceConfidenceLabel(value: string) {
  return ({ low: "低", medium: "中", high: "高" } as Record<string, string>)[value] || value || "未标注";
}
