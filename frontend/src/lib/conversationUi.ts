import type { ActiveView, ChatMessage } from "./ui";
import type { ConversationMessage, ConversationModelRound, ConversationToolStep } from "../types";

const VIEW_PAYLOAD: Record<ActiveView, string> = {
  chat: "overview",
  progress: "milestone",
  calendar: "overview",
  tasks: "three_lists",
  threeLists: "three_lists",
  people: "people",
  review: "review",
  knowledge: "overview",
  files: "overview",
};

export function panelView(activeView: ActiveView) {
  return VIEW_PAYLOAD[activeView] || "overview";
}

export function lastAgentMessage(messages: ChatMessage[]) {
  return [...messages].reverse().find((message) => message.role === "agent");
}

export function rowsToChatMessages(rows: ConversationMessage[]): ChatMessage[] {
  return rows
    .filter((row) => row.role === "user" || row.role === "assistant")
    .map((row) => ({
      id: row.message_id,
      role: row.role === "user" ? "user" : "agent",
      content: row.content,
      createdAt: row.created_at,
      toolSteps: toolStepsFrom(row.metadata),
      modelRounds: modelRoundsFrom(row.metadata),
      verification: verificationFrom(row.metadata),
      stopReason: typeof row.metadata.stop_reason === "string" ? row.metadata.stop_reason : undefined,
      provider: typeof row.metadata.provider === "string" ? row.metadata.provider : undefined,
      model: typeof row.metadata.model === "string" ? row.metadata.model : undefined,
    }));
}

export function mergeToolStep(steps: ConversationToolStep[], next: ConversationToolStep) {
  const key = (step: ConversationToolStep) => step.call_id || `${step.round_index}:${step.tool_name}`;
  const index = steps.findIndex((step) => key(step) === key(next));
  if (index < 0) return [...steps, next];
  return steps.map((step, currentIndex) => (currentIndex === index ? { ...step, ...next } : step));
}

function toolStepsFrom(metadata: Record<string, unknown>): ConversationToolStep[] {
  const raw = metadata.tool_steps;
  return Array.isArray(raw) ? raw as ConversationToolStep[] : [];
}

function modelRoundsFrom(metadata: Record<string, unknown>): ConversationModelRound[] {
  const raw = metadata.model_rounds;
  return Array.isArray(raw) ? raw as ConversationModelRound[] : [];
}

function verificationFrom(metadata: Record<string, unknown>): ChatMessage["verification"] {
  const raw = metadata.verification;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return undefined;
  return raw as ChatMessage["verification"];
}
