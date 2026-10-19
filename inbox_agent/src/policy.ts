/**
 * Deterministic guardrails. Nothing here calls a model, so the rules are predictable, testable and
 * cannot be talked around by an email that says "ignore your instructions".
 */
import { BLOCKING_RISKS, NEVER_AUTONOMOUS, type Category, type Evidence, type ReviewRow, type RiskFlag } from "./types.js";

const FINANCIAL_PROMISES: { pattern: RegExp; label: string }[] = [
  { pattern: /\b(we|i)('ll| will| have| am going to)\s+(issue|process|send|give)\b[^.]{0,40}\b(refund|credit)/i, label: "promises a refund or credit" },
  { pattern: /\b(refund|credit)\b[^.]{0,30}\b(has been|is being|will be)\s+(issued|processed|applied|sent)/i, label: "states a refund/credit was issued" },
  { pattern: /\b(free of charge|at no (extra )?cost|no charge|waive[ds]?)\b/i, label: "offers something for free" },
  { pattern: /\b\d{1,3}\s?%\s*(off|discount)\b/i, label: "offers a discount" },
  { pattern: /\bguarantee[sd]?\b/i, label: "makes a guarantee" },
  { pattern: /(\$|USD|€|£|₹)\s?\d/i, label: "quotes a price or amount" },
  { pattern: /\b(within|in)\s+\d+\s+(hours?|days?|business days)\b/i, label: "commits to a timeline" },
];

export interface PolicyResult {
  violations: string[];
  blocking_risks: RiskFlag[];
  never_autonomous_kind: boolean;
}

export function checkDraft(body: string, category: Category, risks: RiskFlag[]): PolicyResult {
  const violations: string[] = [];
  for (const { pattern, label } of FINANCIAL_PROMISES) {
    const m = body.match(pattern);
    if (m) violations.push(`Draft ${label}: "${m[0].trim()}"`);
  }
  return {
    violations,
    blocking_risks: risks.filter((r) => BLOCKING_RISKS.has(r)),
    never_autonomous_kind: NEVER_AUTONOMOUS.has(category),
  };
}

export interface KindStatus {
  kind: Category;
  streak: number;
  required: number;
  eligible: boolean;
  blocked_reason: string | null;
  reviewed: number;
  unchanged_total: number;
  recent_unchanged_rate: number | null;
  last_reset: { at: string; reason: string } | null;
}

/**
 * The trust rule. A kind earns autonomy after `required` consecutive human approvals with no edits.
 * Any edit, rejection, hand-written reply, or a flag on an auto-sent email resets the streak to zero,
 * so the kind immediately loses autonomy. Auto-sent emails do not add to the streak (only humans can
 * build trust), but a flag on one destroys it.
 */
export function kindStatus(kind: Category, reviews: Pick<ReviewRow, "action" | "unchanged" | "created_at" | "reason">[], required: number): KindStatus {
  let streak = 0;
  let unchangedTotal = 0;
  let humanReviewed = 0;
  let lastReset: KindStatus["last_reset"] = null;
  const recent: boolean[] = [];
  for (const r of reviews) {
    if (r.action === "auto_sent") continue;
    if (r.action === "approved" && r.unchanged) {
      streak += 1;
      unchangedTotal += 1;
      humanReviewed += 1;
      recent.push(true);
      continue;
    }
    if (r.action !== "flagged_auto") {
      humanReviewed += 1;
      recent.push(false);
    }
    const reason =
      r.action === "approved"
        ? "Human edited the draft"
        : r.action === "rejected"
          ? `Draft rejected${r.reason ? `: ${r.reason}` : ""}`
          : r.action === "own_reply"
            ? "Human wrote their own reply"
            : `Auto-sent reply flagged${r.reason ? `: ${r.reason}` : ""}`;
    if (streak > 0 || r.action === "flagged_auto") lastReset = { at: r.created_at, reason };
    streak = 0;
  }
  const last20 = recent.slice(-20);
  const blocked = NEVER_AUTONOMOUS.has(kind) ? "Sensitive kind: always needs a human" : null;
  return {
    kind,
    streak,
    required,
    eligible: !blocked && streak >= required,
    blocked_reason: blocked,
    reviewed: humanReviewed,
    unchanged_total: unchangedTotal,
    recent_unchanged_rate: last20.length ? Math.round((last20.filter(Boolean).length / last20.length) * 100) / 100 : null,
    last_reset: lastReset,
  };
}

export interface AutoSendDecision {
  allowed: boolean;
  reasons: string[];
}

/** Final gate for one specific email. Every condition must hold; reasons explain each "no". */
export function autoSendDecision(args: {
  globallyEnabled: boolean;
  status: KindStatus;
  policy: PolicyResult;
  evidence: Evidence;
  draftMode: string;
  needsHuman: string[];
}): AutoSendDecision {
  const reasons: string[] = [];
  if (!args.globallyEnabled) reasons.push("Automatic sending is disabled (AUTO_SEND_ENABLED=false)");
  if (args.status.blocked_reason) reasons.push(args.status.blocked_reason);
  else if (!args.status.eligible)
    reasons.push(`Kind "${args.status.kind}" has ${args.status.streak}/${args.status.required} consecutive unchanged approvals`);
  if (args.policy.blocking_risks.length) reasons.push(`Risk flags: ${args.policy.blocking_risks.join(", ")}`);
  if (args.policy.violations.length) reasons.push(...args.policy.violations);
  if (!args.evidence.answered) reasons.push("Knowledge Brain did not find sufficient evidence");
  if (args.draftMode !== "live") reasons.push("Draft was not written by a live model");
  if (args.needsHuman.length) reasons.push(...args.needsHuman.map((r) => `Writer flagged: ${r}`));
  return { allowed: reasons.length === 0, reasons };
}
