"""Knowledge Brain HTTP API (Task 1). Interactive docs at /docs; contract described in knowledge_brain/API.md."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .db import Database
from .ingest import Ingestor
from .llm import LLMClient, LLMError
from .service import KnowledgeService

log = logging.getLogger("brain.api")


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=12)
    create_escalation: bool = True
    source: Literal["chat", "inbox", "scope", "api"] = "api"
    context: str | None = Field(default=None, max_length=4000, description="Optional caller context stored on escalations")


class SearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=2000)
    top_k: int = Field(default=6, ge=1, le=20)


class DocumentRequest(BaseModel):
    title: str = Field(min_length=3, max_length=300)
    text: str = Field(min_length=10, max_length=50_000)
    category: str | None = None


class ExpertAnswer(BaseModel):
    answer: str = Field(min_length=5, max_length=20_000)
    expert_name: str = Field(min_length=1, max_length=100)


class SyncRequest(BaseModel):
    limit: int | None = Field(default=25, ge=1, le=5000, description="Max articles to fetch in this run (None = all)")
    describe_images: bool = True
    max_images: int | None = Field(default=50, ge=0, le=5000)


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    s = settings or get_settings()
    db = Database(s.db_path)
    llm = LLMClient(s, transport=transport)
    ingestor = Ingestor(db, llm, s, transport=transport)
    service = KnowledgeService(db, llm, s, ingestor)
    jobs: set[asyncio.Task[Any]] = set()

    def start_job(coro: Any) -> None:
        task = asyncio.create_task(coro)
        jobs.add(task)
        task.add_done_callback(jobs.discard)

    async def daily_sync_loop() -> None:
        while True:
            now = datetime.now(UTC)
            target = now.replace(hour=s.daily_sync_hour_utc, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            try:
                log.info("daily sync starting")
                await ingestor.sync(limit=s.daily_sync_batch_limit or None)
            except Exception:
                log.exception("daily sync failed; will retry tomorrow")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        log.info("Knowledge Brain starting: db=%s llm=%s", s.db_path, llm.describe())
        if s.daily_sync_enabled:
            start_job(daily_sync_loop())
        yield
        for t in list(jobs):
            t.cancel()

    app = FastAPI(
        title="Knowledge Brain API",
        version="1.0.0",
        description="Help-center memory for the AI Tech Consultant. Tasks 2 and 3 call /v1/ask and /v1/search.",
        lifespan=lifespan,
    )
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.state.service = service

    def admin(x_admin_token: str | None = Header(default=None)) -> None:
        if s.brain_admin_token and x_admin_token != s.brain_admin_token:
            raise HTTPException(401, "Missing or invalid X-Admin-Token")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "service": "knowledge-brain", "llm": llm.describe(), "kb_version": db.kb_version()}

    @app.post("/v1/ask")
    async def ask(req: AskRequest) -> dict[str, Any]:
        return await service.ask(
            req.question, top_k=req.top_k, create_escalation=req.create_escalation, source=req.source, context=req.context
        )

    @app.post("/v1/search")
    async def search(req: SearchRequest) -> dict[str, Any]:
        result, warnings = await service.search(req.query, req.top_k)
        return {
            "query": req.query,
            "mode": result.mode,
            "confidence": result.confidence,
            "sufficient": result.sufficient,
            "warnings": warnings,
            "results": [
                {
                    "chunk_id": h.chunk_id,
                    "article_id": h.article_id,
                    "title": h.title,
                    "url": h.url if h.source == "helpcenter" else None,
                    "source": h.source,
                    "heading": h.heading,
                    "text": h.text,
                    "is_screenshot": h.image_id is not None,
                    "score": h.score,
                    "dense": h.dense,
                    "coverage": h.coverage,
                }
                for h in result.hits
            ],
        }

    @app.get("/v1/stats")
    def stats() -> dict[str, Any]:
        return service.stats()

    @app.get("/v1/articles")
    def articles(
        status: str | None = None,
        source: str | None = None,
        q: str | None = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        where, params = [], []
        if status:
            where.append("a.status=?")
            params.append(status)
        if source:
            where.append("a.source=?")
            params.append(source)
        if q:
            where.append("(a.title LIKE ? OR a.url LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total_row = db.one(f"SELECT COUNT(*) n FROM articles a {clause}", tuple(params))
        rows = db.query(
            f"""SELECT a.id, a.url, a.source, a.title, a.category, a.folder, a.status, a.lastmod, a.fetched_at,
                       a.attempts, a.last_error,
                       (SELECT COUNT(*) FROM chunks c WHERE c.article_id=a.id) AS chunks,
                       (SELECT COUNT(*) FROM images i WHERE i.article_id=a.id) AS images
                FROM articles a {clause} ORDER BY a.updated_at DESC, a.id DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        )
        return {"total": total_row["n"] if total_row else 0, "items": [dict(r) for r in rows]}

    @app.get("/v1/articles/{article_id}")
    def article(article_id: int) -> dict[str, Any]:
        row = db.one("SELECT * FROM articles WHERE id=?", (article_id,))
        if not row:
            raise HTTPException(404, "Article not found")
        images = db.query("SELECT id, url, alt, section, description, status FROM images WHERE article_id=?", (article_id,))
        chunks = db.query("SELECT id, heading, text, embed_model FROM chunks WHERE article_id=? ORDER BY ord", (article_id,))
        return {"article": dict(row), "images": [dict(i) for i in images], "chunks": [dict(c) for c in chunks]}

    @app.post("/v1/documents", dependencies=[Depends(admin)])
    async def add_document(req: DocumentRequest) -> dict[str, Any]:
        article_id = await service.add_document(title=req.title, text=req.text, category=req.category)
        return {"article_id": article_id}

    @app.delete("/v1/documents/{article_id}", dependencies=[Depends(admin)])
    def delete_document(article_id: int) -> dict[str, Any]:
        if not service.delete_document(article_id):
            raise HTTPException(404, "Custom document not found (help-center articles are managed by sync)")
        return {"deleted": article_id}

    @app.get("/v1/escalations")
    def escalations(status: str | None = "open", limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
        if status:
            rows = db.query("SELECT * FROM escalations WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
        else:
            rows = db.query("SELECT * FROM escalations ORDER BY id DESC LIMIT ?", (limit,))
        return {"items": [dict(r) for r in rows]}

    @app.post("/v1/escalations/{escalation_id}/answer", dependencies=[Depends(admin)])
    async def answer_escalation(escalation_id: int, req: ExpertAnswer) -> dict[str, Any]:
        try:
            return await service.resolve_escalation(escalation_id, req.answer, req.expert_name)
        except KeyError:
            raise HTTPException(404, "Escalation not found") from None

    @app.post("/v1/escalations/{escalation_id}/dismiss", dependencies=[Depends(admin)])
    def dismiss_escalation(escalation_id: int) -> dict[str, Any]:
        service.dismiss_escalation(escalation_id)
        return {"escalation_id": escalation_id, "status": "dismissed"}

    @app.post("/v1/sync", status_code=202, dependencies=[Depends(admin)])
    async def sync(req: SyncRequest) -> dict[str, Any]:
        if ingestor.lock.locked():
            raise HTTPException(409, "A sync is already running")

        async def run() -> None:
            try:
                await ingestor.sync(limit=req.limit, describe_images=req.describe_images, max_images=req.max_images)
            except Exception:
                log.exception("sync job failed")

        start_job(run())
        await asyncio.sleep(0)
        return {"status": "started", "params": req.model_dump()}

    @app.get("/v1/sync/runs")
    def sync_runs(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
        rows = db.query("SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,))
        return {"running": ingestor.lock.locked(), "items": [dict(r) for r in rows]}

    @app.post("/v1/images/describe", dependencies=[Depends(admin)])
    async def describe(limit: int = Query(25, ge=1, le=1000)) -> dict[str, Any]:
        if ingestor.lock.locked():
            raise HTTPException(409, "A sync is already running")
        try:
            return await ingestor.describe_images(limit)
        except LLMError as exc:
            raise HTTPException(503, str(exc)) from None

    @app.post("/v1/embeddings/backfill", dependencies=[Depends(admin)])
    async def backfill() -> dict[str, Any]:
        warnings: list[str] = []
        done = await ingestor.embed_pending(warnings)
        return {"embedded": done, "warnings": warnings}

    return app
