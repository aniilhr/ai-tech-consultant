import express, { type NextFunction, type Request, type Response } from "express";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { z } from "zod";
import { BrainClient } from "./brainClient.js";
import type { Config } from "./config.js";
import { Db } from "./db.js";
import { LLMClient } from "./llm.js";
import { Conflict, InboxService, NotFound } from "./service.js";

export const SAMPLE_FILE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "data", "sample_emails.json");

const EmailBody = z.object({
  from_name: z.string().trim().min(1).max(120),
  from_email: z.email().max(200),
  subject: z.string().trim().min(1).max(300),
  body: z.string().trim().min(5).max(20000),
});
const ApproveBody = z.object({
  draft_id: z.number().int().positive(),
  final_body: z.string().trim().min(5).max(20000),
  reviewer: z.string().trim().min(1).max(100).default("reviewer"),
});
const RejectBody = z.object({
  draft_id: z.number().int().positive(),
  reason: z.string().trim().min(3).max(1000),
  reviewer: z.string().trim().min(1).max(100).default("reviewer"),
});
const OwnReplyBody = z.object({ body: z.string().trim().min(5).max(20000), reviewer: z.string().trim().min(1).max(100).default("reviewer") });
const FlagBody = z.object({ reason: z.string().trim().min(3).max(1000), reviewer: z.string().trim().min(1).max(100).default("reviewer") });

type Handler = (req: Request, res: Response) => Promise<unknown> | unknown;
const wrap = (fn: Handler) => async (req: Request, res: Response, next: NextFunction) => {
  try {
    const result = await fn(req, res);
    if (!res.headersSent) res.json(result);
  } catch (err) {
    next(err);
  }
};

export interface AppDeps {
  cfg: Config;
  db?: Db;
  fetchFn?: typeof fetch;
  llmBackoffMs?: number;
}

export function createApp({ cfg, db, fetchFn, llmBackoffMs }: AppDeps): { app: express.Express; service: InboxService } {
  const database = db ?? new Db(cfg.dbPath);
  const llm = new LLMClient(cfg, fetchFn ?? fetch, llmBackoffMs ?? 1000);
  const brain = new BrainClient(cfg.brainUrl, cfg.brainTimeoutMs, fetchFn ?? fetch);
  const service = new InboxService(database, llm, brain, cfg);

  const app = express();
  app.use(express.json({ limit: "1mb" }));
  app.use((req, res, next) => {
    if (process.env.NODE_ENV === "test") return next();
    const started = Date.now();
    res.on("finish", () => console.log(`[inbox] ${req.method} ${req.path} ${res.statusCode} ${Date.now() - started}ms`));
    next();
  });

  app.get("/health", wrap(async () => ({ status: "ok", service: "inbox-agent", llm: llm.describe(), brain_reachable: await brain.health() })));
  app.get("/stats", wrap(() => service.stats()));
  app.get(
    "/emails",
    wrap((req) =>
      service.listEmails({
        status: typeof req.query.status === "string" ? req.query.status : undefined,
        category: typeof req.query.category === "string" ? req.query.category : undefined,
        q: typeof req.query.q === "string" ? req.query.q : undefined,
      }),
    ),
  );
  app.post(
    "/emails",
    wrap((req, res) => {
      const id = service.createEmail(EmailBody.parse(req.body));
      res.status(201);
      return service.getEmail(id);
    }),
  );
  app.post("/emails/process-batch", wrap((req) => service.processBatch(z.object({ limit: z.number().int().min(1).max(60).default(10) }).parse(req.body ?? {}).limit)));
  app.get("/emails/:id", wrap((req) => service.getEmail(String(req.params.id))));
  app.post("/emails/:id/process", wrap((req) => service.process(String(req.params.id))));
  app.post("/emails/:id/approve", wrap((req) => service.approve(String(req.params.id), ApproveBody.parse(req.body))));
  app.post("/emails/:id/reject", wrap((req) => service.reject(String(req.params.id), RejectBody.parse(req.body))));
  app.post("/emails/:id/own-reply", wrap((req) => service.ownReply(String(req.params.id), OwnReplyBody.parse(req.body))));
  app.post("/emails/:id/flag", wrap((req) => service.flagAutoSent(String(req.params.id), FlagBody.parse(req.body))));

  app.get(
    "/learning",
    wrap(() => ({
      kinds: service.learning.kindTable(),
      rules: service.learning.versions(),
      edit_pairs_available: service.learning.editPairs(100).length,
      auto_send_enabled: cfg.autoSendEnabled,
      streak_required: cfg.autoSendStreakRequired,
    })),
  );
  app.post("/learning/rules/propose", wrap(() => service.learning.proposeRules()));
  app.post(
    "/learning/rules/:version/activate",
    wrap((req) => {
      service.learning.activate(Number(req.params.version));
      return { active: Number(req.params.version) };
    }),
  );
  app.post("/admin/reseed", wrap(() => ({ inserted: service.seedFromFile(SAMPLE_FILE, true) })));

  app.use((err: unknown, _req: Request, res: Response, _next: NextFunction) => {
    void _next;
    if (err instanceof z.ZodError) return res.status(422).json({ error: "validation_error", issues: err.issues });
    if (err instanceof NotFound) return res.status(404).json({ error: err.message });
    if (err instanceof Conflict) return res.status(409).json({ error: err.message });
    const message = err instanceof Error ? err.message : String(err);
    if (message.startsWith("Need at least") || message.startsWith("Unknown rules")) return res.status(400).json({ error: message });
    console.error("[inbox] unhandled error", err);
    return res.status(500).json({ error: "internal_error", message });
  });

  return { app, service };
}
