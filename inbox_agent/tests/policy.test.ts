import { describe, expect, it } from "vitest";
import { autoSendDecision, checkDraft, kindStatus } from "../src/policy.js";
import { editRatio, isUnchanged } from "../src/similarity.js";
import type { Evidence, ReviewRow } from "../src/types.js";

type R = Pick<ReviewRow, "action" | "unchanged" | "created_at" | "reason">;
const approve = (unchanged: boolean): R => ({ action: "approved", unchanged: unchanged ? 1 : 0, created_at: "t", reason: null });
const evidence: Evidence = {
  available: true,
  answered: true,
  answer: "",
  confidence: 0.8,
  citations: [],
  images: [],
  escalation_id: null,
  mode: "live",
  question: "q",
};

describe("similarity", () => {
  it("treats whitespace-only changes as unchanged", () => {
    expect(isUnchanged("Hi Ana,\n\nThanks!", "Hi Ana,\n\n\n\nThanks!  ")).toBe(true);
  });
  it("counts a single word change as a change", () => {
    expect(isUnchanged("You can export members.", "You cannot export members.")).toBe(false);
    expect(editRatio("You can export members.", "You cannot export members.")).toBe(0.25);
  });
  it("edit ratio is 1 for a full rewrite", () => {
    expect(editRatio("a b c", "x y z")).toBe(1);
  });
});

describe("trust streak (20 unchanged in a row)", () => {
  it("becomes eligible exactly at the required streak", () => {
    const reviews = Array.from({ length: 19 }, () => approve(true));
    expect(kindStatus("how_to", reviews, 20).eligible).toBe(false);
    expect(kindStatus("how_to", [...reviews, approve(true)], 20)).toMatchObject({ streak: 20, eligible: true });
  });
  it("any edit resets the streak to zero", () => {
    const reviews = [...Array.from({ length: 25 }, () => approve(true)), approve(false), approve(true)];
    const s = kindStatus("how_to", reviews, 20);
    expect(s.streak).toBe(1);
    expect(s.eligible).toBe(false);
    expect(s.last_reset?.reason).toBe("Human edited the draft");
  });
  it("rejections, own replies and flags on auto-sent mail reset; auto-sent mail does not build trust", () => {
    const base = Array.from({ length: 20 }, () => approve(true));
    for (const action of ["rejected", "own_reply", "flagged_auto"] as const)
      expect(kindStatus("how_to", [...base, { action, unchanged: 0, created_at: "t", reason: "x" }], 20).streak).toBe(0);
    const withAuto = [...base, { action: "auto_sent" as const, unchanged: 1, created_at: "t", reason: null }];
    expect(kindStatus("how_to", withAuto, 20).streak).toBe(20);
  });
  it("sensitive kinds never become eligible", () => {
    const reviews = Array.from({ length: 50 }, () => approve(true));
    for (const kind of ["refund_request", "cancellation", "complaint", "new_project_quote", "billing_question", "account_access"] as const) {
      const s = kindStatus(kind, reviews, 20);
      expect(s.eligible).toBe(false);
      expect(s.blocked_reason).toMatch(/Sensitive/);
    }
  });
});

describe("draft policy guard", () => {
  it.each([
    ["We will issue a full refund to your card today.", "refund"],
    ["Your refund has been processed.", "refund/credit was issued"],
    ["We can do this free of charge.", "free"],
    ["Here is 20% off your next invoice.", "discount"],
    ["We guarantee the fix by Friday.", "guarantee"],
    ["The custom work costs $1,500.", "price"],
    ["We'll have it fixed within 2 business days.", "timeline"],
  ])("flags %s", (body, label) => {
    const result = checkDraft(body, "how_to", []);
    expect(result.violations.join(" ")).toContain(label);
  });
  it("does not flag a normal procedural reply", () => {
    const body = "Hi Tom,\n\nGo to Members > Import and upload the CSV template. Our billing team will review your request and reply to you directly.\n\nBest regards";
    expect(checkDraft(body, "how_to", []).violations).toEqual([]);
  });
  it("auto-send needs every condition and explains each refusal", () => {
    const eligible = kindStatus("how_to", Array.from({ length: 20 }, () => approve(true)), 20);
    const clean = checkDraft("Steps: go to Settings.", "how_to", []);
    const ok = autoSendDecision({ globallyEnabled: true, status: eligible, policy: clean, evidence, draftMode: "live", needsHuman: [] });
    expect(ok).toEqual({ allowed: true, reasons: [] });
    const off = autoSendDecision({ globallyEnabled: false, status: eligible, policy: clean, evidence, draftMode: "live", needsHuman: [] });
    expect(off.allowed).toBe(false);
    expect(off.reasons[0]).toMatch(/AUTO_SEND_ENABLED=false/);
    const risky = autoSendDecision({
      globallyEnabled: true,
      status: eligible,
      policy: checkDraft("ok", "how_to", ["angry_customer"]),
      evidence: { ...evidence, answered: false },
      draftMode: "offline",
      needsHuman: [],
    });
    expect(risky.reasons).toEqual([
      "Risk flags: angry_customer",
      "Knowledge Brain did not find sufficient evidence",
      "Draft was not written by a live model",
    ]);
  });
});
