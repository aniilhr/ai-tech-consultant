import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { ruleTriage } from "../src/agents/triage.js";
import { templateDraft } from "../src/agents/writer.js";
import { mineRules } from "../src/learning.js";
import { checkDraft } from "../src/policy.js";
import { SAMPLE_FILE } from "../src/app.js";
import type { EmailInput, Evidence } from "../src/types.js";

const samples = JSON.parse(readFileSync(SAMPLE_FILE, "utf8")) as (EmailInput & { id: string; expected: { category: string } })[];
const ctx = { company: "Acme", signature: "The Acme Support Team", rules: [], examples: [] };
const answered: Evidence = {
  available: true,
  answered: true,
  answer: "Here is what the help center says (extractive answer, no AI model configured):\n\n> Go to Members > Membership Plans.",
  confidence: 0.7,
  citations: [{ title: "Plans", url: "https://help.test/plans", source: "helpcenter", snippet: "" }],
  images: [],
  escalation_id: null,
  mode: "offline",
  question: "q",
};

describe("sample dataset", () => {
  it("has at least 50 labelled emails covering every category", () => {
    expect(samples.length).toBeGreaterThanOrEqual(50);
    expect(new Set(samples.map((s) => s.id)).size).toBe(samples.length);
    expect(new Set(samples.map((s) => s.expected.category)).size).toBe(12);
  });
});

describe("rule-based triage fallback", () => {
  it("catches the prompt-injection refund email as a refund with risk flags", () => {
    const email = samples.find((s) => s.id === "E028");
    expect(email).toBeDefined();
    const t = ruleTriage(email as EmailInput);
    expect(t.category).toBe("refund_request");
    expect(t.risk_flags).toContain("refund");
  });
  it("uses the customer's question sentences as the Knowledge Brain query", () => {
    const t = ruleTriage(samples.find((s) => s.id === "E001") as EmailInput);
    expect(t.kb_question).toBe("How do I add a new membership plan and set the price?");
  });
  it("marks outages urgent and legal threats", () => {
    expect(ruleTriage(samples.find((s) => s.id === "E013") as EmailInput)).toMatchObject({ category: "technical_issue", urgency: "urgent" });
    expect(ruleTriage(samples.find((s) => s.id === "E027") as EmailInput).risk_flags).toContain("legal_threat");
  });
});

describe("template writer", () => {
  it("never makes a financial promise for any of the sample emails", () => {
    for (const email of samples) {
      const triage = ruleTriage(email);
      for (const ev of [answered, { ...answered, answered: false, escalation_id: 7 }]) {
        const draft = templateDraft(email, triage, ev, ctx);
        expect(checkDraft(draft.body, triage.category, []).violations, email.id).toEqual([]);
        expect(draft.body).toContain("The Acme Support Team");
      }
    }
  });
  it("uses brain facts and links when answered, escalation reference when not", () => {
    const email = samples[0] as EmailInput;
    const t = ruleTriage(email);
    const yes = templateDraft(email, t, answered, ctx);
    expect(yes.body).toContain("Go to Members > Membership Plans.");
    expect(yes.body).toContain("https://help.test/plans");
    expect(yes.body).not.toContain("extractive answer");
    const no = templateDraft(email, t, { ...answered, answered: false, escalation_id: 7 }, ctx);
    expect(no.body).toContain("reference #7");
    expect(no.needs_human_reasons).toContain("Knowledge Brain found no sufficient answer");
  });
});

describe("offline rule mining", () => {
  it("learns phrases humans keep adding and removing", () => {
    const pairs = [1, 2, 3].map(() => ({
      draft: "Hi there. Thanks for reaching out to us today. Go to Settings and click Save.",
      final: "Hi there. Go to Settings and click Save. Let us know if you need anything else at all.",
    }));
    const rules = mineRules(pairs);
    expect(rules.some((r) => r.includes("let us know if you need anything else at all"))).toBe(true);
    expect(rules.some((r) => r.includes("thanks for reaching out to us today"))).toBe(true);
  });
});
