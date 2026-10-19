from collections.abc import Callable

from fastapi.testclient import TestClient

from brain.api import create_app
from brain.llm import to_gemini_schema
from brain.service import AnswerOut, KnowledgeService

from .conftest import FakeGemini, make_settings


async def synced(make_service: Callable[..., KnowledgeService], **kw: object) -> KnowledgeService:
    svc = make_service(**kw)
    await svc.ingestor.sync(describe_images=False)
    return svc


async def test_offline_answer_is_extractive_with_citations(make_service: Callable[..., KnowledgeService]) -> None:
    svc = await synced(make_service)
    r = await svc.ask("How do I add a new membership plan?")
    assert r["answered"] is True
    assert r["llm"]["mode"] == "offline"
    assert "extractive" in r["answer"]
    assert r["citations"][0]["title"] == "How to Add a New Membership Plan"
    assert r["citations"][0]["url"].endswith("101-how-to-add-a-new-membership-plan")
    assert r["images"] and r["images"][0]["analysed"] is False
    assert r["escalation_id"] is None


async def test_unknown_question_escalates_and_dedupes(make_service: Callable[..., KnowledgeService]) -> None:
    svc = await synced(make_service)
    r1 = await svc.ask("Does the platform integrate with Salesforce CRM?", source="inbox")
    assert r1["answered"] is False and r1["escalation_id"]
    assert "won't guess" in r1["answer"]
    assert r1["citations"] == []
    r2 = await svc.ask("does the platform integrate with salesforce crm", source="chat")
    assert r2["escalation_id"] == r1["escalation_id"]
    esc = svc.db.one("SELECT times_asked, source FROM escalations WHERE id=?", (r1["escalation_id"],))
    assert esc is not None and esc["times_asked"] == 2 and esc["source"] == "inbox"


async def test_expert_answer_flows_back_into_memory(make_service: Callable[..., KnowledgeService]) -> None:
    svc = await synced(make_service)
    q = "Does the platform integrate with Salesforce CRM?"
    r1 = await svc.ask(q)
    await svc.resolve_escalation(
        r1["escalation_id"],
        "There is no native Salesforce CRM integration. Use Zapier webhooks to sync new members to Salesforce CRM.",
        "Priya",
    )
    r2 = await svc.ask(q)
    assert r2["answered"] is True
    assert r2["citations"][0]["source"] == "expert"
    assert svc.stats()["escalations_answered"] == 1


async def test_live_answer_uses_gemini_and_requires_citations(
    make_service: Callable[..., KnowledgeService], gemini: FakeGemini
) -> None:
    svc = await synced(make_service, gemini_api_key="k")
    r = await svc.ask("How do I add a new membership plan?")
    assert r["answered"] and r["llm"]["mode"] == "live"
    assert r["llm"]["model"] == "gemini-3.8-flash"
    assert r["llm"]["input_tokens"] == 500 and r["llm"]["cost_usd"] == round(500 / 1e6 * 0.5 + 80 / 1e6 * 3.0, 6)
    assert r["retrieval_mode"] == "hybrid"
    # Model claims an answer but cites nothing -> treated as not answered (grounding rule)
    gemini.responder = lambda body: {"answerable": True, "answer": "Sure!", "cited_evidence": [], "missing_information": ""}
    r2 = await svc.ask("How do I add a new membership plan?", create_escalation=False)
    assert r2["answered"] is False


async def test_invalid_key_degrades_gracefully(make_service: Callable[..., KnowledgeService], gemini: FakeGemini) -> None:
    svc = await synced(make_service)  # sync offline first
    svc2 = make_service(gemini_api_key="bad-key")
    gemini.mode = "invalid_key"
    r = await svc2.ask("How do I add a new membership plan?")
    assert r["llm"]["mode"] == "fallback"
    assert any("auth" in w for w in r["warnings"])
    assert r["answered"] is True  # extractive answer still available
    del svc


async def test_server_errors_are_retried_then_fall_back(
    make_service: Callable[..., KnowledgeService], gemini: FakeGemini
) -> None:
    svc = await synced(make_service, gemini_api_key="k")
    gemini.mode = "server_error"
    calls_before = len(gemini.calls)
    r = await svc.ask("How do I connect Stripe?")
    assert r["llm"]["mode"] == "fallback"
    gen_calls = [c for c in gemini.calls[calls_before:] if c.endswith(":generateContent")]
    assert len(gen_calls) == 3


async def test_invalid_json_gets_one_corrective_retry(make_service: Callable[..., KnowledgeService], gemini: FakeGemini) -> None:
    svc = await synced(make_service, gemini_api_key="k")
    replies = iter(
        [{"wrong": "shape"}, {"answerable": True, "answer": "Use Settings > Payment Gateways.", "cited_evidence": [1]}]
    )
    gemini.responder = lambda body: next(replies)
    r = await svc.ask("How do I connect the Stripe payment gateway?")
    assert r["answered"] and r["llm"]["mode"] == "live"


async def test_custom_document_is_searchable_and_deletable(make_service: Callable[..., KnowledgeService]) -> None:
    svc = await synced(make_service)
    doc_id = await svc.add_document(
        title="Office hours", text="Our support team works Monday to Friday, 9am to 5pm Eastern time."
    )
    r = await svc.ask("What are the support team office hours?")
    assert r["answered"] and r["citations"][0]["source"] == "document"
    assert svc.delete_document(doc_id)
    r2 = await svc.ask("What are the support team office hours?", create_escalation=False)
    assert not r2["answered"]


def test_gemini_schema_conversion() -> None:
    from brain.llm import inline_refs

    schema = to_gemini_schema(inline_refs(AnswerOut.model_json_schema()))
    assert schema["type"] == "OBJECT"
    assert schema["properties"]["cited_evidence"] == {
        "type": "ARRAY",
        "items": {"type": "INTEGER"},
        "description": "Evidence numbers used, e.g. [1, 3]",
    }
    assert "title" not in schema and "additionalProperties" not in schema


def test_http_api_contract(tmp_path, transport) -> None:  # type: ignore[no-untyped-def]
    app = create_app(make_settings(tmp_path, brain_admin_token="secret"), transport=transport)
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert client.post("/v1/ask", json={"question": "hi"}).status_code == 422  # too short
        assert client.post("/v1/sync", json={"limit": 2}).status_code == 401  # admin token enforced
        r = client.post(
            "/v1/documents",
            json={"title": "Office hours", "text": "Support works Monday to Friday 9 to 5."},
            headers={"X-Admin-Token": "secret"},
        )
        assert r.status_code == 200
        ask = client.post("/v1/ask", json={"question": "When does support work, which days?", "source": "inbox"}).json()
        assert set(ask) >= {"answered", "answer", "citations", "images", "escalation_id", "confidence", "llm", "warnings"}
        search = client.post("/v1/search", json={"query": "support office hours"}).json()
        assert search["results"][0]["title"] == "Office hours"
        assert client.get("/v1/escalations").status_code == 200
        assert client.get("/v1/stats").json()["documents"] == 1
