# Knowledge Brain API (Task 1)

Base URL: `BRAIN_URL` (local default `http://127.0.0.1:8001`; through the web gateway: `/api/brain`).
Interactive OpenAPI docs: `GET /docs`. All bodies are JSON. Endpoints marked **admin** require the
`X-Admin-Token` header when `BRAIN_ADMIN_TOKEN` is set.

## Ask a question: `POST /v1/ask`

The main endpoint. It retrieves evidence, writes a grounded answer, and opens an escalation when the
evidence isn't enough.

```json
{ "question": "How do I add a membership plan?", "top_k": 6, "create_escalation": true,
  "source": "inbox", "context": "optional text stored on the escalation" }
```

| field | type | notes |
|---|---|---|
| question | string, 3–2000 chars | required |
| top_k | int 1–12 | evidence passages to consider (default 6) |
| create_escalation | bool | open a human task if not answerable (default true) |
| source | `chat` \| `inbox` \| `scope` \| `api` | who is asking (stored for analytics) |

Response:

```json
{
  "question": "...",
  "answered": true,
  "answer": "Go to Members > Membership Plans ...",
  "confidence": 0.71,
  "retrieval_mode": "hybrid",
  "citations": [{"article_id": 12, "title": "...", "url": "https://...", "source": "helpcenter",
                 "heading": "...", "snippet": "...", "score": 0.032, "dense": 0.71, "coverage": 0.8}],
  "related": [],
  "images": [{"url": "https://...png", "description": "The Membership Plans screen ...", "analysed": true,
              "article_title": "...", "article_url": "...", "directly_cited": true}],
  "escalation_id": null,
  "warnings": [],
  "llm": {"mode": "live", "provider": "gemini", "model": "gemini-3.8-flash", "prompt_version": "answer-v1",
          "input_tokens": 1450, "output_tokens": 160, "cost_usd": 0.0012},
  "latency_ms": 2100
}
```

* `answered=false` means the help center does not support an answer. `citations` is then empty,
  `related` lists the closest passages, and `escalation_id` is set if `create_escalation` was true.
  Identical open questions are de-duplicated: they reuse the existing escalation and increment `times_asked`.
* `llm.mode` is `live` (a model wrote the answer), `offline` (no API key: an extractive answer that
  quotes the help center), or `fallback` (the model call failed; see `warnings`). Callers can show this
  to users so nobody mistakes a quote for an AI answer.
* Answers are only `answered=true` when the model cites at least one evidence passage (grounding rule).

## Raw retrieval: `POST /v1/search`

`{"query": "stripe gateway", "top_k": 6}` returns ranked chunks (`title`, `url`, `heading`, `text`,
`is_screenshot`, `score`, `dense`, `coverage`) and `confidence`/`sufficient`. Use this when you want the
evidence without a generated answer.

## Knowledge management

| method | path | purpose |
|---|---|---|
| GET | `/v1/stats` | counts of articles by status, chunks, embeddings, images, escalations, query stats, last sync |
| GET | `/v1/articles?status=&source=&q=&limit=&offset=` | browse the memory |
| GET | `/v1/articles/{id}` | one article with chunks and images |
| POST **admin** | `/v1/documents` `{title, text, category?}` | add a custom document (any business can plug in its own knowledge) |
| DELETE **admin** | `/v1/documents/{id}` | remove a custom document |

## Escalations (human expert loop)

| method | path | purpose |
|---|---|---|
| GET | `/v1/escalations?status=open\|answered\|dismissed` | the expert queue |
| POST **admin** | `/v1/escalations/{id}/answer` `{answer, expert_name}` | expert answers; the answer is stored as a new `expert` knowledge article and is used for future questions |
| POST **admin** | `/v1/escalations/{id}/dismiss` | close without answering |

## Ingestion

| method | path | purpose |
|---|---|---|
| POST **admin** | `/v1/sync` `{limit: 25, describe_images: true, max_images: 50}` | start an incremental sitemap sync in the background (202). 409 if one is already running |
| GET | `/v1/sync/runs` | history with stats: new, changed, removed, failed, remaining, images described |
| POST **admin** | `/v1/images/describe?limit=25` | describe pending screenshots with the vision model |
| POST **admin** | `/v1/embeddings/backfill` | embed chunks that have no embedding (e.g. after adding a key) |

The CLI does the same jobs without the server: `python -m brain.cli sync --limit 25`.

## Errors

* `422` invalid input (FastAPI validation body)
* `401` missing or invalid admin token
* `409` a sync is already running
* `503` image description requested with no vision-capable key

LLM failures never surface as 5xx on `/v1/ask`. The Brain falls back to an extractive answer and
reports the problem in `warnings` and `llm.mode = "fallback"`.
