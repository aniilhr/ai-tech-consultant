import { z } from "zod";

export const CATEGORIES = [
  "how_to",
  "technical_issue",
  "billing_question",
  "refund_request",
  "cancellation",
  "account_access",
  "feature_request",
  "new_project_quote",
  "complaint",
  "follow_up",
  "sales_inquiry",
  "other",
] as const;
export type Category = (typeof CATEGORIES)[number];

export const URGENCIES = ["low", "normal", "high", "urgent"] as const;
export type Urgency = (typeof URGENCIES)[number];

export const RISK_FLAGS = [
  "refund",
  "financial_commitment",
  "legal_threat",
  "security_privacy",
  "angry_customer",
  "churn_risk",
  "data_loss",
  "outage",
] as const;
export type RiskFlag = (typeof RISK_FLAGS)[number];

/** Kinds of email that may never be sent without a human, however good the track record. */
export const NEVER_AUTONOMOUS: ReadonlySet<Category> = new Set<Category>([
  "refund_request",
  "cancellation",
  "complaint",
  "new_project_quote",
  "billing_question",
  "account_access",
]);
/** Risk flags that force human review on any individual email. */
export const BLOCKING_RISKS: ReadonlySet<RiskFlag> = new Set<RiskFlag>([
  "refund",
  "financial_commitment",
  "legal_threat",
  "security_privacy",
  "angry_customer",
  "data_loss",
  "outage",
]);

export const TriageSchema = z.object({
  category: z.enum(CATEGORIES),
  urgency: z.enum(URGENCIES),
  risk_flags: z.array(z.enum(RISK_FLAGS)),
  sentiment: z.enum(["positive", "neutral", "negative"]),
  summary: z.string().describe("One sentence: what the customer wants"),
  kb_question: z
    .string()
    .describe("A standalone help-center question that would give the facts needed for the reply; empty if none"),
  confidence: z.number().min(0).max(1),
});
export type Triage = z.infer<typeof TriageSchema>;

export const DraftSchema = z.object({
  subject: z.string(),
  body: z.string().describe("Plain-text email body including greeting and sign-off"),
  used_citation_urls: z.array(z.string()),
  needs_human_reasons: z.array(z.string()).describe("Anything a human must check before sending"),
  confidence: z.number().min(0).max(1),
});
export type DraftOut = z.infer<typeof DraftSchema>;

export interface EmailInput {
  from_name: string;
  from_email: string;
  subject: string;
  body: string;
}

export interface EmailRow extends EmailInput {
  id: string;
  received_at: string;
  status: EmailStatus;
  expected_json: string | null;
  is_sample: number;
}

export type EmailStatus = "new" | "drafted" | "approved" | "rejected" | "replied_manually" | "auto_sent";

export interface BrainCitation {
  title: string;
  url: string | null;
  source: string;
  snippet: string;
}

export interface Evidence {
  available: boolean;
  answered: boolean;
  answer: string;
  confidence: number;
  citations: BrainCitation[];
  images: { url: string; description: string | null; article_title: string }[];
  escalation_id: number | null;
  mode: string;
  error?: string;
  question: string;
}

export interface LlmMeta {
  mode: "live" | "offline" | "fallback";
  provider: string;
  model: string | null;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
  latency_ms: number;
  warning?: string;
}

export type ReviewAction = "approved" | "rejected" | "own_reply" | "auto_sent" | "flagged_auto";

export interface ReviewRow {
  id: number;
  email_id: string;
  draft_id: number | null;
  kind: Category;
  action: ReviewAction;
  final_body: string | null;
  unchanged: number;
  edit_ratio: number | null;
  reviewer: string | null;
  reason: string | null;
  rules_version: number | null;
  created_at: string;
}
