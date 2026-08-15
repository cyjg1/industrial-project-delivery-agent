import { DeleteOutlined, DownloadOutlined, EditOutlined, PlusOutlined, SettingOutlined, UploadOutlined } from "@ant-design/icons";
import { Button, Card, Form, Input, Modal, Popconfirm, Select, Space, Tag, Tooltip, Upload } from "antd";
import { useState } from "react";
import {
  createPeopleGroup,
  deletePeopleGroup,
  downloadPeopleImportTemplate,
  importPeopleFromTemplate,
  updatePeopleGroup,
  updatePersonProfile,
  uploadPeopleAsset,
} from "../api/client";
import type { PeopleWorkspaceItem, Workspace } from "../types";
import { PanelEmpty } from "./panelKit";
import { useAsyncAction } from "./useAsyncAction";

type PeoplePanelProps = {
  workspace: Workspace;
  onWorkspaceChange: (workspace: Workspace) => void;
};

type PeopleDraft = {
  personId: string;
  originalName: string;
  name: string;
  group: string;
  role: string;
  responsibility_note: string;
};

export function PeoplePanel({ workspace, onWorkspaceChange }: PeoplePanelProps) {
  const groups = groupPeopleByBusinessBoard(workspace.people_workspace.people, workspace.people_workspace.groups);
  const groupOptions = workspace.people_workspace.groups.map(({ name }) => ({ value: name, label: name }));
  const canEditPeople = ["pm", "pmo"].includes(workspace.access.role);
  const [editing, setEditing] = useState<PeopleDraft | null>(null);
  const [groupManagerOpen, setGroupManagerOpen] = useState(false);
  const [newGroupName, setNewGroupName] = useState("");
  const [groupEditing, setGroupEditing] = useState<{ originalName: string; name: string } | null>(null);
  const hasPeople = workspace.people_workspace.people.length > 0;

  const saveAction = useAsyncAction(
    async (draft: PeopleDraft) => {
      const next = await updatePersonProfile(draft.personId || draft.name.trim(), {
        name: draft.name.trim(),
        group: draft.group.trim(),
        role: draft.role.trim(),
        responsibility_note: draft.responsibility_note.trim(),
        notes: "从人员看板保存人员安排",
      });
      onWorkspaceChange(next);
    },
    { errorText: "人员保存失败", successText: "人员安排已保存" },
  );
  const uploadAction = useAsyncAction(
    async (file: File) => {
      const next = await uploadPeopleAsset(file);
      onWorkspaceChange(next);
    },
    { errorText: "人员资产上传失败", successText: "人员资产已更新" },
  );
  const importAction = useAsyncAction(
    async (file: File) => {
      const result = await importPeopleFromTemplate(file);
      onWorkspaceChange(result.workspace);
    },
    { errorText: "人员批量导入失败", successText: "人员批量导入完成" },
  );
  const templateAction = useAsyncAction(
    async () => downloadPeopleImportTemplate(),
    { errorText: "人员模板下载失败", successText: "人员导入模板已下载" },
  );
  const groupCreateAction = useAsyncAction(
    async (name: string) => {
      const next = await createPeopleGroup({ name, notes: "从人员组织看板新增组别" });
      onWorkspaceChange(next);
    },
    { errorText: "组别新增失败", successText: "组别已新增" },
  );
  const groupUpdateAction = useAsyncAction(
    async (draft: { originalName: string; name: string }) => {
      const next = await updatePeopleGroup(draft.originalName, { name: draft.name, notes: "从人员组织看板修改组别" });
      onWorkspaceChange(next);
    },
    { errorText: "组别修改失败", successText: "组别已修改" },
  );
  const groupDeleteAction = useAsyncAction(
    async (name: string) => {
      const next = await deletePeopleGroup(name);
      onWorkspaceChange(next);
    },
    { errorText: "组别删除失败", successText: "组别已删除", key: (name) => name },
  );

  async function savePerson() {
    if (!editing?.name.trim() || !editing.group.trim() || !editing.role.trim()) return;
    const saved = await saveAction.run(editing);
    if (!saved) return;
    setEditing(null);
  }

  async function createGroup() {
    const name = newGroupName.trim();
    if (!name) return;
    const saved = await groupCreateAction.run(name);
    if (saved) setNewGroupName("");
  }

  async function saveGroupName() {
    if (!groupEditing?.name.trim()) return;
    const saved = await groupUpdateAction.run({ ...groupEditing, name: groupEditing.name.trim() });
    if (saved) setGroupEditing(null);
  }

  return (
    <section className="panel-stack" aria-label="人员组织看板">
      <Card
        size="small"
        title="人员组织看板"
      >
        <Space wrap>
          <Tag>人员 {workspace.people_workspace.summary.people_count}</Tag>
          <Tag>分组 {workspace.people_workspace.summary.group_count}</Tag>
          <Tag>正在做 {workspace.people_workspace.summary.active_work_total}</Tag>
          <Tag color={workspace.inputs.people_asset.people_count > 0 ? "success" : "warning"}>
            人员资产 {workspace.inputs.people_asset.people_count > 0 ? "已加载" : "待补充"}
          </Tag>
          <Tag>{canEditPeople ? "可维护人员安排" : "只读视图"}</Tag>
          {workspace.people_workspace.summary.duplicate_review_count > 0 ? (
            <Tag color="warning">同名待确认 {workspace.people_workspace.summary.duplicate_review_count}</Tag>
          ) : null}
        </Space>
        <div className="people-maintenance-bar">
          <div className="people-maintenance-copy">
            <strong>人员维护</strong>
            <span>
              {canEditPeople
                ? "可编辑人员、维护组别，并通过模板增量导入人员信息。"
                : "当前身份为只读；切换到项目经理或PMO后可使用下列功能。"}
            </span>
          </div>
          <Space size={8} wrap>
            <Tooltip title={canEditPeople ? "下载标准Excel模板" : "请先切换到项目经理或PMO身份"}>
              <Button
                icon={<DownloadOutlined />}
                loading={templateAction.pending}
                disabled={!canEditPeople}
                onClick={() => templateAction.run()}
              >
                下载模板
              </Button>
            </Tooltip>
            <Upload
              accept=".csv,.xlsx,.xlsm"
              disabled={!canEditPeople}
              showUploadList={false}
              beforeUpload={async (file) => {
                await importAction.run(file);
                return false;
              }}
            >
              <Tooltip title={canEditPeople ? "按模板增量新增或更新人员" : "请先切换到项目经理或PMO身份"}>
                <Button icon={<UploadOutlined />} loading={importAction.pending} disabled={!canEditPeople}>批量导入</Button>
              </Tooltip>
            </Upload>
            <Tooltip title={canEditPeople ? "新增、重命名或删除空组" : "请先切换到项目经理或PMO身份"}>
              <Button icon={<SettingOutlined />} disabled={!canEditPeople} onClick={() => setGroupManagerOpen(true)}>组别管理</Button>
            </Tooltip>
            <Upload
              accept=".csv,.xlsx,.xlsm,.xmind"
              disabled={!canEditPeople}
              showUploadList={false}
              beforeUpload={async (file) => {
                await uploadAction.run(file);
                return false;
              }}
            >
              <Tooltip title={canEditPeople ? "用上传文件完全替换当前人员资产" : "请先切换到项目经理或PMO身份"}>
                <Button icon={<UploadOutlined />} loading={uploadAction.pending} disabled={!canEditPeople}>整表更新</Button>
              </Tooltip>
            </Upload>
            <Tooltip title={canEditPeople ? "手工新增一名人员" : "请先切换到项目经理或PMO身份"}>
              <Button
                type="primary"
                icon={<PlusOutlined />}
                disabled={!canEditPeople}
                onClick={() => setEditing(emptyDraft())}
              >
                新增人员
              </Button>
            </Tooltip>
          </Space>
        </div>
      </Card>

      {hasPeople ? (
        <>
          <Card size="small" title="组别快速总览" className="people-overview-card">
            <div className="people-overview-grid">
              {groups.map((group, index) => (
                <a className="people-overview-item" href={`#people-group-${index}`} key={group.name}>
                  <span>{group.name}</span>
                  <strong>{group.peopleCount} 人</strong>
                  <small>{group.activeWorkCount} 项进行中工作</small>
                </a>
              ))}
            </div>
          </Card>

          <div className="people-board">
            {groups.map((group, index) => (
              <section className="people-column" id={`people-group-${index}`} key={group.name}>
                <header className="people-column-header">
                  <strong>{group.name}</strong>
                  <span>{group.peopleCount} 人 · {group.activeWorkCount} 项工作</span>
                </header>
                <div className="people-card-list" aria-label={`${group.name}人员列表`}>
                  {group.people.map((person) => (
                    <PersonCard
                      key={person.person_id}
                      person={person}
                      canEdit={canEditPeople && person.editable}
                      onEdit={() => setEditing(draftFromPerson(person))}
                    />
                  ))}
                </div>
              </section>
            ))}
          </div>
        </>
      ) : (
        <Card size="small">
          <PanelEmpty
            title="还没有人员数据"
            hint="人员资产为空，责任分配和会议理解将保持待确认。"
          />
        </Card>
      )}

      <Modal
        title={editing?.personId ? "修改人员" : "新增人员"}
        open={Boolean(editing)}
        okText="保存"
        cancelText="取消"
        confirmLoading={saveAction.pending}
        okButtonProps={{ disabled: !editing?.name.trim() || !editing?.group.trim() || !editing?.role.trim() }}
        onOk={savePerson}
        onCancel={() => setEditing(null)}
        destroyOnHidden
      >
        {editing ? (
          <Form layout="vertical">
            <Form.Item label="姓名" required>
              <Input value={editing.name} onChange={(event) => setEditing({ ...editing, name: event.target.value })} />
            </Form.Item>
            <div className="form-grid">
              <Form.Item label="所属板块" required>
                <Select
                  value={editing.group || undefined}
                  options={groupOptions}
                  placeholder="选择所属板块"
                  showSearch
                  onChange={(group) => setEditing({ ...editing, group })}
                />
              </Form.Item>
              <Form.Item label="职责/角色" required>
                <Input value={editing.role} onChange={(event) => setEditing({ ...editing, role: event.target.value })} />
              </Form.Item>
            </div>
            <Form.Item label="备注/职责说明">
              <Input.TextArea
                rows={3}
                value={editing.responsibility_note}
                onChange={(event) => setEditing({ ...editing, responsibility_note: event.target.value })}
              />
            </Form.Item>
          </Form>
        ) : null}
      </Modal>

      <Modal
        title="组别管理"
        open={groupManagerOpen}
        footer={null}
        onCancel={() => {
          setGroupManagerOpen(false);
          setGroupEditing(null);
        }}
        destroyOnHidden
      >
        <div className="people-group-create">
          <Input
            value={newGroupName}
            placeholder="输入新组别名称"
            onChange={(event) => setNewGroupName(event.target.value)}
            onPressEnter={createGroup}
          />
          <Button type="primary" icon={<PlusOutlined />} loading={groupCreateAction.pending} disabled={!newGroupName.trim()} onClick={createGroup}>
            新增组别
          </Button>
        </div>
        <div className="people-group-manager-list">
          {workspace.people_workspace.groups.map((group) => (
            <div className="people-group-manager-row" key={group.name}>
              {groupEditing?.originalName === group.name ? (
                <>
                  <Input value={groupEditing.name} onChange={(event) => setGroupEditing({ ...groupEditing, name: event.target.value })} onPressEnter={saveGroupName} />
                  <Space size={6}>
                    <Button type="primary" size="small" loading={groupUpdateAction.pending} disabled={!groupEditing.name.trim()} onClick={saveGroupName}>保存</Button>
                    <Button size="small" onClick={() => setGroupEditing(null)}>取消</Button>
                  </Space>
                </>
              ) : (
                <>
                  <div className="people-group-manager-name">
                    <strong>{group.name}</strong>
                    <span>{group.people_count} 人</span>
                  </div>
                  <Space size={4}>
                    <Button size="small" type="text" icon={<EditOutlined />} onClick={() => setGroupEditing({ originalName: group.name, name: group.name })}>修改</Button>
                    <Tooltip title={group.people_count ? "请先把组内人员调整到其他板块" : "删除空组"}>
                      <span>
                        <Popconfirm
                          title={`确认删除组别“${group.name}”？`}
                          description="删除后不能直接恢复。"
                          disabled={group.people_count > 0}
                          okText="删除"
                          cancelText="取消"
                          onConfirm={() => groupDeleteAction.run(group.name)}
                        >
                          <Button
                            danger
                            size="small"
                            type="text"
                            icon={<DeleteOutlined />}
                            disabled={group.people_count > 0}
                            loading={groupDeleteAction.isPending(group.name)}
                          >
                            删除
                          </Button>
                        </Popconfirm>
                      </span>
                    </Tooltip>
                  </Space>
                </>
              )}
            </div>
          ))}
        </div>
      </Modal>
    </section>
  );
}

function PersonCard({ person, canEdit, onEdit }: { person: PeopleWorkspaceItem; canEdit: boolean; onEdit: () => void }) {
  return (
    <article className="people-card">
      <div className="people-card-main">
        <strong>{person.name}</strong>
        <Tag>{person.role || "角色待补"}</Tag>
      </div>
      <div className="people-card-meta">
        <span>{person.responsibility_note || "暂无职责备注"}</span>
        <span>正在做 {person.active_work_count} 项</span>
      </div>
      <Tooltip title={canEdit ? "编辑姓名、所属板块、职责和备注" : "请切换到项目经理或PMO身份后编辑"}>
        <Button size="small" icon={<EditOutlined />} disabled={!canEdit} onClick={onEdit}>
          编辑
        </Button>
      </Tooltip>
    </article>
  );
}

function emptyDraft(): PeopleDraft {
  return {
    personId: "",
    originalName: "",
    name: "",
    group: "",
    role: "",
    responsibility_note: "",
  };
}

function draftFromPerson(person: PeopleWorkspaceItem): PeopleDraft {
  return {
    personId: person.person_id,
    originalName: person.name,
    name: person.name,
    group: person.group,
    role: person.role,
    responsibility_note: person.responsibility_note || person.responsibility_summary,
  };
}

function groupPeopleByBusinessBoard(
  people: PeopleWorkspaceItem[],
  declaredGroups: Workspace["people_workspace"]["groups"],
) {
  const groupMap = new Map<string, PeopleWorkspaceItem[]>();
  declaredGroups.forEach((group) => groupMap.set(group.name, []));
  people.forEach((person) => {
    // 人员表里的“所属业务板块”是分组口径的唯一来源。
    const group = person.group || "待归类";
    groupMap.set(group, [...(groupMap.get(group) || []), person]);
  });
  return Array.from(groupMap.keys())
    .map((name) => {
      const groupPeople = groupMap.get(name) || [];
      return {
        name,
        peopleCount: groupPeople.length,
        activeWorkCount: groupPeople.reduce((total, person) => total + person.active_work_count, 0),
        people: [...groupPeople].sort((left, right) => {
          const roleOrder = rolePriority(left.role) - rolePriority(right.role);
          return roleOrder || left.name.localeCompare(right.name, "zh-CN");
        }),
      };
    });
}

function rolePriority(role: string) {
  if (role.trim() === "专题负责人") return 0;
  if (role.trim() === "专业统筹") return 1;
  if (role.trim().toUpperCase() === "PMO") return 2;
  if (role.trim() === "实施人员") return 3;
  return 4;
}
