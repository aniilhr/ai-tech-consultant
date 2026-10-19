"""Ingestion: sitemap -> diff -> polite fetch -> parse -> chunk -> embed -> describe images.

Design choices (each answers a brief requirement):
* Polite first read: a global rate limiter (CRAWL_DELAY_SECONDS between requests) plus a small concurrency
  cap; retries with exponential backoff; HTTP 429 honours Retry-After.
* Stops halfway safely: every article is committed on its own. Unprocessed URLs stay `pending` and
  failed ones stay `failed` with an attempt counter, so the next run simply continues.
* Daily freshness: the sitemap's <lastmod> plus a content hash decide what changed. Unchanged pages are
  never re-fetched; pages whose lastmod moved but whose content is identical are not re-embedded.
* Removals: URLs that leave the sitemap (or return 404/410) are marked `removed` and their chunks are
  deleted, so they stop appearing in answers. A safety ratio blocks mass deletion if the sitemap
  is suddenly truncated.
* Batching: `limit` caps how many articles one run processes, so a first read can be done in slices.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import numpy as np
from pydantic import BaseModel, Field

from .chunker import chunk_sections, image_chunk_text
from .config import Settings
from .db import Database, now_iso
from .llm import LLMClient, LLMError
from .parser import ParsedArticle, SitemapEntry, parse_article, parse_sitemap
from .prompts import VISION_PROMPT_VERSION, VISION_SYSTEM, VISION_USER

log = logging.getLogger("brain.ingest")

SUPPORTED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ImageDescription(BaseModel):
    relevant: bool = Field(description="False for logos, avatars, decorative images")
    description: str = Field(description="2-5 factual sentences: screen, menu path, fields, buttons, values")
    ui_elements: list[str] = Field(default_factory=list, description="Exact labels of buttons/fields/menus visible")


class PoliteFetcher:
    """Global rate limit + concurrency cap + retries. One instance per sync run."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.s = settings
        self._sem = asyncio.Semaphore(max(1, settings.crawl_concurrency))
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.requests = 0
        self.client = httpx.AsyncClient(
            timeout=settings.crawl_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": settings.crawl_user_agent},
            transport=transport,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _wait_turn(self) -> None:
        async with self._lock:
            delay = self.s.crawl_delay_seconds - (time.monotonic() - self._last)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()

    async def get(self, url: str) -> httpx.Response:
        attempts = max(1, self.s.crawl_max_retries)
        last: FetchError | None = None
        async with self._sem:
            for attempt in range(1, attempts + 1):
                await self._wait_turn()
                self.requests += 1
                try:
                    resp = await self.client.get(url)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last = FetchError(f"{type(exc).__name__}: {exc}")
                else:
                    if resp.status_code < 400:
                        return resp
                    if resp.status_code in (404, 410):
                        raise FetchError(f"HTTP {resp.status_code}", resp.status_code)
                    last = FetchError(f"HTTP {resp.status_code}", resp.status_code)
                    if resp.status_code not in (408, 425, 429) and resp.status_code < 500:
                        raise last
                    retry_after = resp.headers.get("retry-after", "")
                    if retry_after.isdigit():
                        await asyncio.sleep(min(int(retry_after), 60))
                        continue
                if attempt < attempts:
                    await asyncio.sleep(self.s.crawl_delay_seconds * 2**attempt)
        assert last is not None
        raise last


@dataclass
class SyncStats:
    sitemap_urls: int = 0
    new: int = 0
    changed: int = 0
    removed: int = 0
    removal_blocked: bool = False
    queued: int = 0
    processed: int = 0
    succeeded: int = 0
    unchanged_content: int = 0
    failed: int = 0
    remaining: int = 0
    chunks_embedded: int = 0
    images_described: int = 0
    images_failed: int = 0
    http_requests: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class Ingestor:
    def __init__(
        self, db: Database, llm: LLMClient, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.db = db
        self.llm = llm
        self.s = settings
        self._transport = transport
        self.lock = asyncio.Lock()

    # ------------------------------------------------------------------ sitemap
    async def read_sitemap(self, fetcher: PoliteFetcher) -> list[SitemapEntry]:
        pattern = self.s.crawl_url_pattern
        todo: list[str] = [self.s.sitemap_url]
        seen_maps: set[str] = set()
        entries: dict[str, SitemapEntry] = {}
        while todo and len(seen_maps) < 50:
            url = todo.pop(0)
            if url in seen_maps:
                continue
            seen_maps.add(url)
            resp = await fetcher.get(url)
            found, children = parse_sitemap(resp.text)
            todo.extend(children)
            for e in found:
                if not pattern or pattern in e.url:
                    entries[e.url] = e
        return list(entries.values())

    # ------------------------------------------------------------------ main sync
    async def sync(self, limit: int | None = None, describe_images: bool = True, max_images: int | None = None) -> dict[str, Any]:
        if self.lock.locked():
            raise RuntimeError("A sync is already running")
        async with self.lock:
            run_id = self.db.execute(
                "INSERT INTO sync_runs(kind, status, params, started_at) VALUES('sitemap_sync','running',?,?)",
                (json.dumps({"limit": limit, "describe_images": describe_images, "max_images": max_images}), now_iso()),
            )
            stats = SyncStats()
            fetcher = PoliteFetcher(self.s, self._transport)
            try:
                await self._sync(fetcher, stats, limit)
                stats.chunks_embedded = await self.embed_pending(stats.warnings)
                if describe_images:
                    if self.llm.vision_enabled:
                        await self._describe_images(fetcher, stats, max_images)
                        stats.chunks_embedded += await self.embed_pending(stats.warnings)
                    else:
                        stats.warnings.append("Image understanding skipped: no GEMINI_API_KEY configured.")
                stats.http_requests = fetcher.requests
                self.db.execute(
                    "UPDATE sync_runs SET status='completed', stats=?, finished_at=? WHERE id=?",
                    (json.dumps(stats.to_dict()), now_iso(), run_id),
                )
            except Exception as exc:
                log.exception("sync failed")
                stats.http_requests = fetcher.requests
                self.db.execute(
                    "UPDATE sync_runs SET status='failed', stats=?, error=?, finished_at=? WHERE id=?",
                    (json.dumps(stats.to_dict()), str(exc)[:1000], now_iso(), run_id),
                )
                raise
            finally:
                await fetcher.close()
            return {"run_id": run_id, **stats.to_dict()}

    async def _sync(self, fetcher: PoliteFetcher, stats: SyncStats, limit: int | None) -> None:
        entries = await self.read_sitemap(fetcher)
        stats.sitemap_urls = len(entries)
        if not entries:
            raise RuntimeError("Sitemap returned no article URLs; refusing to change the knowledge base.")
        by_url = {e.url: e for e in entries}
        known = {r["url"]: r for r in self.db.query("SELECT id, url, status, lastmod FROM articles WHERE source='helpcenter'")}
        active_count = sum(1 for r in known.values() if r["status"] == "active")

        gone = [r for u, r in known.items() if u not in by_url and r["status"] != "removed"]
        if gone and active_count and len(entries) < self.s.removal_safety_ratio * active_count:
            stats.removal_blocked = True
            stats.warnings.append(
                f"Sitemap shrank to {len(entries)} URLs vs {active_count} active articles; removals skipped for safety."
            )
        else:
            for r in gone:
                self._mark_removed(r["id"], "Not in sitemap")
                stats.removed += 1

        now = now_iso()
        with self.db.tx() as conn:
            for url, e in by_url.items():
                row = known.get(url)
                if row is None:
                    conn.execute(
                        "INSERT INTO articles(url, lastmod, status, first_seen_at, updated_at) VALUES(?,?, 'pending', ?, ?)",
                        (url, e.lastmod, now, now),
                    )
                    stats.new += 1
                elif row["status"] == "removed" or (row["status"] == "active" and e.lastmod and e.lastmod != row["lastmod"]):
                    conn.execute(
                        "UPDATE articles SET status='pending', lastmod=?, attempts=0, updated_at=? WHERE id=?",
                        (e.lastmod, now, row["id"]),
                    )
                    stats.changed += 1

        sql = (
            "SELECT id, url FROM articles WHERE source='helpcenter' AND "
            "(status='pending' OR (status='failed' AND attempts < ?)) ORDER BY id"
        )
        queue = self.db.query(sql, (self.s.crawl_max_retries,))
        if limit:
            queue = queue[:limit]
        stats.queued = len(queue)

        async def work(row: Any) -> None:
            outcome = await self._process_article(fetcher, row["id"], row["url"])
            stats.processed += 1
            if outcome == "failed":
                stats.failed += 1
            elif outcome == "removed":
                stats.removed += 1
            else:
                stats.succeeded += 1
                if outcome == "unchanged":
                    stats.unchanged_content += 1

        await asyncio.gather(*(work(r) for r in queue))
        remaining = self.db.one(
            "SELECT COUNT(*) AS n FROM articles WHERE source='helpcenter' AND (status='pending' OR (status='failed' AND attempts < ?))",
            (self.s.crawl_max_retries,),
        )
        stats.remaining = int(remaining["n"]) if remaining else 0

    def _mark_removed(self, article_id: int, reason: str) -> None:
        with self.db.tx() as conn:
            conn.execute("DELETE FROM chunks WHERE article_id=?", (article_id,))
            conn.execute("DELETE FROM images WHERE article_id=?", (article_id,))
            conn.execute(
                "UPDATE articles SET status='removed', last_error=?, updated_at=? WHERE id=?", (reason, now_iso(), article_id)
            )
            self.db.bump_version(conn)

    async def _process_article(self, fetcher: PoliteFetcher, article_id: int, url: str) -> str:
        try:
            resp = await fetcher.get(url)
            parsed = parse_article(resp.text, url)
            if not parsed.sections:
                raise FetchError("No article body found")
        except FetchError as exc:
            if exc.status in (404, 410):
                self._mark_removed(article_id, f"HTTP {exc.status}")
                return "removed"
            self._mark_failed(article_id, str(exc))
            return "failed"
        except Exception as exc:  # parser bugs must not kill the batch
            log.exception("failed to process %s", url)
            self._mark_failed(article_id, f"{type(exc).__name__}: {exc}")
            return "failed"
        return self.store_article(article_id, parsed)

    def _mark_failed(self, article_id: int, error: str) -> None:
        self.db.execute(
            "UPDATE articles SET status='failed', attempts=attempts+1, last_error=?, updated_at=? WHERE id=?",
            (error[:500], now_iso(), article_id),
        )

    def store_article(self, article_id: int, parsed: ParsedArticle) -> str:
        """Write parsed content. Returns 'unchanged' when the content hash did not move."""
        current = self.db.one("SELECT content_hash, status FROM articles WHERE id=?", (article_id,))
        now = now_iso()
        new_hash = parsed.content_hash
        if (
            current
            and current["content_hash"] == new_hash
            and self.db.one("SELECT 1 FROM chunks WHERE article_id=? LIMIT 1", (article_id,))
        ):
            with self.db.tx() as conn:
                conn.execute(
                    "UPDATE articles SET status='active', attempts=0, last_error=NULL, fetched_at=?, updated_at=? WHERE id=?",
                    (now, now, article_id),
                )
                self.db.bump_version(conn)
            return "unchanged"
        chunks = chunk_sections(parsed.title, [(s.heading, s.paragraphs) for s in parsed.sections])
        with self.db.tx() as conn:
            conn.execute(
                """UPDATE articles SET title=?, category=?, folder=?, body_text=?, content_hash=?, status='active',
                   attempts=0, last_error=NULL, fetched_at=?, updated_at=? WHERE id=?""",
                (parsed.title, parsed.category, parsed.folder, parsed.text, new_hash, now, now, article_id),
            )
            conn.execute("DELETE FROM chunks WHERE article_id=? AND image_id IS NULL", (article_id,))
            for i, c in enumerate(chunks):
                conn.execute(
                    "INSERT INTO chunks(article_id, ord, heading, text) VALUES(?,?,?,?)", (article_id, i, c.heading, c.text)
                )
            keep = [img.url for img in parsed.images[: self.s.max_images_per_article]]
            existing = {r["url"] for r in conn.execute("SELECT url FROM images WHERE article_id=?", (article_id,))}
            for stale in existing - set(keep):
                conn.execute("DELETE FROM images WHERE article_id=? AND url=?", (article_id, stale))
            for img in parsed.images[: self.s.max_images_per_article]:
                conn.execute(
                    """INSERT INTO images(article_id, url, alt, context, section) VALUES(?,?,?,?,?)
                       ON CONFLICT(article_id, url) DO UPDATE SET alt=excluded.alt, context=excluded.context,
                       section=excluded.section""",
                    (article_id, img.url, img.alt, img.context, img.section),
                )
            self.db.bump_version(conn)
        return "stored"

    # ------------------------------------------------------------------ embeddings
    async def embed_pending(self, warnings: list[str] | None = None, batch: int = 100) -> int:
        """Embed chunks that have no embedding for the current model. Safe to call repeatedly."""
        if not self.llm.embeddings_enabled:
            return 0
        model = self.s.gemini_embed_model
        done = 0
        while True:
            rows = self.db.query(
                "SELECT id, text FROM chunks WHERE embedding IS NULL OR embed_model IS NOT ? LIMIT ?", (model, batch)
            )
            if not rows:
                break
            try:
                vectors, _ = await self.llm.embed([r["text"] for r in rows], kind="document")
            except LLMError as exc:
                msg = f"Embedding failed ({exc.kind}): {exc}. Lexical search still works."
                log.warning(msg)
                if warnings is not None:
                    warnings.append(msg)
                break
            with self.db.tx() as conn:
                for r, v in zip(rows, vectors, strict=True):
                    conn.execute(
                        "UPDATE chunks SET embedding=?, embed_model=? WHERE id=?",
                        (np.asarray(v, dtype=np.float32).tobytes(), model, r["id"]),
                    )
                self.db.bump_version(conn)
            done += len(rows)
        return done

    # ------------------------------------------------------------------ images
    async def describe_images(self, limit: int | None = None) -> dict[str, Any]:
        if not self.llm.vision_enabled:
            raise LLMError("disabled", "Image understanding needs GEMINI_API_KEY (or ANTHROPIC_API_KEY).")
        async with self.lock:
            stats = SyncStats()
            fetcher = PoliteFetcher(self.s, self._transport)
            try:
                await self._describe_images(fetcher, stats, limit)
                stats.chunks_embedded = await self.embed_pending(stats.warnings)
            finally:
                await fetcher.close()
            return stats.to_dict()

    async def _describe_images(self, fetcher: PoliteFetcher, stats: SyncStats, limit: int | None) -> None:
        rows = self.db.query(
            """SELECT i.id, i.url, i.alt, i.context, i.section, i.article_id, a.title FROM images i
               JOIN articles a ON a.id=i.article_id WHERE i.status='pending' AND a.status='active' ORDER BY i.id LIMIT ?""",
            (limit if limit else 1_000_000,),
        )
        for r in rows:
            try:
                resp = await fetcher.get(r["url"])
                mime = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if mime == "image/jpg":
                    mime = "image/jpeg"
                if mime not in SUPPORTED_IMAGE_TYPES or len(resp.content) > self.s.max_image_bytes:
                    self.db.execute("UPDATE images SET status='skipped', error=? WHERE id=?", (f"type={mime}", r["id"]))
                    continue
                out, call = await self.llm.generate_json(
                    tier="vision",
                    system=VISION_SYSTEM,
                    prompt=VISION_USER.format(
                        title=r["title"], section=r["section"] or "-", alt=r["alt"] or "-", context=r["context"] or "-"
                    ),
                    schema=ImageDescription,
                    images=[(resp.content, mime)],
                )
            except (FetchError, LLMError) as exc:
                stats.images_failed += 1
                self.db.execute("UPDATE images SET status='failed', error=? WHERE id=?", (str(exc)[:300], r["id"]))
                if isinstance(exc, LLMError) and exc.kind == "auth":
                    stats.warnings.append(str(exc))
                    return
                continue
            if not out.relevant:
                self.db.execute("UPDATE images SET status='skipped', error='decorative' WHERE id=?", (r["id"],))
                continue
            description = out.description.strip()
            if out.ui_elements:
                description += " Visible labels: " + ", ".join(out.ui_elements[:15]) + "."
            with self.db.tx() as conn:
                conn.execute(
                    "UPDATE images SET status='described', description=?, model=?, error=NULL WHERE id=?",
                    (description, f"{call.model} ({VISION_PROMPT_VERSION})", r["id"]),
                )
                conn.execute("DELETE FROM chunks WHERE image_id=?", (r["id"],))
                conn.execute(
                    "INSERT INTO chunks(article_id, image_id, ord, heading, text) VALUES(?,?,?,?,?)",
                    (
                        r["article_id"],
                        r["id"],
                        10_000 + r["id"],
                        r["section"],
                        image_chunk_text(r["title"], r["section"], description),
                    ),
                )
                self.db.bump_version(conn)
            stats.images_described += 1
