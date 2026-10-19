"""Split an article into retrieval chunks.

Rules: never cross a section heading; keep chunks under ~1,100 characters; carry the last paragraph of the
previous chunk forward as overlap; prefix every chunk with "Title > Heading" so a chunk read alone still
says what it is about (this also helps lexical and embedding search).
"""

from __future__ import annotations

from dataclasses import dataclass

from .parser import ParsedArticle

MAX_CHARS = 1100


@dataclass
class Chunk:
    heading: str
    text: str


def _split_long(paragraph: str, limit: int) -> list[str]:
    if len(paragraph) <= limit:
        return [paragraph]
    out, current = [], ""
    for sentence in paragraph.replace("? ", "?|").replace(". ", ".|").replace("! ", "!|").split("|"):
        if len(current) + len(sentence) + 1 > limit and current:
            out.append(current.strip())
            current = ""
        current += sentence + " "
    if current.strip():
        out.append(current.strip())
    # A "sentence" can itself be longer than the limit (tables, run-on text): hard-split on words.
    final: list[str] = []
    for piece in out:
        while len(piece) > limit:
            cut = piece.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            final.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            final.append(piece)
    return final


def chunk_sections(title: str, sections: list[tuple[str, list[str]]], max_chars: int = MAX_CHARS) -> list[Chunk]:
    chunks: list[Chunk] = []
    for heading, paragraphs in sections:
        label = f"{title} > {heading}" if heading else title
        pieces: list[str] = []
        for p in paragraphs:
            pieces.extend(_split_long(p, max_chars))
        buf: list[str] = []
        size = 0
        for piece in pieces:
            if buf and size + len(piece) > max_chars:
                chunks.append(Chunk(heading=heading, text=f"{label}\n" + "\n".join(buf)))
                overlap = buf[-1] if len(buf[-1]) < max_chars // 3 else ""
                buf = [overlap] if overlap else []
                size = len(overlap)
            buf.append(piece)
            size += len(piece)
        if buf:
            chunks.append(Chunk(heading=heading, text=f"{label}\n" + "\n".join(buf)))
    return chunks


def chunk_article(article: ParsedArticle, max_chars: int = MAX_CHARS) -> list[Chunk]:
    return chunk_sections(article.title, [(s.heading, s.paragraphs) for s in article.sections], max_chars)


def image_chunk_text(title: str, section: str, description: str) -> str:
    where = f"{title} > {section}" if section else title
    return f"{where}\n[Screenshot] {description}"
