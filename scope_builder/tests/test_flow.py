from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scope.api import create_app
from scope.service import Conflict, ScopeService

from .conftest import FakeBrain, FakeGemini, settings


async def demo(svc: ScopeService) -> tuple[int, list[dict[str, Any]]]:
    pid = svc.seed_demo()[0]
    return pid, svc.get_project(pid)["transcripts"]


async def test_offline_end_to_end_with_change_report_and_approvals(
    make_service: Callable[..., ScopeService], brain: FakeBrain
) -> None:
    svc = make_service()
    pid, transcripts = await demo(svc)
    initial, follow_up = transcripts[0]["id"], transcripts[1]["id"]

    with pytest.raises(Conflict):  # follow-up needs an approved baseline
        await svc.analyze(pid, follow_up)

    v1 = await svc.analyze(pid, initial)
    data = v1["data"]
    assert v1["status"] == "draft" and v1["version"] == 1
    assert data["llm"]["mode"] == "offline"
    assert brain.questions and all(q["source"] == "scope" and q["create_escalation"] is False for q in brain.questions)
    statuses = {c["status"] for c in data["capability_checks"].values()}
    assert {"supported_by_configuration", "custom_development", "needs_clarification"} <= statuses
    assert data["estimate"]["totals"]["low"] <= data["estimate"]["totals"]["likely"] <= data["estimate"]["totals"]["high"]

    # A proposal for an unapproved scope can be drafted but not approved.
    p1 = await svc.generate_proposal(v1["id"])
    assert p1["status"] == "draft" and "DRAFT" in p1["markdown"] and p1["ready_to_send"] is False
    with pytest.raises(Conflict):
        svc.approve_proposal(p1["id"], "anil", None)

    svc.approve_scope(v1["id"], "anil", "baseline agreed")
    v2 = await svc.analyze(pid, follow_up)
    report = v2["change_report"]
    assert v2["version"] == 2 and report["baseline_version"] == 1
    kinds = {(i["change_type"]) for i in report["items"]}
    assert {"added", "modified", "removed"} <= kinds
    assert all(i["out_of_scope"] for i in report["items"] if i["change_type"] == "added")
    assert report["out_of_scope_hours"]["likely"] > 0
    titles = " ".join(i["summary"].lower() for i in report["items"])
    assert "spanish" in titles and "dentrix" in titles and "newsletter" in titles

    proposal = await svc.generate_proposal(v2["id"])
    assert "Changes since the approved baseline" in proposal["markdown"]
    assert "$" not in proposal["markdown"]  # no invented prices
    edited = svc.update_proposal(proposal["id"], proposal["markdown"] + "\n\nReviewed by Anil.", "anil")
    assert edited["edited"] == 1
    with pytest.raises(Conflict):
        svc.approve_proposal(proposal["id"], "anil", None)  # v2 not approved yet
    svc.approve_scope(v2["id"], "anil", None)
    approved = svc.approve_proposal(proposal["id"], "anil", "ok to send")
    assert approved["ready_to_send"] is True
    with pytest.raises(Conflict):
        svc.update_proposal(proposal["id"], "x" * 30, "anil")
    assert svc.get_scope(v1["id"])["status"] == "superseded"
    actions = [a["action"] for a in svc.get_project(pid)["audit"]]
    assert "approved_ready_to_send" in actions and "approved_as_baseline" in actions


async def test_brain_outage_means_no_capability_claims(make_service: Callable[..., ScopeService], brain: FakeBrain) -> None:
    brain.down = True
    svc = make_service()
    pid, transcripts = await demo(svc)
    v1 = await svc.analyze(pid, transcripts[0]["id"])
    statuses = {c["status"] for c in v1["data"]["capability_checks"].values()}
    assert statuses <= {"custom_development", "needs_clarification"}
    assert any("unreachable" in w for w in v1["data"]["warnings"])


def scripted(gemini: FakeGemini, invented_quote: bool = False) -> None:
    def extraction(prompt: str) -> dict[str, Any]:
        reqs = [
            {
                "title": "Two paid membership tiers",
                "description": "Basic and featured listings",
                "priority": "must",
                "source_quote": "We were thinking two levels: a basic listing and a featured listing",
                "speaker": "Mark",
            },
            {
                "title": "Smarter search",
                "description": "Search better than competitor",
                "priority": "should",
                "source_quote": "I'd like ours to be smarter somehow.",
                "ambiguous": True,
                "clarification_question": "Which searches fail today?",
            },
            {
                "title": "Dentrix slot sync",
                "description": "Pull appointment slots from Dentrix",
                "priority": "could",
                "source_quote": "pull the dentist's available appointment slots from Dentrix automatically",
            },
        ]
        if invented_quote:
            reqs.append(
                {"title": "Crypto payments", "description": "Accept bitcoin", "source_quote": "We must accept bitcoin payments."}
            )
        return {
            "requirements": reqs,
            "decisions": [{"text": "Use Stripe", "source_quote": "Let's go with Stripe for this too."}],
            "assumptions": [],
            "dependencies": [{"text": "Spreadsheet by Nov 15", "source_quote": "I can get it to you by November fifteenth."}],
            "open_questions": [{"text": "Review removal policy?", "source_quote": ""}],
        }

    def capability(prompt: str) -> dict[str, Any]:
        # The model (wrongly) claims everything is supported; the code guard must fix Dentrix.
        return {
            "status": "needs_clarification" if "Ambiguous: True" in prompt else "supported_by_configuration",
            "rationale": "Evidence shows this.",
            "product_features": ["Membership Plans"],
        }

    def estimator(prompt: str) -> dict[str, Any]:
        ids = [line.split(" ")[1] for line in prompt.splitlines() if line.startswith("- R")]
        return {
            "items": [
                {
                    "requirement_id": rid,
                    "tasks": [{"title": f"Build {rid}", "role": "developer", "low": 3, "likely": 6, "high": 10}],
                }
                for rid in ids
            ]
        }

    def change(prompt: str) -> dict[str, Any]:
        return {
            "changes": [
                {
                    "change_type": "modified",
                    "baseline_requirement_id": "R1",
                    "title": "Three membership tiers",
                    "description": "Add Enterprise tier for multi-location groups",
                    "source_quote": "They want a third membership level for dental groups with multiple locations.",
                },
                {
                    "change_type": "removed",
                    "baseline_requirement_id": "R3",
                    "title": "Dentrix slot sync",
                    "description": "Postponed to phase two",
                    "source_quote": "So please remove the Dentrix integration from this phase.",
                },
                {
                    "change_type": "added",
                    "title": "Spanish site",
                    "description": "Full Spanish version",
                    "source_quote": "We want a Spanish version of the whole site",
                    "client_requested_extra_quote": True,
                },
                {
                    "change_type": "removed",
                    "baseline_requirement_id": "R99",
                    "title": "Ghost",
                    "description": "Unknown ID must be ignored",
                },
            ]
        }

    gemini.responders = {
        "senior technical consultant reading": extraction,
        "You decide how a client requirement": capability,
        "delivery lead estimating": estimator,
        "You compare a follow-up": change,
        "executive summary": lambda p: {"executive_summary": "BrightSmile needs a dentist directory."},
    }


async def test_live_flow_uses_agents_and_guards(make_service: Callable[..., ScopeService], gemini: FakeGemini) -> None:
    scripted(gemini, invented_quote=True)
    svc = make_service(gemini_api_key="k", model_prices_json='{"gemini-3.8-flash": [1, 2]}')
    pid, transcripts = await demo(svc)
    v1 = await svc.analyze(pid, transcripts[0]["id"])
    d = v1["data"]
    assert d["llm"]["mode"] == "live" and d["llm"]["calls"] >= 5 and d["llm"]["cost_usd"] is not None
    by_title = {r["title"]: r for r in d["requirements"]}
    assert by_title["Crypto payments"]["quote_verified"] is False
    assert any("not found in the transcript" in w for w in d["warnings"])
    dentrix = d["capability_checks"][by_title["Dentrix slot sync"]["id"]]
    assert dentrix["status"] == "custom_development" and dentrix["guard_note"]  # model's claim downgraded
    assert d["capability_checks"][by_title["Smarter search"]["id"]]["status"] == "needs_clarification"
    assert d["capability_checks"][by_title["Two paid membership tiers"]["id"]]["status"] == "supported_by_configuration"

    svc.approve_scope(v1["id"], "anil", None)
    v2 = await svc.analyze(pid, transcripts[1]["id"])
    r = v2["change_report"]
    assert (r["added"], r["modified"], r["removed"]) == (1, 1, 1)  # unknown R99 ignored
    ids = [x["id"] for x in v2["data"]["requirements"]]
    assert "R3" not in ids and "R5" in ids  # Dentrix removed; Spanish site added with a fresh ID
    # unchanged requirement keeps its approved estimate (baseline tasks reused)
    assert v2["data"]["estimate"]["by_requirement"]["R2"] == v1["data"]["estimate"]["by_requirement"]["R2"]
    proposal = await svc.generate_proposal(v2["id"])
    assert "BrightSmile needs a dentist directory." in proposal["markdown"]


async def test_long_transcript_is_map_reduced(make_service: Callable[..., ScopeService], gemini: FakeGemini) -> None:
    scripted(gemini)
    svc = make_service(gemini_api_key="k", long_transcript_chars=2000, segment_chars=1500)
    pid, transcripts = await demo(svc)
    v1 = await svc.analyze(pid, transcripts[0]["id"])
    segments = v1["data"]["llm"]["segments"]
    assert segments > 3
    assert gemini.calls.count("senior technical consultant reading") == segments
    assert len(v1["data"]["requirements"]) == 3  # identical extractions from each segment were merged


async def test_invalid_key_falls_back_with_warnings(make_service: Callable[..., ScopeService], gemini: FakeGemini) -> None:
    gemini.invalid_key = True
    svc = make_service(gemini_api_key="bad")
    pid, transcripts = await demo(svc)
    v1 = await svc.analyze(pid, transcripts[0]["id"])
    assert v1["data"]["llm"]["mode"] == "fallback"
    assert any("auth" in w for w in v1["data"]["warnings"])
    assert v1["data"]["requirements"]


def test_api_contract(transport) -> None:  # type: ignore[no-untyped-def]
    app = create_app(settings(), transport=transport)
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert len(client.get("/samples").json()) >= 3
        project = client.post("/projects", json={"name": "Demo", "client": "Acme"}).json()
        pid = project["project"]["id"]
        assert client.post(f"/projects/{pid}/transcripts", json={"text": "too short"}).status_code == 422
        t = client.post(f"/projects/{pid}/transcripts", json={"sample_id": "03_fitpro_kickoff"}).json()
        assert client.post(f"/projects/{pid}/transcripts", json={"sample_id": "../../etc/passwd"}).status_code == 404
        scope = client.post(f"/projects/{pid}/analyze", json={"transcript_id": t["id"]}).json()
        assert scope["data"]["requirements"]
        assert client.post(f"/scopes/{scope['id']}/approve", json={"approver": ""}).status_code == 422
        assert client.post(f"/scopes/{scope['id']}/approve", json={"approver": "anil"}).json()["status"] == "approved"
        assert client.post(f"/scopes/{scope['id']}/approve", json={"approver": "anil"}).status_code == 409
        prop = client.post(f"/scopes/{scope['id']}/proposal").json()
        assert client.post(f"/proposals/{prop['id']}/approve", json={"approver": "anil"}).json()["ready_to_send"] is True
        assert client.get("/projects").json()[0]["approved_proposals"] == 1
