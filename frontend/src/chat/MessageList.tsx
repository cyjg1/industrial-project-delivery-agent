import { DownOutlined } from "@ant-design/icons";
import { Button } from "antd";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Bubble } from "@ant-design/x";
import { agentAvatar, renderAgentMessage } from "./AgentBubble";
import type { ChatMessage } from "../lib/ui";

type MessageListProps = {
  messages: ChatMessage[];
};

// 距底部小于该值就认为用户还在跟读，超过则视为主动往上翻，暂停自动跟随。
const FOLLOW_THRESHOLD = 80;

const shellStyle: CSSProperties = {
  position: "relative",
  flex: "1 1 auto",
  minHeight: 0,
  display: "flex",
  flexDirection: "column",
};

const jumpStyle: CSSProperties = {
  position: "absolute",
  left: "50%",
  bottom: 16,
  transform: "translateX(-50%)",
  zIndex: 3,
  boxShadow: "var(--shadow-md)",
};

export function MessageList({ messages }: MessageListProps) {
  const listRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);
  const [following, setFollowing] = useState(true);

  const items = useMemo(
    () => messages.map((message) => ({
      key: message.id,
      placement: message.role === "user" ? ("end" as const) : ("start" as const),
      avatar: agentAvatar(message),
      variant: message.role === "user" ? ("filled" as const) : ("outlined" as const),
      shape: "corner" as const,
      content: message.content,
      messageRender: message.role === "agent"
        ? (content: string) => renderAgentMessage(String(content), message)
        : undefined,
      header: message.role === "agent" ? "项目助理" : "你",
      footer: message.model || message.provider
        ? `模型：${message.model || message.provider}`
        : message.createdAt || "",
    })),
    [messages],
  );

  const scrollSignature = useMemo(() => {
    const last = messages[messages.length - 1];
    return `${messages.length}:${last?.id || ""}:${last?.content.length || 0}:${last?.toolSteps?.length || 0}`;
  }, [messages]);

  const jumpToBottom = useCallback(() => {
    const node = listRef.current;
    if (!node) return;
    followRef.current = true;
    setFollowing(true);
    // 直接跳到底部：平滑滚动的中间帧会被下面的 scroll 监听误判成“用户又往上翻了”。
    node.scrollTop = node.scrollHeight;
  }, []);

  useEffect(() => {
    const node = listRef.current;
    if (!node) return;
    const handleScroll = () => {
      const near = node.scrollHeight - node.scrollTop - node.clientHeight < FOLLOW_THRESHOLD;
      if (followRef.current === near) return;
      followRef.current = near;
      setFollowing(near);
    };
    node.addEventListener("scroll", handleScroll, { passive: true });
    return () => node.removeEventListener("scroll", handleScroll);
  }, []);

  useEffect(() => {
    const node = listRef.current;
    if (!node || !followRef.current) return;
    requestAnimationFrame(() => {
      if (!followRef.current) return;
      node.scrollTop = node.scrollHeight;
    });
  }, [scrollSignature]);

  return (
    <div style={shellStyle}>
      <div className="message-list" ref={listRef} style={{ scrollBehavior: "auto" }}>
        <Bubble.List items={items} />
      </div>
      {following ? null : (
        <Button size="small" shape="round" icon={<DownOutlined />} style={jumpStyle} onClick={jumpToBottom}>
          回到底部
        </Button>
      )}
    </div>
  );
}
