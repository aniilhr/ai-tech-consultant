"""Deterministic text helpers: transcript parsing, segmentation for long transcripts, quote verification and
fuzzy matching. Kept free of LLM calls so they can be unit-tested and trusted."""

from __future__ import annotations

import re
from dataclasses import dataclass

STOP = set(
    "a an the and or of to in on for with we our you your it is are be that this as at by from i me my so "
    "would like want need should must will can could just also have has do does".split()
)
FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


@dataclass
class Turn:
    speaker: str
    role: str
    text: str


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    m = FRONT_MATTER.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, text[m.end() :]


def parse_turns(text: str) -> list[Turn]:
    """Lines like 'Sarah (Client - Ops): text' or 'Sarah: text'. Unlabelled lines continue the last turn."""
    turns: list[Turn] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^([A-Z][\w .'-]{0,40}?)\s*(?:\(([^)]*)\))?:\s+(.*)$", line)
        if m:
            turns.append(Turn(speaker=m.group(1).strip(), role=(m.group(2) or "").strip(), text=m.group(3).strip()))
        elif turns:
            turns[-1].text += " " + line
        else:
            turns.append(Turn(speaker="Unknown", role="", text=line))
    return turns


def segment(text: str, max_chars: int, overlap: int = 800) -> list[str]:
    """Split on line boundaries into segments <= max_chars, each starting with ~overlap chars of context."""
    if len(text) <= max_chars:
        return [text]
    lines = text.splitlines(keepends=True)
    segments: list[str] = []
    current = ""
    for line in lines:
        if len(current) + len(line) > max_chars and current:
            segments.append(current)
            current = current[-overlap:] if overlap else ""
            nl = current.find("\n")
            current = current[nl + 1 :] if nl >= 0 else current
        current += line
    if current.strip():
        segments.append(current)
    return segments


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def tokens(text: str) -> set[str]:
    return {w.rstrip("s") for w in _words(text) if w not in STOP and len(w) > 2}


def jaccard(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def quote_in_transcript(quote: str, transcript: str) -> bool:
    """True when the quote appears verbatim (ignoring case/punctuation/spacing) or 85% of its words appear in
    order-insensitive form within one transcript line. Guards against invented requirements."""
    q = _words(quote)
    if len(q) < 3:
        return False
    flat = " ".join(_words(transcript))
    if " ".join(q) in flat:
        return True
    qset = set(q)
    for line in transcript.splitlines():
        lw = set(_words(line))
        if lw and len(qset & lw) / len(qset) >= 0.85:
            return True
    return False
