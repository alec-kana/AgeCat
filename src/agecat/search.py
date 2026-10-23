"""Search AGE's catalogue through the site's own ``/search`` page."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode, urlsplit

from bs4 import Tag

from .errors import ParseError
from .extractor import parse_html
from .urls import DETAIL_PATH_PATTERN, rebase_url

SEARCH_PATH = "/search"
RESULTS_PER_PAGE = 24
DEFAULT_SEARCH_LIMIT = 48
TOTAL_PATTERN = re.compile(r"共\s*(\d+)\s*条")
LOGGER = logging.getLogger(__name__)


class SearchSource(Protocol):
    """Minimal HTTP dependency required to read search result pages."""

    def get_page(self, url: str, *, page_url: str | None = None) -> str:
        """Return raw HTML for an AGE page."""

        ...


@dataclass(frozen=True)
class SearchResult:
    """One anime listed on an AGE search result page."""

    anime_id: str
    title: str
    url: str
    status: str = ""
    kind: str = ""
    premiere: str = ""
    play_state: str = ""


@dataclass(frozen=True)
class SearchPage:
    """One page of search results plus the total match count the site reports."""

    results: list[SearchResult]
    total: int


@dataclass(frozen=True)
class SearchOutcome:
    """Collected search results, and how many the site matched in total."""

    results: list[SearchResult]
    total: int


def search_url(site_url: str, keyword: str, page: int = 1) -> str:
    """Return the search page URL for a keyword on the live site."""

    query = {"query": keyword} if page <= 1 else {"query": keyword, "page": str(page)}
    return f"{site_url.rstrip('/')}{SEARCH_PATH}?{urlencode(query)}"


def parse_search_page(html: str, site_url: str) -> SearchPage:
    """Parse the result cards and total match count of one search page."""

    soup = parse_html(html)
    if soup.select_one("#search_results_wrapper") is None:
        raise ParseError("page is not an AGE search result page")

    results = [
        result
        for card in soup.select(".cata_video_item")
        if (result := parse_result_card(card, site_url)) is not None
    ]

    total = len(results)
    summary = soup.select_one(".search_keywords") or soup.select_one(".pagination")
    if summary is not None and (match := TOTAL_PATTERN.search(summary.get_text(" ", strip=True))):
        total = int(match.group(1))
    return SearchPage(results=results, total=total)


def parse_result_card(card: Tag, site_url: str) -> SearchResult | None:
    """Convert one result card, or return None when it has no detail link."""

    anchor = card.select_one(".card-title a[href]")
    href = anchor.get("href") if anchor is not None else None
    if anchor is None or not isinstance(href, str):
        LOGGER.warning("Skipping search result without a detail link")
        return None

    url = rebase_url(href, site_url)
    match = DETAIL_PATH_PATTERN.fullmatch(urlsplit(url).path)
    if match is None:
        LOGGER.warning("Skipping search result with an unexpected link: %s", href)
        return None

    details: dict[str, str] = {}
    for info in card.select(".video_detail_info"):
        label = info.select_one("span")
        if label is None:
            continue
        name = label.get_text(strip=True).rstrip("：:")
        value = info.get_text(" ", strip=True).removeprefix(label.get_text(strip=True)).strip()
        details[name] = value

    status = card.select_one(".video_play_status")
    return SearchResult(
        anime_id=match.group("anime_id"),
        title=anchor.get_text(" ", strip=True),
        url=url,
        status=status.get_text(strip=True) if status else "",
        kind=details.get("动画种类", ""),
        premiere=details.get("首播时间", ""),
        play_state=details.get("播放状态", ""),
    )


def search_anime(
    client: SearchSource,
    site_url: str,
    keyword: str,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> SearchOutcome:
    """Collect up to ``limit`` matches for ``keyword``, following result pages as needed."""

    if limit < 1:
        raise ValueError("limit must be at least 1")

    results: list[SearchResult] = []
    total = 0
    page_number = 1
    while len(results) < limit:
        url = search_url(site_url, keyword, page_number)
        page = parse_search_page(client.get_page(url), site_url)
        total = max(total, page.total)
        results.extend(page.results)
        if not page.results or len(results) >= total:
            break
        page_number += 1

    LOGGER.info("Search %r matched %d anime; showing %d", keyword, total, min(len(results), limit))
    return SearchOutcome(results=results[:limit], total=total)
