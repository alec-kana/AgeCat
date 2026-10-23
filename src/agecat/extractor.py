from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup, FeatureNotFound, Tag

from .constants import DEFAULT_SOURCE_PRIORITY
from .errors import AgeCatError, DownloadCancelled, ParseError
from .models import Episode
from .player import (
    PlayerStream,
    build_vip_request,
    parse_m3u8_player,
    parse_vip_player,
    parse_vip_response,
    player_kind,
    stream_kind,
)
from .urls import PLAY_PATH_PATTERN, parse_play_url

SOURCE_TARGET_PREFIX = "#playlist-source-"
EPISODE_NUMBER_PATTERN = re.compile(r"第\s*0*(\d+)\s*[集话話]")
BARE_EPISODE_NUMBER_PATTERN = re.compile(r"^0*(\d+)$")
PAGE_TITLE_SUFFIX_PATTERN = re.compile(r"\s*-\s*(?:在线播放\s*-\s*)?AGE动漫\s*$")
HLS_SIGNATURE = "#EXTM3U"
PROBE_RANGE_HEADERS = {"Range": "bytes=0-0"}
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlaylistEpisode:
    """One episode link inside a playlist tab."""

    label: str
    url: str
    number: int | None = None


@dataclass(frozen=True)
class PlaylistSource:
    """One playlist tab (a mirror) and its episode links, in page order."""

    key: str
    label: str
    episodes: list[PlaylistEpisode]
    vip: bool = False


@dataclass(frozen=True)
class DetailPage:
    """Parsed anime detail page: its name and every playlist tab."""

    anime_name: str | None
    sources: list[PlaylistSource]


@dataclass(frozen=True)
class PlayPage:
    """Parsed play page: page title, embedded player, and every playlist tab."""

    page_title: str | None
    player_url: str | None
    sources: list[PlaylistSource]


@dataclass(frozen=True)
class SeasonEpisodes:
    """Episode URLs collected from a detail URL with its anime name."""

    episode_urls: list[str]
    anime_name: str | None = None
    episode_numbers: list[int | None] = field(default_factory=list)


@dataclass(frozen=True)
class SourceCandidate:
    """One mirror that may serve an episode, tried in priority order."""

    key: str
    label: str
    play_url: str
    player_url: str | None = None


@dataclass(frozen=True)
class EpisodePage:
    """Episode identity parsed from its play page, before any stream is resolved.

    Resolving in two phases lets a caller decide the output filename, and so
    skip an episode it already has, without asking any player for a stream.
    """

    page_url: str
    title: str
    anime_name: str | None = None
    candidates: tuple[SourceCandidate, ...] = ()

    def unresolved_episode(self) -> Episode:
        """Return episode metadata with no stream, for reporting a skipped download."""

        return Episode(
            page_url=self.page_url,
            title=self.title,
            stream_url="",
            anime_name=self.anime_name,
        )


class EpisodeSource(Protocol):
    """Minimal HTTP dependency required by AgeExtractor."""

    def get_page(self, url: str, *, page_url: str | None = None) -> str:
        """Return raw HTML for an AGE page."""

        ...

    def get_frame(self, url: str, *, page_url: str) -> str:
        """Return raw HTML for an embedded player page."""

        ...

    def post_form(
        self,
        url: str,
        data: Mapping[str, str],
        *,
        headers: Mapping[str, str] | None = None,
        page_url: str | None = None,
    ) -> requests.Response:
        """Return the raw response of a form-encoded player API request."""

        ...

    def get_resource(
        self,
        url: str,
        *,
        page_url: str | None = None,
        headers: Mapping[str, str] | None = None,
        paced: bool = True,
    ) -> requests.Response:
        """Return the raw response for a playlist, key, or segment URL."""

        ...


def parse_html(html: str) -> BeautifulSoup:
    """Create a BeautifulSoup document with lxml fallback handling."""

    try:
        return BeautifulSoup(html, "lxml")
    except FeatureNotFound:
        # Keep the CLI usable even when the optional native lxml parser is missing.
        return BeautifulSoup(html, "html.parser")


def parse_episode_number(label: str) -> int | None:
    """Extract the episode number from a playlist label like ``第01集``."""

    label = label.strip()
    match = EPISODE_NUMBER_PATTERN.search(label) or BARE_EPISODE_NUMBER_PATTERN.fullmatch(label)
    return int(match.group(1)) if match else None


def parse_playlists(soup: BeautifulSoup, base_url: str) -> list[PlaylistSource]:
    """Parse every playlist tab, keeping the page's tab order."""

    sources: list[PlaylistSource] = []
    for button in soup.select(f'[data-bs-target^="{SOURCE_TARGET_PREFIX}"]'):
        target = button.get("data-bs-target")
        if not isinstance(target, str):
            continue
        key = target.removeprefix(SOURCE_TARGET_PREFIX)
        pane = soup.find(id=f"playlist-source-{key}")
        if not isinstance(pane, Tag):
            continue

        badge = button.select_one(".badge")
        vip = badge is not None and "VIP" in badge.get_text(strip=True).upper()
        if badge is not None:
            badge.extract()

        episodes = [
            PlaylistEpisode(
                label=label,
                url=urljoin(base_url, href),
                number=parse_episode_number(label),
            )
            for anchor in pane.select("a[href]")
            if isinstance(href := anchor.get("href"), str)
            and PLAY_PATH_PATTERN.fullmatch(urlsplit(urljoin(base_url, href)).path)
            and (label := anchor.get_text(" ", strip=True))
        ]
        sources.append(
            PlaylistSource(
                key=key,
                label=button.get_text(" ", strip=True) or key,
                episodes=episodes,
                vip=vip,
            )
        )
    return sources


def parse_detail_page(html: str, base_url: str) -> DetailPage:
    """Parse an anime detail page into its name and playlist tabs."""

    soup = parse_html(html)
    title = soup.select_one("h2.video_detail_title")
    anime_name = title.get_text(" ", strip=True) if title else None
    if not anime_name:
        anime_name = page_title(soup)

    return DetailPage(anime_name=anime_name or None, sources=parse_playlists(soup, base_url))


def parse_play_page(html: str, base_url: str) -> PlayPage:
    """Parse a play page into its title, embedded player URL, and playlist tabs."""

    soup = parse_html(html)
    iframe = soup.select_one("iframe#iframeForVideo[src]")
    src = iframe.get("src") if iframe else None
    return PlayPage(
        page_title=page_title(soup),
        player_url=urljoin(base_url, src) if isinstance(src, str) and src else None,
        sources=parse_playlists(soup, base_url),
    )


def page_title(soup: BeautifulSoup) -> str | None:
    """Return the document title without the trailing site name."""

    title = soup.select_one("title")
    if title is None:
        return None
    text = PAGE_TITLE_SUFFIX_PATTERN.sub("", title.get_text(" ", strip=True)).strip()
    return text or None


def find_episode(source: PlaylistSource, url: str) -> PlaylistEpisode | None:
    """Return the playlist entry whose URL path matches ``url``."""

    path = urlsplit(url).path.rstrip("/")
    for episode in source.episodes:
        if urlsplit(episode.url).path.rstrip("/") == path:
            return episode
    return None


def matching_episode(source: PlaylistSource, wanted: PlaylistEpisode) -> PlaylistEpisode | None:
    """Return the entry in another mirror that is the same episode as ``wanted``.

    Mirrors label episodes differently (``第01集`` vs ``第1集``) and some are
    missing episodes, so match by number first and fall back to the slot.
    """

    if wanted.number is not None:
        for episode in source.episodes:
            if episode.number == wanted.number:
                return episode
        return None

    for episode in source.episodes:
        if episode.label == wanted.label:
            return episode

    slot = parse_play_url(wanted.url).episode_index
    for episode in source.episodes:
        if parse_play_url(episode.url).episode_index == slot:
            return episode
    return None


def order_sources(
    sources: Sequence[PlaylistSource],
    preferred: Sequence[str] | None = None,
) -> list[PlaylistSource]:
    """Order mirrors by preference.

    With ``preferred`` (keys, tab labels, or 1-based tab numbers) only the
    named mirrors are used, in that order. Without it, known mirrors follow
    :data:`DEFAULT_SOURCE_PRIORITY` and unknown ones keep page order after them.
    """

    if preferred:
        ordered: list[PlaylistSource] = []
        for wanted in preferred:
            source = select_source(sources, wanted)
            if source is not None and source not in ordered:
                ordered.append(source)
        return ordered

    rank = {key: index for index, key in enumerate(DEFAULT_SOURCE_PRIORITY)}
    return sorted(
        sources,
        key=lambda source: (rank.get(source.key, len(rank)), sources.index(source)),
    )


def select_source(sources: Sequence[PlaylistSource], wanted: str) -> PlaylistSource | None:
    """Find a mirror by key (``ffm3u8``), tab label (``非凡``), or tab number (``2``)."""

    wanted = wanted.strip()
    lowered = wanted.lower()
    for source in sources:
        if source.key.lower() == lowered or source.label == wanted:
            return source
    if wanted.isdigit() and 1 <= int(wanted) <= len(sources):
        return sources[int(wanted) - 1]
    return None


def episode_title(anime_name: str | None, label: str) -> str:
    """Build the output title for an episode, e.g. ``欺诈游戏 第01集``."""

    return f"{anime_name} {label}" if anime_name else label


class AgeExtractor:
    """Extract episode lists and playable streams from AGE pages."""

    def __init__(
        self,
        client: EpisodeSource,
        *,
        source_priority: Sequence[str] | None = None,
    ) -> None:
        self.client = client
        self.source_priority = list(source_priority) if source_priority else None

    def season_episode_urls(self, url: str) -> SeasonEpisodes:
        """Collect every episode of an anime from its detail page.

        Mirrors carry different episode counts, so the list comes from the
        most complete mirror, preferring higher-priority mirrors on ties.
        """

        detail = parse_detail_page(self.client.get_page(url), url)
        sources = order_sources(detail.sources, self.source_priority) or order_sources(
            detail.sources
        )
        if not sources:
            raise ParseError(f"detail page has no playlist: {url}")

        best = max(sources, key=lambda source: len(source.episodes))
        if not best.episodes:
            raise ParseError(f"detail page has no episode links: {url}")

        return SeasonEpisodes(
            episode_urls=[episode.url for episode in best.episodes],
            anime_name=detail.anime_name,
            episode_numbers=[episode.number for episode in best.episodes],
        )

    def episode_page(self, url: str) -> EpisodePage:
        """Parse one play page into its title and the mirrors that can serve it."""

        page = parse_play_page(self.client.get_page(url), url)
        current_source, current = self._current_episode(page.sources, url)

        label = current.label if current else None
        anime_name = page.page_title
        if anime_name and label and anime_name.endswith(label):
            anime_name = anime_name[: -len(label)].strip() or None
        title = episode_title(anime_name, label) if label else page.page_title
        if not title:
            raise ParseError(f"play page is missing a title: {url}")

        candidates: list[SourceCandidate] = []
        for source in order_sources(page.sources, self.source_priority):
            entry = (
                current
                if source is current_source
                else (matching_episode(source, current) if current else None)
            )
            if entry is None:
                continue
            is_current_page = urlsplit(entry.url).path.rstrip("/") == urlsplit(url).path.rstrip("/")
            candidates.append(
                SourceCandidate(
                    key=source.key,
                    label=source.label,
                    play_url=entry.url,
                    player_url=page.player_url if is_current_page else None,
                )
            )

        if not candidates and page.player_url and not self.source_priority:
            # No playlist markup at all: fall back to the page's own player.
            candidates.append(
                SourceCandidate(key="page", label="page", play_url=url, player_url=page.player_url)
            )

        return EpisodePage(
            page_url=url,
            title=title,
            anime_name=anime_name,
            candidates=tuple(candidates),
        )

    def resolve_stream(self, page: EpisodePage) -> Episode:
        """Resolve the first mirror that yields a reachable stream."""

        if not page.candidates:
            raise ParseError(f"no playable source for {page.title}")

        failures: list[str] = []
        for candidate in page.candidates:
            try:
                player_url, stream = self._resolve_candidate(candidate)
                self._probe(stream, player_url)
            except DownloadCancelled:
                raise
            except (AgeCatError, requests.RequestException) as error:
                LOGGER.info("Source %s failed for %s: %s", candidate.label, page.title, error)
                failures.append(f"{candidate.label}: {error}")
                continue

            LOGGER.info("Using source %s (%s) for %s", candidate.label, stream.kind, page.title)
            return Episode(
                page_url=page.page_url,
                title=page.title,
                stream_url=stream.url,
                anime_name=page.anime_name,
                kind=stream.kind,
                source=candidate.label,
                # The VIP player sets referrer policy no-referrer; m3u8 players do not.
                player_url=None if player_kind(player_url) == "vip" else player_url,
            )

        raise ParseError(f"no playable source for {page.title}: " + "; ".join(failures))

    def episode(self, url: str) -> Episode:
        """Resolve one play URL into a download-ready stream."""

        return self.resolve_stream(self.episode_page(url))

    def _current_episode(
        self, sources: Sequence[PlaylistSource], url: str
    ) -> tuple[PlaylistSource | None, PlaylistEpisode | None]:
        """Return the mirror and playlist entry a play URL belongs to."""

        for source in sources:
            entry = find_episode(source, url)
            if entry is not None:
                return source, entry
        return None, None

    def _resolve_candidate(self, candidate: SourceCandidate) -> tuple[str, PlayerStream]:
        """Resolve one mirror's player page into its stream URL."""

        player_url = candidate.player_url
        if player_url is None:
            page = parse_play_page(self.client.get_page(candidate.play_url), candidate.play_url)
            player_url = page.player_url
        if player_url is None:
            raise ParseError("play page has no embedded player")

        html = self.client.get_frame(player_url, page_url=candidate.play_url)
        kind = player_kind(player_url)
        if kind == "vip":
            config = parse_vip_player(html)
            request = build_vip_request(config, player_url)
            response = self.client.post_form(
                request.url, request.data, headers=request.headers, page_url=player_url
            )
            try:
                payload = response.json()
            except ValueError as error:
                raise ParseError(f"VIP API response is not JSON: {response.text[:200]}") from error
            return player_url, parse_vip_response(payload, config)

        # Unknown player paths usually still print a plain Vurl.
        url = urljoin(player_url, parse_m3u8_player(html))
        return player_url, PlayerStream(url=url, kind=stream_kind(url))

    def _probe(self, stream: PlayerStream, player_url: str) -> None:
        """Fail fast on dead mirrors before a download is started from them."""

        referer_page = None if player_kind(player_url) == "vip" else player_url
        if stream.kind == "mp4":
            # One byte is enough to prove the signed URL is live.
            response = self.client.get_resource(
                stream.url, page_url=referer_page, headers=PROBE_RANGE_HEADERS, paced=False
            )
            response.close()
            return

        response = self.client.get_resource(stream.url, page_url=referer_page)
        if not response.text.lstrip().startswith(HLS_SIGNATURE):
            raise ParseError(f"playlist is not HLS: {stream.url}")
