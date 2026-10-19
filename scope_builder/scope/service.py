"""ScopeService - the "team lead" that runs the agents in order and owns versioning and approvals.

Initial meeting:   Extractor -> Capability checker (calls Knowledge Brain) -> Estimator -> scope vN (draft)
Follow-up meeting: Change analyst (vs APPROVED baseline) -> apply changes in code -> Capability checker and
                   Estimator only for added/modified requirements -> scope vN+1 (draft) + change report
Proposal:          Proposal writer renders a draft; a human must approve the scope AND the proposal before the
                   proposal is marked ready to send. Nothing is ever sent automatically."""

from __future__ import annotations

import json
import logging
from typing import Any

from .agents import capability, change, estimator, extractor, proposal
from .brain_client import BrainClient
from .config import SAMPLES_DIR, Settings
from .db import Database, now_iso
from .llm import LLMCall, LLMClient, LLMError
from .models import ChangeAnalysis, ChangeReport, Extraction, Item, ScopeData, TaskDraft, Totals
from .prompts import PROMPT_VERSION
from .text import jaccard, parse_front_matter

log = logging.getLogger("scope.service")


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


def _llm_summary(provider: str, calls: list[LLMCall], mode: str, **extra: Any) -> dict[str, Any]:
    costs = [c.cost_usd for c in calls if c.cost_usd is not None]
    return {
        "mode": mode,
        "provider": provider,
        "prompt_version": PROMPT_VERSION,
        "calls": len(calls),
        "models": sorted({c.model for c in calls}),
        "input_tokens": sum(c.input_tokens for c in calls),
        "output_tokens": sum(c.output_tokens for c in calls),
        "cost_usd": round(sum(costs), 6) if costs else None,
        "latency_ms": sum(c.latency_ms for c in calls),
        **extra,
    }


def _merge_items(base: list[Item], new: list[Item]) -> list[Item]:
    out = list(base)
    for it in new:
        if not any(jaccard(it.text, b.text) >= 0.6 for b in out):
            out.append(it)
    return out


class ScopeService:
    def __init__(self, db: Database, llm: LLMClient, brain: BrainClient, settings: Settings) -> None:
        self.db = db
        self.llm = llm
        self.brain = brain
        self.s = settings

    # ------------------------------------------------------------------ projects & transcripts
    def create_project(self, name: str, client: str) -> int:
        pid = self.db.execute("INSERT INTO projects(name, client, created_at) VALUES(?,?,?)", (name, client, now_iso()))
        self.db.audit(pid, "project", pid, "created", None)
        return pid

    def list_projects(self) -> list[dict[str, Any]]:
        return self.db.query(
            """SELECT p.*, (SELECT COUNT(*) FROM transcripts t WHERE t.project_id=p.id) AS transcripts,
                      (SELECT MAX(version) FROM scopes s WHERE s.project_id=p.id) AS latest_version,
                      (SELECT version FROM scopes s WHERE s.project_id=p.id AND s.status='approved') AS baseline_version,
                      (SELECT COUNT(*) FROM proposals pr JOIN scopes s ON s.id=pr.scope_id WHERE s.project_id=p.id AND pr.status='approved') AS approved_proposals
               FROM projects p ORDER BY p.id DESC"""
        )

    def get_project(self, project_id: int) -> dict[str, Any]:
        project = self.db.one("SELECT * FROM projects WHERE id=?", (project_id,))
        if not project:
            raise NotFound(f"Project {project_id} not found")
        scopes = []
        for s in self.db.query("SELECT * FROM scopes WHERE project_id=? ORDER BY version DESC", (project_id,)):
            data = ScopeData.model_validate_json(s["data"])
            scopes.append(
                {
                    "id": s["id"],
                    "version": s["version"],
                    "status": s["status"],
                    "transcript_id": s["transcript_id"],
                    "created_at": s["created_at"],
                    "approved_by": s["approved_by"],
                    "requirements": len(data.requirements),
                    "totals": data.estimate.totals.model_dump(),
                    "is_change": bool(s["change_report"]),
                    "llm_mode": data.llm.get("mode"),
                }
            )
        return {
            "project": project,
            "transcripts": self.db.query(
                "SELECT id, title, kind, length(text) AS chars, created_at FROM transcripts WHERE project_id=? ORDER BY id",
                (project_id,),
            ),
            "scopes": scopes,
            "proposals": self.db.query(
                """SELECT pr.id, pr.scope_id, s.version, pr.status, pr.edited, pr.reviewer, pr.created_at, pr.reviewed_at
                   FROM proposals pr JOIN scopes s ON s.id=pr.scope_id WHERE s.project_id=? ORDER BY pr.id DESC""",
                (project_id,),
            ),
            "audit": self.db.query("SELECT * FROM audit WHERE project_id=? ORDER BY id DESC LIMIT 50", (project_id,)),
        }

    def add_transcript(self, project_id: int, title: str, kind: str, text: str) -> int:
        if not self.db.one("SELECT 1 FROM projects WHERE id=?", (project_id,)):
            raise NotFound(f"Project {project_id} not found")
        tid = self.db.execute(
            "INSERT INTO transcripts(project_id, title, kind, text, created_at) VALUES(?,?,?,?,?)",
            (project_id, title, kind, text, now_iso()),
        )
        self.db.audit(project_id, "transcript", tid, "added", None, title)
        return tid

    def get_transcript(self, transcript_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM transcripts WHERE id=?", (transcript_id,))
        if not row:
            raise NotFound(f"Transcript {transcript_id} not found")
        return row

    @staticmethod
    def samples() -> list[dict[str, Any]]:
        out = []
        for f in sorted(SAMPLES_DIR.glob("*.md")):
            meta, body = parse_front_matter(f.read_text())
            out.append(
                {
                    "id": f.stem,
                    "title": meta.get("title", f.stem),
                    "client": meta.get("client", ""),
                    "kind": meta.get("kind", "initial"),
                    "date": meta.get("date"),
                    "note": meta.get("note"),
                    "chars": len(body),
                }
            )
        return out

    def load_sample(self, project_id: int, sample_id: str) -> int:
        path = SAMPLES_DIR / f"{sample_id}.md"
        if not path.exists() or path.parent != SAMPLES_DIR:
            raise NotFound(f"Sample {sample_id} not found")
        meta, body = parse_front_matter(path.read_text())
        return self.add_transcript(project_id, meta.get("title", sample_id), meta.get("kind", "initial"), body.strip())

    def seed_demo(self) -> list[int]:
        ids = []
        by_client: dict[str, list[dict[str, Any]]] = {}
        for s in self.samples():
            by_client.setdefault(s["client"] or s["id"], []).append(s)
        for client, samples in by_client.items():
            pid = self.create_project(f"{client} directory", client)
            for s in samples:
                self.load_sample(pid, s["id"])
            ids.append(pid)
        return ids

    # ------------------------------------------------------------------ analysis
    def baseline(self, project_id: int) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM scopes WHERE project_id=? AND status='approved'", (project_id,))

    async def analyze(self, project_id: int, transcript_id: int) -> dict[str, Any]:
        t = self.get_transcript(transcript_id)
        if t["project_id"] != project_id:
            raise NotFound("Transcript does not belong to this project")
        base = self.baseline(project_id)
        if t["kind"] == "follow_up":
            if not base:
                raise Conflict("Approve a baseline scope before analysing a follow-up meeting.")
            scope, report = await self._analyze_followup(base, t["text"])
        else:
            scope, report = await self._analyze_initial(t["text"]), None
        version = (
            int((self.db.one("SELECT MAX(version) v FROM scopes WHERE project_id=?", (project_id,)) or {}).get("v") or 0) + 1
        )
        if report:
            report.baseline_version = base["version"] if base else 0
        sid = self.db.execute(
            """INSERT INTO scopes(project_id, version, transcript_id, parent_scope_id, status, data, change_report, created_at)
               VALUES(?,?,?,?, 'draft', ?,?,?)""",
            (
                project_id,
                version,
                transcript_id,
                base["id"] if (base and report) else None,
                scope.model_dump_json(),
                report.model_dump_json() if report else None,
                now_iso(),
            ),
        )
        self.db.audit(
            project_id,
            "scope",
            sid,
            "generated",
            "agents",
            f"v{version} from transcript {transcript_id} ({scope.llm.get('mode')})",
        )
        return self.get_scope(sid)

    async def _extract(self, text: str, calls: list[LLMCall], warnings: list[str]) -> tuple[Extraction, str, int]:
        if self.llm.enabled:
            try:
                ex, ex_calls, segments = await extractor.extract(
                    self.llm, text, self.s.company_name, self.s.long_transcript_chars, self.s.segment_chars
                )
                calls.extend(ex_calls)
                return ex, "live", segments
            except LLMError as exc:
                warnings.append(f"Extractor model unavailable ({exc.kind}: {exc}); used offline keyword extraction.")
                return extractor.heuristic_extract(text), "fallback", 1
        return extractor.heuristic_extract(text), "offline", 1

    async def _analyze_initial(self, text: str) -> ScopeData:
        calls: list[LLMCall] = []
        warnings: list[str] = []
        ex, mode, segments = await self._extract(text, calls, warnings)
        reqs, items = extractor.finalize(ex, text)
        unverified = [r.id for r in reqs if not r.quote_verified]
        if unverified:
            warnings.append(f"Quotes for {', '.join(unverified)} were not found in the transcript; review before approving.")
        checks, c_calls, c_warn = await capability.check_all(
            self.llm, self.brain, reqs, self.s.company_name, self.s.brain_concurrency
        )
        calls += c_calls
        warnings += c_warn
        est, e_call, e_warn = await estimator.estimate(self.llm, reqs, checks, self.s.company_name)
        if e_call:
            calls.append(e_call)
        if e_warn:
            warnings.append(e_warn)
        return ScopeData(
            requirements=reqs,
            capability_checks=checks,
            estimate=est,
            warnings=warnings,
            llm=_llm_summary(self.llm.provider, calls, mode, segments=segments),
            **items,
        )

    async def _analyze_followup(self, base_row: dict[str, Any], text: str) -> tuple[ScopeData, ChangeReport]:
        base = ScopeData.model_validate_json(base_row["data"])
        version = int(base_row["version"]) + 1
        calls: list[LLMCall] = []
        warnings: list[str] = []
        mode = "offline"
        analysis: ChangeAnalysis
        if self.llm.enabled:
            try:
                analysis, call = await change.analyze_changes(self.llm, base.requirements, text, self.s.company_name)
                calls.append(call)
                mode = "live"
            except LLMError as exc:
                warnings.append(f"Change analyst model unavailable ({exc.kind}); used offline matching.")
                analysis, mode = change.heuristic_changes(base.requirements, text), "fallback"
        else:
            analysis = change.heuristic_changes(base.requirements, text)
        new_reqs, items = change.apply_changes(base.requirements, analysis, text, version)
        touched = {i.requirement_id for i in items if i.change_type in ("added", "modified")}

        # New decisions / questions etc. from the follow-up are merged into the baseline lists.
        ex, _, _ = await self._extract(text, calls, warnings)
        _, new_items = extractor.finalize(Extraction(**{**ex.model_dump(), "requirements": []}), text)

        checks = {
            rid: c for rid, c in base.capability_checks.items() if rid not in touched and any(r.id == rid for r in new_reqs)
        }
        new_checks, c_calls, c_warn = await capability.check_all(
            self.llm, self.brain, [r for r in new_reqs if r.id in touched], self.s.company_name, self.s.brain_concurrency
        )
        checks.update(new_checks)
        calls += c_calls
        warnings += c_warn
        fixed = {
            rid: [
                TaskDraft.model_validate(t.model_dump(exclude={"id", "requirement_id"}))
                for t in base.estimate.tasks
                if t.requirement_id == rid
            ]
            for rid in base.estimate.by_requirement
            if rid not in touched and any(r.id == rid for r in new_reqs)
        }
        est, e_call, e_warn = await estimator.estimate(self.llm, new_reqs, checks, self.s.company_name, fixed=fixed)
        if e_call:
            calls.append(e_call)
        if e_warn:
            warnings.append(e_warn)

        zero = Totals()
        for item in items:
            before = base.estimate.by_requirement.get(item.baseline_requirement_id or "", zero)
            after = est.by_requirement.get(item.requirement_id, zero) if item.change_type != "removed" else zero
            item.hours_delta = Totals(
                low=round(after.low - before.low, 1),
                likely=round(after.likely - before.likely, 1),
                high=round(after.high - before.high, 1),
            )
            if item.change_type == "modified" and item.hours_delta.likely > 0:
                item.out_of_scope = True
        oos = [i for i in items if i.out_of_scope]
        report = ChangeReport(
            baseline_scope_id=int(base_row["id"]),
            baseline_version=int(base_row["version"]),
            items=items,
            added=sum(i.change_type == "added" for i in items),
            modified=sum(i.change_type == "modified" for i in items),
            removed=sum(i.change_type == "removed" for i in items),
            out_of_scope_hours=Totals(
                low=round(sum(i.hours_delta.low for i in oos), 1),
                likely=round(sum(i.hours_delta.likely for i in oos), 1),
                high=round(sum(i.hours_delta.high for i in oos), 1),
            ),
            total_delta=Totals(
                low=round(est.totals.low - base.estimate.totals.low, 1),
                likely=round(est.totals.likely - base.estimate.totals.likely, 1),
                high=round(est.totals.high - base.estimate.totals.high, 1),
            ),
        )
        scope = ScopeData(
            requirements=new_reqs,
            decisions=_merge_items(base.decisions, new_items["decisions"]),
            assumptions=_merge_items(base.assumptions, new_items["assumptions"]),
            dependencies=_merge_items(base.dependencies, new_items["dependencies"]),
            open_questions=_merge_items(base.open_questions, new_items["open_questions"]),
            capability_checks=checks,
            estimate=est,
            warnings=warnings,
            llm=_llm_summary(self.llm.provider, calls, mode),
        )
        return scope, report

    def get_scope(self, scope_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM scopes WHERE id=?", (scope_id,))
        if not row:
            raise NotFound(f"Scope {scope_id} not found")
        return {
            **{k: v for k, v in row.items() if k not in ("data", "change_report")},
            "data": ScopeData.model_validate_json(row["data"]).model_dump(),
            "change_report": ChangeReport.model_validate_json(row["change_report"]).model_dump()
            if row["change_report"]
            else None,
        }

    def approve_scope(self, scope_id: int, approver: str, note: str | None) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM scopes WHERE id=?", (scope_id,))
        if not row:
            raise NotFound(f"Scope {scope_id} not found")
        if row["status"] != "draft":
            raise Conflict(f"Scope is {row['status']}; only drafts can be approved")
        self.db.execute("UPDATE scopes SET status='superseded' WHERE project_id=? AND status='approved'", (row["project_id"],))
        self.db.execute(
            "UPDATE scopes SET status='approved', approved_by=?, approved_at=? WHERE id=?", (approver, now_iso(), scope_id)
        )
        self.db.audit(row["project_id"], "scope", scope_id, "approved_as_baseline", approver, note)
        return self.get_scope(scope_id)

    # ------------------------------------------------------------------ proposals
    async def generate_proposal(self, scope_id: int) -> dict[str, Any]:
        row = self.db.one(
            "SELECT s.*, p.name, p.client FROM scopes s JOIN projects p ON p.id=s.project_id WHERE s.id=?", (scope_id,)
        )
        if not row:
            raise NotFound(f"Scope {scope_id} not found")
        scope = ScopeData.model_validate_json(row["data"])
        report = ChangeReport.model_validate_json(row["change_report"]) if row["change_report"] else None
        summary, call, mode = await proposal.write_summary(self.llm, row["client"], scope, self.s.company_name)
        md = proposal.render(
            project_name=row["name"],
            client=row["client"],
            version=row["version"],
            scope=scope,
            summary=summary,
            change_report=report,
            hourly_rate=self.s.indicative_hourly_rate,
        )
        llm = _llm_summary(self.llm.provider, [call] if call else [], mode)
        pid = self.db.execute(
            "INSERT INTO proposals(scope_id, markdown, status, llm, created_at) VALUES(?,?, 'draft', ?, ?)",
            (scope_id, md, json.dumps(llm), now_iso()),
        )
        self.db.audit(row["project_id"], "proposal", pid, "generated", "agents", f"for scope v{row['version']}")
        return self.get_proposal(pid)

    def get_proposal(self, proposal_id: int) -> dict[str, Any]:
        row = self.db.one(
            "SELECT pr.*, s.version, s.status AS scope_status, s.project_id FROM proposals pr JOIN scopes s ON s.id=pr.scope_id WHERE pr.id=?",
            (proposal_id,),
        )
        if not row:
            raise NotFound(f"Proposal {proposal_id} not found")
        return {**row, "ready_to_send": row["status"] == "approved"}

    def update_proposal(self, proposal_id: int, markdown: str, editor: str) -> dict[str, Any]:
        p = self.get_proposal(proposal_id)
        if p["status"] != "draft":
            raise Conflict(f"Proposal is {p['status']}; only drafts can be edited")
        self.db.execute("UPDATE proposals SET markdown=?, edited=1 WHERE id=?", (markdown, proposal_id))
        self.db.audit(p["project_id"], "proposal", proposal_id, "edited", editor)
        return self.get_proposal(proposal_id)

    def approve_proposal(self, proposal_id: int, approver: str, note: str | None) -> dict[str, Any]:
        p = self.get_proposal(proposal_id)
        if p["status"] != "draft":
            raise Conflict(f"Proposal is {p['status']}")
        if p["scope_status"] != "approved":
            raise Conflict("Approve the scope version first; a proposal can only be sent for an approved scope.")
        self.db.execute(
            "UPDATE proposals SET status='approved', reviewer=?, review_note=?, reviewed_at=? WHERE id=?",
            (approver, note, now_iso(), proposal_id),
        )
        self.db.audit(p["project_id"], "proposal", proposal_id, "approved_ready_to_send", approver, note)
        return self.get_proposal(proposal_id)

    def reject_proposal(self, proposal_id: int, reviewer: str, reason: str) -> dict[str, Any]:
        p = self.get_proposal(proposal_id)
        if p["status"] != "draft":
            raise Conflict(f"Proposal is {p['status']}")
        self.db.execute(
            "UPDATE proposals SET status='rejected', reviewer=?, review_note=?, reviewed_at=? WHERE id=?",
            (reviewer, reason, now_iso(), proposal_id),
        )
        self.db.audit(p["project_id"], "proposal", proposal_id, "rejected", reviewer, reason)
        return self.get_proposal(proposal_id)
