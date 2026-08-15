import type {
  ConversationMessage,
  ConversationModelCatalog,
  ConversationResponse,
  ConversationSession,
  ConversationToolStep,
  DailyBrief,
  DailyWorkRecord,
  Deliverable,
  FileAnalysisWorkspace,
  IngestionInputKind,
  IngestionJob,
  ProjectCalendarResponse,
  ProjectFilesWorkspace,
  RuntimeProviderConfigInput,
  RuntimeProviderStatus,
  SwitchableUser,
  Workspace,
} from "../types";
import { validateWorkspacePayload } from "./workspaceValidation";
import { parseSseEvent, SseReplayCursor, type ParsedSseEvent } from "./sse";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "";
const DEFAULT_ACTOR_ID = "u_pmo";
let currentActorId = DEFAULT_ACTOR_ID;

export function setCurrentActorId(actorId: string) {
  currentActorId = actorId || DEFAULT_ACTOR_ID;
}

export function getCurrentActorId() {
  return currentActorId;
}

export type ThreeListPayload = {
  title?: string;
  description?: string;
  status?: string;
  owner_candidates?: string[];
  due_date?: string;
  deliverable?: string;
  acceptance_criteria?: string;
  linked_issue_ids?: string[];
  linked_task_ids?: string[];
  notes?: string;
};

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const isFormData = options?.body instanceof FormData;
  const headers = {
    "X-Actor-Id": currentActorId,
    ...(options?.headers || {})
  };
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: isFormData
      ? headers
      : {
          "Content-Type": "application/json",
          ...headers
        },
  });

  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `Request failed with HTTP ${response.status}`);
  }

  return response.json() as Promise<T>;
}

export function loadWorkspace(actorId?: string): Promise<Workspace> {
  return request<unknown>("/api/workspace", actorId ? {
    headers: { "X-Actor-Id": actorId },
  } : undefined).then(validateWorkspacePayload);
}

export type SkillPackageSummary = {
  name: string;
  description: string;
  version: string;
  origin: "external" | "project";
  status: string;
  error: string;
  folder: string;
  allowed_tools: string[];
  resources: string[];
  body_chars: number;
};

export type ContractSkill = {
  capability_id: string;
  name: string;
  description: string;
  version: string;
  origin: "contract";
  status: string;
  error: string;
  when_to_use: string;
  tool_names: string[];
};

export type SkillPackagesResponse = {
  packages: SkillPackageSummary[];
  external_skills_dir: string;
  contract_skills: ContractSkill[];
};

export function loadSkillPackages(): Promise<SkillPackagesResponse> {
  return request<SkillPackagesResponse>("/api/skills/packages");
}

export function loadSwitchableUsers(actorId?: string): Promise<SwitchableUser[]> {
  return request<{ users: SwitchableUser[] }>("/api/dev/switchable-users", actorId ? {
    headers: { "X-Actor-Id": actorId },
  } : undefined).then((payload) => payload.users);
}

export async function archiveAndClearWorkspace(): Promise<{
  archive: { items: number; tasks: number; runs: number; sessions: number };
  preserved_sources: number;
  workspace: Workspace;
}> {
  const result = await request<{
    archive: { items: number; tasks: number; runs: number; sessions: number };
    preserved_sources: number;
    workspace: unknown;
  }>("/api/workspace/archive-and-clear", {
    method: "POST",
    body: JSON.stringify({ confirmation: "ARCHIVE" }),
  });
  return { ...result, workspace: validateWorkspacePayload(result.workspace) };
}

export async function generateDailyBrief(pushFeishu = false): Promise<{ brief: DailyBrief; workspace: Workspace }> {
  const result = await request<{ brief: DailyBrief; workspace: unknown }>("/api/brief/generate", {
    method: "POST",
    body: JSON.stringify({ push_feishu: pushFeishu })
  });
  return { brief: result.brief, workspace: validateWorkspacePayload(result.workspace) };
}

export async function generateDailyJournal(reportDate?: string): Promise<Workspace> {
  const result = await request<{ journal: Record<string, unknown>; workspace: unknown }>("/api/daily-journal/generate", {
    method: "POST",
    body: JSON.stringify({ report_date: reportDate || null })
  });
  return validateWorkspacePayload(result.workspace);
}

export function loadDailyWorkRecords(): Promise<DailyWorkRecord[]> {
  return request<{ records: DailyWorkRecord[] }>("/api/daily-work-records")
    .then((payload) => payload.records);
}

export function updateDailyWorkRecord(
  recordId: string,
  patch: Partial<Pick<DailyWorkRecord, "record_date" | "kind" | "text" | "solution_options">>,
): Promise<DailyWorkRecord> {
  return request<{ record: DailyWorkRecord }>(`/api/daily-work-records/${encodeURIComponent(recordId)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  }).then((payload) => payload.record);
}

export type DeliverablePayload = {
  title?: string;
  type_label?: string;
  acceptance_criteria?: string;
  due_date?: string;
  required?: boolean;
  sensitivity?: string;
  sort_order?: number;
  status?: string;
};

export function loadDeliverables(milestoneId: string): Promise<Deliverable[]> {
  return request<{ deliverables: Deliverable[] }>(
    `/api/deliverables?milestone_id=${encodeURIComponent(milestoneId)}`,
  )
    .then((payload) => payload.deliverables);
}

export function createDeliverable(payload: DeliverablePayload): Promise<Deliverable> {
  return request<{ deliverable: Deliverable }>("/api/deliverables", {
    method: "POST",
    body: JSON.stringify(payload),
  }).then((result) => result.deliverable);
}

export function updateDeliverable(deliverableId: string, patch: DeliverablePayload): Promise<Deliverable> {
  return request<{ deliverable: Deliverable }>(`/api/deliverables/${encodeURIComponent(deliverableId)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  }).then((result) => result.deliverable);
}

export async function downloadDeliverableFile(fileId: string, filename: string): Promise<void> {
  const response = await fetch(`${API_BASE}/api/deliverable-files/${encodeURIComponent(fileId)}/download`, {
    headers: { "X-Actor-Id": currentActorId },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `下载失败，HTTP ${response.status}`);
  }
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

export async function compileProjectSkill(methodId: string): Promise<Workspace> {
  const result = await request<{ skill: Record<string, unknown>; workspace: unknown }>("/api/project-skills/compile", {
    method: "POST",
    body: JSON.stringify({ method_id: methodId })
  });
  return validateWorkspacePayload(result.workspace);
}

export async function testProjectSkill(skillId: string): Promise<Workspace> {
  const result = await request<{ skill: Record<string, unknown>; workspace: unknown }>(`/api/project-skills/${encodeURIComponent(skillId)}/test`, {
    method: "POST",
    body: JSON.stringify({})
  });
  return validateWorkspacePayload(result.workspace);
}

export async function publishProjectSkill(skillId: string): Promise<Workspace> {
  const result = await request<{ skill: Record<string, unknown>; workspace: unknown }>(`/api/project-skills/${encodeURIComponent(skillId)}/publish`, {
    method: "POST",
    body: JSON.stringify({})
  });
  return validateWorkspacePayload(result.workspace);
}

export function loadFileWorkspace(): Promise<FileAnalysisWorkspace> {
  return request<FileAnalysisWorkspace>("/api/file-workspace");
}

export function runFileAnalysisStep(stepId: string): Promise<FileAnalysisWorkspace> {
  return request<FileAnalysisWorkspace>(`/api/file-workspace/steps/${stepId}/run`, {
    method: "POST",
    body: JSON.stringify({})
  });
}

export function confirmFileAnalysisStep(stepId: string, notes: string): Promise<FileAnalysisWorkspace> {
  return request<FileAnalysisWorkspace>(`/api/file-workspace/steps/${stepId}/confirm`, {
    method: "POST",
    body: JSON.stringify({ notes })
  });
}

export async function uploadMeetingNote(payload: {
  file: File;
  rawFile?: File | null;
  title: string;
  meetingDate: string;
  topic: string;
  inputKind: IngestionInputKind;
}): Promise<{ job: IngestionJob; source: Record<string, unknown> }> {
  const body = new FormData();
  body.append("file", payload.file);
  if (payload.rawFile) {
    body.append("raw_file", payload.rawFile);
  }
  body.append("title", payload.title);
  body.append("meeting_date", payload.meetingDate);
  body.append("topic", payload.topic);
  body.append("input_kind", payload.inputKind);
  return request<{ job: IngestionJob; source: Record<string, unknown> }>("/api/sources/upload", {
    method: "POST",
    body
  });
}

export function loadProjectFiles(): Promise<ProjectFilesWorkspace> {
  return request<ProjectFilesWorkspace>("/api/project-files");
}

export function createProjectFolder(parent: string, name: string): Promise<ProjectFilesWorkspace> {
  return request<{ workspace: ProjectFilesWorkspace }>("/api/project-files/folders", {
    method: "POST",
    body: JSON.stringify({ parent, name }),
  }).then((payload) => payload.workspace);
}

export function uploadProjectFiles(payload: {
  files: File[];
  folder: string;
  uploadType: "minutes" | "project_document";
  title?: string;
  meetingDate?: string;
  topic?: string;
}): Promise<ProjectFilesWorkspace> {
  const body = new FormData();
  payload.files.forEach((file) => body.append("files", file));
  body.append("folder", payload.folder);
  body.append("upload_type", payload.uploadType);
  body.append("title", payload.title || "");
  body.append("meeting_date", payload.meetingDate || "");
  body.append("topic", payload.topic || "");
  return request<{ workspace: ProjectFilesWorkspace }>("/api/project-files/upload", {
    method: "POST",
    body,
  }).then((result) => result.workspace);
}

export async function downloadProjectFile(path: string, filename: string): Promise<void> {
  const response = await fetch(`${API_BASE}/api/project-files/download?path=${encodeURIComponent(path)}`, {
    headers: { "X-Actor-Id": currentActorId },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `下载失败：HTTP ${response.status}`);
  }
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

export function retryIngestionJob(jobId: string): Promise<IngestionJob> {
  return request<{ job: IngestionJob }>(`/api/ingestion/jobs/${encodeURIComponent(jobId)}/retry`, {
    method: "POST",
    body: JSON.stringify({}),
  }).then((payload) => payload.job);
}

export async function uploadPeopleAsset(file: File): Promise<Workspace> {
  const body = new FormData();
  body.append("file", file);
  const result = await request<{ workspace: unknown }>("/api/assets/people/upload", {
    method: "POST",
    body
  });
  return validateWorkspacePayload(result.workspace);
}

export function loadProjectCalendar(month: string): Promise<ProjectCalendarResponse> {
  return request<ProjectCalendarResponse>(`/api/project-calendar?month=${encodeURIComponent(month)}`);
}

export async function importPeopleFromTemplate(file: File): Promise<{
  workspace: Workspace;
  summary: {
    people_added: number;
    people_updated: number;
    groups_added: number;
    people_total: number;
    groups_total: number;
  };
}> {
  const body = new FormData();
  body.append("file", file);
  const result = await request<{ workspace: unknown; import_summary: {
    people_added: number;
    people_updated: number;
    groups_added: number;
    people_total: number;
    groups_total: number;
  } }>("/api/assets/people/import", { method: "POST", body });
  return { workspace: validateWorkspacePayload(result.workspace), summary: result.import_summary };
}

export async function downloadPeopleImportTemplate(): Promise<void> {
  const response = await fetch(`${API_BASE}/api/assets/people/template`, {
    headers: { "X-Actor-Id": currentActorId },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `模板下载失败，HTTP ${response.status}`);
  }
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "人员批量导入模板.xlsx";
  anchor.click();
  URL.revokeObjectURL(url);
}

export async function sendWorkspaceMessage(payload: {
  view: string;
  message: string;
  model: string;
  sessionId?: string | null;
  file?: File | null;
  files?: File[];
  inputKind?: IngestionInputKind;
}): Promise<ConversationResponse> {
  const files = [...(payload.files || []), ...(payload.file ? [payload.file] : [])];
  if (files.length) {
    const body = new FormData();
    body.append("view", payload.view);
    body.append("message", payload.message);
    body.append("model", payload.model);
    if (payload.sessionId) body.append("session_id", payload.sessionId);
    body.append("input_kind", payload.inputKind || "auto");
    files.forEach((file) => body.append("files", file));
    const result = await request<Omit<ConversationResponse, "workspace"> & { workspace: unknown }>("/api/conversation", {
      method: "POST",
      body,
    });
    return { ...result, workspace: validateWorkspacePayload(result.workspace) };
  }
  const result = await request<Omit<ConversationResponse, "workspace"> & { workspace: unknown }>("/api/conversation", {
    method: "POST",
    body: JSON.stringify({
      view: payload.view,
      message: payload.message,
      model: payload.model,
      session_id: payload.sessionId || null,
    }),
  });
  return { ...result, workspace: validateWorkspacePayload(result.workspace) };
}

export function loadConversationModels(): Promise<ConversationModelCatalog> {
  return request<ConversationModelCatalog>("/api/conversation/models");
}

export function loadRuntimeProviderConfig(): Promise<RuntimeProviderStatus> {
  return request<RuntimeProviderStatus>("/api/runtime/provider-config");
}

export function saveRuntimeProviderConfig(payload: RuntimeProviderConfigInput): Promise<RuntimeProviderStatus> {
  return request<RuntimeProviderStatus>("/api/runtime/provider-config", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function clearRuntimeProviderConfig(): Promise<RuntimeProviderStatus> {
  return request<RuntimeProviderStatus>("/api/runtime/provider-config", { method: "DELETE" });
}

export function testRuntimeProviderConfig(): Promise<{ ok: boolean; provider: string; model: string }> {
  return request<{ ok: boolean; provider: string; model: string }>("/api/runtime/provider-config/test", {
    method: "POST",
  });
}

export function loadConversationSessions(actorId?: string): Promise<ConversationSession[]> {
  return request<{ sessions: ConversationSession[] }>("/api/conversation/sessions", actorId ? {
    headers: { "X-Actor-Id": actorId },
  } : undefined).then((payload) => payload.sessions);
}

export function loadConversationMessages(sessionId: string): Promise<ConversationMessage[]> {
  return request<{ messages: ConversationMessage[] }>(`/api/conversation/sessions/${encodeURIComponent(sessionId)}/messages`).then((payload) => payload.messages);
}

export function renameConversationSession(sessionId: string, title: string): Promise<ConversationSession[]> {
  return request<{ sessions: ConversationSession[] }>(`/api/conversation/sessions/${encodeURIComponent(sessionId)}/rename`, {
    method: "POST",
    body: JSON.stringify({ title }),
  }).then((payload) => payload.sessions);
}

export function archiveConversationSession(sessionId: string, archived = true): Promise<ConversationSession[]> {
  return request<{ sessions: ConversationSession[] }>(`/api/conversation/sessions/${encodeURIComponent(sessionId)}/archive`, {
    method: "POST",
    body: JSON.stringify({ archived }),
  }).then((payload) => payload.sessions);
}

export function deleteConversationSession(sessionId: string): Promise<ConversationSession[]> {
  return request<{ sessions: ConversationSession[] }>(`/api/conversation/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  }).then((payload) => payload.sessions);
}

export type StreamConnectionState = {
  status: "connecting" | "streaming" | "reconnecting";
  attempt: number;
  retryDelayMs?: number;
  detail?: string;
};

const MAX_STREAM_ATTEMPTS = 4;
const STREAM_RETRY_BASE_MS = 250;

export function createRequestId(): string {
  return (
    globalThis.crypto?.randomUUID?.()
    ?? `req-${Date.now()}-${Math.random().toString(16).slice(2)}`
  );
}

export function approveExecutionPolicy(policyId: string): Promise<void> {
  return request<Record<string, unknown>>(
    `/api/memory-skill-evolution/policies/${encodeURIComponent(policyId)}/approve`,
    { method: "POST", body: JSON.stringify({}) },
  ).then(() => undefined);
}

export function retireExecutionPolicy(policyId: string): Promise<void> {
  return request<Record<string, unknown>>(
    `/api/memory-skill-evolution/policies/${encodeURIComponent(policyId)}/retire`,
    { method: "POST", body: JSON.stringify({}) },
  ).then(() => undefined);
}

export function approveEnvironmentCognition(cognitionId: string): Promise<void> {
  return request<Record<string, unknown>>(
    `/api/memory-skill-evolution/cognitions/${encodeURIComponent(cognitionId)}/approve`,
    { method: "POST", body: JSON.stringify({}) },
  ).then(() => undefined);
}

export function retireEnvironmentCognition(cognitionId: string): Promise<void> {
  return request<Record<string, unknown>>(
    `/api/memory-skill-evolution/cognitions/${encodeURIComponent(cognitionId)}/retire`,
    { method: "POST", body: JSON.stringify({}) },
  ).then(() => undefined);
}

export async function streamWorkspaceMessage(payload: {
  view: string;
  message: string;
  model: string;
  sessionId?: string | null;
  file?: File | null;
  files?: File[];
  inputKind?: IngestionInputKind;
  onToolStep?: (step: ConversationToolStep) => void;
  onDelta?: (text: string) => void;
  onReset?: () => void;
  onConnectionChange?: (state: StreamConnectionState) => void;
}): Promise<ConversationResponse> {
  const clientRequestId = createRequestId();
  const files = [...(payload.files || []), ...(payload.file ? [payload.file] : [])];
  const body = files.length ? new FormData() : JSON.stringify({
    view: payload.view,
    message: payload.message,
    model: payload.model,
    session_id: payload.sessionId || null,
    client_request_id: clientRequestId,
  });
  if (body instanceof FormData) {
    body.append("view", payload.view);
    body.append("message", payload.message);
    body.append("model", payload.model);
    if (payload.sessionId) body.append("session_id", payload.sessionId);
    body.append("client_request_id", clientRequestId);
    body.append("input_kind", payload.inputKind || "auto");
    files.forEach((file) => body.append("files", file));
  }
  let finalPayload: ConversationResponse | null = null;
  const cursor = new SseReplayCursor();
  let attempt = 0;
  payload.onConnectionChange?.({ status: "connecting", attempt: 1 });
  while (!finalPayload) {
    attempt += 1;
    const isReplay = Boolean(cursor.runId);
    cursor.startAttempt();
    try {
      const response = await fetch(
        isReplay
          ? `${API_BASE}/api/conversation/runs/${encodeURIComponent(cursor.runId)}/events?after=${cursor.lastEventId}`
          : `${API_BASE}/api/conversation/stream`,
        isReplay
          ? { headers: { "X-Actor-Id": currentActorId } }
          : {
              method: "POST",
              headers: body instanceof FormData
                ? { "X-Actor-Id": currentActorId }
                : { "Content-Type": "application/json", "X-Actor-Id": currentActorId },
              body,
            },
      );
      if (!response.ok || !response.body) {
        const errorPayload = await response.json().catch(() => ({}));
        const detail = String(errorPayload.detail || `Request failed with HTTP ${response.status}`);
        if (isRetryableStatus(response.status)) throw new RetryableStreamError(detail);
        throw new NonRetryableStreamError(detail);
      }
      payload.onConnectionChange?.({ status: "streaming", attempt });
      await consumeSse(response.body, (event) => {
        if (!cursor.accept(event)) return;
        if (event.event === "tool_start" || event.event === "tool_result" || event.event === "tool") {
          payload.onToolStep?.(event.data as ConversationToolStep);
        }
        if (event.event === "delta") {
          payload.onDelta?.(String((event.data as { text?: string }).text || ""));
        }
        if (event.event === "model_output_reset" || event.event === "reset") {
          payload.onReset?.();
        }
        if (event.event === "error") {
          const error = event.data as { detail?: string; error_id?: string };
          throw new AgentStreamError(
            `${error.detail || "Agent stream failed"}${error.error_id ? `（${error.error_id}）` : ""}`,
          );
        }
        if (event.event === "final") finalPayload = event.data as ConversationResponse;
      });
      if (!finalPayload) {
        throw new RetryableStreamError("Agent stream ended before the final event");
      }
    } catch (reason) {
      if (reason instanceof AgentStreamError || reason instanceof NonRetryableStreamError) {
        throw reason;
      }
      if (attempt >= MAX_STREAM_ATTEMPTS) throw reason;
      const retryDelayMs = Math.min(
        STREAM_RETRY_BASE_MS * (2 ** (attempt - 1)),
        2000,
      );
      payload.onConnectionChange?.({
        status: "reconnecting",
        attempt: attempt + 1,
        retryDelayMs,
        detail: errorMessage(reason),
      });
      await delay(retryDelayMs);
    }
  }
  const completedPayload = finalPayload as ConversationResponse | null;
  if (!completedPayload) throw new Error("Agent stream ended without final payload");
  return {
    ...completedPayload,
    workspace: validateWorkspacePayload(completedPayload.workspace),
  };
}

class AgentStreamError extends Error {}
class RetryableStreamError extends Error {}
class NonRetryableStreamError extends Error {}

function isRetryableStatus(status: number): boolean {
  return status === 408 || status === 425 || status === 429 || status >= 500;
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason || "连接中断");
}

async function consumeSse(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: ParsedSseEvent) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split(/\r?\n\r?\n/);
      buffer = events.pop() || "";
      for (const eventText of events) {
        const event = parseSseEvent(eventText);
        if (event) onEvent(event);
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) {
      const event = parseSseEvent(buffer);
      if (event) onEvent(event);
    }
  } catch (reason) {
    await reader.cancel(reason).catch(() => undefined);
    throw reason;
  } finally {
    reader.releaseLock();
  }
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

export function confirmItem(itemId: string, notes: string): Promise<unknown> {
  return request(`/api/items/${itemId}/confirm`, {
    method: "POST",
    body: JSON.stringify({ notes })
  });
}

export function rejectItem(itemId: string, notes: string): Promise<unknown> {
  return request(`/api/items/${itemId}/reject`, {
    method: "POST",
    body: JSON.stringify({ notes })
  });
}

export function publishItemAsTask(itemId: string): Promise<Workspace> {
  return request<unknown>(`/api/items/${itemId}/publish-task`, {
    method: "POST",
    body: JSON.stringify({})
  }).then(validateWorkspacePayload);
}

export function archiveTaskCandidate(itemId: string): Promise<Workspace> {
  return request<unknown>(`/api/items/${itemId}/archive-task-candidate`, {
    method: "POST",
    body: JSON.stringify({})
  }).then(validateWorkspacePayload);
}

export function updateCandidateItem(itemId: string, payload: {
  status?: string;
  title?: string;
  description?: string;
  due_date?: string;
  deliverable?: string;
  acceptance_criteria?: string;
  owner_candidates?: string[];
  linked_issue_ids?: string[];
  linked_task_ids?: string[];
  notes?: string;
}): Promise<Workspace> {
  return request<unknown>(`/api/items/${itemId}/edit`, {
    method: "POST",
    body: JSON.stringify(payload)
  }).then(validateWorkspacePayload);
}

export function updateMilestonePlan(payload: {
  name?: string;
  date_start?: string;
  date_end?: string;
  scenario_id?: string;
  chain_name?: string;
  status?: string;
  acceptance_criteria?: string[];
}): Promise<Workspace> {
  return request<unknown>("/api/milestones/config", {
    method: "POST",
    body: JSON.stringify(payload)
  }).then(validateWorkspacePayload);
}

export function updateWorkItem(workItemId: string, payload: {
  title?: string;
  description?: string;
  status?: string;
  due_date?: string;
  deliverable?: string;
  acceptance_criteria?: string;
  professional_id?: string;
  board_id?: string;
  planned_start?: string;
  progress_percent?: number | null;
  owner_candidates?: string[];
  linked_issue_ids?: string[];
  linked_task_ids?: string[];
  notes?: string;
}): Promise<Workspace> {
  return request<unknown>(`/api/work-items/${workItemId}`, {
    method: "POST",
    body: JSON.stringify(payload)
  }).then(validateWorkspacePayload);
}

export function createThreeListIssue(payload: ThreeListPayload): Promise<Workspace> {
  return request<unknown>("/api/three-lists/issues", {
    method: "POST",
    body: JSON.stringify(payload),
  }).then(validateWorkspacePayload);
}

export function updateThreeListIssue(issueId: string, payload: ThreeListPayload): Promise<Workspace> {
  return request<unknown>(`/api/three-lists/issues/${encodeURIComponent(issueId)}`, {
    method: "POST",
    body: JSON.stringify(payload),
  }).then(validateWorkspacePayload);
}

export function archiveThreeListIssue(issueId: string): Promise<Workspace> {
  return request<unknown>(`/api/three-lists/issues/${encodeURIComponent(issueId)}/archive`, {
    method: "POST",
    body: JSON.stringify({ notes: "从三清单归档" }),
  }).then(validateWorkspacePayload);
}

export function createThreeListTask(payload: ThreeListPayload): Promise<Workspace> {
  return request<unknown>("/api/three-lists/tasks", {
    method: "POST",
    body: JSON.stringify(payload),
  }).then(validateWorkspacePayload);
}

export function updateThreeListTask(taskId: string, payload: ThreeListPayload): Promise<Workspace> {
  return request<unknown>(`/api/three-lists/tasks/${encodeURIComponent(taskId)}`, {
    method: "POST",
    body: JSON.stringify(payload),
  }).then(validateWorkspacePayload);
}

export function archiveThreeListTask(taskId: string): Promise<Workspace> {
  return request<unknown>(`/api/three-lists/tasks/${encodeURIComponent(taskId)}/archive`, {
    method: "POST",
    body: JSON.stringify({ notes: "从三清单归档" }),
  }).then(validateWorkspacePayload);
}

export function createThreeListMethod(payload: ThreeListPayload): Promise<Workspace> {
  return request<unknown>("/api/three-lists/methods", {
    method: "POST",
    body: JSON.stringify(payload),
  }).then(validateWorkspacePayload);
}

export function updateThreeListMethod(methodId: string, payload: ThreeListPayload): Promise<Workspace> {
  return request<unknown>(`/api/three-lists/methods/${encodeURIComponent(methodId)}`, {
    method: "POST",
    body: JSON.stringify(payload),
  }).then(validateWorkspacePayload);
}

export function archiveThreeListMethod(methodId: string): Promise<Workspace> {
  return request<unknown>(`/api/three-lists/methods/${encodeURIComponent(methodId)}/archive`, {
    method: "POST",
    body: JSON.stringify({ notes: "从三清单归档" }),
  }).then(validateWorkspacePayload);
}

export function updatePersonProfile(personId: string, payload: {
  name?: string;
  group?: string;
  role?: string;
  path?: string;
  responsibility_note?: string;
  notes?: string;
}): Promise<Workspace> {
  return request<unknown>(`/api/people/${encodeURIComponent(personId)}`, {
    method: "POST",
    body: JSON.stringify(payload)
  }).then(validateWorkspacePayload);
}

export function createPeopleGroup(payload: { name: string; path?: string; notes?: string }): Promise<Workspace> {
  return request<unknown>("/api/people/groups", {
    method: "POST",
    body: JSON.stringify(payload),
  }).then(validateWorkspacePayload);
}

export function updatePeopleGroup(groupName: string, payload: { name: string; path?: string; notes?: string }): Promise<Workspace> {
  return request<unknown>(`/api/people/groups/${encodeURIComponent(groupName)}`, {
    method: "POST",
    body: JSON.stringify(payload),
  }).then(validateWorkspacePayload);
}

export function deletePeopleGroup(groupName: string): Promise<Workspace> {
  return request<unknown>(`/api/people/groups/${encodeURIComponent(groupName)}/delete`, {
    method: "POST",
    body: JSON.stringify({ notes: "从人员组织看板删除空组" }),
  }).then(validateWorkspacePayload);
}
