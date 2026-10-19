/** SQLite storage via Node's built-in node:sqlite (no native build step, deploys anywhere Node 22+ runs). */
import { mkdirSync } from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

const SCHEMA = `
CREATE TABLE IF NOT EXISTS emails (
  id TEXT PRIMARY KEY,
  from_name TEXT NOT NULL,
  from_email TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  received_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new',
  expected_json TEXT,
  is_sample INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS triage (
  email_id TEXT PRIMARY KEY REFERENCES emails(id) ON DELETE CASCADE,
  category TEXT NOT NULL,
  urgency TEXT NOT NULL,
  risk_flags TEXT NOT NULL,
  sentiment TEXT NOT NULL,
  summary TEXT NOT NULL,
  kb_question TEXT NOT NULL,
  confidence REAL NOT NULL,
  llm_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email_id TEXT NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  needs_human_json TEXT NOT NULL,
  policy_json TEXT NOT NULL,
  confidence REAL NOT NULL,
  llm_json TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  rules_version INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email_id TEXT NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
  draft_id INTEGER,
  kind TEXT NOT NULL,
  action TEXT NOT NULL,
  final_body TEXT,
  unchanged INTEGER NOT NULL DEFAULT 0,
  edit_ratio REAL,
  reviewer TEXT,
  reason TEXT,
  rules_version INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_kind ON reviews(kind, id);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email_id TEXT NOT NULL,
  draft_id INTEGER,
  to_email TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  sent_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS style_rules (
  version INTEGER PRIMARY KEY AUTOINCREMENT,
  rules_json TEXT NOT NULL,
  source TEXT NOT NULL,
  note TEXT,
  active INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
`;

export type Row = Record<string, unknown>;
type Param = string | number | null;

export class Db {
  readonly raw: DatabaseSync;

  constructor(file: string) {
    if (file !== ":memory:") mkdirSync(path.dirname(file), { recursive: true });
    this.raw = new DatabaseSync(file);
    this.raw.exec("PRAGMA foreign_keys = ON; PRAGMA journal_mode = WAL;");
    this.raw.exec(SCHEMA);
  }

  all<T = Row>(sql: string, ...params: Param[]): T[] {
    return this.raw.prepare(sql).all(...params) as T[];
  }

  get<T = Row>(sql: string, ...params: Param[]): T | undefined {
    return this.raw.prepare(sql).get(...params) as T | undefined;
  }

  run(sql: string, ...params: Param[]): number {
    return Number(this.raw.prepare(sql).run(...params).lastInsertRowid);
  }

  tx<T>(fn: () => T): T {
    this.raw.exec("BEGIN");
    try {
      const result = fn();
      this.raw.exec("COMMIT");
      return result;
    } catch (err) {
      this.raw.exec("ROLLBACK");
      throw err;
    }
  }
}

export const nowIso = (): string => new Date().toISOString();
