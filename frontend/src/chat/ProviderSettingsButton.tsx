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


const PROVIDER_PRESETS = [
  {
    label: "阿里云百炼",
    value: "aliyun_bailian",
    defaultModel: "qwen-plus",
    baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
  },
  {
    label: "火山引擎方舟",
    value: "volcengine_ark",
    defaultModel: "",
    baseUrl: "https://ark.cn-beijing.volces.com/api/v3",
  },
  {
    label: "智谱开放平台",
    value: "glm",
    defaultModel: "glm-4-flash",
    baseUrl: "https://open.bigmodel.cn/api/paas/v4",
  },
  {
    label: "DeepSeek 开放平台",
    value: "deepseek",
    defaultModel: "deepseek-v4-flash",
    baseUrl: "https://api.deepseek.com",
  },
  {
    label: "其他 OpenAI 兼容服务",
    value: "openai_compatible",
    defaultModel: "",
    baseUrl: "",
  },
] as const;

const PROVIDERS = PROVIDER_PRESETS.map(({ label, value }) => ({ label, value }));

function findPreset(provider: string) {
  return PROVIDER_PRESETS.find((item) => item.value === provider) ?? PROVIDER_PRESETS[0];
}

type ProviderSettingsButtonProps = {
  disabled?: boolean;
  compact?: boolean;
  onConfigured: () => Promise<void>;
};

export function ProviderSettingsButton({ disabled, compact = false, onConfigured }: ProviderSettingsButtonProps) {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<RuntimeProviderStatus | null>(null);
  const [form, setForm] = useState<RuntimeProviderConfigInput>({
    provider: "aliyun_bailian",
    model: "qwen-plus",
    api_key: "",
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
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
      const provider = next.provider || "aliyun_bailian";
      const preset = findPreset(provider);
      setForm({
        provider,
        model: next.model || preset.defaultModel,
        api_key: "",
        base_url: next.base_url || preset.baseUrl,
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
    const preset = findPreset(provider);
    setForm((current) => ({
      ...current,
      provider,
      model: preset.defaultModel,
      base_url: preset.baseUrl,
    }));
  }

  const selectedPreset = findPreset(form.provider);
  const modelNeedsInput = !selectedPreset.defaultModel;

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
            <Typography.Text strong>模型名称 / 推理接入点 ID</Typography.Text>
            <Input
              value={form.model}
              placeholder={
                form.provider === "volcengine_ark"
                  ? "请从方舟控制台复制完整的模型或推理接入点 ID"
                  : "请从服务商控制台复制完整的模型 ID"
              }
              style={{ marginTop: 6 }}
              onChange={(event) => setForm((current) => ({ ...current, model: event.target.value }))}
            />
            <Typography.Text type="secondary" style={{ display: "block", marginTop: 4 }}>
              {modelNeedsInput
                ? "此项必填。请直接从服务商控制台复制，版本号、大小写和连字符均需完全一致。"
                : `已自动填入常用模型 ${selectedPreset.defaultModel}。如果账号无法使用，请从服务商控制台复制完整模型 ID 替换，版本号、大小写和连字符均需完全一致。`}
            </Typography.Text>
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
          <label>
            <Typography.Text strong>接口地址</Typography.Text>
            <Input
              value={form.base_url}
              placeholder="https://provider.example/v1"
              style={{ marginTop: 6 }}
              onChange={(event) => setForm((current) => ({ ...current, base_url: event.target.value }))}
            />
            <Typography.Text type="secondary" style={{ display: "block", marginTop: 4 }}>
              国内平台已自动填写，通常无需修改。
            </Typography.Text>
          </label>
        </Space>
      </Modal>
    </>
  );
}
