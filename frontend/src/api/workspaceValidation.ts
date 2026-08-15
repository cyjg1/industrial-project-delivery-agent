import type { Workspace } from "../types";

const REQUIRED_TOP_LEVEL_FIELDS = [
  "access",
  "run_status",
  "milestone",
  "storage",
  "daily_brief",
  "daily_journal",
  "daily_report_workspace",
  "memory",
  "memory_skill_evolution",
  "inputs",
  "sedimentation",
  "milestone_control",
  "milestone_workspace",
  "progress_dashboard",
  "people_workspace",
  "task_pool",
  "impact_analysis",
  "three_lists",
  "context",
  "confirmation_cards"
] as const;

const REQUIRED_INPUT_FIELDS = [
  "source_counts",
  "uploaded_sources",
  "ingestion_jobs",
  "source_documents",
  "upload_dir",
  "obsidian",
  "people_asset"
] as const;

const REQUIRED_STORAGE_FIELDS = [
  "store_dir",
  "agent_charter_path",
  "database_path",
  "archive_dir",
  "vault_dir",
  "method_vault_dir",
  "meeting_vault_dir",
  "brief_vault_dir"
] as const;

const REQUIRED_SOURCE_COUNT_FIELDS = [
  "total",
  "uploaded",
  "curated_ready",
  "raw_matched",
  "raw_pending"
] as const;

const REQUIRED_CONTEXT_FIELDS = [
  "assembly_steps",
  "agent_trace",
  "model_io_events",
  "harness",
  "rounds",
  "audit_trail",
  "tool_chain"
] as const;

const REQUIRED_MILESTONE_CONTROL_FIELDS = [
  "plan",
  "time_progress",
  "material_progress",
  "todo_backschedule",
  "run_progress",
  "adjustment_suggestions",
  "context_brief"
] as const;

const REQUIRED_MILESTONE_WORKSPACE_FIELDS = [
  "summary",
  "rows"
] as const;

const REQUIRED_PEOPLE_WORKSPACE_FIELDS = [
  "summary",
  "groups",
  "people"
] as const;

const REQUIRED_THREE_LISTS_FIELDS = [
  "summary",
  "issues",
  "tasks",
  "methods"
] as const;

const REQUIRED_ACCESS_FIELDS = [
  "actor",
  "role",
  "view_mode",
  "capabilities"
] as const;

export function validateWorkspacePayload(payload: unknown): Workspace {
  if (!isRecord(payload)) {
    throw new Error("后端返回的工作区数据不是对象，请重启 FastAPI 后刷新页面。");
  }

  const missing = missingKeys(payload, REQUIRED_TOP_LEVEL_FIELDS);
  const inputs = payload.inputs;
  const storage = payload.storage;
  const access = payload.access;
  if (!isRecord(access)) {
    missing.push("access");
  } else {
    missing.push(...missingKeys(access, REQUIRED_ACCESS_FIELDS).map((key) => `access.${key}`));
    if (!isRecord(access.actor)) {
      missing.push("access.actor");
    }
    if (!isRecord(access.capabilities)) {
      missing.push("access.capabilities");
    }
  }
  if (!isRecord(storage)) {
    missing.push("storage");
  } else {
    missing.push(...missingKeys(storage, REQUIRED_STORAGE_FIELDS).map((key) => `storage.${key}`));
  }

  if (!isRecord(inputs)) {
    missing.push("inputs");
  } else {
    missing.push(...missingKeys(inputs, REQUIRED_INPUT_FIELDS).map((key) => `inputs.${key}`));
    if (!isRecord(inputs.source_counts)) {
      missing.push("inputs.source_counts");
    } else {
      missing.push(...missingKeys(inputs.source_counts, REQUIRED_SOURCE_COUNT_FIELDS).map((key) => `inputs.source_counts.${key}`));
    }
    if (!Array.isArray(inputs.ingestion_jobs)) {
      missing.push("inputs.ingestion_jobs");
    }
  }

  const runStatus = payload.run_status;
  if (!isRecord(runStatus)) {
    missing.push("run_status");
  } else if (!isRecord(runStatus.verification)) {
    missing.push("run_status.verification");
  }

  if (!isRecord(payload.daily_journal)) {
    missing.push("daily_journal");
  }
  const memorySkillEvolution = payload.memory_skill_evolution;
  if (!isRecord(memorySkillEvolution)) {
    missing.push("memory_skill_evolution");
  } else {
    for (const field of ["recent_episodes", "policies", "cognitions", "skill_reliability"] as const) {
      if (!Array.isArray(memorySkillEvolution[field])) {
        missing.push(`memory_skill_evolution.${field}`);
      }
    }
    if (!isRecord(memorySkillEvolution.summary)) {
      missing.push("memory_skill_evolution.summary");
    }
  }
  if (!isRecord(payload.daily_report_workspace)) {
    missing.push("daily_report_workspace");
  }

  const milestoneControl = payload.milestone_control;
  if (!isRecord(milestoneControl)) {
    missing.push("milestone_control");
  } else {
    missing.push(...missingKeys(milestoneControl, REQUIRED_MILESTONE_CONTROL_FIELDS).map((key) => `milestone_control.${key}`));
  }

  const milestoneWorkspace = payload.milestone_workspace;
  if (!isRecord(milestoneWorkspace)) {
    missing.push("milestone_workspace");
  } else {
    missing.push(...missingKeys(milestoneWorkspace, REQUIRED_MILESTONE_WORKSPACE_FIELDS).map((key) => `milestone_workspace.${key}`));
    if (!Array.isArray(milestoneWorkspace.rows)) {
      missing.push("milestone_workspace.rows");
    }
  }

  const peopleWorkspace = payload.people_workspace;
  if (!isRecord(peopleWorkspace)) {
    missing.push("people_workspace");
  } else {
    missing.push(...missingKeys(peopleWorkspace, REQUIRED_PEOPLE_WORKSPACE_FIELDS).map((key) => `people_workspace.${key}`));
    if (!Array.isArray(peopleWorkspace.people)) {
      missing.push("people_workspace.people");
    }
    if (!Array.isArray(peopleWorkspace.groups)) {
      missing.push("people_workspace.groups");
    }
  }

  const threeLists = payload.three_lists;
  if (!isRecord(threeLists)) {
    missing.push("three_lists");
  } else {
    missing.push(...missingKeys(threeLists, REQUIRED_THREE_LISTS_FIELDS).map((key) => `three_lists.${key}`));
    for (const field of ["issues", "tasks", "methods"] as const) {
      if (!Array.isArray(threeLists[field])) {
        missing.push(`three_lists.${field}`);
      }
    }
  }

  const context = payload.context;
  if (!isRecord(context)) {
    missing.push("context");
  } else {
    missing.push(...missingKeys(context, REQUIRED_CONTEXT_FIELDS).map((key) => `context.${key}`));
  }

  if (missing.length) {
    throw new Error(`后端返回的工作区数据缺少 ${dedupe(missing).join("、")}，请重启 FastAPI 后刷新页面。`);
  }

  return payload as Workspace;
}

function missingKeys<T extends readonly string[]>(payload: Record<string, unknown>, keys: T): string[] {
  return keys.filter((key) => !(key in payload));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function dedupe(values: string[]): string[] {
  return Array.from(new Set(values));
}
