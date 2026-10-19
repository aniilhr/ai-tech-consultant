"""Scope Builder HTTP API (Task 3). Interactive docs at /docs."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .brain_client import BrainClient
from .config import Settings, get_settings
from .db import Database
from .llm import LLMClient
from .service import Conflict, NotFound, ScopeService

log = logging.getLogger("scope.api")


class ProjectIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    client: str = Field(min_length=2, max_length=200)


class TranscriptIn(BaseModel):
    title: str = Field(default="Meeting transcript", min_length=2, max_length=300)
    kind: Literal["initial", "follow_up"] = "initial"
    text: str | None = Field(default=None, max_length=400_000)
    sample_id: str | None = None


class AnalyzeIn(BaseModel):
    transcript_id: int


class ApprovalIn(BaseModel):
    approver: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=2000)


class ProposalEditIn(BaseModel):
    markdown: str = Field(min_length=20, max_length=200_000)
    editor: str = Field(default="reviewer", max_length=100)


class RejectIn(BaseModel):
    reviewer: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=3, max_length=2000)


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    s = settings or get_settings()
    db = Database(s.db_path)
    llm = LLMClient(s, transport=transport)
    brain = BrainClient(s.brain_url, s.brain_timeout_seconds, transport=transport)
    service = ScopeService(db, llm, brain, s)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        log.info("Scope Builder starting: db=%s brain=%s llm=%s", s.db_path, s.brain_url, llm.describe())
        yield

    app = FastAPI(
        title="Scope Builder API",
        version="1.0.0",
        lifespan=lifespan,
        description="Meeting transcripts -> scope of work, estimate and proposal. Calls the Knowledge Brain API.",
    )
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.state.service = service

    @app.exception_handler(NotFound)
    async def not_found(_: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(Conflict)
    async def conflict(_: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "service": "scope-builder", "llm": llm.describe(), "brain_reachable": await brain.health()}

    @app.get("/samples")
    def samples() -> list[dict[str, Any]]:
        return service.samples()

    @app.post("/demo/seed")
    def seed() -> dict[str, Any]:
        return {"project_ids": service.seed_demo()}

    @app.get("/projects")
    def projects() -> list[dict[str, Any]]:
        return service.list_projects()

    @app.post("/projects", status_code=201)
    def create_project(body: ProjectIn) -> dict[str, Any]:
        return service.get_project(service.create_project(body.name, body.client))

    @app.get("/projects/{project_id}")
    def project(project_id: int) -> dict[str, Any]:
        return service.get_project(project_id)

    @app.post("/projects/{project_id}/transcripts", status_code=201)
    def add_transcript(project_id: int, body: TranscriptIn) -> dict[str, Any]:
        if body.sample_id:
            tid = service.load_sample(project_id, body.sample_id)
        elif body.text and len(body.text.strip()) >= 50:
            tid = service.add_transcript(project_id, body.title, body.kind, body.text.strip())
        else:
            raise HTTPException(422, "Provide sample_id or a transcript text of at least 50 characters")
        return service.get_transcript(tid)

    @app.get("/transcripts/{transcript_id}")
    def transcript(transcript_id: int) -> dict[str, Any]:
        return service.get_transcript(transcript_id)

    @app.post("/projects/{project_id}/analyze")
    async def analyze(project_id: int, body: AnalyzeIn) -> dict[str, Any]:
        return await service.analyze(project_id, body.transcript_id)

    @app.get("/scopes/{scope_id}")
    def scope(scope_id: int) -> dict[str, Any]:
        return service.get_scope(scope_id)

    @app.post("/scopes/{scope_id}/approve")
    def approve_scope(scope_id: int, body: ApprovalIn) -> dict[str, Any]:
        return service.approve_scope(scope_id, body.approver, body.note)

    @app.post("/scopes/{scope_id}/proposal", status_code=201)
    async def proposal(scope_id: int) -> dict[str, Any]:
        return await service.generate_proposal(scope_id)

    @app.get("/proposals/{proposal_id}")
    def get_proposal(proposal_id: int) -> dict[str, Any]:
        return service.get_proposal(proposal_id)

    @app.put("/proposals/{proposal_id}")
    def edit_proposal(proposal_id: int, body: ProposalEditIn) -> dict[str, Any]:
        return service.update_proposal(proposal_id, body.markdown, body.editor)

    @app.post("/proposals/{proposal_id}/approve")
    def approve_proposal(proposal_id: int, body: ApprovalIn) -> dict[str, Any]:
        return service.approve_proposal(proposal_id, body.approver, body.note)

    @app.post("/proposals/{proposal_id}/reject")
    def reject_proposal(proposal_id: int, body: RejectIn) -> dict[str, Any]:
        return service.reject_proposal(proposal_id, body.reviewer, body.reason)

    return app
