// @ts-check
/**
 * Production gateway: serves the built dashboard and proxies /api/{brain,inbox,scope} to the three services,
 * so a deployment exposes a single public port. Optional APP_BASIC_AUTH="user:password" protects everything.
 * BRAIN_ADMIN_TOKEN (if set) is added server-side to Brain requests, so the token never reaches the browser.
 */
import express from "express";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createProxyMiddleware } from "http-proxy-middleware";

const here = path.dirname(fileURLToPath(import.meta.url));
const envFile = path.resolve(here, "..", ".env");
if (existsSync(envFile)) {
  for (const line of readFileSync(envFile, "utf8").split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (m && m[1] && process.env[m[1]] === undefined) process.env[m[1]] = (m[2] ?? "").replace(/^["']|["']$/g, "");
  }
}

const PORT = Number(process.env.PORT || 8080);
const services = {
  brain: process.env.BRAIN_URL || "http://127.0.0.1:8001",
  inbox: process.env.INBOX_URL || "http://127.0.0.1:8002",
  scope: process.env.SCOPE_URL || "http://127.0.0.1:8003",
};
const basicAuth = process.env.APP_BASIC_AUTH || "";
const adminToken = process.env.BRAIN_ADMIN_TOKEN || "";

const app = express();
app.disable("x-powered-by");

if (basicAuth) {
  const expected = "Basic " + Buffer.from(basicAuth).toString("base64");
  app.use((req, res, next) => {
    if (req.path === "/healthz" || req.headers.authorization === expected) return next();
    res.set("WWW-Authenticate", 'Basic realm="AI Tech Consultant"').status(401).send("Authentication required");
  });
}

app.get("/healthz", (_req, res) => res.json({ status: "ok", services }));

for (const [name, target] of Object.entries(services)) {
  app.use(
    `/api/${name}`,
    createProxyMiddleware({
      target,
      changeOrigin: true,
      proxyTimeout: 300_000,
      timeout: 300_000,
      on: {
        proxyReq: (proxyReq) => {
          if (name === "brain" && adminToken) proxyReq.setHeader("X-Admin-Token", adminToken);
        },
        error: (err, _req, res) => {
          const r = /** @type {import("node:http").ServerResponse} */ (res);
          if (!r.headersSent) r.writeHead(502, { "content-type": "application/json" });
          r.end(JSON.stringify({ error: `${name} service unavailable`, detail: String(err.message) }));
        },
      },
    }),
  );
}

const dist = path.join(here, "dist");
app.use(express.static(dist, { maxAge: "1h", index: false }));
app.get(/.*/, (_req, res) => {
  if (!existsSync(path.join(dist, "index.html"))) return res.status(503).send("Web UI not built. Run `npm run build` in web/.");
  res.sendFile(path.join(dist, "index.html"));
});

app.listen(PORT, "0.0.0.0", () => console.log(`[web] dashboard + gateway on http://localhost:${PORT} ->`, services));
