import { loadConfig, type Config } from "../src/config.js";

export interface FakeState {
  brainCalls: { question: string; source: string; create_escalation: boolean }[];
  brainDown: boolean;
  brainAnswered: boolean;
  geminiMode: "ok" | "invalid_key";
  geminiCalls: string[];
  writerBody: string;
}

export function newState(): FakeState {
  return {
    brainCalls: [],
    brainDown: false,
    brainAnswered: true,
    geminiMode: "ok",
    geminiCalls: [],
    writerBody: "Hi Maria,\n\nGo to Members > Membership Plans and click Add New Plan.\n\nBest regards,\nSupport",
  };
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

/** A fetch double that plays both the Knowledge Brain and the Gemini API. */
export function fakeFetch(state: FakeState): typeof fetch {
  return (async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input instanceof Request ? input.url : input);
    if (url.startsWith("http://brain.test")) {
      if (state.brainDown) throw new TypeError("fetch failed: ECONNREFUSED");
      if (url.endsWith("/health")) return json({ status: "ok" });
      const body = JSON.parse(String(init?.body)) as { question: string; source: string; create_escalation: boolean };
      state.brainCalls.push(body);
      return json(
        state.brainAnswered
          ? {
              answered: true,
              answer: "Go to Members > Membership Plans and click Add New Plan.",
              confidence: 0.8,
              citations: [{ title: "How to Add a New Membership Plan", url: "https://help.test/101", source: "helpcenter", snippet: "Members > Membership Plans" }],
              images: [],
              escalation_id: null,
              llm: { mode: "live" },
            }
          : {
              answered: false,
              answer: "I couldn't find a reliable answer.",
              confidence: 0.1,
              citations: [],
              images: [],
              escalation_id: body.create_escalation ? 42 : null,
              llm: { mode: "live" },
            },
      );
    }
    if (url.startsWith("https://generativelanguage.googleapis.com")) {
      state.geminiCalls.push(url);
      if (state.geminiMode === "invalid_key")
        return json({ error: { code: 400, message: "API key not valid. Please pass a valid API key." } }, 400);
      const req = JSON.parse(String(init?.body)) as { systemInstruction: { parts: { text: string }[] } };
      const system = req.systemInstruction.parts[0]?.text ?? "";
      const payload = system.includes("You triage")
        ? {
            category: "how_to",
            urgency: "normal",
            risk_flags: [],
            sentiment: "neutral",
            summary: "Wants to add a plan",
            kb_question: "How do I add a membership plan?",
            confidence: 0.9,
          }
        : { subject: "Re: test", body: state.writerBody, used_citation_urls: ["https://help.test/101"], needs_human_reasons: [], confidence: 0.85 };
      return json({ candidates: [{ content: { parts: [{ text: JSON.stringify(payload) }] } }], usageMetadata: { promptTokenCount: 300, candidatesTokenCount: 60 } });
    }
    throw new Error(`unexpected URL in test: ${url}`);
  }) as typeof fetch;
}

export function testConfig(overrides: Partial<Config> = {}): Config {
  return {
    ...loadConfig({}),
    dbPath: ":memory:",
    brainUrl: "http://brain.test",
    brainTimeoutMs: 2000,
    llmProvider: "auto",
    geminiApiKey: "",
    anthropicApiKey: "",
    ...overrides,
  };
}
