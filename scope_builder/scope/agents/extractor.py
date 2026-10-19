"""Extractor agent: one job - turn a transcript into requirements, decisions, assumptions, dependencies and
open questions, each tied to a verbatim quote.

Long transcripts: above LONG_TRANSCRIPT_CHARS the transcript is split into overlapping segments, each segment
is extracted in parallel (map) and the results are merged and de-duplicated (reduce)."""

from __future__ import annotations

import asyncio
import re

from ..llm import LLMCall, LLMClient
from ..models import ExtractedItem, ExtractedRequirement, Extraction, Item, Requirement
from ..prompts import EXTRACTOR_SYSTEM, EXTRACTOR_USER
from ..text import jaccard, parse_turns, quote_in_transcript, segment

NEED = re.compile(
    r"\b(need|needs|want|wants|we'd like|would like|should|must|would be (great|nice|amazing)|we'd love|we were thinking|have to|it'd be|ideally|button|form)\b",
    re.I,
)
# Product vocabulary: a client sentence must mention a concrete capability to count as a requirement offline.
FEATURE = re.compile(
    r"\b(search|filter|sort|profile|listing|review|import|spreadsheet|payment|stripe|billing|membership|tier|level|plan|map|"
    r"booking|appointment|request|calendar|newsletter|email|seo|pages?|dashboard|stats|views|badge|spanish|language|"
    r"mobile|phones?|domain|design|brand|logo|blog|events?|messag\w*|inbox|zoom|leads?|integrat\w*|api|sync|"
    r"claim|invite|admin|report\w*|upload|photos?|video|verified|license)\b",
    re.I,
)
NOT_REQ = re.compile(r"\b(prices?|a month|board|timeline|launch|no idea|not sure|embarrassing|thanks|honestly)\b", re.I)
DECISION = re.compile(r"\b(decided|agreed|let's go with|we'll go with|it is for|that's fixed|is the target|it is\.)\b", re.I)
ASSUME = re.compile(r"\b(assum\w+|i think|presumably|probably)\b", re.I)
DEPEND = re.compile(
    r"\b(depends on|by (november|december|january|february|march|monday|friday)|need .{0,30} access|api access|waiting on|coordinate with)\b",
    re.I,
)
OPEN = re.compile(
    r"\b(not sure|no idea|need to check|tbd|to be decided|haven't finalised|hard to say|we'll need to confirm|open question)\b",
    re.I,
)
VAGUE = re.compile(r"\b(better|smarter|somehow|modern|nice|clunky|great)\b", re.I)
LATER = re.compile(r"\b(would be (great|nice|amazing)|later|phase two|nice to have|if it's expensive)\b", re.I)
FIRM = re.compile(r"\b(must|need|required|fixed|critical)\b", re.I)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 15]


def _title(sentence: str) -> str:
    cleaned = re.sub(r"^(and|so|but|also|okay|ok|yes|right)[, ]+", "", sentence.strip(), flags=re.I)
    cleaned = re.sub(
        r"\b(we need|we want|we'd like|we would like|it would be great if|i'd like|i want)\b\s*(to\s+)?", "", cleaned, flags=re.I
    )
    words = re.findall(r"[A-Za-z0-9'-]+", cleaned)
    return " ".join(words[:8]).capitalize() or sentence[:60]


def heuristic_extract(transcript: str) -> Extraction:
    """Offline fallback: keyword rules over client turns. Clearly weaker than a model; labelled as offline."""
    out = Extraction()
    for turn in parse_turns(transcript):
        is_client = "client" in turn.role.lower() or not turn.role
        for s in _sentences(turn.text):
            if (
                is_client
                and (NEED.search(s) or re.match(r"^(a|an)\s", s, re.I))
                and FEATURE.search(s)
                and not NOT_REQ.search(s)
                and not s.endswith("?")
            ):
                ambiguous = bool(VAGUE.search(s)) and len(s) < 120
                out.requirements.append(
                    ExtractedRequirement(
                        title=_title(s),
                        description=s,
                        priority="could" if LATER.search(s) else ("must" if FIRM.search(s) else "should"),
                        source_quote=s,
                        speaker=turn.speaker,
                        ambiguous=ambiguous,
                        clarification_question="What specifically should be different, with an example?" if ambiguous else "",
                    )
                )
            if DECISION.search(s):
                out.decisions.append(ExtractedItem(text=s, source_quote=s))
            if ASSUME.search(s):
                out.assumptions.append(ExtractedItem(text=s, source_quote=s))
            if DEPEND.search(s):
                out.dependencies.append(ExtractedItem(text=s, source_quote=s))
            if OPEN.search(s):
                out.open_questions.append(ExtractedItem(text=s, source_quote=s))
    out.requirements = _dedupe_reqs(out.requirements)
    return out


def _dedupe_reqs(reqs: list[ExtractedRequirement]) -> list[ExtractedRequirement]:
    kept: list[ExtractedRequirement] = []
    for r in reqs:
        if not any(jaccard(r.description, k.description) >= 0.5 for k in kept):
            kept.append(r)
    return kept


def _dedupe_items(items: list[ExtractedItem], threshold: float = 0.6) -> list[ExtractedItem]:
    kept: list[ExtractedItem] = []
    for it in items:
        if not any(jaccard(it.text, k.text) >= threshold for k in kept):
            kept.append(it)
    return kept


def merge(parts: list[Extraction]) -> Extraction:
    """Reduce step: merge segment extractions; near-duplicate requirements keep the longer description."""
    reqs: list[ExtractedRequirement] = []
    for part in parts:
        for r in part.requirements:
            match = next((k for k in reqs if jaccard(f"{k.title} {k.description}", f"{r.title} {r.description}") >= 0.5), None)
            if match is None:
                reqs.append(r)
            elif len(r.description) > len(match.description):
                match.description = r.description
    return Extraction(
        requirements=reqs,
        decisions=_dedupe_items([d for p in parts for d in p.decisions]),
        assumptions=_dedupe_items([d for p in parts for d in p.assumptions]),
        dependencies=_dedupe_items([d for p in parts for d in p.dependencies]),
        open_questions=_dedupe_items([d for p in parts for d in p.open_questions]),
    )


def finalize(
    extraction: Extraction, transcript: str, id_prefix: str = "R", start: int = 1
) -> tuple[list[Requirement], dict[str, list[Item]]]:
    """Assign IDs and verify every quote against the transcript."""
    reqs = [
        Requirement(**r.model_dump(), id=f"{id_prefix}{i}", quote_verified=quote_in_transcript(r.source_quote, transcript))
        for i, r in enumerate(extraction.requirements, start)
    ]

    def items(xs: list[ExtractedItem]) -> list[Item]:
        return [
            Item(**x.model_dump(), quote_verified=(not x.source_quote) or quote_in_transcript(x.source_quote, transcript))
            for x in xs
        ]

    return reqs, {
        "decisions": items(extraction.decisions),
        "assumptions": items(extraction.assumptions),
        "dependencies": items(extraction.dependencies),
        "open_questions": items(extraction.open_questions),
    }


async def extract(
    llm: LLMClient, transcript: str, company: str, long_chars: int, segment_chars: int
) -> tuple[Extraction, list[LLMCall], int]:
    """Returns (extraction, llm calls, number of segments). Raises LLMError for the caller to fall back."""
    segments = [transcript] if len(transcript) <= long_chars else segment(transcript, segment_chars)
    system = EXTRACTOR_SYSTEM.format(company=company)

    async def one(i: int, text: str) -> tuple[Extraction, LLMCall]:
        note = f" (segment {i + 1} of {len(segments)}; earlier segments are processed separately)" if len(segments) > 1 else ""
        return await llm.generate_json(
            tier="smart", system=system, prompt=EXTRACTOR_USER.format(segment_note=note, transcript=text), schema=Extraction
        )

    results = await asyncio.gather(*(one(i, s) for i, s in enumerate(segments)))
    extraction = merge([r[0] for r in results]) if len(results) > 1 else results[0][0]
    return extraction, [r[1] for r in results], len(segments)
