"""Read the logged-in account's favorites (``/my/collects``).

The favorites page is only visible when logged in, so its markup is parsed
defensively: favorite cards (``.video_item``, which the site's own script
uses to remove a card after un-favoriting) are preferred, and otherwise any
detail link in the page body outside the header and footer counts.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol
from urllib.parse import urlencode, urlsplit

import requests
from bs4 import BeautifulSoup, Tag

from .auth import NotLoggedInError, site_root
from .extractor import parse_html
from .search import SearchResult
from .urls import DETAIL_PATH_PATTERN, rebase_url

COLLECTS_PATH = "/my/collects"
CARD_SELECTOR = ".video_item"
CHROME_SELECTORS = ("header", "nav", "footer", ".foot_content_wrapper", ".modal")
# .video_item--info is the episode badge on the site's own list cards.
STATUS_SELECTORS = (
    ".video_item--info",
    ".video_play_status",
    "[class*='status']",
    "[class*='episode']",
)
TOTAL_PATTERN = re.compile(r"共\s*(\d+)\s*条")
MAX_PAGES = 100
LOGGER = logging.getLogger(__name__)


class CollectsSource(Protocol):
    """HTTP surface needed to read account pages."""

    def get_document(self, url: str, *, page_url: str | None = None) -> requests.Response:
        """Return the final response of a page navigation, after redirects."""

        ...


def collects_url(site_url: str, page: int = 1) -> str:
    """Return the favorites page URL."""

    base = f"{site_url.rstrip('/')}{COLLECTS_PATH}"
    return base if page <= 1 else f"{base}?{urlencode({'page': page})}"


def parse_collects_page(html: str, site_url: str) -> tuple[list[SearchResult], int | None]:
    """Parse one favorites page into entries and the total the page reports, if any."""

    soup = parse_html(html)
    for selector in CHROME_SELECTORS:
        for element in soup.select(selector):
            element.decompose()

    cards = soup.select(CARD_SELECTOR)
    entries = _entries_from_cards(cards, site_url) if cards else _entries_from_links(soup, site_url)

    match = TOTAL_PATTERN.search(soup.get_text(" ", strip=True))
    return entries, int(match.group(1)) if match else None


def fetch_collects(client: CollectsSource, site_url: str) -> list[SearchResult]:
    """Return every favorite, following pages until no new entries appear."""

    entries: list[SearchResult] = []
    seen: set[str] = set()
    total: int | None = None
    for page in range(1, MAX_PAGES + 1):
        response = client.get_document(collects_url(site_url, page), page_url=site_root(site_url))
        # Logged-out visitors are redirected to the home page.
        if urlsplit(response.url).path.rstrip("/") != COLLECTS_PATH:
            raise NotLoggedInError("not logged in to AGE")

        page_entries, page_total = parse_collects_page(response.text, site_url)
        total = page_total if page_total is not None else total
        fresh = [entry for entry in page_entries if entry.anime_id not in seen]
        if not fresh:
            break
        for entry in fresh:
            seen.add(entry.anime_id)
            entries.append(entry)
        if total is not None and len(entries) >= total:
            break

    LOGGER.info("Loaded %d favorite(s)", len(entries))
    return entries


def _entries_from_cards(cards: list[Tag], site_url: str) -> list[SearchResult]:
    """Build entries from favorite cards, one per card."""

    entries: list[SearchResult] = []
    seen: set[str] = set()
    for card in cards:
        links = [link for link in card.select("a[href]") if _detail_id(link, site_url)]
        if not links:
            continue
        anime_id = _detail_id(links[0], site_url)
        if anime_id is None or anime_id in seen:
            continue
        seen.add(anime_id)
        entries.append(
            SearchResult(
                anime_id=anime_id,
                title=_best_title(links),
                url=rebase_url(_href(links[0]), site_url),
                status=_status(card),
            )
        )
    return entries


def _entries_from_links(soup: BeautifulSoup, site_url: str) -> list[SearchResult]:
    """Build entries from every detail link left on the page, grouped by anime id."""

    grouped: dict[str, list[Tag]] = {}
    for link in soup.select("a[href]"):
        anime_id = _detail_id(link, site_url)
        if anime_id is not None:
            grouped.setdefault(anime_id, []).append(link)

    return [
        SearchResult(
            anime_id=anime_id,
            title=_best_title(links),
            url=rebase_url(_href(links[0]), site_url),
        )
        for anime_id, links in grouped.items()
    ]


def _detail_id(link: Tag, site_url: str) -> str | None:
    """Return the anime id when a link points at a detail page."""

    href = _href(link)
    if not href:
        return None
    match = DETAIL_PATH_PATTERN.fullmatch(urlsplit(rebase_url(href, site_url)).path)
    return match.group("anime_id") if match else None


def _href(link: Tag) -> str:
    """Return a link's href as a string."""

    href = link.get("href")
    return href if isinstance(href, str) else ""


def _best_title(links: list[Tag]) -> str:
    """Pick the most descriptive title among the links for one anime."""

    candidates: list[str] = []
    for link in links:
        for value in (link.get("title"), link.get_text(" ", strip=True)):
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
        image = link.select_one("img[alt]")
        alt = image.get("alt") if image is not None else None
        if isinstance(alt, str) and alt.strip():
            candidates.append(alt.strip())
    # Buttons like "资源详情" are short; the real title is the longest text.
    return max(candidates, key=len) if candidates else ""


def _status(card: Tag) -> str:
    """Return the episode/status badge of a card, when it has one."""

    for selector in STATUS_SELECTORS:
        element = card.select_one(selector)
        if element is not None and (text := element.get_text(" ", strip=True)):
            return text
    return ""
