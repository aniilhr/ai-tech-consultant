"""Capability checker agent: one job - decide, per requirement, whether existing product features cover it.

It asks the Knowledge Brain (Task 1 API) and then judges the evidence. A deterministic guard enforces the
brief's rule "never invent product capabilities": without Brain evidence a requirement can never be marked
supported or partially supported."""

from __future__ import annotations

import asyncio
from typing import Any

from ..brain_client import BrainClient
from ..llm import LLMCall, LLMClient, LLMError
from ..models import CapabilityCheck, CapabilityJudgement, Evidence, Requirement, Status
from ..prompts import CAPABILITY_SYSTEM, CAPABILITY_USER


def brain_question(req: Requirement) -> str:
    return f"Can the platform do this with built-in features or settings (no custom code)? {req.title}: {req.description}"


def offline_status(req: Requirement, brain: dict[str, Any]) -> tuple[Status, str]:
    if req.ambiguous:
        return "needs_clarification", "The client's wording is too vague to match to a product feature."
    if brain.get("answered"):
        if float(brain.get("confidence") or 0) >= 0.75:
            return "supported_by_configuration", "Help-center evidence describes this feature (offline judgement; verify fit)."
        return "partially_supported", "Help-center evidence covers part of this (offline judgement; verify fit)."
    return "custom_development", "No help-center evidence found for this capability; treat as potential custom work."


def guard(status: Status, brain: dict[str, Any]) -> tuple[Status, str]:
    has_evidence = bool(brain.get("answered")) and bool(brain.get("citations"))
    if status in ("supported_by_configuration", "partially_supported") and not has_evidence:
        return "custom_development", "Downgraded: the model claimed support but the Knowledge Brain returned no evidence."
    return status, ""


async def check_one(
    llm: LLMClient, brain_client: BrainClient, req: Requirement, company: str
) -> tuple[CapabilityCheck, LLMCall | None, str | None]:
    question = brain_question(req)
    brain = await brain_client.ask(question)
    evidence = [
        Evidence(title=c.get("title", ""), url=c.get("url"), snippet=c.get("snippet", "")) for c in brain.get("citations", [])
    ]
    warning = None if brain.get("available", True) else brain.get("error", "Knowledge Brain unavailable")
    call: LLMCall | None = None
    features: list[str] = []
    mode = "offline"
    if llm.enabled:
        try:
            judgement, call = await llm.generate_json(
                tier="fast",
                system=CAPABILITY_SYSTEM.format(company=company),
                prompt=CAPABILITY_USER.format(
                    rid=req.id,
                    title=req.title,
                    description=req.description,
                    ambiguous=req.ambiguous,
                    answered=brain.get("answered", False),
                    answer=brain.get("answer", "") or "(none)",
                    evidence="\n".join(f"- {e.title}: {e.snippet}" for e in evidence) or "(no evidence)",
                ),
                schema=CapabilityJudgement,
            )
            status, rationale, features, mode = judgement.status, judgement.rationale, judgement.product_features, "live"
        except LLMError as exc:
            status, rationale = offline_status(req, brain)
            mode, warning = "fallback", f"Capability check for {req.id} used offline rules ({exc.kind})."
    else:
        status, rationale = offline_status(req, brain)
    status, note = guard(status, brain)
    return (
        CapabilityCheck(
            requirement_id=req.id,
            status=status,
            rationale=rationale,
            product_features=features,
            brain_question=question,
            brain_answered=bool(brain.get("answered")),
            brain_answer=str(brain.get("answer", ""))[:1200],
            evidence=evidence,
            mode=mode,
            guard_note=note,
        ),
        call,
        warning,
    )


async def check_all(
    llm: LLMClient, brain_client: BrainClient, reqs: list[Requirement], company: str, concurrency: int = 3
) -> tuple[dict[str, CapabilityCheck], list[LLMCall], list[str]]:
    sem = asyncio.Semaphore(concurrency)

    async def bounded(r: Requirement) -> tuple[CapabilityCheck, LLMCall | None, str | None]:
        async with sem:
            return await check_one(llm, brain_client, r, company)

    results = await asyncio.gather(*(bounded(r) for r in reqs))
    warnings = sorted({w for _, _, w in results if w})
    return {c.requirement_id: c for c, _, _ in results}, [call for _, call, _ in results if call], warnings
