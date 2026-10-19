"""Estimator agent: one job - turn requirements + delivery approach into engineering tasks with low / likely /
high hours. Code, not the model, enforces ordering, sanity bands per approach, and PM/QA overheads."""

from __future__ import annotations

from ..llm import LLMCall, LLMClient, LLMError
from ..models import CapabilityCheck, Estimate, EstimateOut, Requirement, Status, Task, TaskDraft, Totals
from ..prompts import ESTIMATOR_SYSTEM, ESTIMATOR_USER

# Heuristic hours per approach (low, likely, high) used offline and as sanity bands for model output.
DEFAULT_HOURS: dict[Status, tuple[float, float, float]] = {
    "supported_by_configuration": (1, 2, 4),
    "partially_supported": (4, 8, 14),
    "custom_development": (12, 24, 48),
    "needs_clarification": (1, 2, 3),
}
BAND_MAX_HIGH: dict[Status, float] = {
    "supported_by_configuration": 16,
    "partially_supported": 60,
    "custom_development": 240,
    "needs_clarification": 8,
}
QA_SHARE = 0.15
PM_SHARE = 0.10


def heuristic_tasks(req: Requirement, status: Status) -> list[TaskDraft]:
    low, likely, high = DEFAULT_HOURS[status]
    if status == "needs_clarification":
        return [
            TaskDraft(
                title=f"Discovery: clarify '{req.title}'",
                role="consultant",
                low=low,
                likely=likely,
                high=high,
                notes="Estimate the build after the client answers the clarification question.",
            )
        ]
    if status == "supported_by_configuration":
        return [TaskDraft(title=f"Configure: {req.title}", role="consultant", low=low, likely=likely, high=high)]
    return [
        TaskDraft(title=f"Design approach: {req.title}", role="consultant", low=1, likely=2, high=3),
        TaskDraft(title=f"Build: {req.title}", role="developer", low=low, likely=likely, high=high),
    ]


def sanitize(task: TaskDraft, status: Status) -> TaskDraft:
    low, likely, high = sorted([max(0.0, task.low), max(0.0, task.likely), max(0.0, task.high)])
    cap = BAND_MAX_HIGH[status]
    if high > cap:  # model went off the rails for this kind of work: clamp and say so
        scale = cap / high
        low, likely, high = round(low * scale, 1), round(likely * scale, 1), cap
        task.notes = (task.notes + " " if task.notes else "") + f"(clamped to {cap:g}h band for {status})"
    task.low, task.likely, task.high = round(low, 1), round(likely, 1), round(high, 1)
    return task


def build_estimate(
    reqs: list[Requirement], checks: dict[str, CapabilityCheck], drafts: dict[str, list[TaskDraft]], method: str
) -> Estimate:
    tasks: list[Task] = []
    by_req: dict[str, Totals] = {}
    n = 1
    for r in reqs:
        status = checks[r.id].status if r.id in checks else "custom_development"
        req_tasks = drafts.get(r.id) or heuristic_tasks(r, status)
        totals = Totals()
        for t in req_tasks:
            t = sanitize(t, status)
            tasks.append(Task(**t.model_dump(), id=f"T{n}", requirement_id=r.id))
            n += 1
            totals.low += t.low
            totals.likely += t.likely
            totals.high += t.high
        by_req[r.id] = Totals(low=round(totals.low, 1), likely=round(totals.likely, 1), high=round(totals.high, 1))
    build = Totals(low=sum(t.low for t in tasks), likely=sum(t.likely for t in tasks), high=sum(t.high for t in tasks))
    for title, role, share in (
        ("QA, UAT and launch checks", "qa", QA_SHARE),
        ("Project management", "project_manager", PM_SHARE),
    ):
        if build.likely:
            tasks.append(
                Task(
                    id=f"T{n}",
                    requirement_id=None,
                    title=title,
                    role=role,  # type: ignore[arg-type]
                    low=round(build.low * share, 1),
                    likely=round(build.likely * share, 1),
                    high=round(build.high * share, 1),
                    notes=f"{int(share * 100)}% of build hours",
                )
            )
            n += 1
    totals = Totals(
        low=round(sum(t.low for t in tasks), 1),
        likely=round(sum(t.likely for t in tasks), 1),
        high=round(sum(t.high for t in tasks), 1),
    )
    notes = []
    unclear = [r.id for r in reqs if checks.get(r.id) and checks[r.id].status == "needs_clarification"]
    if unclear:
        notes.append(f"{', '.join(unclear)} only include discovery time; build hours follow after clarification.")
    return Estimate(tasks=tasks, totals=totals, by_requirement=by_req, method=method, notes=notes)


async def estimate(
    llm: LLMClient,
    reqs: list[Requirement],
    checks: dict[str, CapabilityCheck],
    company: str,
    fixed: dict[str, list[TaskDraft]] | None = None,
) -> tuple[Estimate, LLMCall | None, str | None]:
    """`fixed` holds already-approved tasks (unchanged baseline requirements); only the rest is estimated."""
    fixed = {k: [t.model_copy() for t in v] for k, v in (fixed or {}).items()}
    todo = [r for r in reqs if r.id not in fixed]
    if not todo:
        return build_estimate(reqs, checks, fixed, "baseline tasks reused"), None, None
    if not llm.enabled:
        return build_estimate(reqs, checks, fixed, "heuristic (offline)"), None, None
    lines = []
    for r in todo:
        c = checks.get(r.id)
        status = c.status if c else "custom_development"
        features = f"; existing features: {', '.join(c.product_features)}" if c and c.product_features else ""
        lines.append(f"- {r.id} [{status}] {r.title}: {r.description}{features}")
    try:
        out, call = await llm.generate_json(
            tier="smart",
            system=ESTIMATOR_SYSTEM.format(company=company),
            prompt=ESTIMATOR_USER.format(requirements="\n".join(lines)),
            schema=EstimateOut,
        )
    except LLMError as exc:
        return (
            build_estimate(reqs, checks, fixed, "heuristic (model unavailable)"),
            None,
            f"Estimator fell back to heuristics ({exc.kind}).",
        )
    wanted = {r.id for r in todo}
    drafts = {**fixed, **{i.requirement_id: i.tasks for i in out.items if i.tasks and i.requirement_id in wanted}}
    missing = [r.id for r in todo if r.id not in drafts]
    est = build_estimate(reqs, checks, drafts, f"model ({call.model})")
    if missing:
        est.notes.append(f"Model gave no tasks for {', '.join(missing)}; heuristic hours used.")
    return est, call, None
