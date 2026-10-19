import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

/** Load the repo-root .env without overriding variables that are already set (real env wins). */
export function loadDotEnv(file = path.join(REPO_ROOT, ".env")): void {
  if (!existsSync(file)) return;
  for (const line of readFileSync(file, "utf8").split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (!m || !m[1]) continue;
    const value = (m[2] ?? "").replace(/^["']|["']$/g, "");
    if (process.env[m[1]] === undefined) process.env[m[1]] = value;
  }
}

export interface Config {
  port: number;
  dbPath: string;
  brainUrl: string;
  brainTimeoutMs: number;
  llmProvider: string;
  geminiApiKey: string;
  geminiModelFast: string;
  geminiModelSmart: string;
  anthropicApiKey: string;
  anthropicModelFast: string;
  anthropicModelSmart: string;
  modelPrices: Record<string, [number, number]>;
  companyName: string;
  signature: string;
  autoSendEnabled: boolean;
  autoSendStreakRequired: number;
}

function parsePrices(raw: string | undefined): Record<string, [number, number]> {
  try {
    const parsed = JSON.parse(raw || "{}") as Record<string, [number, number]>;
    return typeof parsed === "object" && parsed ? parsed : {};
  } catch {
    return {};
  }
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const dataDir = path.resolve(REPO_ROOT, env.DATA_DIR || "data");
  return {
    port: Number(env.INBOX_PORT || 8002),
    dbPath: env.INBOX_DB_PATH || path.join(dataDir, "inbox.db"),
    brainUrl: (env.BRAIN_URL || "http://127.0.0.1:8001").replace(/\/$/, ""),
    brainTimeoutMs: Number(env.BRAIN_TIMEOUT_MS || 45000),
    llmProvider: (env.LLM_PROVIDER || "auto").toLowerCase(),
    geminiApiKey: env.GEMINI_API_KEY || "",
    geminiModelFast: env.GEMINI_MODEL_FAST || "gemini-3.5-flash-lite",
    geminiModelSmart: env.GEMINI_MODEL_SMART || "gemini-3.8-flash",
    anthropicApiKey: env.ANTHROPIC_API_KEY || "",
    anthropicModelFast: env.ANTHROPIC_MODEL_FAST || "claude-haiku-5-5",
    anthropicModelSmart: env.ANTHROPIC_MODEL_SMART || "claude-sonnet-5-5",
    modelPrices: parsePrices(env.MODEL_PRICES_JSON),
    companyName: env.COMPANY_NAME || "Brilliant Directories",
    signature: env.SUPPORT_SIGNATURE || "The Brilliant Directories Support Team",
    autoSendEnabled: (env.AUTO_SEND_ENABLED || "false").toLowerCase() === "true",
    autoSendStreakRequired: Math.max(1, Number(env.AUTO_SEND_STREAK_REQUIRED || 20)),
  };
}
