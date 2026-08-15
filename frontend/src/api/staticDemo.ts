import staticDemoData from "../demo/staticDemoData.json";

type RequestOptions = RequestInit | undefined;

const data = staticDemoData as Record<string, any>;

export function staticDemoRequest<T>(path: string, options?: RequestOptions, actorId = "u_pmo"): Promise<T> {
  const method = (options?.method || "GET").toUpperCase();
  if (method !== "GET") {
    return Promise.reject(new Error("只读演示环境不接受新增、修改或删除操作。"));
  }

  const pathname = path.split("?")[0];
  let payload: unknown;
  if (pathname === "/api/workspace") payload = data.workspaces[actorId] || data.workspaces[data.defaultActorId];
  else if (pathname === "/api/conversation/sessions") payload = data.sessions[actorId] || { sessions: [] };
  else if (pathname === "/api/conversation/models") payload = data.conversationModels;
  else if (pathname === "/api/dev/switchable-users") payload = data.switchableUsers;
  else if (pathname === "/api/daily-work-records") payload = data.dailyWorkRecords;
  else if (pathname === "/api/file-workspace") payload = data.fileWorkspace;
  else if (pathname === "/api/project-files") payload = data.projectFiles;
  else if (pathname === "/api/project-calendar") payload = data.projectCalendar;
  else if (pathname === "/api/deliverables") payload = data.deliverables;
  else if (pathname === "/api/skills/packages") payload = data.skillPackages;
  else return Promise.reject(new Error(`只读演示中未提供此数据视图：${pathname}`));

  const cloned = structuredClone(payload) as any;
  if (pathname === "/api/workspace" && cloned?.access?.capabilities) {
    cloned.access.capabilities.edit_workspace = false;
    cloned.access.capabilities.generate_management_brief = false;
  }
  return Promise.resolve(cloned as T);
}
