import request from "supertest";
import { describe, expect, it } from "vitest";
import { SAMPLE_FILE, createApp } from "../src/app.js";
import type { Config } from "../src/config.js";
import { fakeFetch, newState, testConfig, type FakeState } from "./helpers.js";

function setup(cfg: Partial<Config> = {}, state: FakeState = newState()) {
  const { app, service } = createApp({ cfg: testConfig(cfg), fetchFn: fakeFetch(state), llmBackoffMs: 0 });
  service.seedFromFile(SAMPLE_FILE);
  return { app, service, state };
}

interface Detail {
  email: { status: string };
  triage: { category: string; llm: { mode: string; warning?: string } };
  drafts: { id: number; body: string; needs_human: string[]; evidence: { answered: boolean; citations: unknown[] }; llm: { mode: string } }[];
  kind_status: { streak: number; eligible: boolean };
  auto_send?: { allowed: boolean; reasons: string[] };
}

describe("Inbox Agent API", () => {
  it("lists 55 seeded emails", async () => {
    const { app } = setup();
    const res = await request(app).get("/emails");
    expect(res.status).toBe(200);
    expect(res.body).toHaveLength(55);
  });

  it("processing calls the Knowledge Brain API and stores evidence + draft (offline mode)", async () => {
    const { app, state } = setup();
    const res = await request(app).post("/emails/E001/process");
    expect(res.status).toBe(200);
    const d = res.body as Detail;
    expect(state.brainCalls).toHaveLength(1);
    expect(state.brainCalls[0]).toMatchObject({ source: "inbox", create_escalation: true });
    expect(d.triage.category).toBe("how_to");
    expect(d.triage.llm.mode).toBe("offline");
    expect(d.drafts[0]?.evidence.answered).toBe(true);
    expect(d.drafts[0]?.evidence.citations).toHaveLength(1);
    expect(d.drafts[0]?.llm.mode).toBe("offline");
    expect(d.email.status).toBe("drafted");
    expect(d.auto_send?.allowed).toBe(false);
  });

  it("a refund email does not ask the Brain to escalate and stays human-only", async () => {
    const { app, state } = setup();
    const d = (await request(app).post("/emails/E024/process")).body as Detail;
    expect(d.triage.category).toBe("refund_request");
    expect(state.brainCalls[0]?.create_escalation).toBe(false);
    expect(d.auto_send?.reasons.join(" ")).toMatch(/Sensitive kind/);
  });

  it("missing knowledge produces an honest fallback draft with an escalation reference", async () => {
    const state = newState();
    state.brainAnswered = false;
    const { app } = setup({}, state);
    const d = (await request(app).post("/emails/E037/process")).body as Detail;
    expect(d.drafts[0]?.body).toContain("reference #42");
    expect(d.drafts[0]?.needs_human).toContain("Knowledge Brain found no sufficient answer");
  });

  it("Knowledge Brain outage still yields a draft flagged for a human", async () => {
    const state = newState();
    state.brainDown = true;
    const { app } = setup({}, state);
    const d = (await request(app).post("/emails/E002/process")).body as Detail;
    expect(d.drafts).toHaveLength(1);
    expect(d.drafts[0]?.needs_human.join(" ")).toMatch(/unreachable/);
  });

  it("invalid Gemini key degrades to rules + template and reports the problem", async () => {
    const state = newState();
    state.geminiMode = "invalid_key";
    const { app } = setup({ geminiApiKey: "bad" }, state);
    const d = (await request(app).post("/emails/E001/process")).body as Detail;
    expect(d.triage.llm.mode).toBe("fallback");
    expect(d.triage.llm.warning).toMatch(/auth/);
    expect(d.drafts[0]?.llm.mode).toBe("fallback");
    expect(state.geminiCalls.length).toBe(2); // auth errors are not retried
  });

  it("live mode uses Gemini for triage and writing", async () => {
    const { app, state } = setup({ geminiApiKey: "k", modelPrices: { "gemini-3.8-flash": [1, 2] } });
    const d = (await request(app).post("/emails/E001/process")).body as Detail & { drafts: { llm: { model: string; cost_usd: number } }[] };
    expect(d.triage.llm.mode).toBe("live");
    expect(d.drafts[0]?.llm.model).toBe("gemini-3.8-flash");
    expect(d.drafts[0]?.llm.cost_usd).toBeCloseTo(300e-6 + 120e-6, 9);
    expect(state.geminiCalls.some((u) => u.includes("gemini-3.5-flash-lite"))).toBe(true);
  });

  it("stores human edits, rejection and own replies with history", async () => {
    const { app } = setup();
    let d = (await request(app).post("/emails/E002/process")).body as Detail;
    const draft = d.drafts[0];
    if (!draft) throw new Error("no draft");
    const edited = `${draft.body}\nP.S. Remember to switch Test Mode off.`;
    const res = await request(app).post("/emails/E002/approve").send({ draft_id: draft.id, final_body: edited, reviewer: "anil" });
    expect(res.status).toBe(200);
    const reviewed = res.body as { reviews: { action: string; unchanged: number; edit_ratio: number }[]; outbox: unknown[] };
    expect(reviewed.reviews[0]).toMatchObject({ action: "approved", unchanged: 0 });
    expect(reviewed.reviews[0]?.edit_ratio).toBeGreaterThan(0);
    expect(reviewed.outbox).toHaveLength(1);
    expect((await request(app).post("/emails/E002/approve").send({ draft_id: draft.id, final_body: edited })).status).toBe(409);

    d = (await request(app).post("/emails/E003/process")).body as Detail;
    const rej = await request(app).post("/emails/E003/reject").send({ draft_id: d.drafts[0]?.id, reason: "Wrong DNS advice" });
    expect((rej.body as Detail).email.status).toBe("rejected");
    // regenerate after rejection is allowed
    expect((await request(app).post("/emails/E003/process")).status).toBe(200);

    const own = await request(app).post("/emails/E055/own-reply").send({ body: "Thank you so much, Ella!", reviewer: "anil" });
    expect((own.body as Detail).email.status).toBe("replied_manually");
  });

  it("validates input", async () => {
    const { app } = setup();
    expect((await request(app).post("/emails").send({ from_name: "A", from_email: "not-an-email", subject: "x", body: "hello there" })).status).toBe(422);
    const created = await request(app).post("/emails").send({ from_name: "Ann", from_email: "ann@x.example", subject: "Help", body: "How do I add a plan?" });
    expect(created.status).toBe(201);
    expect((await request(app).get("/emails/NOPE")).status).toBe(404);
  });

  it("20 unchanged approvals earn autonomy; auto-send only when enabled; a flag revokes it", async () => {
    const { app, service } = setup({ geminiApiKey: "k", autoSendEnabled: true, autoSendStreakRequired: 20 });
    const send = async (i: number) =>
      (await request(app).post("/emails").send({ from_name: "Maria", from_email: `m${i}@x.example`, subject: `Plan ${i}`, body: "How do I add a new membership plan?" }))
        .body as { email: { id: string } };
    for (let i = 0; i < 20; i++) {
      const id = (await send(i)).email.id;
      const d = (await request(app).post(`/emails/${id}/process`)).body as Detail;
      expect(d.auto_send?.allowed).toBe(false);
      const draft = d.drafts[0];
      await request(app).post(`/emails/${id}/approve`).send({ draft_id: draft?.id, final_body: draft?.body });
    }
    const learning = (await request(app).get("/learning")).body as { kinds: { kind: string; streak: number; eligible: boolean }[] };
    expect(learning.kinds.find((k) => k.kind === "how_to")).toMatchObject({ streak: 20, eligible: true });

    const id21 = (await send(21)).email.id;
    const auto = (await request(app).post(`/emails/${id21}/process`)).body as Detail;
    expect(auto.auto_send?.allowed).toBe(true);
    expect(auto.email.status).toBe("auto_sent");

    await request(app).post(`/emails/${id21}/flag`).send({ reason: "Linked the wrong article" });
    const after = service.learning.kindTable().find((k) => k.kind === "how_to");
    expect(after).toMatchObject({ streak: 0, eligible: false });
  });

  it("with auto-send disabled (default) an eligible kind is still never sent", async () => {
    const { app } = setup({ geminiApiKey: "k", autoSendStreakRequired: 1 });
    let d = (await request(app).post("/emails/E001/process")).body as Detail;
    await request(app).post("/emails/E001/approve").send({ draft_id: d.drafts[0]?.id, final_body: d.drafts[0]?.body });
    d = (await request(app).post("/emails/E002/process")).body as Detail;
    expect(d.kind_status.eligible).toBe(true);
    expect(d.auto_send?.allowed).toBe(false);
    expect(d.email.status).toBe("drafted");
  });

  it("learns style rules from edits and can roll back", async () => {
    const { app } = setup();
    for (const id of ["E001", "E002", "E003"]) {
      const d = (await request(app).post(`/emails/${id}/process`)).body as Detail;
      const body = `${d.drafts[0]?.body}\n\nP.S. Our live chat is open 24/7 if you need us.`;
      await request(app).post(`/emails/${id}/approve`).send({ draft_id: d.drafts[0]?.id, final_body: body });
    }
    const proposed = await request(app).post("/learning/rules/propose");
    expect(proposed.status).toBe(200);
    expect((proposed.body as { rules: string[] }).rules.join(" ")).toContain("our live chat is open 24/7 if you need us.");
    const v = (proposed.body as { version: number }).version;
    let learning = (await request(app).get("/learning")).body as { rules: { version: number; active: boolean }[] };
    expect(learning.rules.find((r) => r.version === v)?.active).toBe(true);
    await request(app).post("/learning/rules/0/activate");
    learning = (await request(app).get("/learning")).body as { rules: { version: number; active: boolean }[] };
    expect(learning.rules.find((r) => r.version === 0)?.active).toBe(true);
    expect((await request(app).post("/learning/rules/99/activate")).status).toBe(400);
  });
});
