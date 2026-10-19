/**
 * Inbox Agent eval over the labelled sample emails.
 *
 *   npm run eval -- --label rules                    # LLM_PROVIDER=offline -> rule-based triage
 *   npm run eval -- --label flash --drafts           # current .env model; also writes drafts
 *   GEMINI_MODEL_FAST=gemini-3.8-flash npm run eval -- --label flash-as-triage
 *
 * Measures: category accuracy, urgency accuracy, recall of critical risk flags (refund, legal_threat,
 * security_privacy), policy violations in drafts (financial promises), latency, tokens and cost.
 * With --drafts the Knowledge Brain at BRAIN_URL is called for evidence (escalations disabled).
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { triageEmail } from "../src/agents/triage.js";
import { writeDraft } from "../src/agents/writer.js";
import { SAMPLE_FILE } from "../src/app.js";
import { BrainClient } from "../src/brainClient.js";
import { loadConfig, loadDotEnv } from "../src/config.js";
import { LLMClient } from "../src/llm.js";
import { checkDraft } from "../src/policy.js";
import type { EmailInput, Evidence, RiskFlag } from "../src/types.js";

const CRITICAL: RiskFlag[] = ["refund", "legal_threat", "security_privacy"];

interface Sample extends EmailInput {
  id: string;
  expected: { category: string; urgency: string; risk_flags: RiskFlag[] };
}

async function main(): Promise<void> {
  loadDotEnv();
  const args = process.argv.slice(2);
  const label = args[args.indexOf("--label") + 1] ?? "default";
  const withDrafts = args.includes("--drafts");
  const limitArg = args.indexOf("--limit");
  const cfg = loadConfig();
  const llm = new LLMClient(cfg);
  const brain = new BrainClient(cfg.brainUrl, cfg.brainTimeoutMs);
  const samples = (JSON.parse(readFileSync(SAMPLE_FILE, "utf8")) as Sample[]).slice(0, limitArg >= 0 ? Number(args[limitArg + 1]) : undefined);

  const rows = [];
  for (const s of samples) {
    const { triage, meta } = await triageEmail(llm, s, cfg.companyName);
    const expectedCritical = s.expected.risk_flags.filter((f) => CRITICAL.includes(f));
    const row: Record<string, unknown> = {
      id: s.id,
      expected_category: s.expected.category,
      category: triage.category,
      category_ok: triage.category === s.expected.category,
      urgency_ok: triage.urgency === s.expected.urgency,
      critical_expected: expectedCritical.length,
      critical_found: expectedCritical.filter((f) => triage.risk_flags.includes(f)).length,
      triage_mode: meta.mode,
      triage_ms: meta.latency_ms,
      tokens: meta.input_tokens + meta.output_tokens,
      cost_usd: meta.cost_usd ?? 0,
    };
    if (withDrafts) {
      const ev: Evidence = triage.kb_question
        ? await brain.ask(triage.kb_question, { createEscalation: false })
        : { available: true, answered: false, answer: "", confidence: 0, citations: [], images: [], escalation_id: null, mode: "skipped", question: "" };
      const { draft, meta: wm } = await writeDraft(llm, s, triage, ev, { company: cfg.companyName, signature: cfg.signature, rules: [], examples: [] });
      const violations = checkDraft(draft.body, triage.category, triage.risk_flags).violations;
      Object.assign(row, {
        brain_answered: ev.answered,
        draft_mode: wm.mode,
        draft_ms: wm.latency_ms,
        violations,
        tokens: Number(row.tokens) + wm.input_tokens + wm.output_tokens,
        cost_usd: Number(row.cost_usd) + (wm.cost_usd ?? 0),
      });
    }
    rows.push(row);
    console.log(`${s.id} ${row.category_ok ? "ok " : "BAD"} ${s.expected.category} -> ${triage.category}`);
  }

  const n = rows.length;
  const pct = (k: number) => Math.round((k / Math.max(n, 1)) * 1000) / 1000;
  const critExpected = rows.reduce((a, r) => a + Number(r.critical_expected), 0);
  const critFound = rows.reduce((a, r) => a + Number(r.critical_found), 0);
  const median = (xs: number[]) => xs.sort((a, b) => a - b)[Math.floor(xs.length / 2)] ?? 0;
  const summary = {
    label,
    provider: llm.provider,
    triage_model: llm.enabled ? llm.modelFor("fast") : "rules",
    writer_model: withDrafts ? (llm.enabled ? llm.modelFor("smart") : "template") : null,
    emails: n,
    category_accuracy: pct(rows.filter((r) => r.category_ok).length),
    urgency_accuracy: pct(rows.filter((r) => r.urgency_ok).length),
    critical_risk_recall: critExpected ? Math.round((critFound / critExpected) * 1000) / 1000 : null,
    drafts_with_policy_violations: withDrafts ? rows.filter((r) => (r.violations as string[]).length).length : null,
    p50_triage_ms: median(rows.map((r) => Number(r.triage_ms))),
    avg_tokens_per_email: Math.round(rows.reduce((a, r) => a + Number(r.tokens), 0) / Math.max(n, 1)),
    cost_per_email_usd: Math.round((rows.reduce((a, r) => a + Number(r.cost_usd), 0) / Math.max(n, 1)) * 1e6) / 1e6,
  };
  const outDir = path.join(path.dirname(fileURLToPath(import.meta.url)), "results");
  mkdirSync(outDir, { recursive: true });
  writeFileSync(path.join(outDir, `${label}.json`), JSON.stringify({ summary, rows }, null, 2));
  console.log(JSON.stringify(summary, null, 2));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
