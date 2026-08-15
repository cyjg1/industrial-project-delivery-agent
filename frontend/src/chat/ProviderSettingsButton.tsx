import { ApiOutlined } from "@ant-design/icons";
import { Alert, Button, Input, Modal, Select, Space, Tooltip, Typography } from "antd";
import { useState } from "react";

import {
  clearRuntimeProviderConfig,
  loadRuntimeProviderConfig,
  saveRuntimeProviderConfig,
  testRuntimeProviderConfig,
} from "../api/client";
import type { RuntimeProviderConfigInput, RuntimeProviderStatus } from "../types";


const PROVIDERS = [
  { label: "OpenAI", value: "openai" },
  { label: "智谱 GLM", value: "glm" },
  { label: "OpenAI Compatible", value: "openai_compatible" },
];

const DEFAULT_BASE_URLS: Record<string, string> = {
  openai: "",
  glm: "https://open.bigmodel.cn/api/paas/v4",
  openai_compatible: "https://dashscope.aliyuncs.com/compatible-mode/v1",
};

type ProviderSettingsButtonProps = {
  disabled?: boolean;
  compact?: boolean;
  onConfigured: () => Promise<void>;
};

export function ProviderSettingsButton({ disabled, compact = false, onConfigured }: ProviderSettingsButtonProps) {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<RuntimeProviderStatus | null>(null);
  const [form, setForm] = useState<RuntimeProviderConfigInput>({
    provider: "openai_compatible",
    model: "",
    api_key: "",
    base_url: DEFAULT_BASE_URLS.openai_compatible,
    api_surface: "chat_completions",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  async function showSettings() {
    setOpen(true);
    setError("");
    setSuccess("");
    try {
      const next = await loadRuntimeProviderConfig();
      setStatus(next);
      const provider = next.provider || "openai_compatible";
      setForm({
        provider,
        model: next.model || "",
        api_key: "",
        base_url: next.base_url || DEFAULT_BASE_URLS[provider] || "",
        api_surface: next.api_surface || "chat_completions",
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取模型配置");
    }
  }

  async function saveAndTest() {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      const next = await saveRuntimeProviderConfig(form);
      setStatus(next);
      await testRuntimeProviderConfig();
      await onConfigured();
      setForm((current) => ({ ...current, api_key: "" }));
      setSuccess("连接测试成功。API Key 仅保存在当前后端进程内存中。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "模型配置或连接测试失败");
    } finally {
      setBusy(false);
    }
  }

  async function clearConfig() {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      const next = await clearRuntimeProviderConfig();
      setStatus(next);
      setForm((current) => ({ ...current, api_key: "", model: "" }));
      await onConfigured();
      setSuccess("本次运行中的模型配置已清除。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "清除配置失败");
    } finally {
      setBusy(false);
    }
  }

  function changeProvider(provider: string) {
    setForm((current) => ({
      ...current,
      provider,
      base_url: DEFAULT_BASE_URLS[provider] || "",
    }));
  }

  return (
    <>
      <Tooltip title="配置评委自己的模型 API">
        <Button
          type={compact ? "text" : "default"}
          size={compact ? undefined : "middle"}
          icon={<ApiOutlined />}
          disabled={disabled}
          aria-label="配置模型 API"
          onClick={showSettings}
        >
          {compact ? null : "填写 API Key"}
        </Button>
      </Tooltip>
      <Modal
        open={open}
        title="本地模型 API 设置"
        onCancel={() => setOpen(false)}
        footer={[
          <Button key="clear" danger disabled={busy || !status?.configured} onClick={clearConfig}>
            清除配置
          </Button>,
          <Button key="cancel" disabled={busy} onClick={() => setOpen(false)}>
            关闭
          </Button>,
          <Button key="save" type="primary" loading={busy} onClick={saveAndTest}>
            保存并测试
          </Button>,
        ]}
        destroyOnHidden
      >
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Alert
            type="info"
            showIcon
            message="密钥不会写入仓库、浏览器存储或项目数据库，关闭后端进程后自动失效。连接测试会产生一次极小的模型调用。"
          />
          {error ? <Alert type="error" showIcon message={error} /> : null}
          {success ? <Alert type="success" showIcon message={success} /> : null}
          <label>
            <Typography.Text strong>服务商</Typography.Text>
            <Select
              value={form.provider}
              options={PROVIDERS}
              style={{ width: "100%", marginTop: 6 }}
              onChange={changeProvider}
            />
          </label>
          <label>
            <Typography.Text strong>模型名称</Typography.Text>
            <Input
              value={form.model}
              placeholder="例如：gpt-4.1-mini 或服务商提供的模型 ID"
              style={{ marginTop: 6 }}
              onChange={(event) => setForm((current) => ({ ...current, model: event.target.value }))}
            />
          </label>
          <label>
            <Typography.Text strong>API Key</Typography.Text>
            <Input.Password
              value={form.api_key}
              autoComplete="new-password"
              placeholder={status?.api_key_configured ? "已配置；留空可继续使用当前密钥" : "请输入评委自己的 API Key"}
              style={{ marginTop: 6 }}
              onChange={(event) => setForm((current) => ({ ...current, api_key: event.target.value }))}
            />
          </label>
          {form.provider !== "openai" ? (
            <label>
              <Typography.Text strong>Base URL</Typography.Text>
              <Input
                value={form.base_url}
                placeholder="https://provider.example/v1"
                style={{ marginTop: 6 }}
                onChange={(event) => setForm((current) => ({ ...current, base_url: event.target.value }))}
              />
            </label>
          ) : null}
        </Space>
      </Modal>
    </>
  );
}
