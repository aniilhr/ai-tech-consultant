/**
 * HTTP client for the Knowledge Brain (Task 1). The Inbox Agent has no retrieval logic of its own:
 * every product fact in a draft comes through POST /v1/ask.
 */
import type { BrainCitation, Evidence } from "./types.js";

type FetchFn = typeof fetch;

interface AskResponse {
  answered: boolean;
  answer: string;
  confidence: number;
  citations: BrainCitation[];
  images: { url: string; description: string | null; article_title: string }[];
  escalation_id: number | null;
  llm: { mode: string };
}

export class BrainClient {
  constructor(
    private readonly baseUrl: string,
    private readonly timeoutMs = 45000,
    private readonly fetchFn: FetchFn = fetch,
  ) {}

  async ask(question: string, opts: { createEscalation: boolean; context?: string }): Promise<Evidence> {
    let lastError = "";
    for (let attempt = 1; attempt <= 2; attempt++) {
      try {
        const resp = await this.fetchFn(`${this.baseUrl}/v1/ask`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            question: question.slice(0, 2000),
            source: "inbox",
            create_escalation: opts.createEscalation,
            context: opts.context?.slice(0, 4000),
          }),
          signal: AbortSignal.timeout(this.timeoutMs),
        });
        if (!resp.ok) {
          lastError = `Knowledge Brain HTTP ${resp.status}`;
          if (resp.status < 500) break;
          continue;
        }
        const data = (await resp.json()) as AskResponse;
        return {
          available: true,
          answered: data.answered,
          answer: data.answer,
          confidence: data.confidence,
          citations: data.citations ?? [],
          images: data.images ?? [],
          escalation_id: data.escalation_id,
          mode: data.llm?.mode ?? "unknown",
          question,
        };
      } catch (err) {
        lastError = `Knowledge Brain unreachable: ${String(err)}`;
      }
    }
    return {
      available: false,
      answered: false,
      answer: "",
      confidence: 0,
      citations: [],
      images: [],
      escalation_id: null,
      mode: "unavailable",
      error: lastError,
      question,
    };
  }

  async health(): Promise<boolean> {
    try {
      const resp = await this.fetchFn(`${this.baseUrl}/health`, { signal: AbortSignal.timeout(3000) });
      return resp.ok;
    } catch {
      return false;
    }
  }
}
