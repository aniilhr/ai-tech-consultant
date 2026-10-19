import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev server proxies the same /api/* paths as the production gateway (server.mjs).
const target = (env: string | undefined, port: number) => env || `http://127.0.0.1:${port}`;
const strip = (prefix: string) => (path: string) => path.replace(new RegExp(`^${prefix}`), "");

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api/brain": { target: target(process.env.BRAIN_URL, 8001), changeOrigin: true, rewrite: strip("/api/brain") },
      "/api/inbox": { target: target(process.env.INBOX_URL, 8002), changeOrigin: true, rewrite: strip("/api/inbox") },
      "/api/scope": { target: target(process.env.SCOPE_URL, 8003), changeOrigin: true, rewrite: strip("/api/scope") },
    },
  },
});
