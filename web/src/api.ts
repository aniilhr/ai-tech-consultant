/** Typed client for the three services, all reached through the gateway at /api/{brain,inbox,scope}. */

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(base: string, path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const { json, ...rest } = init ?? {};
  let resp: Response;
  try {
    resp = await fetch(`/api/${base}${path}`, {
      ...rest,
      headers: { ...(json !== undefined ? { "content-type": "application/json" } : {}), ...(rest.headers ?? {}) },
      body: json !== undefined ? JSON.stringify(json) : rest.body,
    });
  } catch {
    throw new ApiError(`Cannot reach the ${base} service. Is it running?`, 0);
  }
  const text = await resp.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!resp.ok) {
    const d = data as Record<string, unknown> | string | null;
    let msg = typeof d === "string" ? d : "";
    if (d && typeof d === "object") {
      const detail = d.detail ?? d.error ?? d.message;
      msg = Array.isArray(detail)
        ? detail.map((x: { msg?: string; loc?: unknown[] }) => `${(x.loc ?? []).slice(1).join(".")}: ${x.msg ?? ""}`).join("; ")
        : String(detail ?? "");
      if (d.issues && Array.isArray(d.issues)) msg = (d.issues as { message: string; path: string[] }[]).map((i) => `${i.path.join(".")}: ${i.message}`).join("; ");
    }
    throw new ApiError(msg || `${base} returned HTTP ${resp.status}`, resp.status);
  }
  return data as T;
}

export const brainApi = <T>(path: string, init?: RequestInit & { json?: unknown }) => request<T>("brain", path, init);
export const inboxApi = <T>(path: string, init?: RequestInit & { json?: unknown }) => request<T>("inbox", path, init);
export const scopeApi = <T>(path: string, init?: RequestInit & { json?: unknown }) => request<T>("scope", path, init);

// ------------------------------------------------------------------ shared types
export interface LlmInfo {
  provider: string;
  models?: Record<string, string>;
  embeddings?: string | null;
}

export interface Citation {
  article_id: number;
  title: string;
  url: string | null;
  source: string;
  heading: string;
  snippet: string;
  score: number;
  dense: number | null;
  coverage: number;
}

export interface AskResponse {
  question: string;
  answered: boolean;
  answer: string;
  confidence: number;
  retrieval_mode: string;
  citations: Citation[];
  related: Citation[];
  images: { image_id: number; url: string; alt: string; description: string | null; analysed: boolean; article_title: string; article_url: string; directly_cited: boolean }[];
  escalation_id: number | null;
  warnings: string[];
  llm: { mode: string; provider: string; model: string | null; prompt_version: string; input_tokens: number; output_tokens: number; cost_usd: number | null };
  latency_ms: number;
}

export interface Escalation {
  id: number;
  question: string;
  source: string;
  context: string | null;
  confidence: number | null;
  status: string;
  expert_answer: string | null;
  expert_name: string | null;
  times_asked: number;
  created_at: string;
}

export interface BrainStats {
  articles: Record<string, number>;
  expert_answers: number;
  documents: number;
  chunks: number;
  chunks_embedded: number;
  images: Record<string, number>;
  escalations_open: number;
  escalations_answered: number;
  queries: { total: number; answer_rate: number | null; avg_latency_ms: number | null; total_cost_usd: number | null };
  last_sync: SyncRun | null;
  sync_running: boolean;
  llm: LlmInfo;
}

export interface SyncRun {
  id: number;
  kind: string;
  status: string;
  params: string | null;
  stats: string | null;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface Article {
  id: number;
  url: string;
  source: string;
  title: string | null;
  category: string | null;
  folder: string | null;
  status: string;
  lastmod: string | null;
  fetched_at: string | null;
  attempts: number;
  last_error: string | null;
  chunks: number;
  images: number;
}

// Inbox
export interface EmailListItem {
  id: string;
  from_name: string;
  from_email: string;
  subject: string;
  preview: string;
  received_at: string;
  status: string;
  category: string | null;
  urgency: string | null;
  risk_flags: string[];
}

export interface Evidence {
  available: boolean;
  answered: boolean;
  answer: string;
  confidence: number;
  citations: { title: string; url: string | null; source: string; snippet: string }[];
  images: { url: string; description: string | null; article_title: string }[];
  escalation_id: number | null;
  mode: string;
  error?: string;
  question: string;
}

export interface LlmMeta {
  mode: string;
  provider: string;
  model: string | null;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
  latency_ms: number;
  warning?: string;
}

export interface Draft {
  id: number;
  kind: string;
  subject: string;
  body: string;
  evidence: Evidence;
  needs_human: string[];
  policy: { violations: string[]; blocking_risks: string[]; never_autonomous_kind: boolean };
  confidence: number;
  llm: LlmMeta;
  prompt_version: string;
  rules_version: number | null;
  created_at: string;
}

export interface Review {
  id: number;
  action: string;
  final_body: string | null;
  unchanged: number;
  edit_ratio: number | null;
  reviewer: string | null;
  reason: string | null;
  created_at: string;
}

export interface KindStatus {
  kind: string;
  streak: number;
  required: number;
  eligible: boolean;
  blocked_reason: string | null;
  reviewed: number;
  unchanged_total: number;
  recent_unchanged_rate: number | null;
  last_reset: { at: string; reason: string } | null;
}

export interface EmailDetail {
  email: { id: string; from_name: string; from_email: string; subject: string; body: string; received_at: string; status: string; expected: { category: string; urgency: string } | null };
  triage: { category: string; urgency: string; risk_flags: string[]; sentiment: string; summary: string; kb_question: string; confidence: number; llm: LlmMeta } | null;
  drafts: Draft[];
  reviews: Review[];
  outbox: { id: number; sent_by: string; created_at: string }[];
  kind_status: KindStatus | null;
  auto_send?: { allowed: boolean; reasons: string[] };
}

export interface RulesVersion {
  version: number;
  rules: string[];
  source: string;
  note: string | null;
  active: boolean;
  created_at: string;
  metrics: { drafts_reviewed: number; unchanged_rate: number | null; avg_edit_ratio: number | null };
}

// Scope
export interface Totals {
  low: number;
  likely: number;
  high: number;
}

export interface Requirement {
  id: string;
  title: string;
  description: string;
  priority: string;
  source_quote: string;
  speaker: string;
  ambiguous: boolean;
  clarification_question: string;
  quote_verified: boolean;
  origin: string;
}

export interface CapabilityCheck {
  requirement_id: string;
  status: string;
  rationale: string;
  product_features: string[];
  brain_question: string;
  brain_answered: boolean;
  brain_answer: string;
  evidence: { title: string; url: string | null; snippet: string }[];
  mode: string;
  guard_note: string;
}

export interface Item {
  text: string;
  source_quote: string;
  quote_verified: boolean;
}

export interface Task {
  id: string;
  requirement_id: string | null;
  title: string;
  role: string;
  low: number;
  likely: number;
  high: number;
  notes: string;
}

export interface ScopeData {
  requirements: Requirement[];
  decisions: Item[];
  assumptions: Item[];
  dependencies: Item[];
  open_questions: Item[];
  capability_checks: Record<string, CapabilityCheck>;
  estimate: { tasks: Task[]; totals: Totals; by_requirement: Record<string, Totals>; method: string; notes: string[] };
  llm: { mode?: string; provider?: string; calls?: number; models?: string[]; input_tokens?: number; output_tokens?: number; cost_usd?: number | null; latency_ms?: number; segments?: number };
  warnings: string[];
}

export interface ChangeItem {
  change_type: string;
  requirement_id: string;
  baseline_requirement_id: string | null;
  title: string;
  summary: string;
  source_quote: string;
  quote_verified: boolean;
  out_of_scope: boolean;
  hours_delta: Totals;
}

export interface Scope {
  id: number;
  project_id: number;
  version: number;
  transcript_id: number | null;
  status: string;
  approved_by: string | null;
  approved_at: string | null;
  created_at: string;
  data: ScopeData;
  change_report: { baseline_version: number; items: ChangeItem[]; added: number; modified: number; removed: number; out_of_scope_hours: Totals; total_delta: Totals } | null;
}

export interface Proposal {
  id: number;
  scope_id: number;
  version: number;
  markdown: string;
  status: string;
  edited: number;
  reviewer: string | null;
  review_note: string | null;
  scope_status: string;
  ready_to_send: boolean;
  created_at: string;
}

export interface ProjectSummary {
  id: number;
  name: string;
  client: string;
  created_at: string;
  transcripts: number;
  latest_version: number | null;
  baseline_version: number | null;
  approved_proposals: number;
}

export interface ProjectDetail {
  project: { id: number; name: string; client: string; created_at: string };
  transcripts: { id: number; title: string; kind: string; chars: number; created_at: string }[];
  scopes: { id: number; version: number; status: string; transcript_id: number; created_at: string; approved_by: string | null; requirements: number; totals: Totals; is_change: boolean; llm_mode: string }[];
  proposals: { id: number; scope_id: number; version: number; status: string; edited: number; reviewer: string | null; created_at: string }[];
  audit: { id: number; entity: string; entity_id: number; action: string; actor: string | null; note: string | null; created_at: string }[];
}
