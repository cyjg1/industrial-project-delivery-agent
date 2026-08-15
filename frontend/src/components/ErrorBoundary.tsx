import { App as AntdApp, Button, Result, Typography } from "antd";
import { Component, type ErrorInfo, type ReactNode } from "react";

type ErrorBoundaryProps = {
  children: ReactNode;
  title?: string;
  scope?: string;
  onReset?: () => void;
};

type ErrorBoundaryState = {
  error: Error | null;
  componentStack: string;
};

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null, componentStack: "" };

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    this.setState({ componentStack: info.componentStack || "" });
    console.error("[ErrorBoundary]", this.props.scope || "root", error);
  }

  handleReset = () => {
    this.setState({ error: null, componentStack: "" });
    this.props.onReset?.();
  };

  render() {
    const { error, componentStack } = this.state;
    if (!error) return this.props.children;
    return (
      <ErrorFallback
        title={this.props.title || "界面出现异常"}
        scope={this.props.scope || "root"}
        error={error}
        componentStack={componentStack}
        onReset={this.handleReset}
      />
    );
  }
}

type ErrorFallbackProps = {
  title: string;
  scope: string;
  error: Error;
  componentStack: string;
  onReset: () => void;
};

function ErrorFallback({ title, scope, error, componentStack, onReset }: ErrorFallbackProps) {
  const { message } = AntdApp.useApp();
  const detail = [
    `范围：${scope}`,
    `时间：${new Date().toISOString()}`,
    `地址：${window.location.href}`,
    `错误：${error.name}: ${error.message}`,
    error.stack ? `堆栈：\n${error.stack}` : "",
    componentStack ? `组件树：${componentStack}` : "",
  ].filter(Boolean).join("\n");

  async function copyDetail() {
    try {
      await navigator.clipboard.writeText(detail);
      message.success("错误详情已复制到剪贴板");
    } catch {
      message.error("复制失败，请手动选中下方详情复制");
    }
  }

  return (
    <section className="error-boundary-stage" aria-label="界面异常">
      <Result
        status="error"
        title={title}
        subTitle="该区域渲染失败，其他区域仍可继续使用。"
        extra={[
          <Button key="reset" type="primary" onClick={onReset}>重新加载</Button>,
          <Button key="copy" onClick={copyDetail}>复制错误详情</Button>,
        ]}
      >
        <Typography.Paragraph type="secondary" className="error-boundary-detail">
          {error.name}: {error.message}
        </Typography.Paragraph>
      </Result>
    </section>
  );
}
