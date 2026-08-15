import { MenuFoldOutlined, MenuUnfoldOutlined, MoreOutlined, PlusOutlined } from "@ant-design/icons";
import { Avatar, Button, Collapse, Dropdown, Modal, Tooltip, Typography } from "antd";
import { Conversations } from "@ant-design/x";
import type { ConversationSession, Workspace } from "../types";

type ProjectListPaneProps = {
  workspace: Workspace;
  sessions: ConversationSession[];
  activeSessionId: string;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  onSessionSelect: (sessionId: string) => void;
  onNewSession: () => void;
  onSessionRename: (session: ConversationSession) => void;
  onSessionArchive: (session: ConversationSession, archived: boolean) => void;
  onSessionDelete: (session: ConversationSession) => void;
  onProjectArchiveAndClear: () => Promise<void>;
};

export function ProjectListPane({
  workspace,
  sessions,
  activeSessionId,
  collapsed,
  onToggleCollapsed,
  onSessionSelect,
  onNewSession,
  onSessionRename,
  onSessionArchive,
  onSessionDelete,
  onProjectArchiveAndClear,
}: ProjectListPaneProps) {
  const [modal, modalContextHolder] = Modal.useModal();

  if (collapsed) {
    return (
      <aside className="project-list-pane is-collapsed" aria-label="项目与会话">
        <Tooltip title="展开项目与会话" placement="right">
          <button
            type="button"
            className="pane-reveal"
            aria-expanded={false}
            aria-label="展开项目与会话列表"
            onClick={onToggleCollapsed}
          >
            <MenuUnfoldOutlined aria-hidden="true" />
          </button>
        </Tooltip>
      </aside>
    );
  }

  const projectName = workspace.milestone_control.plan.project || workspace.milestone.name;
  const owner = workspace.people_workspace.people[0]?.name || "负责人待确认";
  const activeSessions = sessions.filter((session) => !session.archived);
  const archivedSessions = sessions.filter((session) => session.archived);
  const canArchiveProject = ["pm", "pmo"].includes(workspace.access.role);
  const projectItems = [
    {
      key: workspace.milestone_control.plan.milestone_id || "current-project",
      label: (
        <div className="project-conversation-item">
          <Avatar shape="square" className="project-avatar">
            项
          </Avatar>
          <div className="project-conversation-copy">
            <strong>{projectName}</strong>
            <span>负责人：{owner}</span>
          </div>
          {canArchiveProject ? (
            <Dropdown
              trigger={["click"]}
              menu={{
                items: [{ key: "archive-clear", label: "归档并清空工作台", danger: true }],
                onClick: ({ key, domEvent }) => {
                  domEvent.stopPropagation();
                  if (key === "archive-clear") confirmProjectArchive(modal, onProjectArchiveAndClear);
                },
              }}
            >
              <Button
                type="text"
                size="small"
                icon={<MoreOutlined />}
                aria-label="项目操作"
                onClick={(event) => event.stopPropagation()}
              />
            </Dropdown>
          ) : null}
        </div>
      ),
    },
  ];

  return (
    <aside className="project-list-pane" aria-label="项目与会话">
      {modalContextHolder}
      <div className="pane-body">
        <div className="pane-title-row">
          <Typography.Title level={3}>全部项目</Typography.Title>
          <div className="pane-title-actions">
            <Tooltip title="新建会话">
              <Button type="text" icon={<PlusOutlined />} aria-label="新建会话" onClick={onNewSession} />
            </Tooltip>
            <Tooltip title="收起列表">
              <Button
                type="text"
                icon={<MenuFoldOutlined />}
                aria-label="收起项目与会话列表"
                onClick={onToggleCollapsed}
              />
            </Tooltip>
          </div>
        </div>
        <Conversations className="project-conversations" activeKey={projectItems[0].key} items={projectItems} />

        <Typography.Text className="session-list-title">历史会话</Typography.Text>
        <Conversations
          className="project-conversations"
          activeKey={activeSessionId}
          onActiveChange={onSessionSelect}
          items={activeSessions.map((session) => ({
            key: session.session_id,
            label: sessionLabel(session, {
              onRename: onSessionRename,
              onArchive: onSessionArchive,
              onDelete: onSessionDelete,
            }),
          }))}
        />
        <Collapse
          ghost
          size="small"
          className="archived-session-collapse"
          items={[{
            key: "archived",
            label: `已归档 ${archivedSessions.length}`,
            children: (
              <Conversations
                className="project-conversations archived-conversations"
                activeKey={activeSessionId}
                onActiveChange={onSessionSelect}
                items={archivedSessions.map((session) => ({
                  key: session.session_id,
                  label: sessionLabel(session, {
                    onRename: onSessionRename,
                    onArchive: onSessionArchive,
                    onDelete: onSessionDelete,
                  }),
                }))}
              />
            ),
          }]}
        />
      </div>
    </aside>
  );
}

type ModalApi = ReturnType<typeof Modal.useModal>[0];

function confirmProjectArchive(modal: ModalApi, onConfirm: () => Promise<void>) {
  modal.confirm({
    title: "归档并清空当前工作台？",
    content: "候选、正式任务、历史运行和会话将被归档；会议纪要、人员 XMind 和三清单原文件继续保留在项目资料库。",
    okText: "归档并清空",
    okButtonProps: { danger: true },
    cancelText: "取消",
    onOk: async () => {
      try {
        await onConfirm();
      } catch (reason) {
        modal.error({
          title: "工作台归档失败",
          content: reason instanceof Error ? reason.message : "请求未完成，请检查后端状态后重试。",
        });
        throw reason;
      }
    },
  });
}

function sessionLabel(
  session: ConversationSession,
  actions: {
    onRename: (session: ConversationSession) => void;
    onArchive: (session: ConversationSession, archived: boolean) => void;
    onDelete: (session: ConversationSession) => void;
  },
) {
  return (
    <div className="session-conversation-item">
      <div>
        <strong>{session.title}</strong>
        <span>{session.message_count} 条消息</span>
      </div>
      <Dropdown
        trigger={["click"]}
        menu={{
          items: [
            { key: "rename", label: "重命名" },
            { key: "archive", label: session.archived ? "恢复" : "归档" },
            { key: "delete", label: "删除", danger: true },
          ],
          onClick: ({ key, domEvent }) => {
            domEvent.stopPropagation();
            if (key === "rename") actions.onRename(session);
            if (key === "archive") actions.onArchive(session, !session.archived);
            if (key === "delete") actions.onDelete(session);
          },
        }}
      >
        <Button
          type="text"
          size="small"
          icon={<MoreOutlined />}
          aria-label="会话操作"
          onClick={(event) => event.stopPropagation()}
        />
      </Dropdown>
    </div>
  );
}
