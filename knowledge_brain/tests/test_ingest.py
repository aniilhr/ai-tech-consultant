from collections.abc import Callable

import pytest

from brain.service import KnowledgeService

from .conftest import FakeSite

ARTICLE = "/support/solutions/articles/"


async def test_first_sync_in_batches_then_resume(make_service: Callable[..., KnowledgeService], site: FakeSite) -> None:
    svc = make_service()
    first = await svc.ingestor.sync(limit=3)
    assert first["sitemap_urls"] == 7  # non-article URL filtered out by pattern
    assert first["new"] == 7 and first["processed"] == 3 and first["remaining"] == 4
    second = await svc.ingestor.sync(limit=None)
    assert second["new"] == 0 and second["processed"] == 4 and second["remaining"] == 0
    stats = svc.stats()
    assert stats["articles"] == {"active": 7}
    assert stats["chunks"] > 7


async def test_unchanged_sitemap_fetches_nothing(make_service: Callable[..., KnowledgeService], site: FakeSite) -> None:
    svc = make_service()
    await svc.ingestor.sync()
    site.requests.clear()
    result = await svc.ingestor.sync()
    assert result["processed"] == 0
    assert not [r for r in site.requests if r.startswith(ARTICLE)]


async def test_lastmod_change_refetches_and_detects_identical_content(
    make_service: Callable[..., KnowledgeService], site: FakeSite
) -> None:
    svc = make_service()
    await svc.ingestor.sync()
    site.lastmod_bump["103-pointing-your-custom-domain"] = "2026-10-01"
    result = await svc.ingestor.sync()
    assert result["changed"] == 1 and result["processed"] == 1 and result["unchanged_content"] == 1


async def test_changed_content_replaces_chunks(make_service: Callable[..., KnowledgeService], site: FakeSite) -> None:
    from .conftest import FIXTURES

    svc = make_service()
    await svc.ingestor.sync()
    slug = "103-pointing-your-custom-domain"
    site.lastmod_bump[slug] = "2026-10-01"
    site.overrides[ARTICLE + slug] = (200, (FIXTURES / f"{slug}.html").read_text().replace("48 hours", "72 hours"))
    result = await svc.ingestor.sync()
    assert result["unchanged_content"] == 0
    answer = await svc.ask("How long does DNS propagation take for a custom domain?", create_escalation=False)
    assert "72 hours" in answer["answer"] and "48 hours" not in answer["answer"]


async def test_removed_article_stops_appearing(make_service: Callable[..., KnowledgeService], site: FakeSite) -> None:
    svc = make_service()
    await svc.ingestor.sync()
    q = "legacy widget settings"
    before, _ = await svc.search(q)
    assert any("Legacy Widget" in h.title for h in before.hits)
    site.excluded.add("107-legacy-widget-settings")
    result = await svc.ingestor.sync()
    assert result["removed"] == 1
    after, _ = await svc.search(q)
    assert not any("Legacy Widget" in h.title for h in after.hits)


async def test_removal_safety_guard_blocks_mass_deletion(make_service: Callable[..., KnowledgeService], site: FakeSite) -> None:
    svc = make_service()
    await svc.ingestor.sync()
    for slug in ["101", "102", "103", "104", "105"]:
        site.excluded.add(slug + r"-[a-z-]+")
    result = await svc.ingestor.sync()
    assert result["removal_blocked"] is True and result["removed"] == 0
    assert svc.stats()["articles"] == {"active": 7}


async def test_404_marks_removed_and_transient_errors_retry(
    make_service: Callable[..., KnowledgeService], site: FakeSite
) -> None:
    svc = make_service()
    site.overrides[ARTICLE + "106-importing-members-from-a-csv-file"] = (404, "gone")
    site.fail_once[ARTICLE + "105-customizing-the-search-results-page"] = 1  # 503 once, then OK
    result = await svc.ingestor.sync()
    assert result["removed"] == 1
    assert result["failed"] == 0
    assert svc.stats()["articles"] == {"active": 6, "removed": 1}


async def test_persistent_failure_is_recorded_and_retried_next_run(
    make_service: Callable[..., KnowledgeService], site: FakeSite
) -> None:
    svc = make_service()
    path = ARTICLE + "102-connecting-the-stripe-payment-gateway"
    site.fail_once[path] = 2  # both attempts of run 1 fail (crawl_max_retries=2)
    result = await svc.ingestor.sync()
    assert result["failed"] == 1 and result["remaining"] == 1
    row = svc.db.one("SELECT status, attempts, last_error FROM articles WHERE url LIKE ?", ("%102-%",))
    assert row is not None and row["status"] == "failed" and row["attempts"] == 1 and "503" in row["last_error"]
    result = await svc.ingestor.sync()
    assert result["processed"] == 1 and result["succeeded"] == 1


async def test_empty_sitemap_refuses_to_touch_kb(make_service: Callable[..., KnowledgeService], site: FakeSite) -> None:
    svc = make_service()
    await svc.ingestor.sync()
    site.overrides["/support/sitemap-articles.xml"] = (
        200,
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>',
    )
    with pytest.raises(RuntimeError):
        await svc.ingestor.sync()
    assert svc.stats()["articles"] == {"active": 7}
    assert svc.stats()["last_sync"]["status"] == "failed"


async def test_images_and_embeddings_with_gemini(make_service: Callable[..., KnowledgeService], gemini: object) -> None:
    from .conftest import FakeGemini

    assert isinstance(gemini, FakeGemini)
    gemini.responder = lambda body: {
        "relevant": True,
        "description": "The Membership Plans admin screen with an Add New Plan form.",
        "ui_elements": ["Plan Name", "Price", "Save Changes"],
    }
    svc = make_service(gemini_api_key="test-key")
    result = await svc.ingestor.sync()
    assert result["images_described"] == 2
    assert result["chunks_embedded"] == svc.stats()["chunks"]
    row = svc.db.one("SELECT description, status FROM images WHERE url LIKE '%plan-settings%'")
    assert row is not None and row["status"] == "described" and "Save Changes" in row["description"]
    shot = svc.db.one("SELECT text FROM chunks WHERE image_id IS NOT NULL LIMIT 1")
    assert shot is not None and "[Screenshot]" in shot["text"]


async def test_no_key_skips_images_with_warning(make_service: Callable[..., KnowledgeService]) -> None:
    svc = make_service()
    result = await svc.ingestor.sync()
    assert result["images_described"] == 0
    assert any("Image understanding skipped" in w for w in result["warnings"])
    assert svc.stats()["images"] == {"pending": 2}
