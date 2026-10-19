"""KnowledgeService: the one place that answers questions. The HTTP API (and therefore Tasks 2 and 3)
calls only this class."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any

from pydantic import BaseModel, Field

from .chunker import chunk_sections
from .config import Settings
from .db import Database, now_iso
from .ingest import Ingestor
from .llm import LLMCall, LLMClient, LLMError
from .prompts import ANSWER_PROMPT_VERSION, ANSWER_SYSTEM, ANSWER_USER
from .retrieval import Hit, SearchIndex, SearchResult, tokenize

log = logging.getLogger("brain.service")

# Below this retrieval confidence we abstain without spending an LLM call.
HOPELESS_DENSE = 0.35
HOPELESS_COVERAGE = 0.25


class AnswerOut(BaseModel):
    answerable: bool = Field(description="True only if the evidence supports the answer")
    answer: str = Field(description="Answer for the user, or a short explanation that the evidence is insufficient")
    cited_evidence: list[int] = Field(default_factory=list, description="Evidence numbers used, e.g. [1, 3]")
    missing_information: str = Field(default="", description="What the evidence does not cover")


def normalize_question(q: str) -> str:
    return " ".join(sorted(set(tokenize(q))))


class KnowledgeService:
    def __init__(self, db: Database, llm: LLMClient, settings: Settings, ingestor: Ingestor | None = None) -> None:
        self.db = db
        self.llm = llm
        self.s = settings
        self.index = SearchIndex(db)
        self.ingestor = ingestor or Ingestor(db, llm, settings)

    # ------------------------------------------------------------------ search
    async def search(self, query: str, top_k: int | None = None) -> tuple[SearchResult, list[str]]:
        warnings: list[str] = []
        embed_model = self.s.gemini_embed_model if self.llm.embeddings_enabled else None
        self.index.ensure_fresh(embed_model)
        qvec = None
        if embed_model and self.index.has_dense:
            try:
                vectors, _ = await self.llm.embed([query], kind="query")
                qvec = vectors[0]
            except LLMError as exc:
                warnings.append(f"Semantic search unavailable ({exc.kind}); used keyword search only.")
        result = self.index.search(
            query,
            top_k=top_k or self.s.top_k,
            query_vec=qvec,
            dense_min=self.s.dense_min_score,
            coverage_min=self.s.lexical_min_coverage,
        )
        return result, warnings

    # ------------------------------------------------------------------ ask
    async def ask(
        self,
        question: str,
        *,
        top_k: int | None = None,
        create_escalation: bool = True,
        source: str = "chat",
        context: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        question = question.strip()
        result, warnings = await self.search(question, top_k)
        hits = result.hits
        call: LLMCall | None = None
        cited: list[Hit] = []

        hopeless = not hits or (result.confidence < (HOPELESS_DENSE if result.mode == "hybrid" else HOPELESS_COVERAGE))
        if hopeless:
            mode = "live" if self.llm.enabled else "offline"
            answered, answer, missing = False, "", "No help-center passage matches this question."
        elif self.llm.enabled:
            try:
                out, call = await self.llm.generate_json(
                    tier="smart",
                    system=ANSWER_SYSTEM.format(company=self.s.company_name),
                    prompt=ANSWER_USER.format(question=question, evidence=self._format_evidence(hits)),
                    schema=AnswerOut,
                )
                cited = [hits[i - 1] for i in dict.fromkeys(out.cited_evidence) if 1 <= i <= len(hits)]
                # Grounding rule: an answer without valid citations is not an answer.
                answered = out.answerable and bool(cited)
                answer, missing, mode = out.answer.strip(), out.missing_information, "live"
            except LLMError as exc:
                warnings.append(f"LLM unavailable ({exc.kind}): {exc}. Showing an extractive answer instead.")
                answered, answer, cited = self._extractive(result)
                missing, mode = "", "fallback"
        else:
            answered, answer, cited = self._extractive(result)
            missing, mode = "", "offline"

        escalation_id = None
        if not answered:
            answer = "I couldn't find a reliable answer to this in the help center, so I won't guess." + (
                f" What's missing: {missing}" if missing else ""
            )
            if create_escalation:
                escalation_id = self.create_escalation(question, source=source, context=context, confidence=result.confidence)
                answer += f" I've passed it to a product specialist (escalation #{escalation_id})."
            cited = []

        citations = self._citations(cited if answered else hits[:3])
        response: dict[str, Any] = {
            "question": question,
            "answered": answered,
            "answer": answer,
            "confidence": result.confidence,
            "retrieval_mode": result.mode,
            "citations": citations if answered else [],
            "related": [] if answered else citations,
            "images": self._images(cited) if answered else [],
            "escalation_id": escalation_id,
            "warnings": warnings,
            "llm": {
                "mode": mode,
                "provider": call.provider if call else self.llm.provider,
                "model": call.model if call else None,
                "prompt_version": ANSWER_PROMPT_VERSION,
                "input_tokens": call.input_tokens if call else 0,
                "output_tokens": call.output_tokens if call else 0,
                "cost_usd": call.cost_usd if call else None,
            },
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
        self.db.execute(
            """INSERT INTO query_log(question, source, answered, confidence, mode, provider, model, input_tokens,
               output_tokens, cost_usd, latency_ms, escalation_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                question,
                source,
                int(answered),
                result.confidence,
                mode,
                response["llm"]["provider"],
                response["llm"]["model"],
                response["llm"]["input_tokens"],
                response["llm"]["output_tokens"],
                response["llm"]["cost_usd"],
                response["latency_ms"],
                escalation_id,
                now_iso(),
            ),
        )
        return response

    @staticmethod
    def _format_evidence(hits: list[Hit]) -> str:
        lines = []
        for i, h in enumerate(hits, 1):
            kind = "screenshot" if h.image_id else "article"
            lines.append(f"[E{i}] ({kind}: {h.title}{' > ' + h.heading if h.heading else ''})\n{h.text}")
        return "\n\n".join(lines)

    def _extractive(self, result: SearchResult) -> tuple[bool, str, list[Hit]]:
        """No-LLM answer: quote the most relevant sentences from passages that pass the retrieval gate."""
        if not result.sufficient:
            return False, "", []
        terms = set(result.query_terms)
        good = [h for h in result.hits if h.coverage >= self.s.lexical_min_coverage * 0.8][:2] or result.hits[:1]
        quotes: list[str] = []
        for h in good:
            body = h.text.split("\n", 1)[-1]
            sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", body) if len(s.strip()) > 20]
            # Pick the 3 most relevant sentences but keep the article's order, so procedures stay in sequence.
            best = sorted(range(len(sentences)), key=lambda i: -len(terms & set(tokenize(sentences[i]))))[:3]
            quotes.extend(sentences[i] for i in sorted(best))
        text = "Here is what the help center says (extractive answer, no AI model configured):\n\n" + "\n".join(
            f"> {q}" for q in quotes[:5]
        )
        return True, text, good

    def _citations(self, hits: list[Hit]) -> list[dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        for h in hits:
            if h.article_id in out:
                continue
            snippet = h.text.split("\n", 1)[-1][:280]
            out[h.article_id] = {
                "article_id": h.article_id,
                "title": h.title,
                "url": h.url if h.source == "helpcenter" else None,
                "source": h.source,
                "heading": h.heading,
                "snippet": snippet,
                "score": h.score,
                "dense": h.dense,
                "coverage": h.coverage,
            }
        return list(out.values())

    def _images(self, hits: list[Hit]) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(h.article_id for h in hits))
        if not ids:
            return []
        cited_images = {h.image_id for h in hits if h.image_id}
        marks = ",".join("?" * len(ids))
        rows = self.db.query(
            f"""SELECT i.id, i.url, i.alt, i.description, i.status, i.section, a.title, a.url AS article_url
                FROM images i JOIN articles a ON a.id=i.article_id
                WHERE i.article_id IN ({marks}) AND i.status IN ('described','pending') ORDER BY i.id""",
            tuple(ids),
        )
        images = [
            {
                "image_id": r["id"],
                "url": r["url"],
                "alt": r["alt"],
                "description": r["description"],
                "analysed": r["status"] == "described",
                "section": r["section"],
                "article_title": r["title"],
                "article_url": r["article_url"],
                "directly_cited": r["id"] in cited_images,
            }
            for r in rows
        ]
        images.sort(key=lambda i: (not i["directly_cited"], not i["analysed"]))
        return images[:4]

    # ------------------------------------------------------------------ escalations
    def create_escalation(self, question: str, *, source: str, context: str | None, confidence: float | None) -> int:
        norm = normalize_question(question)
        existing = self.db.one("SELECT id FROM escalations WHERE normalized=? AND status='open'", (norm,))
        if existing:
            self.db.execute("UPDATE escalations SET times_asked=times_asked+1 WHERE id=?", (existing["id"],))
            return int(existing["id"])
        return self.db.execute(
            "INSERT INTO escalations(question, normalized, source, context, confidence, created_at) VALUES(?,?,?,?,?,?)",
            (question, norm, source, context, confidence, now_iso()),
        )

    async def resolve_escalation(self, escalation_id: int, answer: str, expert_name: str) -> dict[str, Any]:
        esc = self.db.one("SELECT * FROM escalations WHERE id=?", (escalation_id,))
        if not esc:
            raise KeyError(escalation_id)
        article_id = await self.add_document(
            title=esc["question"],
            text=answer,
            source="expert",
            url=f"expert://escalations/{escalation_id}",
            category="Expert answers",
        )
        self.db.execute(
            "UPDATE escalations SET status='answered', expert_answer=?, expert_name=?, article_id=?, answered_at=? WHERE id=?",
            (answer, expert_name, article_id, now_iso(), escalation_id),
        )
        return {"escalation_id": escalation_id, "article_id": article_id, "status": "answered"}

    def dismiss_escalation(self, escalation_id: int) -> None:
        self.db.execute("UPDATE escalations SET status='dismissed', answered_at=? WHERE id=?", (now_iso(), escalation_id))

    # ------------------------------------------------------------------ custom documents
    async def add_document(
        self, *, title: str, text: str, source: str = "document", url: str | None = None, category: str | None = None
    ) -> int:
        url = url or f"doc://{hashlib.sha1((title + text).encode()).hexdigest()[:16]}"
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n(?=[-*\d])", text) if p.strip()]
        sections: list[tuple[str, list[str]]] = [("", paragraphs)]
        chunks = chunk_sections(title, sections)
        now = now_iso()
        with self.db.tx() as conn:
            row = conn.execute("SELECT id FROM articles WHERE url=?", (url,)).fetchone()
            if row:
                article_id = int(row["id"])
                conn.execute(
                    "UPDATE articles SET title=?, body_text=?, status='active', category=?, updated_at=? WHERE id=?",
                    (title, text, category, now, article_id),
                )
                conn.execute("DELETE FROM chunks WHERE article_id=?", (article_id,))
            else:
                cur = conn.execute(
                    """INSERT INTO articles(url, source, title, category, status, body_text, first_seen_at, fetched_at, updated_at)
                       VALUES(?,?,?,?, 'active', ?,?,?,?)""",
                    (url, source, title, category, text, now, now, now),
                )
                article_id = int(cur.lastrowid or 0)
            for i, c in enumerate(chunks):
                conn.execute(
                    "INSERT INTO chunks(article_id, ord, heading, text) VALUES(?,?,?,?)", (article_id, i, c.heading, c.text)
                )
            self.db.bump_version(conn)
        await self.ingestor.embed_pending()
        return article_id

    def delete_document(self, article_id: int) -> bool:
        row = self.db.one("SELECT source FROM articles WHERE id=?", (article_id,))
        if not row or row["source"] == "helpcenter":
            return False
        with self.db.tx() as conn:
            conn.execute("DELETE FROM articles WHERE id=?", (article_id,))
            self.db.bump_version(conn)
        return True

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict[str, Any]:
        def count(sql: str, params: tuple[Any, ...] = ()) -> int:
            row = self.db.one(sql, params)
            return int(row[0]) if row else 0

        by_status = {
            r["status"]: r["n"]
            for r in self.db.query("SELECT status, COUNT(*) n FROM articles WHERE source='helpcenter' GROUP BY status")
        }
        images = {r["status"]: r["n"] for r in self.db.query("SELECT status, COUNT(*) n FROM images GROUP BY status")}
        last_run = self.db.one("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1")
        q = self.db.one("SELECT COUNT(*) n, AVG(answered) rate, AVG(latency_ms) lat, SUM(cost_usd) cost FROM query_log")
        return {
            "articles": by_status,
            "expert_answers": count("SELECT COUNT(*) FROM articles WHERE source='expert' AND status='active'"),
            "documents": count("SELECT COUNT(*) FROM articles WHERE source='document' AND status='active'"),
            "chunks": count("SELECT COUNT(*) FROM chunks"),
            "chunks_embedded": count(
                "SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL AND embed_model=?", (self.s.gemini_embed_model,)
            ),
            "images": images,
            "escalations_open": count("SELECT COUNT(*) FROM escalations WHERE status='open'"),
            "escalations_answered": count("SELECT COUNT(*) FROM escalations WHERE status='answered'"),
            "queries": {
                "total": q["n"] if q else 0,
                "answer_rate": round(q["rate"], 3) if q and q["rate"] is not None else None,
                "avg_latency_ms": int(q["lat"]) if q and q["lat"] is not None else None,
                "total_cost_usd": round(q["cost"], 4) if q and q["cost"] is not None else None,
            },
            "last_sync": dict(last_run) if last_run else None,
            "sync_running": self.ingestor.lock.locked(),
            "llm": self.llm.describe(),
        }
