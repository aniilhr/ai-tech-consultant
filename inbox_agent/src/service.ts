/**
 * InboxService orchestrates one email end to end:
 *   Triage agent (fast model) -> Knowledge Brain /v1/ask (Task 1 API) -> Writer agent (smart model)
 *   -> Policy guard (deterministic) -> stored draft -> human review (or, only if earned and enabled, auto-send).
 */
import { readFileSync } from "node:fs";
import { triageEmail } from "./agents/triage.js";
import { writeDraft } from "./agents/writer.js";
import type { BrainClient } from "./brainClient.js";
import type { Config } from "./config.js";
import { nowIso, type Db } from "./db.js";
import { Learning } from "./learning.js";
import type { LLMClient } from "./llm.js";
import { autoSendDecision, checkDraft, kindStatus } from "./policy.js";
import { TRIAGE_PROMPT_VERSION, WRITER_PROMPT_VERSION } from "./prompts.js";
import { editRatio, isUnchanged } from "./similarity.js";
import type { Category, EmailInput, EmailRow, EmailStatus, Evidence, ReviewRow, RiskFlag, Triage } from "./types.js";

/** Kinds where an unanswered question is a gap in the knowledge base worth a human expert's time. */
const ESCALATE_KINDS = new Set<Category>(["how_to", "technical_issue", "feature_request", "account_access"]);

export class NotFound extends Error {}
export class Conflict extends Error {}

interface DraftRow {
  id: number;
  email_id: string;
  kind: Category;
  subject: string;
  body: string;
  evidence_json: string;
  needs_human_json: string;
  policy_json: string;
  confidence: number;
  llm_json: string;
  prompt_version: string;
  rules_version: number | null;
  created_at: string;
}

interface TriageRow {
  category: Category;
  urgency: string;
  risk_flags: string;
  sentiment: string;
  summary: string;
  kb_question: string;
  confidence: number;
  llm_json: string;
}

export class InboxService {
  readonly learning: Learning;

  constructor(
    private readonly db: Db,
    private readonly llm: LLMClient,
    private readonly brain: BrainClient,
    private readonly cfg: Config,
  ) {
    this.learning = new Learning(db, llm, cfg);
  }

  // ------------------------------------------------------------------ emails
  seedFromFile(file: string, replace = false): number {
    const samples = JSON.parse(readFileSync(file, "utf8")) as (EmailInput & { id: string; received_at: string; expected: unknown })[];
    return this.db.tx(() => {
      if (replace) this.db.run("DELETE FROM emails WHERE is_sample=1");
      let n = 0;
      for (const s of samples) {
        if (this.db.get("SELECT 1 FROM emails WHERE id=?", s.id)) continue;
        this.db.run(
          "INSERT INTO emails(id, from_name, from_email, subject, body, received_at, status, expected_json, is_sample) VALUES(?,?,?,?,?,?, 'new', ?, 1)",
          s.id,
          s.from_name,
          s.from_email,
          s.subject,
          s.body,
          s.received_at,
          JSON.stringify(s.expected ?? null),
        );
        n++;
      }
      return n;
    });
  }

  createEmail(input: EmailInput): string {
    const id = `M${Date.now().toString(36).toUpperCase()}${Math.floor(Math.random() * 1000)}`;
    this.db.run(
      "INSERT INTO emails(id, from_name, from_email, subject, body, received_at, status) VALUES(?,?,?,?,?,?, 'new')",
      id,
      input.from_name,
      input.from_email,
      input.subject,
      input.body,
      nowIso(),
    );
    return id;
  }

  listEmails(filter: { status?: string; category?: string; q?: string }): unknown[] {
    const where: string[] = [];
    const params: string[] = [];
    if (filter.status) {
      where.push("e.status = ?");
      params.push(filter.status);
    }
    if (filter.category) {
      where.push("t.category = ?");
      params.push(filter.category);
    }
    if (filter.q) {
      where.push("(e.subject LIKE ? OR e.body LIKE ? OR e.from_name LIKE ?)");
      params.push(`%${filter.q}%`, `%${filter.q}%`, `%${filter.q}%`);
    }
    return this.db.all(
      `SELECT e.id, e.from_name, e.from_email, e.subject, substr(e.body, 1, 160) AS preview, e.received_at, e.status,
              t.category, t.urgency, t.risk_flags
       FROM emails e LEFT JOIN triage t ON t.email_id = e.id
       ${where.length ? `WHERE ${where.join(" AND ")}` : ""}
       ORDER BY CASE t.urgency WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, e.received_at DESC`,
      ...params,
    ).map((r) => ({ ...r, risk_flags: r.risk_flags ? JSON.parse(String(r.risk_flags)) : [] }));
  }

  getEmail(id: string): Record<string, unknown> {
    const email = this.db.get<EmailRow>("SELECT * FROM emails WHERE id=?", id);
    if (!email) throw new NotFound(`Email ${id} not found`);
    const triage = this.db.get<TriageRow>("SELECT * FROM triage WHERE email_id=?", id);
    const drafts = this.db.all<DraftRow>("SELECT * FROM drafts WHERE email_id=? ORDER BY id DESC", id).map(parseDraft);
    const reviews = this.db.all<ReviewRow>("SELECT * FROM reviews WHERE email_id=? ORDER BY id DESC", id);
    const outbox = this.db.all("SELECT * FROM outbox WHERE email_id=? ORDER BY id DESC", id);
    const kind = triage?.category;
    return {
      email: { ...email, expected: email.expected_json ? JSON.parse(email.expected_json) : null },
      triage: triage
        ? { ...triage, risk_flags: JSON.parse(triage.risk_flags), llm: JSON.parse(triage.llm_json), llm_json: undefined }
        : null,
      drafts,
      reviews,
      outbox,
      kind_status: kind
        ? kindStatus(kind, this.db.all<ReviewRow>("SELECT * FROM reviews WHERE kind=? ORDER BY id", kind), this.cfg.autoSendStreakRequired)
        : null,
    };
  }

  // ------------------------------------------------------------------ pipeline
  async process(id: string): Promise<Record<string, unknown>> {
    const email = this.db.get<EmailRow>("SELECT * FROM emails WHERE id=?", id);
    if (!email) throw new NotFound(`Email ${id} not found`);
    if (["approved", "replied_manually", "auto_sent"].includes(email.status))
      throw new Conflict(`Email already ${email.status}; it cannot be re-drafted`);

    const { triage, meta: triageMeta } = await triageEmail(this.llm, email, this.cfg.companyName);
    this.db.run(
      `INSERT INTO triage(email_id, category, urgency, risk_flags, sentiment, summary, kb_question, confidence, llm_json, created_at)
       VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(email_id) DO UPDATE SET category=excluded.category, urgency=excluded.urgency,
       risk_flags=excluded.risk_flags, sentiment=excluded.sentiment, summary=excluded.summary, kb_question=excluded.kb_question,
       confidence=excluded.confidence, llm_json=excluded.llm_json, created_at=excluded.created_at`,
      id,
      triage.category,
      triage.urgency,
      JSON.stringify(triage.risk_flags),
      triage.sentiment,
      triage.summary,
      triage.kb_question,
      triage.confidence,
      JSON.stringify({ ...triageMeta, prompt_version: TRIAGE_PROMPT_VERSION }),
      nowIso(),
    );

    const evidence = await this.research(triage, email);
    const rules = this.learning.activeRules();
    const { draft, meta } = await writeDraft(this.llm, email, triage, evidence, {
      company: this.cfg.companyName,
      signature: this.cfg.signature,
      rules: rules.rules,
      examples: this.learning.examplesFor(triage.category),
    });
    const policy = checkDraft(draft.body, triage.category, triage.risk_flags);
    const needsHuman = [...draft.needs_human_reasons];
    if (!evidence.available) needsHuman.push(evidence.error ?? "Knowledge Brain unavailable");
    const draftId = this.db.run(
      `INSERT INTO drafts(email_id, kind, subject, body, evidence_json, needs_human_json, policy_json, confidence, llm_json,
                          prompt_version, rules_version, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)`,
      id,
      triage.category,
      draft.subject,
      draft.body,
      JSON.stringify(evidence),
      JSON.stringify(needsHuman),
      JSON.stringify(policy),
      draft.confidence,
      JSON.stringify(meta),
      WRITER_PROMPT_VERSION,
      rules.version,
      nowIso(),
    );
    this.setStatus(id, "drafted");

    const status = kindStatus(
      triage.category,
      this.db.all<ReviewRow>("SELECT * FROM reviews WHERE kind=? ORDER BY id", triage.category),
      this.cfg.autoSendStreakRequired,
    );
    const decision = autoSendDecision({
      globallyEnabled: this.cfg.autoSendEnabled,
      status,
      policy,
      evidence,
      draftMode: meta.mode,
      needsHuman,
    });
    if (decision.allowed) {
      this.db.tx(() => {
        this.db.run(
          "INSERT INTO reviews(email_id, draft_id, kind, action, final_body, unchanged, edit_ratio, reviewer, rules_version, created_at) VALUES(?,?,?, 'auto_sent', ?, 1, 0, 'agent', ?, ?)",
          id,
          draftId,
          triage.category,
          draft.body,
          rules.version,
          nowIso(),
        );
        this.toOutbox(id, draftId, email.from_email, draft.subject, draft.body, "auto");
        this.setStatus(id, "auto_sent");
      });
    }
    return { ...this.getEmail(id), auto_send: decision };
  }

  private async research(triage: Triage, email: EmailInput): Promise<Evidence> {
    if (!triage.kb_question.trim()) {
      return { available: true, answered: false, answer: "", confidence: 0, citations: [], images: [], escalation_id: null, mode: "skipped", question: "" };
    }
    return this.brain.ask(triage.kb_question, {
      createEscalation: ESCALATE_KINDS.has(triage.category),
      context: `Customer email from ${email.from_email}: ${email.subject}`,
    });
  }

  async processBatch(limit: number): Promise<{ processed: string[]; errors: { id: string; error: string }[] }> {
    const ids = this.db.all<{ id: string }>("SELECT id FROM emails WHERE status='new' ORDER BY received_at LIMIT ?", limit);
    const processed: string[] = [];
    const errors: { id: string; error: string }[] = [];
    for (const { id } of ids) {
      try {
        await this.process(id);
        processed.push(id);
      } catch (err) {
        errors.push({ id, error: String(err) });
      }
    }
    return { processed, errors };
  }

  // ------------------------------------------------------------------ human review
  approve(id: string, args: { draft_id: number; final_body: string; reviewer: string }): Record<string, unknown> {
    const { email, draft, kind } = this.reviewable(id, args.draft_id);
    const unchanged = isUnchanged(draft.body, args.final_body);
    this.db.tx(() => {
      this.db.run(
        "INSERT INTO reviews(email_id, draft_id, kind, action, final_body, unchanged, edit_ratio, reviewer, rules_version, created_at) VALUES(?,?,?, 'approved', ?,?,?,?,?,?)",
        id,
        draft.id,
        kind,
        args.final_body,
        unchanged ? 1 : 0,
        editRatio(draft.body, args.final_body),
        args.reviewer,
        draft.rules_version,
        nowIso(),
      );
      this.toOutbox(id, draft.id, email.from_email, draft.subject, args.final_body, "human");
      this.setStatus(id, "approved");
    });
    return this.getEmail(id);
  }

  reject(id: string, args: { draft_id: number; reason: string; reviewer: string }): Record<string, unknown> {
    const { draft, kind } = this.reviewable(id, args.draft_id);
    this.db.tx(() => {
      this.db.run(
        "INSERT INTO reviews(email_id, draft_id, kind, action, unchanged, edit_ratio, reviewer, reason, rules_version, created_at) VALUES(?,?,?, 'rejected', 0, 1, ?,?,?,?)",
        id,
        draft.id,
        kind,
        args.reviewer,
        args.reason,
        draft.rules_version,
        nowIso(),
      );
      this.setStatus(id, "rejected");
    });
    return this.getEmail(id);
  }

  ownReply(id: string, args: { body: string; reviewer: string }): Record<string, unknown> {
    const email = this.db.get<EmailRow>("SELECT * FROM emails WHERE id=?", id);
    if (!email) throw new NotFound(`Email ${id} not found`);
    if (["approved", "replied_manually", "auto_sent"].includes(email.status)) throw new Conflict(`Email already ${email.status}`);
    const triage = this.db.get<{ category: Category }>("SELECT category FROM triage WHERE email_id=?", id);
    const latest = this.db.get<DraftRow>("SELECT * FROM drafts WHERE email_id=? ORDER BY id DESC LIMIT 1", id);
    this.db.tx(() => {
      this.db.run(
        "INSERT INTO reviews(email_id, draft_id, kind, action, final_body, unchanged, edit_ratio, reviewer, rules_version, created_at) VALUES(?,?,?, 'own_reply', ?, 0, ?, ?, ?, ?)",
        id,
        latest?.id ?? null,
        triage?.category ?? "other",
        args.body,
        latest ? editRatio(latest.body, args.body) : 1,
        args.reviewer,
        latest?.rules_version ?? null,
        nowIso(),
      );
      this.toOutbox(id, latest?.id ?? null, email.from_email, `Re: ${email.subject}`, args.body, "human");
      this.setStatus(id, "replied_manually");
    });
    return this.getEmail(id);
  }

  /** A human says an automatically sent reply was wrong: the kind loses its autonomy immediately. */
  flagAutoSent(id: string, args: { reason: string; reviewer: string }): Record<string, unknown> {
    const auto = this.db.get<ReviewRow>("SELECT * FROM reviews WHERE email_id=? AND action='auto_sent' ORDER BY id DESC LIMIT 1", id);
    if (!auto) throw new Conflict("Only automatically sent emails can be flagged");
    this.db.run(
      "INSERT INTO reviews(email_id, draft_id, kind, action, unchanged, reviewer, reason, rules_version, created_at) VALUES(?,?,?, 'flagged_auto', 0, ?,?,?,?)",
      id,
      auto.draft_id,
      auto.kind,
      args.reviewer,
      args.reason,
      auto.rules_version,
      nowIso(),
    );
    return this.getEmail(id);
  }

  private reviewable(id: string, draftId: number): { email: EmailRow; draft: DraftRow; kind: Category } {
    const email = this.db.get<EmailRow>("SELECT * FROM emails WHERE id=?", id);
    if (!email) throw new NotFound(`Email ${id} not found`);
    if (email.status !== "drafted") throw new Conflict(`Email is ${email.status}; only drafted emails can be reviewed`);
    const draft = this.db.get<DraftRow>("SELECT * FROM drafts WHERE id=? AND email_id=?", draftId, id);
    if (!draft) throw new NotFound(`Draft ${draftId} not found for email ${id}`);
    const latest = this.db.get<{ id: number }>("SELECT id FROM drafts WHERE email_id=? ORDER BY id DESC LIMIT 1", id);
    if (latest?.id !== draft.id) throw new Conflict("A newer draft exists; review the latest draft");
    return { email, draft, kind: draft.kind };
  }

  private toOutbox(emailId: string, draftId: number | null, to: string, subject: string, body: string, by: "human" | "auto"): void {
    // No SMTP: the outbox is the hand-off point to a real mail provider. Sending is simulated on purpose.
    this.db.run(
      "INSERT INTO outbox(email_id, draft_id, to_email, subject, body, sent_by, created_at) VALUES(?,?,?,?,?,?,?)",
      emailId,
      draftId,
      to,
      subject,
      body,
      by,
      nowIso(),
    );
  }

  private setStatus(id: string, status: EmailStatus): void {
    this.db.run("UPDATE emails SET status=? WHERE id=?", status, id);
  }

  // ------------------------------------------------------------------ dashboards
  stats(): Record<string, unknown> {
    const byStatus = Object.fromEntries(
      this.db.all<{ status: string; n: number }>("SELECT status, COUNT(*) AS n FROM emails GROUP BY status").map((r) => [r.status, r.n]),
    );
    const byCategory = Object.fromEntries(
      this.db.all<{ category: string; n: number }>("SELECT category, COUNT(*) AS n FROM triage GROUP BY category").map((r) => [r.category, r.n]),
    );
    const review = this.db.get<{ n: number; unchanged: number | null; ratio: number | null }>(
      "SELECT COUNT(*) AS n, SUM(unchanged) AS unchanged, AVG(edit_ratio) AS ratio FROM reviews WHERE action IN ('approved','rejected','own_reply')",
    );
    const cost = this.db.all<{ llm_json: string }>("SELECT llm_json FROM drafts").reduce((sum, r) => {
      const c = (JSON.parse(r.llm_json) as { cost_usd: number | null }).cost_usd;
      return sum + (c ?? 0);
    }, 0);
    return {
      emails: byStatus,
      categories: byCategory,
      reviews: {
        total: review?.n ?? 0,
        unchanged_rate: review?.n ? Math.round(((review.unchanged ?? 0) / review.n) * 100) / 100 : null,
        avg_edit_ratio: review?.ratio === null || review?.ratio === undefined ? null : Math.round(review.ratio * 1000) / 1000,
      },
      outbox: this.db.get<{ n: number }>("SELECT COUNT(*) AS n FROM outbox")?.n ?? 0,
      draft_cost_usd: Math.round(cost * 1e6) / 1e6,
      auto_send_enabled: this.cfg.autoSendEnabled,
      streak_required: this.cfg.autoSendStreakRequired,
      llm: this.llm.describe(),
    };
  }
}

function parseDraft(d: DraftRow): Record<string, unknown> {
  return {
    ...d,
    evidence: JSON.parse(d.evidence_json) as Evidence,
    needs_human: JSON.parse(d.needs_human_json) as string[],
    policy: JSON.parse(d.policy_json) as { violations: string[]; blocking_risks: RiskFlag[] },
    llm: JSON.parse(d.llm_json),
    evidence_json: undefined,
    needs_human_json: undefined,
    policy_json: undefined,
    llm_json: undefined,
  };
}
