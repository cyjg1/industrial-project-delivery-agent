import { PaperClipOutlined, UserAddOutlined } from "@ant-design/icons";
import { Attachments, Sender } from "@ant-design/x";
import { Button, Dropdown, Segmented, Select, Space, Tag, Tooltip } from "antd";
import { useMemo, useState } from "react";
import type { StreamConnectionState } from "../api/client";
import type { IngestionInputKind } from "../types";
import { ProviderSettingsButton } from "./ProviderSettingsButton";

type SenderBarProps = {
  selectedFiles: File[];
  inputKind: IngestionInputKind;
  selectedModel: string;
  modelOptions: Array<{ label: string; value: string }>;
  loading: boolean;
  connectionState?: StreamConnectionState | null;
  mentionNames?: string[];
  onFilesChange: (files: File[]) => void;
  onInputKindChange: (value: IngestionInputKind) => void;
  onModelChange: (model: string) => void;
  onProviderConfigured: () => Promise<void>;
  onSend: (message: string, model: string) => Promise<void>;
};

export function SenderBar({
  selectedFiles,
  inputKind,
  selectedModel,
  modelOptions,
  loading,
  connectionState,
  mentionNames = [],
  onFilesChange,
  onInputKindChange,
  onModelChange,
  onProviderConfigured,
  onSend,
}: SenderBarProps) {
  const [value, setValue] = useState("");

  const mentionItems = useMemo(
    () => mentionNames.slice(0, 30).map((name) => ({ key: name, label: name })),
    [mentionNames],
  );

  function insertMention(name: string) {
    setValue((current) => (current && !current.endsWith(" ") ? `${current} @${name} ` : `${current}@${name} `));
  }

  async function submit(message: string) {
    const text = message.trim();
    if (!text && selectedFiles.length === 0) return;
    setValue("");
    try {
      await onSend(text, selectedModel);
    } catch {
      setValue((current) => current || text);
    }
  }

  function addFiles(files: File[]) {
    const byKey = new Map(selectedFiles.map((file) => [fileKey(file), file]));
    files.forEach((file) => byKey.set(fileKey(file), file));
    onFilesChange(Array.from(byKey.values()));
  }

  function removeFile(target: File) {
    const targetKey = fileKey(target);
    onFilesChange(selectedFiles.filter((file) => fileKey(file) !== targetKey));
  }

  return (
    <div
      className="sender-shell"
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        event.preventDefault();
        addFiles(Array.from(event.dataTransfer.files || []));
      }}
    >
      {selectedFiles.length ? (
        <div className="attachment-line">
          {selectedFiles.map((file) => (
            <Tag key={fileKey(file)} closable onClose={() => removeFile(file)}>
              {file.name}
            </Tag>
          ))}
          <Segmented
            size="small"
            aria-label="附件内容类型"
            value={inputKind}
            options={[
              { label: "自动识别", value: "auto" },
              { label: "会议纪要", value: "minutes" },
              { label: "原始转写", value: "transcript" },
              { label: "交付物", value: "deliverable" },
            ]}
            onChange={(value) => onInputKindChange(value as IngestionInputKind)}
          />
        </div>
      ) : null}
      {connectionState ? (
        <div className="sender-connection-status" role="status">
          {connectionStateLabel(connectionState)}
        </div>
      ) : null}
      <Sender
        value={value}
        loading={loading}
        placeholder="直接问项目问题，或拖入会议纪要、三清单、人员表等文件。"
        submitType="enter"
        autoSize={{ minRows: 2, maxRows: 5 }}
        onChange={setValue}
        onSubmit={submit}
        onPasteFile={(firstFile) => addFiles([firstFile])}
        prefix={
          <Select
            size="small"
            value={selectedModel}
            options={modelOptions}
            onChange={onModelChange}
            disabled={loading || modelOptions.length === 0}
            popupMatchSelectWidth={false}
          />
        }
        actions={(origin) => (
          <Space size={4}>
            <ProviderSettingsButton disabled={loading} onConfigured={onProviderConfigured} />
            {mentionItems.length ? (
              <Dropdown
                trigger={["click"]}
                menu={{ items: mentionItems, onClick: ({ key }) => insertMention(key) }}
                placement="topLeft"
              >
                <Tooltip title="提及项目成员">
                  <Button type="text" icon={<UserAddOutlined />} aria-label="提及项目成员" />
                </Tooltip>
              </Dropdown>
            ) : null}
            <Attachments
              multiple
              items={selectedFiles.map((file) => ({
                uid: fileKey(file),
                name: file.name,
                status: "done" as const,
              }))}
              beforeUpload={(file) => {
                addFiles([file]);
                return false;
              }}
              placeholder={{ title: "拖入多个文件", description: "会议纪要 / 表格 / 原始转写 / 交付物" }}
              showUploadList={false}
            >
              <Button type="text" icon={<PaperClipOutlined />} aria-label="附件" />
            </Attachments>
            {origin}
          </Space>
        )}
      />
    </div>
  );
}

function fileKey(file: File) {
  return `${file.name}:${file.size}:${file.lastModified}`;
}

function connectionStateLabel(state: StreamConnectionState) {
  if (state.status === "connecting") return "正在连接 Agent";
  if (state.status === "reconnecting") {
    return `连接中断，正在进行第 ${state.attempt} 次恢复`;
  }
  return "Agent 正在运行";
}
