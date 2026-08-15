import {
  DownloadOutlined,
  FileAddOutlined,
  FileTextOutlined,
  FolderAddOutlined,
  FolderOpenOutlined,
} from "@ant-design/icons";
import { Button, Card, DatePicker, Form, Input, Modal, Radio, Statistic, Table, Tag, Upload, message } from "antd";
import type { UploadFile } from "antd";
import { useEffect, useMemo, useState } from "react";
import {
  createProjectFolder,
  downloadProjectFile,
  loadProjectFiles,
  uploadProjectFiles,
} from "../api/client";
import type { ProjectFileEntry, ProjectFilesWorkspace } from "../types";
import { PanelEmpty } from "./panelKit";
import "./ProjectFiles.css";

export function ProjectFilesPanel() {
  const [workspace, setWorkspace] = useState<ProjectFilesWorkspace | null>(null);
  const [folder, setFolder] = useState("");
  const [loading, setLoading] = useState(true);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [folderOpen, setFolderOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [uploadList, setUploadList] = useState<UploadFile[]>([]);
  const [uploadType, setUploadType] = useState<"minutes" | "project_document">("project_document");
  const [form] = Form.useForm();

  useEffect(() => {
    setLoading(true);
    loadProjectFiles()
      .then(setWorkspace)
      .catch((error) => message.error(error instanceof Error ? error.message : "项目文件加载失败"))
      .finally(() => setLoading(false));
  }, []);

  const rows = useMemo(
    () => (workspace?.files || []).filter((item) => item.folder === folder),
    [workspace, folder],
  );

  async function submitFolder() {
    const values = await form.validateFields(["folderName"]);
    setSubmitting(true);
    try {
      const next = await createProjectFolder(folder, values.folderName);
      setWorkspace(next);
      const created = [folder, values.folderName].filter(Boolean).join("/");
      setFolder(created);
      setFolderOpen(false);
      form.resetFields(["folderName"]);
      message.success("文件夹已创建");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "文件夹创建失败");
    } finally {
      setSubmitting(false);
    }
  }

  async function submitUpload() {
    const files = uploadList.flatMap((item) => item.originFileObj ? [item.originFileObj as File] : []);
    if (!files.length) return message.warning("请选择要上传的文件");
    const values = await form.validateFields(uploadType === "minutes" ? ["meetingDate"] : []);
    setSubmitting(true);
    try {
      const next = await uploadProjectFiles({
        files,
        folder,
        uploadType,
        title: values.title,
        meetingDate: values.meetingDate?.format("YYYY-MM-DD"),
        topic: values.topic,
      });
      setWorkspace(next);
      setUploadOpen(false);
      setUploadList([]);
      form.resetFields();
      message.success(uploadType === "minutes" ? "会议纪要已上传，正在识别任务项" : "项目资料已分类并更新索引");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "上传失败");
    } finally {
      setSubmitting(false);
    }
  }

  const columns = [
    {
      title: "文件",
      dataIndex: "name",
      key: "name",
      render: (_: string, item: ProjectFileEntry) => (
        <div className="project-file-name"><strong><FileTextOutlined /> {item.name}</strong><small>{formatBytes(item.size)}</small></div>
      ),
    },
    { title: "资料类型", dataIndex: "document_category", key: "category", width: 130, render: (value: string) => <Tag color="blue">{value}</Tag> },
    { title: "处理方式", dataIndex: "upload_type", key: "uploadType", width: 120, render: (value: string) => value === "minutes" ? "识别任务项" : "分类并索引" },
    { title: "索引状态", dataIndex: "index_status", key: "status", width: 120, render: (value: string) => <Tag color={value === "已建立索引" ? "green" : value === "处理中" ? "processing" : "default"}>{value}</Tag> },
    { title: "上传人", dataIndex: "uploaded_by", key: "uploadedBy", width: 100, render: (value: string) => value || "—" },
    {
      title: "操作", key: "action", width: 82,
      render: (_: unknown, item: ProjectFileEntry) => <Button type="link" icon={<DownloadOutlined />} onClick={() => downloadProjectFile(item.relative_path, item.name)}>下载</Button>,
    },
  ];

  return (
    <section className="panel-stack project-files-panel" aria-label="项目文件">
      <div className="task-summary-grid project-files-summary">
        <StatisticCard title="文件" value={workspace?.summary.file_count || 0} />
        <StatisticCard title="文件夹" value={workspace?.summary.folder_count || 0} />
        <StatisticCard title="会议纪要" value={workspace?.summary.minutes_count || 0} />
        <StatisticCard title="已索引" value={workspace?.summary.indexed_count || 0} />
      </div>
      <div className="project-files-layout">
        <aside className="project-files-sidebar">
          <div className="project-files-sidebar-title"><strong>文件夹</strong><Button size="small" type="text" icon={<FolderAddOutlined />} onClick={() => setFolderOpen(true)} /></div>
          <div className="project-folder-list">
            {(workspace?.folders || [""]).map((item) => (
              <button key={item || "root"} className={`project-folder-button ${folder === item ? "active" : ""}`} onClick={() => setFolder(item)}>
                <FolderOpenOutlined /> {item || "全部文件 / 根目录"}
              </button>
            ))}
          </div>
        </aside>
        <div className="project-files-main">
          <div className="project-files-toolbar">
            <div className="project-files-toolbar-title"><h2>{folder || "根目录"}</h2><span>会议纪要识别任务项；项目资料自动分类并加入搜索索引。</span></div>
            <div className="project-files-actions"><Button icon={<FolderAddOutlined />} onClick={() => setFolderOpen(true)}>新建文件夹</Button><Button type="primary" icon={<FileAddOutlined />} onClick={() => setUploadOpen(true)}>上传文件</Button></div>
          </div>
          <Table<ProjectFileEntry> rowKey="file_id" loading={loading} columns={columns} dataSource={rows} pagination={{ pageSize: 10 }} scroll={{ x: 820 }} locale={{ emptyText: <PanelEmpty compact title="当前文件夹暂无文件" hint="上传会议纪要或项目资料后，会在这里显示分类和索引状态。" /> }} />
        </div>
      </div>

      <Modal title="新建文件夹" open={folderOpen} okText="创建" cancelText="取消" confirmLoading={submitting} onOk={submitFolder} onCancel={() => setFolderOpen(false)}>
        <Form form={form} layout="vertical"><Form.Item label="上级文件夹"><Input value={folder || "根目录"} disabled /></Form.Item><Form.Item name="folderName" label="文件夹名称" rules={[{ required: true, message: "请输入文件夹名称" }]}><Input placeholder="例如：需求资料" autoFocus /></Form.Item></Form>
      </Modal>

      <Modal title={`上传到 ${folder || "根目录"}`} open={uploadOpen} okText="上传并处理" cancelText="取消" width={620} confirmLoading={submitting} onOk={submitUpload} onCancel={() => setUploadOpen(false)}>
        <Form form={form} layout="vertical">
          <Form.Item label="文件类型"><Radio.Group value={uploadType} onChange={(event) => setUploadType(event.target.value)}><Radio.Button value="project_document">项目资料</Radio.Button><Radio.Button value="minutes">会议纪要</Radio.Button></Radio.Group></Form.Item>
          <Form.Item label="选择文件" required><Upload.Dragger multiple beforeUpload={() => false} fileList={uploadList} onChange={({ fileList }) => setUploadList(fileList)}><p className="ant-upload-drag-icon"><FileAddOutlined /></p><p>点击或拖拽文件到此处</p></Upload.Dragger></Form.Item>
          {uploadType === "minutes" ? <><Form.Item name="meetingDate" label="会议日期" rules={[{ required: true, message: "请选择会议日期" }]}><DatePicker style={{ width: "100%" }} /></Form.Item><Form.Item name="title" label="会议名称"><Input placeholder="不填写时使用文件名" /></Form.Item><Form.Item name="topic" label="会议主题"><Input placeholder="例如：项目周例会" /></Form.Item></> : <Form.Item name="title" label="资料标题"><Input placeholder="不填写时使用文件名；系统会自动识别资料类型" /></Form.Item>}
        </Form>
      </Modal>
    </section>
  );
}

function StatisticCard({ title, value }: { title: string; value: number }) {
  return <Card size="small"><Statistic title={title} value={value} /></Card>;
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
