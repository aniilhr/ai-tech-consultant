/**
 * How the Inbox Agent gets better from human feedback, in three measurable, reversible ways:
 *  1. Few-shot memory: the latest human-approved replies of the same kind are shown to the writer.
 *  2. Style rules: edits (draft vs. what the human actually sent) are distilled into short rules.
 *     Each distillation is a new numbered version; any version can be re-activated (undo).
 *  3. Metrics per rules version: unchanged-approval rate and average edit ratio prove whether a
 *     version helped. Trust (autonomy) per kind is tracked in policy.ts.
 */
import { z } from "zod";
import type { Config } from "./config.js";
import { nowIso, type Db } from "./db.js";
import { LLMError, type LLMClient } from "./llm.js";
import { kindStatus, type KindStatus } from "./policy.js";
import { CATEGORIES, type Category, type ReviewRow } from "./types.js";

const RulesSchema = z.object({
  rules: z.array(z.string()).describe("At most 8 short imperative style rules, each grounded in several edits"),
  note: z.string().describe("One sentence on what changed versus the previous rules"),
});

export const RULES_PROMPT = `You improve an AI support-email writer. Below are pairs of (AI draft, what the human actually sent).
Infer short, general style rules that would make future drafts need fewer edits. Only include a rule if at least
two edits support it. Never add rules about specific prices, refunds, or promises. Current rules: {current}`;

function sentences(text: string): string[] {
  return text
    .split(/(?<=[.!?])\s+|\n+/)
    .map((s) => s.trim().toLowerCase().replace(/\s+/g, " "))
    .filter((s) => s.length > 12);
}

/** Offline distillation: phrases humans repeatedly add or delete, and consistent length changes. */
export function mineRules(pairs: { draft: string; final: string }[]): string[] {
  const added = new Map<string, number>();
  const removed = new Map<string, number>();
  let lengthRatioSum = 0;
  for (const p of pairs) {
    const d = new Set(sentences(p.draft));
    const f = new Set(sentences(p.final));
    for (const s of f) if (!d.has(s)) added.set(s, (added.get(s) ?? 0) + 1);
    for (const s of d) if (!f.has(s)) removed.set(s, (removed.get(s) ?? 0) + 1);
    lengthRatioSum += p.final.split(/\s+/).length / Math.max(p.draft.split(/\s+/).length, 1);
  }
  const rules: string[] = [];
  for (const [s, n] of [...added].sort((a, b) => b[1] - a[1]))
    if (n >= 2 && rules.length < 4) rules.push(`Humans added this in ${n} edits; include it when relevant: "${s}"`);
  for (const [s, n] of [...removed].sort((a, b) => b[1] - a[1]))
    if (n >= 2 && rules.length < 7) rules.push(`Humans deleted this in ${n} edits; avoid writing: "${s}"`);
  if (pairs.length >= 3) {
    const ratio = lengthRatioSum / pairs.length;
    if (ratio < 0.8) rules.push(`Keep replies shorter: humans cut about ${Math.round((1 - ratio) * 100)}% of the words.`);
    if (ratio > 1.25) rules.push(`Add more detail: humans lengthened replies by about ${Math.round((ratio - 1) * 100)}%.`);
  }
  return rules;
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

export class Learning {
  constructor(
    private readonly db: Db,
    private readonly llm: LLMClient,
    private readonly cfg: Config,
  ) {}

  activeRules(): { version: number | null; rules: string[] } {
    const row = this.db.get<{ version: number; rules_json: string }>("SELECT version, rules_json FROM style_rules WHERE active=1");
    return row ? { version: row.version, rules: JSON.parse(row.rules_json) as string[] } : { version: null, rules: [] };
  }

  examplesFor(kind: Category, limit = 2): { subject: string; final_body: string }[] {
    return this.db.all<{ subject: string; final_body: string }>(
      `SELECT e.subject, r.final_body FROM reviews r JOIN emails e ON e.id = r.email_id
       WHERE r.kind = ? AND r.action IN ('approved','own_reply') AND r.final_body IS NOT NULL
       ORDER BY r.id DESC LIMIT ?`,
      kind,
      limit,
    );
  }

  editPairs(limit = 15): { draft: string; final: string }[] {
    return this.db.all<{ draft: string; final: string }>(
      `SELECT d.body AS draft, r.final_body AS final FROM reviews r JOIN drafts d ON d.id = r.draft_id
       WHERE r.action IN ('approved','own_reply') AND r.unchanged = 0 AND r.final_body IS NOT NULL
       ORDER BY r.id DESC LIMIT ?`,
      limit,
    );
  }

  async proposeRules(): Promise<RulesVersion> {
    const pairs = this.editPairs();
    if (pairs.length < 2) throw new Error("Need at least 2 edited replies before rules can be learned.");
    const current = this.activeRules().rules;
    let rules: string[];
    let note: string;
    let source: string;
    if (this.llm.enabled) {
      try {
        const sample = pairs
          .map((p, i) => `### Pair ${i + 1}\nAI DRAFT:\n${p.draft}\n\nHUMAN SENT:\n${p.final}`)
          .join("\n\n");
        const { data, call } = await this.llm.generateJson({
          tier: "smart",
          system: RULES_PROMPT.replace("{current}", current.length ? current.join(" | ") : "none"),
          prompt: sample,
          schema: RulesSchema,
        });
        rules = data.rules.slice(0, 8);
        note = data.note;
        source = `llm:${call.model}`;
      } catch (err) {
        rules = mineRules(pairs);
        note = `Model unavailable (${err instanceof LLMError ? err.kind : "error"}); rules mined from edit statistics.`;
        source = "mined";
      }
    } else {
      rules = mineRules(pairs);
      note = "Rules mined from edit statistics (offline mode).";
      source = "mined";
    }
    const version = this.db.tx(() => {
      this.db.run("UPDATE style_rules SET active=0");
      return this.db.run(
        "INSERT INTO style_rules(rules_json, source, note, active, created_at) VALUES(?,?,?,1,?)",
        JSON.stringify(rules),
        source,
        `${note} (from ${pairs.length} edited replies)`,
        nowIso(),
      );
    });
    const created = this.versions().find((v) => v.version === version);
    if (!created) throw new Error("rules version not stored");
    return created;
  }

  /** Activate any version (rollback); version 0 means "no learned rules". */
  activate(version: number): void {
    if (version > 0 && !this.db.get("SELECT 1 FROM style_rules WHERE version=?", version))
      throw new Error("Unknown rules version");
    this.db.tx(() => {
      this.db.run("UPDATE style_rules SET active=0");
      if (version > 0) this.db.run("UPDATE style_rules SET active=1 WHERE version=?", version);
    });
  }

  versions(): RulesVersion[] {
    const metrics = new Map<number | null, RulesVersion["metrics"]>();
    for (const m of this.db.all<{ v: number | null; n: number; unchanged: number; ratio: number | null }>(
      `SELECT rules_version AS v, COUNT(*) AS n, SUM(unchanged) AS unchanged, AVG(edit_ratio) AS ratio
       FROM reviews WHERE action IN ('approved','rejected','own_reply') GROUP BY rules_version`,
    ))
      metrics.set(m.v, {
        drafts_reviewed: m.n,
        unchanged_rate: m.n ? Math.round((m.unchanged / m.n) * 100) / 100 : null,
        avg_edit_ratio: m.ratio === null ? null : Math.round(m.ratio * 1000) / 1000,
      });
    const empty = { drafts_reviewed: 0, unchanged_rate: null, avg_edit_ratio: null };
    const rows = this.db.all<{ version: number; rules_json: string; source: string; note: string | null; active: number; created_at: string }>(
      "SELECT * FROM style_rules ORDER BY version DESC",
    );
    const baseline: RulesVersion = {
      version: 0,
      rules: [],
      source: "baseline",
      note: "No learned rules",
      active: !rows.some((r) => r.active),
      created_at: "",
      metrics: metrics.get(null) ?? empty,
    };
    return [
      ...rows.map((r) => ({
        version: r.version,
        rules: JSON.parse(r.rules_json) as string[],
        source: r.source,
        note: r.note,
        active: Boolean(r.active),
        created_at: r.created_at,
        metrics: metrics.get(r.version) ?? empty,
      })),
      baseline,
    ];
  }

  kindTable(): KindStatus[] {
    return CATEGORIES.map((kind) =>
      kindStatus(
        kind,
        this.db.all<ReviewRow>("SELECT * FROM reviews WHERE kind=? ORDER BY id", kind),
        this.cfg.autoSendStreakRequired,
      ),
    );
  }
}
