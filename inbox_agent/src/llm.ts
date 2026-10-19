/**
 * Minimal LLM client over the Gemini REST API (primary) and Anthropic Messages API (optional).
 * Same request shapes and behaviour as the Python services: tiered models from env, JSON-schema
 * structured output re-validated with zod, retries with backoff, and one corrective retry on bad JSON.
 */
import { z } from "zod";
import type { Config } from "./config.js";

export type Tier = "fast" | "smart";
export type Provider = "gemini" | "anthropic" | "offline";

export class LLMError extends Error {
  constructor(
    public readonly kind: "auth" | "config" | "rate_limit" | "server" | "network" | "bad_output" | "disabled",
    message: string,
    public readonly status?: number,
  ) {
    super(message);
  }
}

export interface LLMCall {
  provider: Provider;
  model: string;
  input_tokens: number;
  output_tokens: number;
  latency_ms: number;
  cost_usd: number | null;
}

type Json = Record<string, unknown>;
type FetchFn = typeof fetch;

const GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta";
const ANTHROPIC_URL = "https://api.anthropic.com/v1/messages";

export class LLMClient {
  constructor(
    private readonly cfg: Config,
    private readonly fetchFn: FetchFn = fetch,
    private readonly backoffMs = 1000,
  ) {}

  get provider(): Provider {
    const p = this.cfg.llmProvider;
    if (p === "auto") {
      if (this.cfg.geminiApiKey) return "gemini";
      if (this.cfg.anthropicApiKey) return "anthropic";
      return "offline";
    }
    if (p === "gemini" && this.cfg.geminiApiKey) return "gemini";
    if (p === "anthropic" && this.cfg.anthropicApiKey) return "anthropic";
    return "offline";
  }

  get enabled(): boolean {
    return this.provider !== "offline";
  }

  modelFor(tier: Tier): string {
    if (this.provider === "anthropic") return tier === "fast" ? this.cfg.anthropicModelFast : this.cfg.anthropicModelSmart;
    return tier === "fast" ? this.cfg.geminiModelFast : this.cfg.geminiModelSmart;
  }

  describe(): Json {
    return {
      provider: this.provider,
      models: this.enabled ? { fast: this.modelFor("fast"), smart: this.modelFor("smart") } : {},
    };
  }

  async generateJson<T>(opts: {
    tier: Tier;
    system: string;
    prompt: string;
    schema: z.ZodType<T>;
    temperature?: number;
  }): Promise<{ data: T; call: LLMCall }> {
    if (!this.enabled) throw new LLMError("disabled", "No LLM provider configured (offline mode).");
    const jsonSchema = z.toJSONSchema(opts.schema) as Json;
    const model = this.modelFor(opts.tier);
    const started = Date.now();
    let prompt = opts.prompt;
    let tokensIn = 0;
    let tokensOut = 0;
    let lastErr = "";
    for (let attempt = 0; attempt < 2; attempt++) {
      const res =
        this.provider === "anthropic"
          ? await this.anthropic(model, opts.system, prompt, jsonSchema, opts.temperature ?? 0.2)
          : await this.gemini(model, opts.system, prompt, jsonSchema, opts.temperature ?? 0.2);
      tokensIn += res.tokensIn;
      tokensOut += res.tokensOut;
      try {
        const raw = typeof res.output === "string" ? parseJson(res.output) : res.output;
        const data = opts.schema.parse(raw);
        return {
          data,
          call: {
            provider: this.provider,
            model,
            input_tokens: tokensIn,
            output_tokens: tokensOut,
            latency_ms: Date.now() - started,
            cost_usd: this.cost(model, tokensIn, tokensOut),
          },
        };
      } catch (err) {
        lastErr = String(err).slice(0, 400);
        prompt = `${opts.prompt}\n\nYour previous reply was not valid JSON for the schema (${lastErr}). Reply again with ONLY valid JSON.`;
      }
    }
    throw new LLMError("bad_output", `Model output failed schema validation: ${lastErr}`);
  }

  private cost(model: string, inp: number, out: number): number | null {
    const price = this.cfg.modelPrices[model];
    if (!price) return null;
    return Math.round(((inp / 1e6) * price[0] + (out / 1e6) * price[1]) * 1e6) / 1e6;
  }

  private async gemini(model: string, system: string, prompt: string, schema: Json, temperature: number) {
    const body = {
      systemInstruction: { parts: [{ text: system }] },
      contents: [{ role: "user", parts: [{ text: prompt }] }],
      generationConfig: { temperature, responseMimeType: "application/json", responseSchema: toGeminiSchema(schema) },
    };
    const data = await this.post(`${GEMINI_BASE}/models/${model}:generateContent`, body, {
      "x-goog-api-key": this.cfg.geminiApiKey,
    });
    const candidates = (data.candidates as Json[] | undefined) ?? [];
    const first = candidates[0];
    if (!first) throw new LLMError("bad_output", "Gemini returned no candidates");
    const parts = ((first.content as Json | undefined)?.parts as Json[] | undefined) ?? [];
    const text = parts
      .filter((p) => !p.thought)
      .map((p) => String(p.text ?? ""))
      .join("");
    if (!text) throw new LLMError("bad_output", `Gemini returned empty content (${String(first.finishReason)})`);
    const usage = (data.usageMetadata as Json | undefined) ?? {};
    return {
      output: text as string | Json,
      tokensIn: Number(usage.promptTokenCount ?? 0),
      tokensOut: Number(usage.candidatesTokenCount ?? 0) + Number(usage.thoughtsTokenCount ?? 0),
    };
  }

  private async anthropic(model: string, system: string, prompt: string, schema: Json, temperature: number) {
    const body = {
      model,
      max_tokens: 2048,
      temperature,
      system,
      messages: [{ role: "user", content: prompt }],
      tools: [{ name: "emit", description: "Return the structured result.", input_schema: stripMeta(schema) }],
      tool_choice: { type: "tool", name: "emit" },
    };
    const data = await this.post(ANTHROPIC_URL, body, {
      "x-api-key": this.cfg.anthropicApiKey,
      "anthropic-version": "2023-06-01",
    });
    const block = ((data.content as Json[] | undefined) ?? []).find((b) => b.type === "tool_use");
    if (!block) throw new LLMError("bad_output", "Anthropic response had no tool_use block");
    const usage = (data.usage as Json | undefined) ?? {};
    return {
      output: (block.input ?? {}) as string | Json,
      tokensIn: Number(usage.input_tokens ?? 0),
      tokensOut: Number(usage.output_tokens ?? 0),
    };
  }

  private async post(url: string, body: Json, headers: Record<string, string>, maxAttempts = 3): Promise<Json> {
    let last: LLMError | undefined;
    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      try {
        const resp = await this.fetchFn(url, {
          method: "POST",
          headers: { "content-type": "application/json", ...headers },
          body: JSON.stringify(body),
          signal: AbortSignal.timeout(60_000),
        });
        if (resp.ok) return (await resp.json()) as Json;
        last = classifyHttpError(resp.status, await resp.text());
        if (last.kind === "auth" || last.kind === "config") throw last;
      } catch (err) {
        if (err instanceof LLMError && (err.kind === "auth" || err.kind === "config")) throw err;
        if (!(err instanceof LLMError)) last = new LLMError("network", String(err));
      }
      if (attempt < maxAttempts) await sleep(this.backoffMs * 2 ** (attempt - 1));
    }
    throw last ?? new LLMError("network", "request failed");
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

export function classifyHttpError(status: number, text: string): LLMError {
  if (status === 401 || status === 403 || text.includes("API_KEY_INVALID") || text.includes("API key not valid"))
    return new LLMError("auth", `Provider rejected the API key (HTTP ${status}). Check your API key.`, status);
  if (status === 404) return new LLMError("config", `Model or endpoint not found (HTTP 404). Check model names.`, status);
  if (status === 429) return new LLMError("rate_limit", "Provider rate limit hit (HTTP 429).", status);
  if (status >= 500) return new LLMError("server", `Provider server error (HTTP ${status}).`, status);
  return new LLMError("config", `Provider rejected the request (HTTP ${status}): ${text.slice(0, 300)}`, status);
}

export function parseJson(raw: string): unknown {
  const fenced = raw.trim().match(/^```(?:json)?\s*([\s\S]*?)\s*```$/);
  return JSON.parse(fenced?.[1] ?? raw.trim());
}

function stripMeta(node: unknown): unknown {
  if (Array.isArray(node)) return node.map(stripMeta);
  if (node && typeof node === "object") {
    const out: Json = {};
    for (const [k, v] of Object.entries(node as Json)) {
      if (k === "$schema" || k === "title") continue;
      out[k] = stripMeta(v);
    }
    return out;
  }
  return node;
}

const GEMINI_KEYS = new Set(["type", "properties", "required", "items", "enum", "description", "nullable", "minimum", "maximum"]);

/** Convert a JSON schema to the OpenAPI subset Gemini accepts as responseSchema. */
export function toGeminiSchema(node: unknown): unknown {
  if (Array.isArray(node)) return node.map(toGeminiSchema);
  if (!node || typeof node !== "object") return node;
  const src = node as Json;
  if (Array.isArray(src.anyOf)) {
    const options = (src.anyOf as Json[]).filter((o) => o.type !== "null");
    const converted = (options[0] ? toGeminiSchema(options[0]) : { type: "STRING" }) as Json;
    if (options.length < (src.anyOf as Json[]).length) converted.nullable = true;
    return converted;
  }
  const out: Json = {};
  for (const [k, v] of Object.entries(src)) {
    if (!GEMINI_KEYS.has(k)) continue;
    if (k === "type") out.type = String(v).toUpperCase();
    else if (k === "properties")
      out.properties = Object.fromEntries(Object.entries(v as Json).map(([pk, pv]) => [pk, toGeminiSchema(pv)]));
    else if (k === "items") out.items = toGeminiSchema(v);
    else out[k] = v;
  }
  if (out.enum && !out.type) out.type = "STRING";
  return out;
}
