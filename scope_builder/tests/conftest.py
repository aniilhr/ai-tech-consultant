from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from scope.brain_client import BrainClient
from scope.config import Settings
from scope.db import Database
from scope.llm import LLMClient
from scope.service import ScopeService

SUPPORTED_WORDS = ("membership", "review", "import", "seo", "stripe", "domain", "listing", "level")


class FakeBrain:
    def __init__(self) -> None:
        self.questions: list[dict[str, Any]] = []
        self.down = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused")
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        body = json.loads(request.content)
        self.questions.append(body)
        q = body["question"].lower()
        ok = any(w in q for w in SUPPORTED_WORDS)
        return httpx.Response(
            200,
            json={
                "answered": ok,
                "answer": "Use Members > Membership Plans." if ok else "I couldn't find a reliable answer.",
                "confidence": 0.8 if ok else 0.1,
                "citations": [{"title": "Membership Plans", "url": "https://help.test/1", "snippet": "Add New Plan"}]
                if ok
                else [],
            },
        )


class FakeGemini:
    """Routes by system prompt to a scripted responder per agent."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.responders: dict[str, Callable[[str], dict[str, Any]]] = {}
        self.invalid_key = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.invalid_key:
            return httpx.Response(400, json={"error": {"message": "API key not valid. Please pass a valid API key."}})
        body = json.loads(request.content)
        system = body["systemInstruction"]["parts"][0]["text"]
        prompt = body["contents"][0]["parts"][0]["text"]
        agent = next((k for k in self.responders if k in system), None)
        self.calls.append(agent or "unknown")
        if agent is None:
            return httpx.Response(500, json={"error": "no responder"})
        payload = self.responders[agent](prompt)
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}],
                "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 200},
            },
        )


@pytest.fixture
def brain() -> FakeBrain:
    return FakeBrain()


@pytest.fixture
def gemini() -> FakeGemini:
    return FakeGemini()


@pytest.fixture
def transport(brain: FakeBrain, gemini: FakeGemini) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "generativelanguage.googleapis.com":
            return gemini.handle(request)
        if request.url.host == "brain.test":
            return brain.handle(request)
        return httpx.Response(599)

    return httpx.MockTransport(handler)


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "scope_db_path": ":memory:",
        "brain_url": "http://brain.test",
        "gemini_api_key": "",
        "anthropic_api_key": "",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def make_service(transport: httpx.MockTransport) -> Callable[..., ScopeService]:
    def factory(**overrides: Any) -> ScopeService:
        s = settings(**overrides)
        return ScopeService(
            Database(":memory:"),
            LLMClient(s, transport=transport, backoff_base=0.0),
            BrainClient(s.brain_url, transport=transport),
            s,
        )

    return factory
