"""Proposal writer agent: one job - present an approved-able scope to the client.

Only the executive summary is model-written. Tables, hours and change lists are rendered from structured data
by code, so numbers in the proposal always match the estimate. No prices are invented: hours only, plus an
indicative figure if (and only if) INDICATIVE_HOURLY_RATE is configured, labelled as not commercially approved."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from ..llm import LLMCall, LLMClient, LLMError
from ..models import ChangeReport, ScopeData
from ..prompts import PROPOSAL_SYSTEM

STATUS_LABEL = {
    "supported_by_configuration": "Existing feature (configuration)",
    "partially_supported": "Existing feature + customisation",
    "custom_development": "Custom development",
    "needs_clarification": "Needs clarification",
}


class SummaryOut(BaseModel):
    executive_summary: str = Field(description="Two short paragraphs")


def _h(x: float) -> str:
    return f"{x:g}"


def template_summary(client: str, scope: ScopeData) -> str:
    counts = {k: 0 for k in STATUS_LABEL}
    for c in scope.capability_checks.values():
        counts[c.status] += 1
    reuse = counts["supported_by_configuration"] + counts["partially_supported"]
    return (
        f"{client} needs {len(scope.requirements)} capabilities delivered. {reuse} of them can be delivered with existing "
        f"platform features (configuration or light customisation), {counts['custom_development']} need custom development, and "
        f"{counts['needs_clarification']} need clarification before they can be estimated.\n\n"
        f"The likely effort is {_h(scope.estimate.totals.likely)} hours (range {_h(scope.estimate.totals.low)}–"
        f"{_h(scope.estimate.totals.high)}). Open questions listed below must be answered before work starts."
    )


async def write_summary(llm: LLMClient, client: str, scope: ScopeData, company: str) -> tuple[str, LLMCall | None, str]:
    if not llm.enabled:
        return template_summary(client, scope), None, "offline"
    facts = template_summary(client, scope)
    reqs = "\n".join(
        f"- {r.title} ({scope.capability_checks[r.id].status if r.id in scope.capability_checks else '?'})"
        for r in scope.requirements
    )
    questions = "\n".join(f"- {q.text}" for q in scope.open_questions)
    try:
        out, call = await llm.generate_json(
            tier="smart",
            system=PROPOSAL_SYSTEM.format(company=company),
            prompt=f"Client: {client}\nFacts: {facts}\nRequirements:\n{reqs}\nOpen questions:\n{questions}",
            schema=SummaryOut,
        )
        return out.executive_summary.strip(), call, "live"
    except LLMError:
        return facts, None, "fallback"


def render(
    *,
    project_name: str,
    client: str,
    version: int,
    scope: ScopeData,
    summary: str,
    change_report: ChangeReport | None,
    hourly_rate: float | None,
) -> str:
    est = scope.estimate
    lines = [
        f"# Proposal: {project_name}",
        "",
        f"**Prepared for:** {client}  ",
        f"**Scope version:** v{version}  ",
        f"**Date:** {date.today().isoformat()}  ",
        "**Status:** DRAFT. Requires internal approval before it is sent to the client.",
        "",
        "## Executive summary",
        "",
        summary,
        "",
    ]
    if change_report:
        lines += [
            "## Changes since the approved baseline",
            "",
            f"Compared with scope v{change_report.baseline_version}: {change_report.added} added, "
            f"{change_report.modified} modified, {change_report.removed} removed.",
            "",
            "| Change | Requirement | Summary | Hours (likely) | Quote separately |",
            "|---|---|---|---|---|",
        ]
        for item in change_report.items:
            lines.append(
                f"| {item.change_type} | {item.title} | {item.summary[:140]} | {_h(item.hours_delta.likely)} | "
                f"{'Yes' if item.out_of_scope else 'No'} |"
            )
        lines += [
            "",
            f"**Out-of-scope work to be quoted separately:** {_h(change_report.out_of_scope_hours.likely)} likely hours "
            f"({_h(change_report.out_of_scope_hours.low)}–{_h(change_report.out_of_scope_hours.high)}).",
            "",
        ]
    lines += ["## Scope of work", "", "| ID | Requirement | Approach | Likely hours |", "|---|---|---|---|"]
    for r in scope.requirements:
        check = scope.capability_checks.get(r.id)
        approach = STATUS_LABEL[check.status] if check else "To be confirmed"
        hours = est.by_requirement.get(r.id)
        lines.append(f"| {r.id} | **{r.title}**: {r.description[:160]} | {approach} | {_h(hours.likely) if hours else '-'} |")
    reuse = [
        r
        for r in scope.requirements
        if (chk := scope.capability_checks.get(r.id)) and chk.status in ("supported_by_configuration", "partially_supported")
    ]
    if reuse:
        lines += ["", "### Delivered with existing platform features", ""]
        for r in reuse:
            c = scope.capability_checks[r.id]
            src = f" (source: {c.evidence[0].title})" if c.evidence else ""
            lines.append(f"- **{r.title}**: {c.rationale}{src}")
    for title, items in (
        ("Decisions", scope.decisions),
        ("Assumptions", scope.assumptions),
        ("Dependencies", scope.dependencies),
    ):
        if items:
            lines += ["", f"## {title}", ""] + [f"- {i.text}" for i in items]
    unclear = [
        r
        for r in scope.requirements
        if r.ambiguous or ((chk := scope.capability_checks.get(r.id)) and chk.status == "needs_clarification")
    ]
    if scope.open_questions or unclear:
        lines += ["", "## Open questions (to resolve before signing)", ""]
        lines += [f"- {i.text}" for i in scope.open_questions]
        lines += [
            f"- {r.title}: {r.clarification_question or 'Please describe the expected behaviour with an example.'}"
            for r in unclear
        ]
    lines += ["", "## Estimate", "", "| Task | Role | Low | Likely | High |", "|---|---|---|---|---|"]
    for t in est.tasks:
        lines.append(f"| {t.id} {t.title} | {t.role.replace('_', ' ')} | {_h(t.low)} | {_h(t.likely)} | {_h(t.high)} |")
    lines.append(f"| **Total** | | **{_h(est.totals.low)}** | **{_h(est.totals.likely)}** | **{_h(est.totals.high)}** |")
    lines += [""] + [f"_Note: {n}_" for n in est.notes]
    lines += ["", "## Commercials", ""]
    if hourly_rate:
        lines.append(
            f"Indicative only, NOT commercially approved: {_h(est.totals.likely)} h x {hourly_rate:g}/h = "
            f"{est.totals.likely * hourly_rate:,.0f} (range {est.totals.low * hourly_rate:,.0f}–{est.totals.high * hourly_rate:,.0f})."
        )
    else:
        lines.append("Pricing will be confirmed by the account manager based on the hours above.")
    lines += [
        "",
        "## Next steps",
        "",
        "1. Internal review and approval of this proposal.",
        "2. Client answers the open questions.",
        "3. Client approval of scope and commercials, then kickoff.",
    ]
    return "\n".join(lines)
