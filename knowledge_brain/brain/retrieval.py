"""Hybrid retrieval: BM25 (always available) + embedding cosine similarity (when Gemini is configured),
merged with reciprocal rank fusion.

Why hybrid: help-center questions mix exact product terms ("Stripe", "member dashboard") where keyword
search shines, with paraphrases ("stop my plan" vs "cancel membership") where embeddings shine. Keeping
BM25 also means the Brain still works, honestly, with no API key at all.

The index lives in memory and is rebuilt whenever the database's kb_version changes. At a few thousand
chunks a rebuild takes well under a second, so no external vector database is needed.
"""

from __future__ import annotations

import math
import re
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace

import numpy as np

from .db import Database

STOPWORDS = set(
    """a an the and or but if then else of to in on at by for with from into about as is are was were be been being
    do does did doing have has had having i me my we our you your he she it its they them their this that these those
    what which who whom whose when where why how can could should would will shall may might must not no nor so too very
    just than also any all each some such there here up down out over under again further once only own same both few
    more most other am s t don doesn didn won isn aren wasn weren hasn haven hadn wouldn shouldn couldn get got
    please hi hello thanks thank need want like know tell let make one way using use""".split()
)


def stem(word: str) -> str:
    """Tiny suffix stripper (propagation/propagate -> propag, plans -> plan). Same function for documents and
    queries, so over-stemming only causes harmless collisions."""
    suffixes = (("ations", ""), ("ation", ""), ("ies", "y"), ("ing", ""), ("ate", ""), ("ed", ""), ("es", ""), ("s", ""))
    for suffix, repl in suffixes:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)] + repl
    return word


def tokenize(text: str) -> list[str]:
    return [stem(t) for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOPWORDS and len(t) > 1]


@dataclass
class Hit:
    chunk_id: int
    article_id: int
    image_id: int | None
    title: str
    url: str
    source: str
    heading: str
    text: str
    score: float = 0.0
    lexical: float = 0.0
    dense: float | None = None
    coverage: float = 0.0


@dataclass
class SearchResult:
    hits: list[Hit]
    mode: str  # hybrid | lexical
    confidence: float
    sufficient: bool
    query_terms: list[str] = field(default_factory=list)


class SearchIndex:
    K1, B, RRF_K = 1.5, 0.75, 60

    def __init__(self, db: Database) -> None:
        self.db = db
        self._lock = threading.Lock()
        self._version = -1
        self._rows: list[Hit] = []
        self._postings: dict[str, list[tuple[int, int]]] = {}
        self._doc_len: list[int] = []
        self._token_sets: list[set[str]] = []
        self._avg_len = 1.0
        self._idf: dict[str, float] = {}
        self._dense: np.ndarray | None = None
        self._dense_rows: list[int] = []
        self.embed_model: str | None = None

    @property
    def size(self) -> int:
        return len(self._rows)

    @property
    def has_dense(self) -> bool:
        return self._dense is not None and len(self._dense_rows) > 0

    def ensure_fresh(self, embed_model: str | None) -> None:
        version = self.db.kb_version()
        with self._lock:
            if version != self._version or embed_model != self.embed_model:
                self._rebuild(embed_model)
                self._version = version

    def _rebuild(self, embed_model: str | None) -> None:
        rows = self.db.query(
            """SELECT c.id, c.article_id, c.image_id, c.heading, c.text, c.embedding, c.embed_model,
                      a.title, a.url, a.source
               FROM chunks c JOIN articles a ON a.id = c.article_id
               WHERE a.status != 'removed' ORDER BY c.id"""
        )
        self._rows = []
        postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self._doc_len, self._token_sets = [], []
        vectors: list[np.ndarray] = []
        self._dense_rows = []
        dim: int | None = None
        for idx, r in enumerate(rows):
            self._rows.append(
                Hit(
                    chunk_id=r["id"],
                    article_id=r["article_id"],
                    image_id=r["image_id"],
                    title=r["title"] or "",
                    url=r["url"],
                    source=r["source"],
                    heading=r["heading"] or "",
                    text=r["text"],
                )
            )
            tokens = tokenize(r["text"])
            for term, tf in Counter(tokens).items():
                postings[term].append((idx, tf))
            self._doc_len.append(len(tokens))
            self._token_sets.append(set(tokens))
            if embed_model and r["embedding"] is not None and r["embed_model"] == embed_model:
                vec = np.frombuffer(r["embedding"], dtype=np.float32)
                if dim is None:
                    dim = vec.shape[0]
                if vec.shape[0] == dim:
                    norm = float(np.linalg.norm(vec)) or 1.0
                    vectors.append(vec / norm)
                    self._dense_rows.append(idx)
        n = max(len(self._rows), 1)
        self._postings = dict(postings)
        self._avg_len = (sum(self._doc_len) / n) or 1.0
        self._idf = {t: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self._postings.items()}
        self._dense = np.vstack(vectors) if vectors else None
        self.embed_model = embed_model

    def _idf_of(self, term: str) -> float:
        n = max(len(self._rows), 1)
        return self._idf.get(term, math.log(1 + (n + 0.5) / 0.5))

    def search(
        self,
        query: str,
        top_k: int = 6,
        query_vec: list[float] | None = None,
        dense_min: float = 0.55,
        coverage_min: float = 0.6,
    ) -> SearchResult:
        terms = list(dict.fromkeys(tokenize(query)))
        lexical: dict[int, float] = defaultdict(float)
        for t in terms:
            idf = self._idf_of(t)
            for doc, tf in self._postings.get(t, []):
                denom = tf + self.K1 * (1 - self.B + self.B * self._doc_len[doc] / self._avg_len)
                lexical[doc] += idf * tf * (self.K1 + 1) / denom
        lex_rank = sorted(lexical, key=lambda d: -lexical[d])[:50]

        dense_scores: dict[int, float] = {}
        if query_vec is not None and self._dense is not None:
            q = np.asarray(query_vec, dtype=np.float32)
            if q.shape[0] == self._dense.shape[1]:
                q = q / (float(np.linalg.norm(q)) or 1.0)
                sims = self._dense @ q
                for pos in np.argsort(-sims)[:50]:
                    dense_scores[self._dense_rows[int(pos)]] = float(sims[int(pos)])
        dense_rank = sorted(dense_scores, key=lambda d: -dense_scores[d])

        fused: dict[int, float] = defaultdict(float)
        for rank, doc in enumerate(lex_rank):
            fused[doc] += 1 / (self.RRF_K + rank + 1)
        for rank, doc in enumerate(dense_rank):
            fused[doc] += 1 / (self.RRF_K + rank + 1)

        total_idf = sum(self._idf_of(t) for t in terms) or 1.0
        hits: list[Hit] = []
        for doc in sorted(fused, key=lambda d: -fused[d])[:top_k]:
            base = self._rows[doc]
            covered = sum(self._idf_of(t) for t in terms if t in self._token_sets[doc])
            hits.append(
                replace(
                    base,
                    score=round(fused[doc], 5),
                    lexical=round(lexical.get(doc, 0.0), 4),
                    dense=round(dense_scores[doc], 4) if doc in dense_scores else None,
                    coverage=round(covered / total_idf, 3),
                )
            )

        mode = "hybrid" if dense_scores else "lexical"
        if not hits:
            return SearchResult(hits=[], mode=mode, confidence=0.0, sufficient=False, query_terms=terms)
        if mode == "hybrid":
            confidence = max(h.dense or 0.0 for h in hits)
            sufficient = confidence >= dense_min
        else:
            confidence = max(h.coverage for h in hits[:3])
            sufficient = confidence >= coverage_min
        return SearchResult(hits=hits, mode=mode, confidence=round(confidence, 3), sufficient=sufficient, query_terms=terms)
