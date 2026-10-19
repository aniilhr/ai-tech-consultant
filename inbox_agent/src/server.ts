import { SAMPLE_FILE, createApp } from "./app.js";
import { loadConfig, loadDotEnv } from "./config.js";

loadDotEnv();
const cfg = loadConfig();
const { app, service } = createApp({ cfg });
const seeded = service.seedFromFile(SAMPLE_FILE);
if (seeded) console.log(`[inbox] seeded ${seeded} sample emails`);

app.listen(cfg.port, "0.0.0.0", () => {
  console.log(`[inbox] Inbox Agent listening on :${cfg.port} (brain=${cfg.brainUrl}, auto-send=${cfg.autoSendEnabled})`);
});
