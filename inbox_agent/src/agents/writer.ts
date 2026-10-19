/** Writer agent: one job - turn triage + Knowledge Brain evidence into a reply in the company voice. */
import { LLMError, type LLMClient } from "../llm.js";
import { WRITER_SYSTEM, WRITER_USER } from "../prompts.js";
import { DraftSchema, type DraftOut, type EmailInput, type Evidence, type LlmMeta, type Triage } from "../types.js";
import { fill } from "./triage.js";

export interface WriterContext {
  company: string;
  signature: string;
  rules: string[];
  examples: { subject: string; final_body: string }[];
}

export function firstName(name: string): string {
  const first = name.trim().split(/\s+/)[0] ?? "";
  return first ? first.charAt(0).toUpperCase() + first.slice(1) : "there";
}

function knowledgeBlock(ev: Evidence): string {
  if (!ev.available) return `UNAVAILABLE (${ev.error ?? "Knowledge Brain did not respond"}). Do not state product facts.`;
  if (!ev.question) return "Not requested for this email.";
  const sources = ev.citations.map((c, i) => `[S${i + 1}] ${c.title}${c.url ? ` - ${c.url}` : ""}: ${c.snippet}`).join("\n");
  const shots = ev.images
    .filter((i) => i.description)
    .map((i) => `Screenshot (${i.article_title}): ${i.description}`)
    .join("\n");
  return [`Answer: ${ev.answer}`, sources && `Sources:\n${sources}`, shots].filter(Boolean).join("\n");
}

const ACK: Record<string, string> = {
  how_to: "Thanks for your question",
  technical_issue: "Thanks for reporting this, and sorry for the trouble",
  billing_question: "Thanks for your billing question",
  refund_request: "Thanks for reaching out about your refund request",
  cancellation: "Thanks for letting us know",
  account_access: "Sorry you're having trouble getting into your account",
  feature_request: "Thanks for the suggestion",
  new_project_quote: "Thanks for thinking of us for your project",
  complaint: "I'm sorry about your experience, and thank you for telling us",
  follow_up: "Thanks for following up",
  sales_inquiry: "Thanks for your interest",
  other: "Thanks for your message",
};

const NEXT_STEP: Record<string, string> = {
  refund_request:
    "I've passed your request to our billing team. They review every refund request individually and will reply to you directly.",
  cancellation: "I've passed this to our billing team so they can confirm the next steps on your account with you.",
  billing_question: "I've also asked our billing team to double-check your account and confirm the details.",
  account_access:
    "For your security, our team will verify account ownership before making any changes. Please reply from the email address on the account.",
  new_project_quote:
    "To prepare an accurate scope, could you share your timeline, the main features you need, and any example sites you like? A consultant will follow up with next steps.",
  complaint: "I've shared your feedback with the team lead, who will review what happened and get back to you.",
  feature_request: "I've logged this with our product team.",
};

/** Deterministic fallback draft. Contains no facts beyond what the Knowledge Brain returned. */
export function templateDraft(email: EmailInput, triage: Triage, ev: Evidence, ctx: WriterContext): DraftOut {
  const lines = [`Hi ${firstName(email.from_name)},`, "", `${ACK[triage.category] ?? ACK.other}.`];
  const needsHuman: string[] = ["Template draft (no AI model configured): check tone and completeness"];
  if (ev.answered) {
    const answer = ev.answer.replace(/^Here is what the help center says[^\n]*\n+/i, "").replace(/^> /gm, "").trim();
    lines.push("", answer);
    const link = ev.citations.find((c) => c.url);
    if (link?.url) lines.push("", `You can read the full guide here: ${link.url}`);
  } else if (ev.question) {
    lines.push(
      "",
      ev.escalation_id
        ? `I want to make sure you get an accurate answer, so I've asked our product specialists to check this (reference #${ev.escalation_id}). We'll follow up as soon as we have it.`
        : "I want to make sure you get an accurate answer, so I'm checking this with our product specialists and will follow up shortly.",
    );
    needsHuman.push("Knowledge Brain found no sufficient answer");
  }
  const next = NEXT_STEP[triage.category];
  if (next) lines.push("", next);
  lines.push("", "Best regards,", ctx.signature);
  return {
    subject: email.subject.toLowerCase().startsWith("re:") ? email.subject : `Re: ${email.subject}`,
    body: lines.join("\n"),
    used_citation_urls: ev.answered ? ev.citations.flatMap((c) => (c.url ? [c.url] : [])) : [],
    needs_human_reasons: needsHuman,
    confidence: ev.answered ? 0.5 : 0.3,
  };
}

export async function writeDraft(
  llm: LLMClient,
  email: EmailInput,
  triage: Triage,
  ev: Evidence,
  ctx: WriterContext,
): Promise<{ draft: DraftOut; meta: LlmMeta }> {
  const started = Date.now();
  const offlineMeta = (mode: "offline" | "fallback", warning?: string): LlmMeta => ({
    mode,
    provider: "template",
    model: null,
    input_tokens: 0,
    output_tokens: 0,
    cost_usd: null,
    latency_ms: Date.now() - started,
    ...(warning ? { warning } : {}),
  });
  if (!llm.enabled) return { draft: templateDraft(email, triage, ev, ctx), meta: offlineMeta("offline") };

  const rules = ctx.rules.length ? `Team style rules learned from human edits:\n${ctx.rules.map((r) => `- ${r}`).join("\n")}` : "";
  const examples = ctx.examples.length
    ? `APPROVED REPLIES TO SIMILAR EMAILS (match this voice, not the facts):\n${ctx.examples
        .map((e, i) => `--- Example ${i + 1} (${e.subject})\n${e.final_body}`)
        .join("\n")}\n`
    : "";
  try {
    const { data, call } = await llm.generateJson({
      tier: "smart",
      system: fill(WRITER_SYSTEM, { company: ctx.company, rules }),
      prompt: fill(WRITER_USER, {
        ...email,
        category: triage.category,
        urgency: triage.urgency,
        risks: triage.risk_flags.join(", ") || "none",
        summary: triage.summary,
        answered: String(ev.answered),
        knowledge: knowledgeBlock(ev),
        examples,
        signature: ctx.signature,
      }),
      schema: DraftSchema,
      temperature: 0.3,
    });
    if (!ev.answered && ev.question && !data.needs_human_reasons.length)
      data.needs_human_reasons.push("Knowledge Brain found no sufficient answer");
    return { draft: data, meta: { mode: "live", ...call } };
  } catch (err) {
    const warning = err instanceof LLMError ? `${err.kind}: ${err.message}` : String(err);
    return { draft: templateDraft(email, triage, ev, ctx), meta: offlineMeta("fallback", warning) };
  }
}
