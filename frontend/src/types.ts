export type EvidenceRow = {
  source_doc_id: string;
  meeting_title: string;
  meeting_date: string;
  curated_source: string;
  raw_source: string;
  raw_source_status: string;
  evidence_level: string;
  raw_locator: string;
  locator: string;
  quote: string;
};

export type IngestionInputKind =
  | "auto"
  | "minutes"
  | "transcript"
  | "three_list_tasks"
  | "three_list_issues"
  | "work_logs"
  | "deliverable";

export type ProjectFileEntry = {
  file_id: string;
  name: string;
  folder: string;
  relative_path: string;
  size: number;
  modified_at: string;
  upload_type: "minutes" | "project_document";
  document_category: string;
  source_id: string;
  job_id: string;
  index_status: string;
  uploaded_by: string;
  created_at: string;
};

export type ProjectFilesWorkspace = {
  folders: string[];
  files: ProjectFileEntry[];
  summary: {
    folder_count: number;
    file_count: number;
    minutes_count: number;
    indexed_count: number;
  };
};

export type IngestionJob = {
  id: string;
  source_id: string;
  source_title: string;
  project_id: string;
  topic_id: string | null;
  sensitivity: string;
  input_kind: IngestionInputKind;
  status: "queued" | "running" | "completed" | "failed" | "interrupted" | string;
  stage: string;
  total_chunks: number;
  processed_chunks: number;
  progress_percent: number;
  candidate_count: number;
  delta_new: number;
  delta_updated: number;
  delta_conflict: number;
  delta_resolved: number;
  delta_auto_merged: number;
  row_count: number;
  import_summary: string;
  attempts: number;
  max_attempts: number;
  error: string;
  created_at: string;
  updated_at: string;
};

export type SwitchableUser = {
  user_id: string;
  org_id: string;
  name: string;
  role: string;
  identity_status?: string;
  topics: Array<{
    topic_id: string;
    name: string;
  }>;
};

export type ConfirmationCard = {
  section: string;
  category: string;
  storage_item_id: string;
  status: "candidate" | "confirmed" | "rejected";
  title: string;
  agent_understanding: string;
  owner_candidates: string[];
  owner_text: string;
  next_step: string;
  matter_type: string;
  facet_types: string[];
  task_contract: {
    due_date: string;
    deliverable: string;
    acceptance_criteria: string;
  };
  methodology: {
    business_goal: string;
    principles: string[];
    reasoning_chain: string[];
    applicable_scope: string;
  };
  inference_note: string;
  confirmation_fields: string[];
  observed_fields: string[];
  confirmation_note: string;
  evidence: EvidenceRow[];
  confirmation_question: string;
};

export type AgentTraceItem = {
  kind: string;
  name: string;
  status: string;
  input_summary: string;
  output_summary: string;
  data?: Record<string, unknown>;
};

export type HarnessTask = {
  step_id: string;
  tool_name: string;
  reason: string;
  status: string;
  attempts: number;
  input_summary: string;
  output_summary: string;
  error: string;
  updated_at: string;
};

export type HarnessEvent = {
  event_id: string;
  event_type: string;
  status: string;
  step_id: string;
  tool_name: string;
  message: string;
  created_at: string;
};

export type HarnessState = {
  phase: string;
  max_steps: number;
  completed_steps: number;
  failure_count: number;
  max_tool_attempts: number;
  tasks: HarnessTask[];
  events: HarnessEvent[];
  hooks: Array<{
    hook_id: string;
    hook_type: string;
    step_id: string;
    tool_name: string;
    message: string;
    created_at: string;
  }>;
  context_window: {
    max_active_chars: number;
    active_chars: number;
    active_count: number;
    archived_count: number;
    active_refs: string[];
    archived_refs: string[];
    last_summary: string;
  };
};

export type RunRound = {
  round_index: number;
  step_id: string;
  phase: string;
  objective: string;
  decision: string;
  tool_name: string;
  status: string;
  attempts: number;
  input_summary: string;
  output_summary: string;
  model_result: string;
  error: string;
  state_changes: string[];
  stop_reason: string;
  round_kind: string;
  raw_response_id: string;
  token_usage: Record<string, number>;
  tool_calls: Array<{
    name: string;
    call_id: string;
    arguments: string;
  }>;
  events: Array<{
    event_type: string;
    status: string;
    message: string;
  }>;
};

export type AuditTraceItem = {
  step_index: number;
  stage: string;
  title: string;
  status: string;
  sent: string;
  assembled_context: string[];
  returned: string;
  raw_reference: string;
  detail: Record<string, unknown>;
};

export type ModelIoEvent = {
  event_id: string;
  provider: string;
  model: string;
  api_surface: string;
  request: {
    agent_name: string;
    input: string;
    instructions: string;
    tools: string[];
    output_type: string;
  };
  responses: Array<{
    raw_response_id: string;
    output_text: string;
    token_usage: Record<string, number>;
    tool_calls: Array<{
      name: string;
      call_id: string;
      arguments: string;
    }>;
    output_items: Array<Record<string, string>>;
  }>;
  final_output_type: string;
};

export type SedimentItem = {
  item_id: string;
  title: string;
  description: string;
  owners: string[];
  source_doc_id: string;
  quote: string;
  status?: string;
  matter_type?: string;
  facet_types?: string[];
  due_date?: string;
  deliverable?: string;
  acceptance_criteria?: string;
  business_goal?: string;
  principles?: string[];
  reasoning_chain?: string[];
};

export type SedimentSection = {
  label: string;
  count: number;
  items: SedimentItem[];
};

export type MilestoneControl = {
  plan: {
    milestone_id: string;
    project: string;
    name: string;
    date_start: string;
    date_end: string;
    scenario: string;
    chain: string;
    status: string;
    trigger_policy: Record<string, unknown>;
    acceptance_criteria: string[];
  };
  time_progress: {
    today: string;
    days_total: number;
    days_elapsed: number;
    days_remaining: number;
    percent: number;
    status_label: string;
  };
  material_progress: {
    source_count: number;
    curated_ready: number;
    raw_matched: number;
    raw_pending: number;
    raw_coverage_percent: number;
    uploaded_count: number;
    source_titles: string[];
  };
  todo_backschedule: {
    total: number;
    overdue: number;
    due_this_week: number;
    missing_due_date: number;
    outside_milestone: number;
    items: Array<{
      item_id: string;
      title: string;
      owner_text: string;
      due_date: string;
      days_to_due: number | null;
      deliverable: string;
      acceptance_criteria: string;
    }>;
  };
  run_progress: {
    status: string;
    report_item_count: number;
    candidate_memory_count: number;
    confirmed_memory_count: number;
    rejected_memory_count: number;
    verification_ok: boolean;
  };
  adjustment_suggestions: Array<{
    level: string;
    type: string;
    message: string;
    action: string;
  }>;
  context_brief: string[];
};

export type WorkItem = {
  work_item_id: string;
  title: string;
  description: string;
  status: string;
  owner_candidates: string[] | null;
  collaborators: string[] | null;
  confirmers: string[] | null;
  due_date: string | null;
  deliverable: string | null;
  acceptance_criteria: string | null;
  professional_id: string;
  board_id: string;
  planned_start: string;
  progress_percent: number | null;
  status_updated_at: string;
  milestone_id: string | null;
  source_candidate_id: string | null;
  source_run_id: string | null;
  version: number;
  created_at: string;
  updated_at: string;
  confirmation_notes: string | null;
  confirmation_editor: string | null;
  evidence_refs: Array<Record<string, unknown>>;
  linked_issue_ids?: string[] | null;
  linked_task_ids?: string[] | null;
};

export type FileAnalysisStep = {
  step_id: string;
  title: string;
  description: string;
  status: string;
  locked: boolean;
  ran_at: string;
  confirmed_at: string;
  confirmation_notes: string;
  artifact_paths: string[];
  summary: Record<string, unknown>;
  artifact_preview: string;
};

export type FileAnalysisWorkspace = {
  source_dir: string;
  output_dir: string;
  state_path: string;
  source_files: Array<{
    name: string;
    path: string;
    size: number;
    meeting_date: string;
    detected_kind: string;
    status: string;
  }>;
  steps: FileAnalysisStep[];
  current_step_id: string;
};

export type ConversationResponse = {
  view: string;
  next_view: string;
  session_id: string;
  message_id: string;
  reply: string;
  tool_steps: ConversationToolStep[];
  verification: {
    checked: boolean;
    unsupported: string[];
    invalid_fact_ids: string[];
    cited_fact_ids: string[];
    citation_coverage: {
      claim_count: number;
      cited_claim_count: number;
      ratio: number;
    };
  };
  stop_reason: string;
  context_bundle: string[];
  workspace: Workspace;
  debug: {
    provider: string;
    model: string;
    model_input: string;
    model_output: string;
    context_char_count?: number;
    context_token_count?: number;
    context_budget?: number;
    context_budget_unit?: string;
    context_token_counter?: string;
    context_retrieval_count?: number;
    context_degraded?: boolean;
    rounds?: ConversationModelRound[];
  };
};

export type ConversationModelCatalog = {
  provider: string;
  default_model: string;
  configured?: boolean;
  models: Array<{
    id: string;
    label: string;
  }>;
};

export type RuntimeProviderStatus = {
  provider: string;
  model: string;
  base_url: string;
  api_surface: string;
  api_key_configured: boolean;
  configured: boolean;
  persistence: "process_memory";
};

export type RuntimeProviderConfigInput = {
  provider: string;
  model: string;
  api_key: string;
  base_url: string;
  api_surface: string;
};

export type ConversationToolStep = {
  round_index: number;
  call_id?: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  reason: string;
  status: string;
  summary: string;
  result: Record<string, unknown>;
  elapsed_ms?: number;
};

export type ConversationModelRound = {
  round_index: number;
  provider: string;
  model: string;
  input_message_count: number;
  input_char_count: number;
  input_token_count: number;
  model_output: string;
  output_char_count: number;
  output_token_count: number;
  model_elapsed_ms: number;
  tool_calls: Array<{
    call_id?: string;
    name: string;
    arguments: Record<string, unknown>;
    reason?: string;
  }>;
  blocked_tool_calls?: Array<{
    call_id?: string;
    name: string;
    arguments: Record<string, unknown>;
    reason?: string;
  }>;
  tool_results: Array<{
    call_id?: string;
    name: string;
    status: string;
    summary: string;
    elapsed_ms: number;
    result: Record<string, unknown>;
  }>;
  verification_errors: string[];
  context_compaction?: {
    before_message_count: number;
    before_char_count: number;
    before_token_count: number;
    after_message_count: number;
    after_char_count: number;
    after_token_count: number;
    tools_disabled_for_retry: boolean;
  };
  status: string;
  run_elapsed_ms?: number;
};

export type ConversationSession = {
  session_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  archived: boolean;
};

export type ConversationMessage = {
  message_id: string;
  session_id: string;
  role: "user" | "assistant" | "tool" | string;
  content: string;
  created_at: string;
  metadata: Record<string, unknown>;
};

export type MilestoneWorkspaceRow = {
  row_id: string;
  level: number;
  parent_id: string;
  row_type: "milestone" | "work_item" | "candidate_task" | string;
  title: string;
  date_start: string;
  date_end: string;
  due_date: string;
  owner_text: string;
  status: string;
  deliverable: string;
  acceptance_criteria: string;
  planned_start: string;
  professional_id: string;
  board_id: string;
  progress_percent: number | null;
  linked_task_ids: string[];
  linked_task_titles: string[];
  editable: boolean;
};

export type MilestoneWorkspace = {
  summary: {
    milestone_id: string;
    name: string;
    date_start: string;
    date_end: string;
    linked_task_count: number;
    formal_task_count: number;
    candidate_task_count: number;
    deliverable_count: number;
  };
  rows: MilestoneWorkspaceRow[];
};

export type PeopleWorkspaceItem = {
  person_id: string;
  name: string;
  identity_status: "confirmed" | "needs_merge";
  group: string;
  taxonomy_group: string;
  role: string;
  path: string;
  source_id: string;
  responsibility_note: string;
  responsibility_summary: string;
  assignments: Array<{
    assignment_id: string;
    group: string;
    role: string;
    path: string;
    responsibility_note: string;
    source_id: string;
  }>;
  active_work_count: number;
  work_items: Array<{
    work_item_id: string;
    title: string;
    status: string;
    due_date: string;
    deliverable: string;
  }>;
  candidate_items: Array<{
    item_id: string;
    title: string;
    status: string;
    category: string;
    due_date: string;
    deliverable: string;
  }>;
  editable: boolean;
};

export type PeopleWorkspace = {
  summary: {
    root_title: string;
    people_count: number;
    active_work_total: number;
    group_count: number;
    scenario_count: number;
    duplicate_review_count: number;
  };
  groups: Array<{
    name: string;
    path: string;
    people_count: number;
    editable: boolean;
  }>;
  people: PeopleWorkspaceItem[];
};

export type ProjectCalendarEvent = {
  event_id: string;
  event_type: "meeting" | "task_deadline";
  date: string;
  title: string;
  time_label: string;
  owner_names: string[];
  status: string;
  deliverable: string;
  description: string;
  is_related: boolean;
};

export type ProjectCalendarResponse = {
  month: string;
  actor: { id: string; name: string };
  events: ProjectCalendarEvent[];
  summary: {
    meeting_count: number;
    deadline_count: number;
    related_count: number;
  };
};

export type ProgressHealth = "green" | "yellow" | "red" | "gray";

export type ProgressSegment = {
  id: string;
  name: string;
  task_count: number;
  progress: number;
  planned_progress: number;
  health: ProgressHealth;
  overdue_count: number;
  missing_plan_count: number;
  missing_progress_count: number;
  missing_owner_count: number;
  top_risks: Array<{
    task_id: string;
    title: string;
    owner_text: string;
    due_date: string;
    health: ProgressHealth;
    missing_progress: boolean;
  }>;
};

export type ProgressDashboard = {
  summary: {
    task_count: number;
    profession_count: number;
    board_count: number;
    owner_count: number;
    status_count: number;
    source_batch_count: number;
    unclassified_profession_count: number;
    unclassified_board_count: number;
    missing_progress_count: number;
    updated_at: string;
  };
  professions: Array<ProgressSegment & {
    boards: ProgressSegment[];
    time_nodes: Array<Omit<ProgressSegment, "id" | "name"> & { date: string }>;
  }>;
  boards: Array<ProgressSegment & {
    professions: ProgressSegment[];
    three_lists: {
      progress: number;
      issue_progress: number;
      task_progress: number;
      method_progress: number;
      health: ProgressHealth;
      issue_count: number;
      task_count: number;
      method_count: number;
      confirmed_count: number;
      unlinked_issue_count: number;
      missing_owner_count: number;
      missing_due_date_count: number;
    };
    time_nodes: Array<Omit<ProgressSegment, "id" | "name"> & { date: string }>;
  }>;
  owners: ProgressSegment[];
  statuses: ProgressSegment[];
  source_batches: ProgressSegment[];
  legend: Record<ProgressHealth, string>;
};

export type ImpactSuggestion = {
  source_item_id: string;
  source_title: string;
  impact_type: string;
  match_type: string;
  matched_work_item_id: string;
  matched_work_item_title: string;
  reason: string;
  suggested_action: string;
  milestone_id: string;
  human_confirmation_required: boolean;
  score?: number;
  evidence_refs: Array<Record<string, unknown>>;
};

export type LinkedIssueRow = {
  issue_id: string;
  source_sheet: string;
  issue_description: string;
  parent_issue: string;
  issue_type: string;
  priority: string;
  status: string;
  issue_source: string;
  registered_at: string;
  owner_text: string;
  planned_resolution_date: string;
  linked_task_ids: string[];
  linked_task_titles: string[];
  task_count: number;
  current_difficulty: string;
  method_ids: string[];
  evidence_count: number;
};

export type LinkedTaskRow = {
  task_id: string;
  source_sheet: string;
  task_description: string;
  task_detail: string;
  task_type: string;
  owner_text: string;
  linked_issue_ids: string[];
  linked_issue_titles: string[];
  linked_task_ids: string[];
  due_date: string;
  acceptance_criteria: string;
  progress: number | null;
  task_status: string;
  deliverable: string;
  parent_task: string;
  source_candidate_id: string;
  source_doc_ids: string[];
  source_batch_id: string;
  evidence_count: number;
};

export type LinkedMethodRow = {
  method_id: string;
  source_sheet: string;
  overview: string;
  content_tags: string;
  detail: string;
  date: string;
  proposer: string;
  linked_issue_ids: string[];
  linked_issue_titles: string[];
  linked_task_ids: string[];
  linked_task_count: number;
  memory_target: string;
  status: string;
  business_goal: string;
  principles: string[];
  reasoning_chain: string[];
  applicable_scope: string;
  evidence_count: number;
  evidence_refs: Array<{
    source_doc_id: string;
    meeting_title: string;
    meeting_date: string;
    curated_source: string;
    raw_source: string;
    raw_source_status: string;
    evidence_level: string;
    raw_locator: string;
    locator: string;
    quote: string;
  }>;
  observed_fields: string[];
  proposed_fields: string[];
  inference_basis: string[];
  inference_confidence: string;
  inference_note: string;
  skill_id: string;
  skill_maturity: "memory" | "candidate" | "tested" | "published" | "revalidation_required" | string;
  skill_gate_failures: string[];
  skill_version: number;
};

export type ThreeListsWorkspace = {
  summary: {
    issue_count: number;
    task_count: number;
    method_count: number;
    linked_issue_count: number;
    unlinked_issue_count: number;
    method_memory_count: number;
    reference_sheets: string[];
  };
  issues: LinkedIssueRow[];
  tasks: LinkedTaskRow[];
  methods: LinkedMethodRow[];
};

export type DailyBrief = {
  brief_id: string;
  brief_date: string;
  title: string;
  content_markdown: string;
  sections: Array<Record<string, unknown>>;
  notification_count: number;
  ai_commentary?: string;
  generated_at: string;
  scheduler?: {
    enabled: boolean;
    running: boolean;
    job_id: string;
    hour: number;
    minute: number;
    timezone: string;
    next_run_time: string;
    error: string;
  };
};

export type DailyJournal = {
  journal_id: string;
  version_id: string;
  version: number;
  report_date: string;
  status: "collecting" | "running" | "completed" | "failed" | string;
  organization_status: "pending" | "model_organized" | "skipped_mock" | "failed" | string;
  message_count: number;
  source_id: string;
  ingestion_job_id: string;
  error: string;
  updated_at: string;
  scheduler?: {
    enabled: boolean;
    running: boolean;
    job_id: string;
    hour: number;
    minute: number;
    timezone: string;
    next_run_time: string;
    last_completed_at: string;
    error: string;
  };
};

export type MemorySkillEpisode = {
  episode_id: string;
  run_id: string;
  author_id: string;
  relation: "new_task" | "follow_up" | "correction" | string;
  status: string;
  terminal_signal: string;
  reward_value: number | null;
  reward_source: string;
  feedback_status: string;
  feedback_note: string;
  closed_at: string;
  payload: {
    task_summary?: string;
    tool_sequence?: string[];
    observable_round_count?: number;
  };
};

export type ExecutionPolicy = {
  policy_id: string;
  status: "candidate" | "approved" | "revalidation_required" | "retired" | string;
  trigger_text: string;
  procedure: Array<{
    step: number;
    action: string;
    done_when: string;
  }>;
  verification: string[];
  boundaries: string[];
  support_episode_ids: string[];
  counter_episode_ids: string[];
  gain: number;
  stability: number;
  updated_at: string;
};

export type EnvironmentCognition = {
  cognition_id: string;
  status: "candidate" | "approved" | "revalidation_required" | "retired" | string;
  entities: string[];
  structures: Array<Record<string, unknown>>;
  regularities: string[];
  constraints: string[];
  support_policy_ids: string[];
  confidence: number;
  updated_at: string;
};

export type SkillReliability = {
  skill_id: string;
  version_id: string;
  name: string;
  retrieval_count: number;
  episode_count: number;
  success_count: number;
  failure_count: number;
  reliability: number;
  lifecycle: "probationary" | "active" | "revalidation_required" | string;
};

export type MemorySkillEvolutionWorkspace = {
  summary: {
    episode_count: number;
    trace_count: number;
    policy_candidate_count: number;
    policy_approved_count: number;
    policy_revalidation_count: number;
    cognition_candidate_count: number;
    cognition_approved_count: number;
    cognition_revalidation_count: number;
    active_skill_count: number;
    probationary_skill_count: number;
  };
  recent_episodes: MemorySkillEpisode[];
  policies: ExecutionPolicy[];
  cognitions: EnvironmentCognition[];
  skill_reliability: SkillReliability[];
  governance: Record<string, unknown>;
};

export type DailyWorkRecordKind = "work" | "conclusion" | "problem" | "output";

export type DailyWorkRecord = {
  record_id: string;
  series_id: string;
  source_id: string;
  source_origin: "conversation_daily_journal" | "historical_excel_backfill" | string;
  org_id: string;
  project_id: string;
  topic_id: string | null;
  author_id: string;
  sensitivity: string;
  subject_user_id: string;
  subject_name: string;
  record_date: string;
  kind: DailyWorkRecordKind;
  text: string;
  solution_options: Array<{ text: string; memory_refs: string[] }>;
  evidence_refs: Array<Record<string, unknown>>;
  source_locator: string;
  status: string;
  edited_by_human: boolean;
  updated_by: string;
  updated_at: string;
  can_edit: boolean;
};

export type DeliverableFile = {
  file_id: string;
  deliverable_id: string;
  version_id: string;
  artifact_role: "formal" | "process" | "evidence" | "reference" | string;
  original_name: string;
  content_hash: string;
  content_type: string;
  size: number;
  created_at: string;
};

export type DeliverableVersion = {
  version_id: string;
  deliverable_id: string;
  version: number;
  submitted_by: string;
  status: string;
  note: string;
  created_at: string;
  files: DeliverableFile[];
};

export type Deliverable = {
  deliverable_id: string;
  org_id: string;
  project_id: string;
  topic_id: string | null;
  author_id: string;
  sensitivity: string;
  milestone_id: string;
  title: string;
  type_label: string;
  required: boolean;
  due_date: string;
  acceptance_criteria: string;
  status: string;
  sort_order: number;
  current_version: number;
  created_at: string;
  updated_at: string;
  can_manage: boolean;
  can_submit: boolean;
  versions: DeliverableVersion[];
};

export type Workspace = {
  run_id: string;
  access: {
    actor: {
      id: string;
      org_id: string;
      name: string;
    };
    role: "viewer" | "exec" | "topic_lead" | "professional_lead" | "pmo" | "pm" | string;
    view_mode: "viewer" | "exec" | "topic_lead" | "professional_lead" | "pmo" | string;
    role_profile?: {
      key: string;
      label: string;
      principle: string;
      layers: {
        l1_focus: string;
        l2_objects: string;
        l3_evidence: string;
        l4_actions: string;
      };
      prompt_rules: string[];
    };
    capabilities: {
      milestone_overview: boolean;
      cross_person_load: boolean;
      management_summary: boolean;
      generate_management_brief?: boolean;
      review_queue: boolean;
      edit_workspace: boolean;
      debug_context: boolean;
      daily_report_workspace?: boolean;
      progress_dashboard?: boolean;
    };
  };
  run_status: {
    status: "idle" | "completed" | "verification_failed" | "failed" | string;
    runtime_kind: string;
    error: string;
    raw_response_count: number;
    stop_reason: string;
    verification: {
      ok: boolean;
      checked_count: number;
      errors: string[];
      warnings: string[];
      raw_evidence_count: number;
      raw_pending_count: number;
    };
  };
  milestone: {
    name: string;
    date_range: string;
    scenario: string;
    chain: string;
  };
  storage: {
    store_dir: string;
    agent_charter_path: string;
    database_path: string;
    archive_dir: string;
    vault_dir: string;
    method_vault_dir: string;
    meeting_vault_dir: string;
    brief_vault_dir: string;
  };
  daily_brief: DailyBrief;
  daily_journal: DailyJournal;
  memory: {
    candidate_count: number;
    confirmed_count: number;
    rejected_count: number;
    latest_confirmed_item_ids: string[];
  };
  memory_skill_evolution: MemorySkillEvolutionWorkspace;
  inputs: {
    source_counts: {
      total: number;
      uploaded: number;
      curated_ready: number;
      raw_matched: number;
      raw_pending: number;
    };
    uploaded_sources: Array<{
      doc_id: string;
      title: string;
      meeting_date: string;
      curated_status: string;
      raw_status: string;
    }>;
    ingestion_jobs: IngestionJob[];
    source_documents: Array<{
      doc_id: string;
      title: string;
      meeting_date: string;
      curated_status: string;
      raw_status: string;
    }>;
    upload_dir: string;
    obsidian: {
      root: string;
      configured: boolean;
      exists: boolean;
      markdown_count: number;
      latest_mtime: number;
    };
    people_asset: {
      path: string;
      root_title: string;
      group_count: number;
      people_count: number;
      scenario_count: number;
      scenario_ids: string[];
    };
  };
  sedimentation: {
    people: SedimentSection;
    things: SedimentSection;
    methods: SedimentSection;
    todos: SedimentSection;
  };
  milestone_control: MilestoneControl;
  milestone_workspace: MilestoneWorkspace;
  progress_dashboard: ProgressDashboard;
  people_workspace: PeopleWorkspace;
  task_pool: {
    summary: {
      total: number;
      status_counts: Record<string, number>;
    };
    work_items: WorkItem[];
  };
  daily_report_workspace: {
    view_mode: string;
    title: string;
    focus: string;
    summary: {
      project_active_work_count: number;
      role_active_work_count: number;
      project_stale_report_count: number;
      role_stale_report_count: number;
      project_risk_count: number;
      role_risk_count: number;
      project_missing_progress_count: number;
      role_missing_progress_count: number;
    };
    my_report_required: boolean;
    items_requiring_update: Array<{
      work_item_id: string;
      title: string;
      owner_text: string;
      status: string;
      due_date: string;
      days_since_update: number;
      overdue: boolean;
      missing_progress: boolean;
      deliverable: string;
    }>;
    risk_items: Array<{
      work_item_id: string;
      title: string;
      owner_text: string;
      status: string;
      due_date: string;
      days_since_update: number;
      overdue: boolean;
      missing_progress: boolean;
      deliverable: string;
    }>;
    owner_rollup: Array<{
      owner: string;
      active_count: number;
      stale_count: number;
      risk_count: number;
    }>;
    recommended_actions: string[];
  };
  impact_analysis: {
    summary: {
      source_followup_count: number;
      work_item_count: number;
      suggestion_count: number;
      matched_existing_count: number;
      new_task_count: number;
    };
    suggestions: ImpactSuggestion[];
  };
  three_lists: ThreeListsWorkspace;
  context: {
    assembly_steps: string[];
    agent_trace: AgentTraceItem[];
    model_io_events: ModelIoEvent[];
    harness: HarnessState;
    rounds: RunRound[];
    audit_trail: AuditTraceItem[];
    tool_chain: Array<{
      tool: string;
      input: string;
      output: string;
    }>;
  };
  confirmation_cards: ConfirmationCard[];
};
