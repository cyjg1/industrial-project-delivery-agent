import { Avatar } from "antd";
import { memo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage } from "../lib/ui";
import "./AgentBubble.css";

const markdownComponents: Components = {
  a({ node: _node, href, ...props }) {
    const external = Boolean(href?.startsWith("https://") || href?.startsWith("http://"));
    return (
      <a
        {...props}
        href={href}
        {...(external ? { target: "_blank", rel: "noopener noreferrer" } : {})}
      />
    );
  },
  table({ node: _node, ...props }) {
    return (
      <div className="markdown-table-wrap" role="region" aria-label="Markdown table" tabIndex={0}>
        <table {...props} />
      </div>
    );
  },
};

export function agentAvatar(message: ChatMessage) {
  return message.role === "agent" ? (
    <Avatar className="agent-avatar">AI</Avatar>
  ) : (
    <Avatar className="user-avatar">我</Avatar>
  );
}

// 流式回答每个 delta 都会重渲染整条消息，Markdown 只在正文真正变化时重新解析，
// 长回答里其它已完成的消息不会被反复 parse。
const MarkdownBody = memo(function MarkdownBody({ content }: { content: string }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
        {content}
      </ReactMarkdown>
    </div>
  );
});

const ToolSteps = memo(function ToolSteps({ steps }: { steps: NonNullable<ChatMessage["toolSteps"]> }) {
  const hasRunningStep = steps.some((step) => step.status === "running");
  return (
    <details className="tool-steps" open={hasRunningStep}>
      <summary>工具调用 {steps.length} 步</summary>
      <ol>
        {steps.map((step) => (
          <li key={`${step.round_index}-${step.tool_name}`}>
            <div className="tool-step-line">
              <span>{step.status === "running" ? "▸" : "✓"}</span>
              <strong>{toolLabel(step.tool_name)}</strong>
              <span>{step.status === "running" ? "正在" : "完成"}</span>
              {step.reason ? <span>原因：{step.reason}</span> : null}
            </div>
            <details className="tool-step-detail">
              <summary>参数和摘要</summary>
              <pre>{JSON.stringify({ arguments: step.arguments, summary: step.summary, result: step.result }, null, 2)}</pre>
            </details>
          </li>
        ))}
      </ol>
    </details>
  );
});

export const AgentMessageContent = memo(function AgentMessageContent({
  content,
  toolSteps,
}: {
  content: string;
  toolSteps?: ChatMessage["toolSteps"];
}) {
  return (
    <div className="agent-message-content">
      {toolSteps?.length ? <ToolSteps steps={toolSteps} /> : null}
      {content ? <MarkdownBody content={content} /> : null}
    </div>
  );
});

export function renderAgentMessage(content: string, message?: ChatMessage) {
  return <AgentMessageContent content={content} toolSteps={message?.toolSteps} />;
}

function toolLabel(name: string) {
  const labels: Record<string, string> = {
    search_memory: "正在检索项目记忆",
    get_tasks: "正在查询任务池",
    get_milestone_status: "正在读取里程碑",
    get_person: "正在查询人员资产",
    ingest_file: "正在处理上传文件",
    draft: "正在起草内容",
    propose_candidates: "正在生成候选",
    search_methods: "正在检索方法库",
    generate_daily_brief: "正在生成晨报",
  };
  return labels[name] || name;
}
