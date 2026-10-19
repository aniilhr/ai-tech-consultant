from brain.chunker import chunk_article, chunk_sections
from brain.parser import parse_article, parse_sitemap
from brain.retrieval import tokenize

from .conftest import BASE, FIXTURES


def test_parse_sitemap_index_and_urlset() -> None:
    entries, children = parse_sitemap((FIXTURES / "sitemap_index.xml").read_text().replace("{base}", BASE))
    assert entries == [] and children == [f"{BASE}/support/sitemap-articles.xml"]
    entries, children = parse_sitemap((FIXTURES / "sitemap-articles.xml").read_text().replace("{base}", BASE))
    assert children == []
    assert len(entries) == 8
    assert entries[1].lastmod == "2026-09-01"


def test_parse_article_extracts_title_breadcrumb_sections_images() -> None:
    url = f"{BASE}/support/solutions/articles/101-how-to-add-a-new-membership-plan"
    art = parse_article((FIXTURES / "101-how-to-add-a-new-membership-plan.html").read_text(), url)
    assert art.title == "How to Add a New Membership Plan"
    assert art.category == "Members & Plans"
    assert art.folder == "Membership Plans"
    headings = [s.heading for s in art.sections]
    assert "Create the plan" in headings and "Free plans" in headings
    assert any("Add New Plan" in p for s in art.sections for p in s.paragraphs)
    # header/nav/footer/script are not content
    assert "Submit a ticket" not in art.text and "tracking" not in art.text
    # 16px icon is skipped; screenshot kept with absolute URL and section context
    assert len(art.images) == 1
    img = art.images[0]
    assert img.url == f"{BASE}/fixtures/img/plan-settings.png"
    assert img.section == "Create the plan" and img.alt == "Membership plan settings screen"
    # inline div text is captured
    assert "Free plans never charge the member." in art.text


def test_content_hash_changes_with_content() -> None:
    html = (FIXTURES / "103-pointing-your-custom-domain.html").read_text()
    a = parse_article(html, "u")
    b = parse_article(html.replace("48 hours", "72 hours"), "u")
    assert a.content_hash != b.content_hash
    assert a.content_hash == parse_article(html, "u").content_hash


def test_parser_falls_back_when_no_freshdesk_markup() -> None:
    art = parse_article(
        "<html><head><title>Plain Page</title></head><body><main><h1>Plain Page</h1><p>Hello world text.</p></main></body></html>",
        "u",
    )
    assert art.title == "Plain Page"
    assert art.text.strip() == "Hello world text."


def test_chunks_carry_title_and_heading_and_respect_size() -> None:
    url = f"{BASE}/support/solutions/articles/101"
    art = parse_article((FIXTURES / "101-how-to-add-a-new-membership-plan.html").read_text(), url)
    chunks = chunk_article(art)
    assert all(c.text.startswith("How to Add a New Membership Plan") for c in chunks)
    long = chunk_sections("T", [("H", ["word " * 400, "short para"])], max_chars=500)
    assert len(long) >= 3
    assert all(len(c.text) <= 520 + len("T > H\n") for c in long)


def test_tokenize_stems_and_drops_stopwords() -> None:
    assert tokenize("How do I add new Membership Plans?") == ["add", "new", "membership", "plan"]
