/** Triage agent: one job - understand what the email wants, how urgent and how risky it is. */
import { LLMError, type LLMClient } from "../llm.js";
import { TRIAGE_SYSTEM, TRIAGE_USER } from "../prompts.js";
import { TriageSchema, type Category, type EmailInput, type LlmMeta, type RiskFlag, type Triage, type Urgency } from "../types.js";

export function fill(template: string, values: Record<string, string>): string {
  return template.replace(/\{(\w+)\}/g, (_, k: string) => values[k] ?? "");
}

// Ordered by priority: the first category whose pattern matches wins on ties.
const RULES: { category: Category; pattern: RegExp }[] = [
  { category: "refund_request", pattern: /\b(refund|money back|reimburse|chargeback|credit (back|my|to my)|charged twice|double charged)\b/i },
  { category: "technical_issue", pattern: /\b(site (is )?down|whole site|500 error|outage|nobody (has been able|can) (to )?pay)\b/i },
  { category: "cancellation", pattern: /\b(cancel|close my account|terminate|downgrade|stop (my|the) (subscription|plan|billing))\b/i },
  { category: "account_access", pattern: /\b(log ?in|password|locked out|2fa|two[- ]factor|reset link|can'?t access|cannot access|account owner)/i },
  { category: "complaint", pattern: /\b(unacceptable|disappointed|frustrat|terrible|worst|ridiculous|complain|poor service|no one (has )?respond)/i },
  { category: "new_project_quote", pattern: /\b(quote|estimate|proposal|custom (development|build|work)|build (us|me) a|new (website|project|site)|hire (you|your team))\b/i },
  { category: "billing_question", pattern: /\b(invoice|billing|charge[ds]?|receipt|payment method|card on file|renewal|vat|tax)\b/i },
  { category: "technical_issue", pattern: /\b(error|broken|not working|doesn'?t work|isn'?t working|bug|crash|blank page|500|404|down|not loading|stopped)\b/i },
  { category: "feature_request", pattern: /\b(feature request|would be (great|nice)|could you add|please add|is there a way to add|roadmap|wish)\b/i },
  { category: "follow_up", pattern: /\b(follow(ing)? up|any update|still waiting|haven'?t heard|ticket #?\d+|as discussed|circling back)\b/i },
  { category: "sales_inquiry", pattern: /\b(pricing plans?|demo|trial|thinking of (switching|signing)|before (we|i) (buy|sign up)|which plan)\b/i },
  { category: "how_to", pattern: /\b(how (do|can|to)|where (do|can|is)|is it possible|can i|steps to|set ?up|configure|cómo)\b/i },
];

const RISK_RULES: { flag: RiskFlag; pattern: RegExp }[] = [
  { flag: "refund", pattern: /\b(refund|money back|reimburse|chargeback)\b/i },
  { flag: "financial_commitment", pattern: /\b(discount|quote|price|pricing|cost|credit|contract|invoice)\b/i },
  { flag: "legal_threat", pattern: /\b(lawyer|attorney|legal action|sue|court|gdpr complaint|small claims)\b/i },
  { flag: "security_privacy", pattern: /\b(hack(ed)?|breach|phishing|password|2fa|gdpr|personal data|delete my data|suspicious login)\b/i },
  { flag: "angry_customer", pattern: /\b(unacceptable|furious|ridiculous|worst|terrible|disgusted|scam)\b|!!/i },
  { flag: "churn_risk", pattern: /\b(cancel|switch(ing)? to|competitor|leave|leaving|close my account)\b/i },
  { flag: "data_loss", pattern: /\b(lost (all|my)|deleted|disappeared|missing (members|data|listings))\b/i },
  { flag: "outage", pattern: /\b(site (is )?down|whole site|not loading|offline|500 error)\b/i },
];

/** Deterministic fallback used when no model is configured or the model call fails. */
export function ruleTriage(email: EmailInput): Triage {
  const text = `${email.subject}\n${email.body}`;
  const category = RULES.find((r) => r.pattern.test(text))?.category ?? "other";
  const risk_flags = RISK_RULES.filter((r) => r.pattern.test(text)).map((r) => r.flag);
  let urgency: Urgency = "normal";
  if (risk_flags.includes("outage") || risk_flags.includes("legal_threat") || /\burgent|asap|immediately\b/i.test(text))
    urgency = "urgent";
  else if (risk_flags.includes("angry_customer") || risk_flags.includes("data_loss") || category === "account_access")
    urgency = "high";
  else if (category === "feature_request" || category === "other" || category === "sales_inquiry") urgency = "low";
  const negative = risk_flags.includes("angry_customer") || category === "complaint";
  const firstSentence = email.body.split(/(?<=[.?!])\s/)[0]?.slice(0, 200) ?? email.subject;
  return {
    category,
    urgency,
    risk_flags,
    sentiment: negative ? "negative" : /\bthank|great|love\b/i.test(text) ? "positive" : "neutral",
    summary: `${email.subject}: ${firstSentence}`.slice(0, 240),
    kb_question: ["other", "follow_up"].includes(category) ? "" : `${email.subject}. ${email.body}`.slice(0, 600),
    confidence: category === "other" ? 0.3 : 0.6,
  };
}

export async function triageEmail(llm: LLMClient, email: EmailInput, company: string): Promise<{ triage: Triage; meta: LlmMeta }> {
  const started = Date.now();
  if (!llm.enabled) {
    return {
      triage: ruleTriage(email),
      meta: { mode: "offline", provider: "rules", model: null, input_tokens: 0, output_tokens: 0, cost_usd: null, latency_ms: Date.now() - started },
    };
  }
  try {
    const { data, call } = await llm.generateJson({
      tier: "fast",
      system: fill(TRIAGE_SYSTEM, { company }),
      prompt: fill(TRIAGE_USER, { ...email }),
      schema: TriageSchema,
      temperature: 0,
    });
    // Merge in rule-detected risks: a model can miss a flag, but a regex hit on "lawyer" is never ignored.
    const ruleRisks = ruleTriage(email).risk_flags.filter((f) => f === "legal_threat" || f === "refund" || f === "security_privacy");
    data.risk_flags = [...new Set([...data.risk_flags, ...ruleRisks])];
    return { triage: data, meta: { mode: "live", ...call } };
  } catch (err) {
    const warning = err instanceof LLMError ? `${err.kind}: ${err.message}` : String(err);
    return {
      triage: ruleTriage(email),
      meta: { mode: "fallback", provider: "rules", model: null, input_tokens: 0, output_tokens: 0, cost_usd: null, latency_ms: Date.now() - started, warning },
    };
  }
}
