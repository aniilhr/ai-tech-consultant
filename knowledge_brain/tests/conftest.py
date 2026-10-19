"""Test doubles: a fake help center (served from fixtures/site) and a fake Gemini API, both behind an
httpx.MockTransport, so tests never touch the network."""

from __future__ import annotations

import hashlib
import json
import re
import struct
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from brain.config import Settings
from brain.db import Database
from brain.ingest import Ingestor
from brain.llm import LLMClient
from brain.service import KnowledgeService

FIXTURES = Path(__file__).parent / "fixtures" / "site"
BASE = "https://help.example.test"


def tiny_png() -> bytes:
    """A valid 1x1 red PNG."""
    scanline = b"\x00\xff\x00\x00"  # filter byte + one RGB pixel
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(scanline)) + chunk(b"IEND", b"")


class FakeSite:
    def __init__(self) -> None:
        self.overrides: dict[str, tuple[int, str]] = {}
        self.excluded: set[str] = set()
        self.lastmod_bump: dict[str, str] = {}
        self.requests: list[str] = []
        self.fail_once: dict[str, int] = {}

    def sitemap(self) -> str:
        text = (FIXTURES / "sitemap-articles.xml").read_text().replace("{base}", BASE)
        for slug in self.excluded:
            text = re.sub(rf"\s*<url><loc>[^<]*{slug}</loc>.*?</url>", "", text)
        for slug, lastmod in self.lastmod_bump.items():
            text = re.sub(rf"({slug}</loc><lastmod>)[^<]*", rf"\g<1>{lastmod}", text)
        return text

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        if path in self.fail_once and self.fail_once[path] > 0:
            self.fail_once[path] -= 1
            return httpx.Response(503, text="busy")
        if path in self.overrides:
            code, body = self.overrides[path]
            return httpx.Response(code, text=body)
        if path == "/support/sitemap.xml":
            return httpx.Response(200, text=(FIXTURES / "sitemap_index.xml").read_text().replace("{base}", BASE))
        if path == "/support/sitemap-articles.xml":
            return httpx.Response(200, text=self.sitemap())
        if path.startswith("/fixtures/img/"):
            return httpx.Response(200, content=tiny_png(), headers={"content-type": "image/png"})
        m = re.match(r"^/support/solutions/articles/(.+)$", path)
        if m and (FIXTURES / f"{m.group(1)}.html").exists():
            return httpx.Response(200, text=(FIXTURES / f"{m.group(1)}.html").read_text())
        return httpx.Response(404, text="not found")


def fake_vector(text: str, dim: int = 64) -> list[float]:
    vec = [0.0] * dim
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        h = int(hashlib.md5(token.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    return vec


class FakeGemini:
    """Answers generateContent with a scripted JSON payload and embeds with a hashed bag of words."""

    def __init__(self) -> None:
        self.mode = "ok"  # ok | invalid_key | server_error
        self.responder: Callable[[dict[str, Any]], dict[str, Any]] = lambda body: {
            "answerable": True,
            "answer": "Go to Members > Membership Plans and click Add New Plan.",
            "cited_evidence": [1],
            "missing_information": "",
        }
        self.calls: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if self.mode == "invalid_key":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": 400,
                        "message": "API key not valid. Please pass a valid API key.",
                        "status": "INVALID_ARGUMENT",
                    }
                },
            )
        if self.mode == "server_error":
            return httpx.Response(500, json={"error": {"message": "internal"}})
        body = json.loads(request.content)
        if path.endswith(":batchEmbedContents"):
            return httpx.Response(
                200, json={"embeddings": [{"values": fake_vector(r["content"]["parts"][0]["text"])} for r in body["requests"]]}
            )
        if path.endswith(":generateContent"):
            payload = self.responder(body)
            return httpx.Response(
                200,
                json={
                    "candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}, "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 500, "candidatesTokenCount": 80},
                },
            )
        return httpx.Response(404)


@pytest.fixture
def site() -> FakeSite:
    return FakeSite()


@pytest.fixture
def gemini() -> FakeGemini:
    return FakeGemini()


@pytest.fixture
def transport(site: FakeSite, gemini: FakeGemini) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "generativelanguage.googleapis.com":
            return gemini.handle(request)
        if request.url.host == "help.example.test":
            return site.handle(request)
        return httpx.Response(599, text="unexpected host in test")

    return httpx.MockTransport(handler)


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "brain_db_path": tmp_path / "brain.db",
        "sitemap_url": f"{BASE}/support/sitemap.xml",
        "crawl_delay_seconds": 0.0,
        "crawl_concurrency": 2,
        "crawl_max_retries": 2,
        "gemini_api_key": "",
        "anthropic_api_key": "",
        "llm_provider": "auto",
        "gemini_embed_dim": 64,
        "dense_min_score": 0.3,
        "model_prices_json": json.dumps({"gemini-3.8-flash": [0.5, 3.0]}),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def make_service(tmp_path: Path, transport: httpx.MockTransport) -> Callable[..., KnowledgeService]:
    def factory(**overrides: Any) -> KnowledgeService:
        s = make_settings(tmp_path, **overrides)
        db = Database(s.db_path)
        llm = LLMClient(s, transport=transport, backoff_base=0.0)
        return KnowledgeService(db, llm, s, Ingestor(db, llm, s, transport=transport))

    return factory
