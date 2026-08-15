import { SunOutlined, UserSwitchOutlined } from "@ant-design/icons";
import { Button, Select, Tooltip } from "antd";
import type { StreamConnectionState } from "../api/client";
import { isStaticDemo } from "../api/client";
import { MessageList } from "../chat/MessageList";
import { ProviderSettingsButton } from "../chat/ProviderSettingsButton";
import { QuickStartCards } from "../chat/QuickStartCards";
import { SenderBar } from "../chat/SenderBar";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { KnowledgePipelinePanel } from "../panels/KnowledgePipeline";
import { ProjectCalendarPanel } from "../panels/ProjectCalendar";
import { ProgressDashboardPanel } from "../panels/ProgressDashboard";
import { PeoplePanel } from "../panels/People";
import { ReviewQueuePanel } from "../panels/ReviewQueue";
import { TaskListPanel } from "../panels/TaskList";
import { ThreeListsPanel } from "../panels/ThreeLists";
import { ProjectFilesPanel } from "../panels/ProjectFiles";
import type { ActiveView, ChatMessage } from "../lib/ui";
import type { IngestionInputKind, SwitchableUser, Workspace } from "../types";

type MainPaneProps = {
  activeView: ActiveView;
  workspace: Workspace;
  messages: ChatMessage[];
  selectedFiles: File[];
  attachmentInputKind: IngestionInputKind;
  selectedModel: string;
  modelOptions: Array<{ label: string; value: string }>;
  loading: boolean;
  connectionState: StreamConnectionState | null;
  error: string;
  currentActorId: string;
  switchableUsers: SwitchableUser[];
  actorSwitchDisabled: boolean;
  onViewChange: (view: ActiveView) => void;
  onActorChange: (actorId: string) => void;
  onModelChange: (model: string) => void;
  onProviderConfigured: () => Promise<void>;
  onSend: (message: string, model: string) => Promise<void>;
  onFilesChange: (files: File[]) => void;
  onAttachmentInputKindChange: (value: IngestionInputKind) => void;
  onWorkspaceChange: (workspace: Workspace) => void;
  onRefresh: () => Promise<void>;
  onGenerateBrief: () => Promise<void>;
};

type ActorOption = {
  value: string;
  label: string;
  name: string;
  roleLabel: string;
  topics: string;
};

export function MainPane({
  activeView,
  workspace,
  messages,
  selectedFiles,
  attachmentInputKind,
  selectedModel,
  modelOptions,
  loading,
  connectionState,
  error,
  currentActorId,
  switchableUsers,
  actorSwitchDisabled,
  onViewChange,
  onActorChange,
  onModelChange,
  onProviderConfigured,
  onSend,
  onFilesChange,
  onAttachmentInputKindChange,
  onWorkspaceChange,
  onRefresh,
  onGenerateBrief,
}: MainPaneProps) {
  const title = workspace.milestone_control.plan.project || workspace.milestone.name;
  const canViewPeople = Boolean(workspace.access.capabilities.cross_person_load);
  const canReview = Boolean(workspace.access.capabilities.review_queue);
  const canGenerateBrief = Boolean(workspace.access.capabilities.generate_management_brief);
  const canViewProgressDashboard = Boolean(workspace.access.capabilities.progress_dashboard);
  const roleProfile = workspace.access.role_profile;
  const actorOptions: ActorOption[] = switchableUsers.map(toActorOption);
  const matchedActor = actorOptions.find((item) => item.value === currentActorId);
  // 当前视角可能不在可切换名单里（例如演示账号），此时用工作区自带的身份兜底，不要暴露原始 ID。
  const fallbackActor = matchedActor ? null : fallbackActorOption(workspace, currentActorId);
  const selectOptions = fallbackActor ? [fallbackActor, ...actorOptions] : actorOptions;
  const currentActor = matchedActor || fallbackActor;
  const mentionNames = Array.from(new Set(switchableUsers.map((user) => user.name).filter(Boolean)));
  const statusText = [dailyBriefStatusText(workspace), dailyJournalStatusText(workspace)]
    .filter(Boolean)
    .join(" · ");
  const effectiveView = resolveView(activeView, { canViewPeople, canReview, canViewProgressDashboard });
  const showQuickStart = effectiveView === "chat" && !messages.some((message) => message.role === "user");

  return (
    <main className="main-pane">
      <header className="main-header">
        <div className="main-header-identity">
          <h1>{title}</h1>
          <span className="main-header-subtitle">
            {workspace.milestone.scenario} · {workspace.milestone.chain}
          </span>
        </div>
        <div className="main-header-actions">
          {!isStaticDemo ? <ProviderSettingsButton disabled={loading} onConfigured={onProviderConfigured} /> : null}
          {canGenerateBrief ? (
            <Button type="primary" icon={<SunOutlined />} onClick={onGenerateBrief} loading={loading} disabled={isStaticDemo}>
              生成晨报
            </Button>
          ) : null}
          <div className="viewer-switcher">
            <UserSwitchOutlined className="viewer-switcher-icon" />
            <span className="viewer-switcher-label">视角</span>
            <Select
              className="viewer-switcher-select"
              variant="borderless"
              value={currentActorId}
              options={selectOptions}
              optionFilterProp="label"
              showSearch
              placeholder="选择演示视角"
              popupMatchSelectWidth={320}
              onChange={onActorChange}
              disabled={actorSwitchDisabled}
              aria-label="切换演示视角"
              labelRender={() => (
                <span className="viewer-switcher-value">
                  {currentActor ? `${currentActor.name} · ${currentActor.roleLabel}` : "选择演示视角"}
                </span>
              )}
              optionRender={(option) => {
                const item = selectOptions.find((entry) => entry.value === option.value);
                if (!item) return option.label;
                return (
                  <div className="viewer-option">
                    <strong>{item.name} · {item.roleLabel}</strong>
                    <small>{item.topics || "未分配专题"}</small>
                  </div>
                );
              }}
            />
          </div>
        </div>
      </header>

      <div className="main-status-line">
        <Tooltip title={statusText}><span>{isStaticDemo ? `只读演示 · ${statusText}` : statusText}</span></Tooltip>
      </div>

      {effectiveView === "files" ? (
        <ErrorBoundary scope="项目文件" title="项目文件加载失败">
          <ProjectFilesPanel />
        </ErrorBoundary>
      ) : effectiveView === "people" ? (
        <ErrorBoundary scope="人员组织看板" title="人员组织看板加载失败">
          <PeoplePanel workspace={workspace} onWorkspaceChange={onWorkspaceChange} />
        </ErrorBoundary>
      ) : effectiveView === "calendar" ? (
        <ErrorBoundary scope="项目日历" title="项目日历加载失败">
          <ProjectCalendarPanel currentActorId={currentActorId} />
        </ErrorBoundary>
      ) : effectiveView === "tasks" ? (
        <ErrorBoundary scope="任务清单" title="任务清单加载失败">
          <TaskListPanel
            workspace={workspace}
            switchableUsers={switchableUsers}
            onWorkspaceChange={onWorkspaceChange}
          />
        </ErrorBoundary>
      ) : effectiveView === "progress" ? (
        <ErrorBoundary scope="项目进度总览" title="项目进度总览加载失败">
          <ProgressDashboardPanel
            workspace={workspace}
            onWorkspaceChange={onWorkspaceChange}
            showManagementViews={canViewProgressDashboard}
          />
        </ErrorBoundary>
      ) : effectiveView === "threeLists" ? (
        <ErrorBoundary scope="三清单" title="三清单加载失败">
          <ThreeListsPanel workspace={workspace} onWorkspaceChange={onWorkspaceChange} />
        </ErrorBoundary>
      ) : effectiveView === "review" ? (
        <ErrorBoundary scope="待确认" title="待确认队列加载失败">
          <ReviewQueuePanel workspace={workspace} onWorkspaceChange={onWorkspaceChange} onRefresh={onRefresh} />
        </ErrorBoundary>
      ) : effectiveView === "knowledge" ? (
        <ErrorBoundary scope="知识沉淀" title="知识沉淀加载失败">
          <KnowledgePipelinePanel
            workspace={workspace}
            onWorkspaceChange={onWorkspaceChange}
            onRefresh={onRefresh}
          />
        </ErrorBoundary>
      ) : (
        <ErrorBoundary scope="对话区" title="对话区加载失败">
          <section className="chat-stage" aria-label="对话区">
            {error ? <div className="error-strip" role="alert">{error}</div> : null}
            {roleProfile ? <div className="role-scope-strip">{roleProfile.label}：{roleProfile.principle}</div> : null}
            <MessageList messages={messages} />
            {showQuickStart ? (
              <QuickStartCards
                workspace={workspace}
                canViewProgress={canViewProgressDashboard}
                canReview={canReview}
                readOnly={isStaticDemo}
                onViewChange={onViewChange}
                onMeetingFilesSelected={(files) => {
                  const merged = new Map(
                    [...selectedFiles, ...files].map((file) => [
                      `${file.name}:${file.size}:${file.lastModified}`,
                      file,
                    ]),
                  );
                  onFilesChange(Array.from(merged.values()));
                  onAttachmentInputKindChange("minutes");
                  document.querySelector<HTMLTextAreaElement>(".sender-shell textarea")?.focus();
                }}
              />
            ) : null}
            {isStaticDemo ? (
              <div className="static-demo-notice" role="note">
                当前为 GitHub Pages 只读演示：可浏览全部合成项目数据，新增、修改、上传及 AI 对话均已关闭。
              </div>
            ) : <SenderBar
              selectedFiles={selectedFiles}
              inputKind={attachmentInputKind}
              selectedModel={selectedModel}
              modelOptions={modelOptions}
              loading={loading}
              connectionState={connectionState}
              mentionNames={mentionNames}
              onFilesChange={onFilesChange}
              onInputKindChange={onAttachmentInputKindChange}
              onModelChange={onModelChange}
              onProviderConfigured={onProviderConfigured}
              onSend={onSend}
            />}
          </section>
        </ErrorBoundary>
      )}
    </main>
  );
}

function resolveView(
  view: ActiveView,
  gates: { canViewPeople: boolean; canReview: boolean; canViewProgressDashboard: boolean },
): ActiveView {
  if (view === "people" && !gates.canViewPeople) return "chat";
  if (view === "review" && !gates.canReview) return "chat";
  if (view === "progress" && !gates.canViewProgressDashboard) return "chat";
  return view;
}

const ROLE_LABELS: Record<string, string> = {
  pm: "项目经理",
  pmo: "PMO / 总体组",
  professional_lead: "专业统筹",
  topic_lead: "专题负责人",
  exec: "实施人员",
  viewer: "查看人员",
};

function fallbackActorOption(workspace: Workspace, actorId: string): ActorOption {
  const access = workspace.access;
  const roleLabel = access.role_profile?.label || ROLE_LABELS[access.role] || "当前视角";
  const name = access.actor.name || roleLabel;
  return {
    value: actorId,
    label: [name, roleLabel].filter(Boolean).join(" · "),
    name,
    roleLabel,
    topics: "当前登录视角",
  };
}

function toActorOption(user: SwitchableUser): ActorOption {
  const roleLabel = ROLE_LABELS[user.role] || user.role;
  const topics = user.topics.map((topic) => topic.name).join("、");
  return {
    value: user.user_id,
    label: [user.name, roleLabel, topics, user.user_id].filter(Boolean).join(" · "),
    name: user.name,
    roleLabel,
    topics,
  };
}

function dailyBriefStatusText(workspace: Workspace) {
  const scheduler = workspace.daily_brief.scheduler;
  if (!scheduler?.enabled) return "晨报调度未启用";
  const time = `${String(scheduler.hour).padStart(2, "0")}:${String(scheduler.minute).padStart(2, "0")}`;
  const next = scheduler.next_run_time ? formatNextRun(scheduler.next_run_time, time) : `下次自动生成 ${time}`;
  if (workspace.daily_brief.brief_date) {
    return `今日晨报已生成 ${time} · ${next}`;
  }
  return `今日晨报未生成 · ${next}`;
}

function dailyJournalStatusText(workspace: Workspace) {
  const journal = workspace.daily_journal;
  const scheduler = journal.scheduler;
  if (journal.error) return `日报整理失败 · ${journal.error}`;
  if (scheduler?.error) return `日报调度异常 · ${scheduler.error}`;
  const today = new Date().toLocaleDateString("sv-SE");
  if (journal.status === "completed" && journal.report_date === today) {
    return `今日日报已整理 · ${journal.message_count} 条人类消息`;
  }
  return "今日日报采集中 · 23:55 自动整理";
}

function formatNextRun(value: string, fallback: string) {
  const time = Date.parse(value);
  if (!Number.isFinite(time)) return `下次自动生成 ${fallback}`;
  const date = new Date(time);
  return `下次自动生成 ${date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" })} ${date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`;
}
