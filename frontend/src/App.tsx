import { Button, Result, Skeleton, Typography } from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  archiveAndClearWorkspace,
  archiveConversationSession,
  deleteConversationSession,
  generateDailyBrief,
  loadConversationMessages,
  loadConversationModels,
  loadConversationSessions,
  loadSwitchableUsers,
  loadWorkspace,
  renameConversationSession,
  setCurrentActorId,
  streamWorkspaceMessage,
  type StreamConnectionState,
} from "./api/client";
import { DetailDrawer } from "./layout/DetailDrawer";
import { IconRail } from "./layout/IconRail";
import { MainPane } from "./layout/MainPane";
import { ProjectListPane } from "./layout/ProjectListPane";
import { useIngestionPolling } from "./hooks/useIngestionPolling";
import { useConversationModelSelection } from "./hooks/useConversationModelSelection";
import type { ActiveView, ChatMessage } from "./lib/ui";
import { openingMessage } from "./lib/ui";
import { lastAgentMessage, mergeToolStep, panelView, rowsToChatMessages } from "./lib/conversationUi";
import type { ConversationSession, IngestionInputKind, SwitchableUser, Workspace } from "./types";
function initialActorId() {
  // The competition build shares localhost storage with any older local build.
  // Always enter through the synthetic PMO actor so a stale private actor ID
  // cannot prevent the public demo from booting. Role switching still works
  // after the workspace has loaded.
  const actorId = "u_pmo";
  setCurrentActorId(actorId);
  return actorId;
}

export default function App() {
  const [currentActorId, setActorIdState] = useState(initialActorId);
  const currentActorIdRef = useRef(currentActorId);
  const hasWorkspaceRef = useRef(false);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [switchableUsers, setSwitchableUsers] = useState<SwitchableUser[]>([]);
  const [sessions, setSessions] = useState<ConversationSession[]>([]);
  const [activeSessionId, setActiveSessionId] = useState("");
  const [activeView, setActiveView] = useState<ActiveView>("chat");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [attachmentInputKind, setAttachmentInputKind] = useState<IngestionInputKind>("auto");
  const { applyCatalog, modelOptions, selectedModel, selectModel } = useConversationModelSelection();
  const [booting, setBooting] = useState(true);
  const [switchingActor, setSwitchingActor] = useState(false);
  const [sending, setSending] = useState(false);
  const [streamConnection, setStreamConnection] = useState<StreamConnectionState | null>(null);
  const [error, setError] = useState("");
  const [bootError, setBootError] = useState("");
  const [reloadToken, setReloadToken] = useState(0);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [sessionPaneOpen, setSessionPaneOpen] = useState(true);

  async function refreshWorkspace() {
    const requestActorId = currentActorIdRef.current;
    setCurrentActorId(requestActorId);
    const next = await loadWorkspace(requestActorId);
    if (currentActorIdRef.current !== requestActorId || next.access.actor.id !== requestActorId) return;
    setWorkspace(next);
  }

  async function refreshSessions() {
    const requestActorId = currentActorIdRef.current;
    const next = await loadConversationSessions(requestActorId);
    if (currentActorIdRef.current !== requestActorId) return;
    setSessions(next);
  }

  async function refreshProviderModels() {
    applyCatalog(await loadConversationModels());
  }

  useEffect(() => {
    let cancelled = false;
    const requestActorId = currentActorId;
    currentActorIdRef.current = requestActorId;
    setCurrentActorId(currentActorId);
    window.localStorage.setItem("project-agent-actor-id", currentActorId);
    const firstLoad = !hasWorkspaceRef.current;
    if (firstLoad) setBooting(true);
    else setSwitchingActor(true);
    setError("");
    setWorkspace(null);
    setSwitchableUsers([]);
    setMessages([]);
    setBootError("");
    setActiveSessionId("");
    Promise.all([
      loadWorkspace(requestActorId),
      loadConversationSessions(requestActorId),
      loadConversationModels(),
      loadSwitchableUsers(requestActorId),
    ])
      .then(([nextWorkspace, nextSessions, nextModelCatalog, nextSwitchableUsers]) => {
        if (
          cancelled
          || currentActorIdRef.current !== requestActorId
          || nextWorkspace.access.actor.id !== requestActorId
        ) return;
        hasWorkspaceRef.current = true;
        setWorkspace(nextWorkspace);
        setSessions(nextSessions);
        setSwitchableUsers(nextSwitchableUsers);
        applyCatalog(nextModelCatalog);
        setMessages([openingMessage(nextWorkspace)]);
      })
      .catch((reason) => {
        if (cancelled) return;
        const message = reason instanceof Error ? reason.message : "工作区加载失败";
        setError(message);
        setBootError(message);
      })
      .finally(() => {
        if (cancelled) return;
        setBooting(false);
        setSwitchingActor(false);
      });
    return () => {
      cancelled = true;
    };
  }, [currentActorId, reloadToken]);

  useEffect(() => {
    if (workspace && messages.length === 0) {
      setMessages([openingMessage(workspace)]);
    }
  }, [workspace, messages.length]);

  // 会话列表只在对话视图默认展开；切到宽表格视图自动收起，用户仍可手动唤回。
  useEffect(() => {
    setSessionPaneOpen(activeView === "chat");
  }, [activeView]);

  useIngestionPolling({ workspace, actorId: currentActorId, refresh: refreshWorkspace, onError: setError });

  const latestAgent = useMemo(() => lastAgentMessage(messages), [messages]);
  // The review badge must describe the queue it opens. Daily-brief alerts also
  // include schedule and decision reminders, so adding that count made the
  // badge disagree with the actual confirmation cards.
  const notificationCount = workspace?.confirmation_cards.length ?? 0;

  async function openSession(sessionId: string) {
    const requestActorId = currentActorIdRef.current;
    const rows = await loadConversationMessages(sessionId);
    if (currentActorIdRef.current !== requestActorId) return;
    setActiveSessionId(sessionId);
    setActiveView("chat");
    setMessages(rowsToChatMessages(rows));
  }

  function newSession() {
    setActiveSessionId("");
    setActiveView("chat");
    if (workspace) {
      setMessages([openingMessage(workspace)]);
    }
  }

  async function handleSessionRename(session: ConversationSession) {
    const title = window.prompt("重命名会话", session.title);
    if (!title || title.trim() === session.title) return;
    const requestActorId = currentActorIdRef.current;
    const next = await renameConversationSession(session.session_id, title.trim());
    if (currentActorIdRef.current === requestActorId) setSessions(next);
  }

  async function handleSessionArchive(session: ConversationSession, archived: boolean) {
    const requestActorId = currentActorIdRef.current;
    const next = await archiveConversationSession(session.session_id, archived);
    if (currentActorIdRef.current === requestActorId) setSessions(next);
  }

  async function handleSessionDelete(session: ConversationSession) {
    if (!window.confirm(`删除会话「${session.title}」？这会删除该会话下的消息。`)) return;
    const requestActorId = currentActorIdRef.current;
    const next = await deleteConversationSession(session.session_id);
    if (currentActorIdRef.current !== requestActorId) return;
    setSessions(next);
    if (activeSessionId === session.session_id) newSession();
  }
  async function handleSend(rawMessage: string, model: string) {
    if (!workspace || sending) return;
    const text = rawMessage.trim();
    if (!text && selectedFiles.length === 0) return;
    const files = selectedFiles;
    const inputKind = attachmentInputKind;
    const displayText = [text || "请识别并处理这些文件", ...files.map((file) => `附件：${file.name}`)]
      .filter(Boolean).join("\n");
    const userMessage: ChatMessage = {
      id: `user-${Date.now()}`,
      role: "user",
      createdAt: new Date().toISOString(),
      content: displayText,
    };
    const agentMessageId = `agent-${Date.now()}`;
    setMessages((items) => [
      ...items,
      userMessage,
      {
        id: agentMessageId,
        role: "agent",
        createdAt: new Date().toISOString(),
        content: "",
        toolSteps: [],
      },
    ]);
    setSending(true);
    setError("");
    try {
      const result = await streamWorkspaceMessage({
        view: panelView(activeView),
        message: text || `请识别并处理这 ${files.length} 个文件`,
        model,
        sessionId: activeSessionId || null,
        files,
        inputKind,
        onConnectionChange: setStreamConnection,
        onToolStep: (step) => {
          setMessages((items) => items.map((item) => (
            item.id === agentMessageId
              ? { ...item, toolSteps: mergeToolStep(item.toolSteps || [], step) }
              : item
          )));
        },
        onDelta: (delta) => {
          setMessages((items) => items.map((item) => (
            item.id === agentMessageId
              ? { ...item, content: `${item.content}${delta}` }
              : item
          )));
        },
        onReset: () => {
          setMessages((items) => items.map((item) => (
            item.id === agentMessageId
              ? { ...item, content: "" }
              : item
          )));
        },
      });
      setWorkspace(result.workspace);
      setActiveSessionId(result.session_id);
      setSelectedFiles([]);
      setAttachmentInputKind("auto");
      await refreshSessions();
      setMessages((items) => items.map((item) => (
        item.id === agentMessageId
          ? {
              ...item,
              id: result.message_id,
              content: result.reply,
              provider: result.debug.provider,
              model: result.debug.model,
              contextBundle: result.context_bundle,
              modelInput: result.debug.model_input,
              modelOutput: result.debug.model_output,
              contextCharCount: result.debug.context_char_count,
              contextTokenCount: result.debug.context_token_count,
              contextBudget: result.debug.context_budget,
              contextBudgetUnit: result.debug.context_budget_unit,
              contextTokenCounter: result.debug.context_token_counter,
              contextRetrievalCount: result.debug.context_retrieval_count,
              contextDegraded: result.debug.context_degraded,
              toolSteps: result.tool_steps,
              modelRounds: result.debug.rounds || [],
              verification: result.verification,
              stopReason: result.stop_reason,
            }
          : item
      )));
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "Agent 调用失败";
      setError(message);
      setMessages((items) => items.map((item) => (
        item.id === agentMessageId
          ? { ...item, content: `**调用失败** — ${message}` }
          : item
      )));
      throw reason;
    } finally {
      setStreamConnection(null);
      setSending(false);
    }
  }

  async function handleGenerateBrief() {
    if (!workspace || sending) return;
    setSending(true);
    setError("");
    try {
      const result = await generateDailyBrief(false);
      setWorkspace(result.workspace);
      setActiveView("chat");
      setMessages((items) => [
        ...items,
        {
          id: `brief-${result.brief.brief_id}-${Date.now()}`,
          role: "agent",
          createdAt: result.brief.generated_at || new Date().toISOString(),
          content: result.brief.content_markdown,
        },
      ]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "晨报生成失败");
    } finally {
      setSending(false);
    }
  }

  async function handleProjectArchiveAndClear() {
    await archiveAndClearWorkspace();
    window.location.reload();
  }

  function handleActorChange(actorId: string) {
    setSwitchingActor(true);
    setWorkspace(null);
    setMessages([]);
    setSelectedFiles([]);
    setAttachmentInputKind("auto");
    setBootError("");
    currentActorIdRef.current = actorId;
    setCurrentActorId(actorId);
    setActiveView("chat");
    setActorIdState(actorId);
  }

  function handleFilesChange(files: File[]) {
    setSelectedFiles(files);
    setAttachmentInputKind("auto");
  }

  function handleWorkspaceChange(nextWorkspace: Workspace) {
    if (nextWorkspace.access.actor.id !== currentActorIdRef.current) return;
    setWorkspace(nextWorkspace);
  }

  if (booting || switchingActor) return <BootSkeleton />;

  if (!workspace) {
    return (
      <BootFailure
        detail={bootError}
        onRetry={() => {
          setSwitchingActor(true);
          setReloadToken((token) => token + 1);
        }}
      />
    );
  }

  return (
    <div className="chat-workspace-shell">
      <IconRail
        activeView={activeView}
        reviewCount={notificationCount}
        canViewDashboard={Boolean(workspace.access.capabilities.progress_dashboard)}
        canViewPeople={Boolean(workspace.access.capabilities.cross_person_load)}
        canReview={Boolean(workspace.access.capabilities.review_queue)}
        onViewChange={setActiveView}
        onDebugOpen={() => setDrawerOpen(true)}
      />
      <ProjectListPane
        workspace={workspace}
        sessions={sessions}
        activeSessionId={activeSessionId}
        collapsed={!sessionPaneOpen}
        onToggleCollapsed={() => setSessionPaneOpen((open) => !open)}
        onSessionSelect={openSession}
        onNewSession={newSession}
        onSessionRename={handleSessionRename}
        onSessionArchive={handleSessionArchive}
        onSessionDelete={handleSessionDelete}
        onProjectArchiveAndClear={handleProjectArchiveAndClear}
      />
      <MainPane
        activeView={activeView}
        workspace={workspace}
        messages={messages}
        selectedFiles={selectedFiles}
        attachmentInputKind={attachmentInputKind}
        selectedModel={selectedModel}
        modelOptions={modelOptions}
        loading={sending}
        connectionState={streamConnection}
        error={error}
        currentActorId={currentActorId}
        switchableUsers={switchableUsers}
        actorSwitchDisabled={sending || switchingActor}
        onViewChange={setActiveView}
        onActorChange={handleActorChange}
        onModelChange={selectModel}
        onProviderConfigured={refreshProviderModels}
        onSend={handleSend}
        onFilesChange={handleFilesChange}
        onAttachmentInputKindChange={setAttachmentInputKind}
        onWorkspaceChange={handleWorkspaceChange}
        onRefresh={refreshWorkspace}
        onGenerateBrief={handleGenerateBrief}
      />
      <DetailDrawer
        open={drawerOpen}
        workspace={workspace}
        latestMessage={latestAgent}
        onClose={() => setDrawerOpen(false)}
      />
    </div>
  );
}

function BootSkeleton() {
  return (
    <div className="boot-skeleton" aria-busy="true" aria-label="正在加载工作台">
      <div className="boot-skeleton-rail">
        <span className="boot-skeleton-logo" />
        {[0, 1, 2, 3, 4, 5].map((index) => <span key={index} className="boot-skeleton-rail-item" />)}
      </div>
      <div className="boot-skeleton-list">
        <Skeleton active title={{ width: "60%" }} paragraph={{ rows: 2 }} />
        <Skeleton active title={{ width: "40%" }} paragraph={{ rows: 3 }} />
        <Skeleton active title={{ width: "50%" }} paragraph={{ rows: 3 }} />
      </div>
      <div className="boot-skeleton-main">
        <div className="boot-skeleton-header">
          <Skeleton active title={{ width: 240 }} paragraph={{ rows: 1, width: ["35%"] }} />
        </div>
        <div className="boot-skeleton-body">
          <Skeleton active avatar paragraph={{ rows: 3 }} />
          <Skeleton active avatar paragraph={{ rows: 4 }} />
        </div>
        <div className="boot-skeleton-footer">
          <Skeleton.Input active block />
        </div>
      </div>
    </div>
  );
}

function BootFailure({ detail, onRetry }: { detail: string; onRetry: () => void }) {
  return (
    <div className="boot-failure">
      <Result
        status="error"
        title="工作台加载失败"
        subTitle="没有拿到项目容器数据，界面无法启动。"
        extra={<Button type="primary" onClick={onRetry}>重试</Button>}
      >
        <Typography.Paragraph className="boot-failure-detail">
          {detail || "未知错误"}
        </Typography.Paragraph>
        <Typography.Paragraph type="secondary">
          请确认后端已启动，然后点击「重试」。
        </Typography.Paragraph>
      </Result>
    </div>
  );
}
