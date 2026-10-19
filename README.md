# AI Tech Consultant

One system that **knows the product**, **answers the inbox**, and **turns client meetings into a scope of work**, with a human approving everything that matters.

**Live demo:** _add your deployment link here_ → `https://ai-tech-consultant-a5rv.onrender.com/`
[click here](https://ai-tech-consultant-a5rv.onrender.com)

**DEMO LOGIN DETAILS :**

- username : admin
- password : Anil1234

Built for the BusinessLabs capstone, using the public [Brilliant Directories Solution Center](https://support.brilliantdirectories.com/support/sitemap.xml) as the knowledge source.

---

## What it does

| Module | Language | What you can do |
|---|---|---|
| **1. Knowledge Brain** | Python · FastAPI | Reads the help center (text + screenshots) and answers questions **with sources**. If the answer isn't there, it says so and creates a task for a human expert. The expert's answer becomes new knowledge. |
| **2. Inbox Agent** | TypeScript · Node | Reads customer emails, sorts them by type, urgency and risk, gets the facts from the Knowledge Brain, and writes a reply draft. A human approves, edits or rejects it. The agent learns from every edit. |
| **3. Scope Builder** | Python · FastAPI | Turns a meeting transcript into requirements, checks each one against the product (via the Knowledge Brain), estimates hours and writes a proposal. A follow-up meeting shows what changed and what must be quoted extra. |

All three are shown in **one web dashboard**.

## How it works

```
                 ┌──────────────────────── Web dashboard (one port: 8080) ────────────────────────┐
                 │  Knowledge Chat · Knowledge Base · Inbox Review · Learning & Trust · Scope       │
                 └───────────────┬──────────────────────────┬──────────────────────────┬──────────┘
                                 │ /api/brain               │ /api/inbox               │ /api/scope
                                 ▼                          ▼                          ▼
 Help center ──sitemap──► 1. Knowledge Brain ◄──/v1/ask── 2. Inbox Agent         3. Scope Builder
 (articles + images)       crawl · chunk · search  ◄────────────────────/v1/ask────────┘
                           answer with citations
                           expert escalations
```

- **Tasks 2 and 3 never search the help center themselves.** They ask the Knowledge Brain through its API (`POST /v1/ask`). See [`knowledge_brain/API.md`](knowledge_brain/API.md).
- **AI model:** Google Gemini by default (Claude optional). Model names are set in `.env`, not in the code.
- **No API key?** The app still runs in **Offline mode**: keyword search, quoted answers and template drafts, all clearly labelled "Offline mode" so nobody mistakes them for AI output.

### Safety rules built in

- Answers only count as answered when they **cite a help-center source**.
- Email drafts are **never sent automatically** by default (`AUTO_SEND_ENABLED=false`).
- An email type can only earn auto-send after **20 approvals in a row with no edits**. One edit, rejection or complaint resets it to zero.
- Refunds, cancellations, billing, complaints, account access and quotes **always need a human**.
- A code check blocks drafts that promise refunds, discounts, prices, deadlines or guarantees.
- Scope requirements must quote the transcript; quotes that can't be found are flagged.
- A requirement is only marked "supported by the product" if the Knowledge Brain found evidence.
- A proposal can only be marked **ready to send** after a human approves both the scope and the proposal.

---

## Install and run

### Option A: one command (Python 3.11+ and Node 22.13+)

```bash
git clone https://github.com/aniilhr/ai-tech-consultant.git
cd ai-tech-consultant
./scripts/start.sh
```

Open **http://localhost:8080**.

The first run creates `.env`, installs everything and builds the app. That takes a few minutes; later starts are fast.

### Option B: Docker

```bash
cp .env.example .env      # then add your API key (see below)
docker compose up --build
```

Open **http://localhost:8080**.

## Where to paste your API key

1. Open the file **`.env`** in the project folder. If it doesn't exist yet, copy `.env.example` to `.env`.
2. Find this line and paste your key after the `=` (no quotes, no spaces):

   ```env
   GEMINI_API_KEY=paste-your-gemini-key-here
   ```

3. Save the file and **restart** the app (`Ctrl+C`, then `./scripts/start.sh` again).

You can get a Gemini key at [Google AI Studio](https://aistudio.google.com/apikey).

> `.env` is in `.gitignore`. **Never commit it** and never paste keys into source files.

**On a hosting service** (Render, Railway, Fly…), don't upload `.env`. Add the same names as **environment variables** in the host's dashboard instead.

### Other settings (optional)

| Variable | What it does | Default |
|---|---|---|
| `GEMINI_MODEL_SMART` / `GEMINI_MODEL_FAST` | Models for answers/drafts and for sorting | `gemini-3.8-flash` / `gemini-3.5-flash-lite` |
| `GEMINI_EMBED_MODEL` | Model for semantic search | `gemini-embedding-001` |
| `LLM_PROVIDER` | `auto`, `gemini`, `anthropic` or `offline` | `auto` |
| `ANTHROPIC_API_KEY` | Use Claude instead (for model comparisons) | empty |
| `APP_BASIC_AUTH` | Password-protect the dashboard, e.g. `admin:secret` | empty |
| `BRAIN_ADMIN_TOKEN` | Protects sync and expert-answer actions | empty |
| `DAILY_SYNC_ENABLED` | Re-check the help center every day | `false` |
| `AUTO_SEND_ENABLED` | Allow trusted email types to send on their own | `false` |
| `MODEL_PRICES_JSON` | Prices per 1M tokens, for cost tracking | `{}` |

The full list with comments is in [`.env.example`](.env.example).

---

## First 5 minutes (demo path)

1. **Knowledge Base:** set *Articles this run* to `25` → **Run sync**. Run it again with a bigger number to continue; finished articles are skipped.
2. **Knowledge Chat:** ask *"How do I add a new membership plan?"* and look at the sources. Then ask something the help center doesn't cover; it goes to the **Expert queue**, where you can answer it.
3. **Inbox Review:** pick an email → **Generate draft** → edit → **Approve**. Try the "Chargeback incoming" email to see the risk flags and the "Always human" rule.
4. **Learning & Trust:** see the 20-in-a-row progress per email type; click **Learn from edits**.
5. **Scope Builder:** **Load demo projects** → *Analyse* the kickoff → **Approve as baseline** → *Compare with baseline* on the follow-up → **Proposal** → approve.

## Deploy

The app ships as **one Docker image** with one public port (`$PORT`, default 8080) and needs a **persistent disk** at `/data` for its databases.

**Render (blueprint included):**

1. Push this repo to GitHub.
2. In Render: **New → Blueprint** → choose this repo. It reads `render.yaml`.
3. Set `GEMINI_API_KEY` and `APP_BASIC_AUTH` when asked.
4. Once it's live, open the dashboard → **Knowledge Base → Run sync**.
5. Paste your link at the top of this README.

Any other host that runs a Dockerfile with a persistent volume works the same way: set the environment variables and mount `/data`.

## Tests

```bash
cd knowledge_brain && ../.venv/bin/python -m pytest -q     # 27 tests
cd scope_builder   && ../.venv/bin/python -m pytest -q     # 16 tests
cd inbox_agent     && npm test                              # 35 tests
```

Evaluation scripts are in each module's `evals/` folder.

## Project layout

```
knowledge_brain/   Task 1: crawler, search, answers, escalations (Python)
inbox_agent/       Task 2: email triage, drafts, review, learning (TypeScript)
scope_builder/     Task 3: transcripts → scope, estimate, proposal (Python)
web/               Dashboard (React) + gateway server
scripts/           start.sh (one command) and run-services.sh
Dockerfile, docker-compose.yml, render.yaml, .env.example
```

## Known limits

- The first full crawl and screenshot analysis happen on your machine or server. They haven't been run against the live help center yet.
- Offline mode (no API key) is much weaker than Gemini. Use it for setup and testing only.
- Emails are not really sent. Approved replies go to an **outbox** inside the app, ready to connect to an email provider.
- Sample emails and meeting transcripts were written for the demo. Replace the transcripts with your own recorded role-play meetings.
