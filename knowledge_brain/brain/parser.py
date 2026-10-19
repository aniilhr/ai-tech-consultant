"""Parsing for sitemaps and help-center article pages.

The Brilliant Directories Solution Center runs on Freshdesk. Freshdesk article pages put the title in a
heading near the top, the body in `#article-body` / `.article-body`, and the category > folder path in a
`.breadcrumb`. Every selector has a generic fallback so the parser degrades instead of failing when the
theme changes.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

HEADINGS = {"h1", "h2", "h3", "h4", "h5"}
BLOCKS = {"p", "li", "pre", "blockquote", "td", "th", "dt", "dd", "figcaption", "caption"}
SKIP = {"script", "style", "noscript", "iframe", "svg", "form", "button", "nav", "footer", "header"}
INLINE = {"span", "strong", "b", "em", "i", "a", "u", "code", "font", "sup", "sub", "small", "mark", "label", "abbr"}


@dataclass
class SitemapEntry:
    url: str
    lastmod: str | None = None


@dataclass
class ImageRef:
    url: str
    alt: str
    section: str
    context: str


@dataclass
class Section:
    heading: str
    paragraphs: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.paragraphs)


@dataclass
class ParsedArticle:
    url: str
    title: str
    category: str | None
    folder: str | None
    sections: list[Section]
    images: list[ImageRef]

    @property
    def text(self) -> str:
        parts = []
        for s in self.sections:
            if s.heading:
                parts.append(f"## {s.heading}")
            parts.append(s.text)
        return "\n".join(p for p in parts if p).strip()

    @property
    def content_hash(self) -> str:
        payload = self.title + "\n" + self.text + "\n" + "\n".join(i.url for i in self.images)
        return hashlib.sha256(payload.encode()).hexdigest()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_sitemap(xml_text: str) -> tuple[list[SitemapEntry], list[str]]:
    """Returns (url entries, child sitemap URLs). Handles both <urlset> and <sitemapindex>."""
    root = ET.fromstring(xml_text.strip().encode())
    entries: list[SitemapEntry] = []
    children: list[str] = []
    for node in root:
        name = _local(node.tag)
        loc = lastmod = None
        for child in node:
            if _local(child.tag) == "loc":
                loc = (child.text or "").strip()
            elif _local(child.tag) == "lastmod":
                lastmod = (child.text or "").strip() or None
        if not loc:
            continue
        if name == "sitemap":
            children.append(loc)
        elif name == "url":
            entries.append(SitemapEntry(url=loc, lastmod=lastmod))
    return entries, children


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _first_text(soup: BeautifulSoup, selectors: list[str]) -> str:
    for sel in selectors:
        node = soup.select_one(sel)
        if node:
            value = node.get("content") if node.name == "meta" else node.get_text(" ", strip=True)
            if value and _clean(str(value)):
                return _clean(str(value))
    return ""


def parse_article(html: str, url: str) -> ParsedArticle:
    soup = BeautifulSoup(html, "html.parser")

    title = _first_text(
        soup,
        ["h1.heading", "h2.heading", ".article-title", "article h1", "h1", 'meta[property="og:title"]', "title"],
    )
    title = re.sub(r"\s*[:|-]\s*(Brilliant Directories|Support|Solution Center).*$", "", title).strip() or url

    crumbs = [_clean(a.get_text(" ")) for a in soup.select(".breadcrumb a, .breadcrumbs a, nav[aria-label*=readcrumb] a")]
    crumbs = [c for c in crumbs if c and c.lower() not in {"home", "solution home", "support home"}]
    category = crumbs[0] if crumbs else None
    folder = crumbs[1] if len(crumbs) > 1 else None

    body = None
    for sel in ["#article-body", ".article-body", "article .article-content", "article", "main", "#content", "body"]:
        body = soup.select_one(sel)
        if body:
            break
    sections: list[Section] = [Section(heading="")]
    images: list[ImageRef] = []
    line: list[str] = []

    def flush() -> None:
        text = _clean(" ".join(line))
        line.clear()
        if text:
            sections[-1].paragraphs.append(text)

    def add_image(img: Tag) -> None:
        src = img.get("data-src") or img.get("src") or ""
        src = str(src).strip()
        if not src or src.startswith("data:"):
            return
        for dim in ("width", "height"):
            try:
                if int(str(img.get(dim, "999")).replace("px", "")) < 48:
                    return  # icons, emoji, spacers
            except ValueError:
                pass
        prior = " ".join(sections[-1].paragraphs[-2:] + line)
        images.append(
            ImageRef(
                url=urljoin(url, src),
                alt=_clean(str(img.get("alt") or img.get("title") or "")),
                section=sections[-1].heading,
                context=_clean(prior)[-400:],
            )
        )

    def walk(node: Tag) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                if child.strip() and child.__class__ is NavigableString:
                    line.append(str(child))
                continue
            if not isinstance(child, Tag) or child.name in SKIP:
                continue
            name = child.name
            if name in HEADINGS:
                flush()
                heading = _clean(child.get_text(" "))
                if heading and heading != title:
                    sections.append(Section(heading=heading))
            elif name == "img":
                flush()
                add_image(child)
            elif name == "br":
                flush()
            elif name in BLOCKS:
                flush()
                for img in child.find_all("img"):
                    add_image(img)
                text = _clean(child.get_text(" "))
                if text:
                    prefix = "- " if name == "li" else ""
                    sections[-1].paragraphs.append(prefix + text)
            elif name in INLINE and not child.find(list(HEADINGS | BLOCKS | {"img", "div", "table"})):
                line.append(child.get_text(" "))
            else:
                flush()
                walk(child)
                flush()

    if body is not None:
        walk(body)
        flush()
    sections = [s for s in sections if s.paragraphs]
    # De-duplicate images that appear twice (lightbox links etc.).
    seen: set[str] = set()
    unique_images = [i for i in images if not (i.url in seen or seen.add(i.url))]  # type: ignore[func-returns-value]
    return ParsedArticle(url=url, title=title, category=category, folder=folder, sections=sections, images=unique_images)
