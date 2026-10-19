"""Change analyst agent: one job - compare a follow-up meeting with the APPROVED baseline.

The model (or the offline matcher) proposes changes; deterministic code applies them to the baseline, so the
new scope version is always "baseline + listed changes" and nothing else moves silently."""

from __future__ import annotations

import re

from ..llm import LLMCall, LLMClient
from ..models import ChangeAnalysis, ChangeDraft, ChangeItem, Requirement
from ..prompts import CHANGE_SYSTEM, CHANGE_USER
from ..text import parse_turns, quote_in_transcript, tokens
from .extractor import FEATURE, NEED, _sentences, _title

REMOVE = re.compile(
    r"\b(drop (it|this|that|the \w+)|decided to drop|remove(d)? (it|the [\w ]{1,40}) from|don't need it|do not need it|"
    r"no longer need|postpon\w+|is removed|out of this phase)\b",
    re.I,
)
MODIFY = re.compile(
    r"\b(instead|changed?|grew|rather than|isn't enough|not enough|anymore|third|more than|now about|switch)\b", re.I
)
EXTRA = re.compile(
    r"\b(quote (it|them)? ?(separately|as an extra)|outside (the|of the)? ?(original|agreed)? ?scope|not in the agreed scope|change request)\b",
    re.I,
)
NEW_HINT = re.compile(r"\b(new things?|we want a|we'd like a|also want|add a|another|is new|also new)\b", re.I)


def _match(sentence: str, baseline: list[Requirement]) -> tuple[Requirement | None, float]:
    """Best baseline requirement by shared words, weighting words that appear in few requirements (idf-like)."""
    docs = {r.id: tokens(f"{r.title} {r.description} {r.source_quote}") for r in baseline}
    df: dict[str, int] = {}
    for toks in docs.values():
        for t in toks:
            df[t] = df.get(t, 0) + 1
    # Only product-feature words and rare specific words (e.g. "dentrix") count; generic words like "site" do not.
    words = {
        t for t in tokens(sentence) if FEATURE.fullmatch(t) or FEATURE.fullmatch(t + "s") or (df.get(t) == 1 and len(t) >= 6)
    }
    best, best_score = None, 0.0
    for r in baseline:
        score = sum(1 / df[t] for t in words & docs[r.id])
        if score > best_score:
            best, best_score = r, score
    return best, best_score


def heuristic_changes(baseline: list[Requirement], transcript: str) -> ChangeAnalysis:
    """Offline matcher over client sentences: strict removal phrases, modification cues, and new-feature cues,
    matched to baseline requirements by rare shared words. Weaker than the model; labelled offline."""
    changes: list[ChangeDraft] = []
    seen: set[str] = set()
    turns = parse_turns(transcript)
    sentences = [(t, s) for t in turns for s in _sentences(t.text)]
    for i, (turn, s) in enumerate(sentences):
        if "client" not in turn.role.lower():
            continue
        best, score = _match(s, baseline)
        nearby = " ".join(x for _, x in sentences[i : i + 6])
        before = " ".join(x for _, x in sentences[max(0, i - 3) : i + 1])
        extra = bool(EXTRA.search(nearby))
        if REMOVE.search(s) and best and score >= 0.5 and best.id not in seen:
            changes.append(
                ChangeDraft(
                    change_type="removed", baseline_requirement_id=best.id, title=best.title, description=s, source_quote=s
                )
            )
            seen.add(best.id)
        elif MODIFY.search(s) and best and score >= 0.5 and best.id not in seen:
            changes.append(
                ChangeDraft(
                    change_type="modified",
                    baseline_requirement_id=best.id,
                    title=best.title,
                    description=s,
                    source_quote=s,
                    client_requested_extra_quote=extra,
                )
            )
            seen.add(best.id)
        elif FEATURE.search(s) and not REMOVE.search(s) and (NEW_HINT.search(before) or (NEED.search(s) and score < 0.5)):
            changes.append(
                ChangeDraft(
                    change_type="added", title=_title(s), description=s, source_quote=s, client_requested_extra_quote=extra
                )
            )
    return ChangeAnalysis(changes=changes)


async def analyze_changes(
    llm: LLMClient, baseline: list[Requirement], transcript: str, company: str
) -> tuple[ChangeAnalysis, LLMCall]:
    listing = "\n".join(f"- {r.id}: {r.title} - {r.description}" for r in baseline)
    return await llm.generate_json(
        tier="smart",
        system=CHANGE_SYSTEM.format(company=company),
        prompt=CHANGE_USER.format(baseline=listing, transcript=transcript),
        schema=ChangeAnalysis,
    )


def apply_changes(
    baseline: list[Requirement], analysis: ChangeAnalysis, transcript: str, version: int
) -> tuple[list[Requirement], list[ChangeItem]]:
    """Returns the new requirement list and one ChangeItem per non-trivial change (hours filled in later)."""
    by_id = {r.id: r for r in baseline}
    new_reqs = [r.model_copy(deep=True) for r in baseline]
    items: list[ChangeItem] = []
    next_id = 1 + max((int(re.sub(r"\D", "", r.id) or 0) for r in baseline), default=0)
    handled: set[str] = set()
    for c in analysis.changes:
        verified = quote_in_transcript(c.source_quote, transcript) if c.source_quote else False
        if c.change_type in ("modified", "removed", "unchanged"):
            if not c.baseline_requirement_id or c.baseline_requirement_id not in by_id or c.baseline_requirement_id in handled:
                continue  # unknown or duplicate reference: ignore rather than guess
            handled.add(c.baseline_requirement_id)
            if c.change_type == "unchanged":
                continue
            if c.change_type == "removed":
                new_reqs = [r for r in new_reqs if r.id != c.baseline_requirement_id]
                items.append(
                    ChangeItem(
                        change_type="removed",
                        requirement_id=c.baseline_requirement_id,
                        baseline_requirement_id=c.baseline_requirement_id,
                        title=by_id[c.baseline_requirement_id].title,
                        summary=c.description or "Removed by the client",
                        source_quote=c.source_quote,
                        quote_verified=verified,
                    )
                )
                continue
            for r in new_reqs:
                if r.id == c.baseline_requirement_id:
                    r.description = c.description or r.description
                    r.source_quote = c.source_quote or r.source_quote
                    r.quote_verified = verified
                    r.origin = f"change:v{version}"
            items.append(
                ChangeItem(
                    change_type="modified",
                    requirement_id=c.baseline_requirement_id,
                    baseline_requirement_id=c.baseline_requirement_id,
                    title=c.title or by_id[c.baseline_requirement_id].title,
                    summary=c.description,
                    source_quote=c.source_quote,
                    quote_verified=verified,
                    out_of_scope=c.client_requested_extra_quote,
                )
            )
        else:  # added
            rid = f"R{next_id}"
            next_id += 1
            new_reqs.append(
                Requirement(
                    id=rid,
                    title=c.title,
                    description=c.description or c.title,
                    priority="should",
                    source_quote=c.source_quote,
                    quote_verified=verified,
                    origin=f"change:v{version}",
                )
            )
            items.append(
                ChangeItem(
                    change_type="added",
                    requirement_id=rid,
                    baseline_requirement_id=None,
                    title=c.title,
                    summary=c.description,
                    source_quote=c.source_quote,
                    quote_verified=verified,
                    out_of_scope=True,
                )
            )
    return new_reqs, items
