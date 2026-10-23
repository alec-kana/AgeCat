"""In-memory AGE site used by service and extractor tests."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import cast

import requests

from agecat.errors import AccessDeniedError, FetchError
from agecat.models import VideoStreamResponse

SITE = "https://www.agedm.io"
PLAYER = "https://jx.example.com:8443"


@dataclass
class FakeSource:
    """One playlist tab of the fake site."""

    key: str
    label: str
    episodes: int
    vip: bool = False
    label_format: str = "第{number:02d}集"


DEFAULT_SOURCES = (
    FakeSource("xigua", "西瓜", 2, vip=True),
    FakeSource("ffm3u8", "非凡", 2),
)


def playlist_html(anime_id: str, sources: tuple[FakeSource, ...]) -> str:
    """Render the playlist tabs and panes exactly as AGE structures them."""

    tabs = []
    panes = []
    for index, source in enumerate(sources, start=1):
        badge = '<span class="badge bg-danger">VIP</span> ' if source.vip else ""
        tabs.append(
            '<li class="nav-item"><button class="nav-link btn-sm" data-bs-toggle="pill" '
            f'data-bs-target="#playlist-source-{source.key}" type="button">'
            f"{badge}{source.label}</button></li>"
        )
        links = "".join(
            f'<li><a href="/play/{anime_id}/{index}/{number}" '
            f'class="video_detail_spisode_link">{source.label_format.format(number=number)}</a></li>'
            for number in range(1, source.episodes + 1)
        )
        panes.append(
            f'<div class="tab-pane fade" id="playlist-source-{source.key}">'
            f'<ul class="video_detail_episode">{links}</ul></div>'
        )
    return (
        '<div class="video_detail_playlist_wrapper pt-4"><ul class="nav nav-pills">'
        + "".join(tabs)
        + '</ul><div class="tab-content">'
        + "".join(panes)
        + "</div></div>"
    )


def detail_html(name: str, anime_id: str, sources: tuple[FakeSource, ...] = DEFAULT_SOURCES) -> str:
    """Render an anime detail page."""

    return (
        f"<html><head><title>{name} - AGE动漫</title></head><body>"
        f'<h2 class="video_detail_title">{name}</h2>'
        + playlist_html(anime_id, sources)
        + "</body></html>"
    )


def play_html(
    name: str,
    anime_id: str,
    source_index: int,
    number: int,
    sources: tuple[FakeSource, ...] = DEFAULT_SOURCES,
    *,
    player_path: str = "m3u8",
) -> str:
    """Render a play page whose iframe points at the fake player host."""

    source = sources[source_index - 1]
    label = source.label_format.format(number=number)
    return (
        f"<html><head><title>{name} {label} - 在线播放 - AGE动漫</title></head><body>"
        f'<iframe id="iframeForVideo" src="{PLAYER}/{player_path}/?url=age_{source.key}_{number}">'
        "</iframe>" + playlist_html(anime_id, sources) + "</body></html>"
    )


class FakeResponse:
    """Just enough of ``requests.Response`` for playlists, keys, and probes."""

    def __init__(self, body: bytes = b"", *, status_code: int = 200) -> None:
        self.content = body
        self.text = body.decode("utf-8", "replace")
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.closed = False

    def close(self) -> None:
        self.closed = True


class VideoResponse:
    status_code = 200
    content = b"a" * 2500

    def __init__(self) -> None:
        self.headers = {"content-length": str(len(self.content))}
        self.closed = False

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        for index in range(0, len(self.content), chunk_size):
            yield self.content[index : index + chunk_size]

    def close(self) -> None:
        self.closed = True


class DeniedMidStreamResponse:
    """Response revoked after the first chunk lands on disk."""

    status_code = 200

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = {"content-length": "6", **headers}
        self.closed = False

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        yield b"abc"
        raise AccessDeniedError("stream denied", status_code=403, headers=self.headers)

    def close(self) -> None:
        self.closed = True


class ResumedResponse:
    status_code = 206

    def __init__(self) -> None:
        self.headers = {"content-range": "bytes 3-5/6"}
        self.closed = False

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        yield b"def"

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeAgeClient:
    """Serves one anime from memory; every m3u8 player resolves to an MP4 URL.

    ``dead_sources`` makes those mirrors' streams fail their probe, and
    ``denials`` revokes that many video streams mid-download.
    """

    name: str = "Demo Anime"
    anime_id: str = "20260001"
    sources: tuple[FakeSource, ...] = DEFAULT_SOURCES
    dead_sources: frozenset[str] = frozenset()
    denials: int = 0
    denial_headers: dict[str, str] = field(default_factory=lambda: {"Server": "nginx"})
    resolve_calls: int = 0
    page_requests: list[str] = field(default_factory=list)
    stream_calls: list[tuple[str, dict[str, str], str | None]] = field(default_factory=list)
    closed: bool = False

    def final_url(self, url: str) -> str:
        return f"{SITE}/"

    def get_page(self, url: str, *, page_url: str | None = None) -> str:
        self.page_requests.append(url)
        path = url.split("://", 1)[1].split("/", 1)[1]
        parts = path.strip("/").split("/")
        if parts[0] == "detail":
            return detail_html(self.name, self.anime_id, self.sources)
        if parts[0] == "play":
            return play_html(self.name, self.anime_id, int(parts[2]), int(parts[3]), self.sources)
        raise AssertionError(f"unexpected page request: {url}")

    def get_frame(self, url: str, *, page_url: str) -> str:
        self.resolve_calls += 1
        token = url.rsplit("age_", 1)[1]
        return f"<script>var Vurl = 'https://cdn.example/{token}.mp4?sig={self.resolve_calls}';</script>"

    def post_form(
        self,
        url: str,
        data: Mapping[str, str],
        *,
        headers: Mapping[str, str] | None = None,
        page_url: str | None = None,
    ) -> requests.Response:
        raise AssertionError("post_form should not be called for m3u8 players")

    def get_resource(
        self,
        url: str,
        *,
        page_url: str | None = None,
        headers: Mapping[str, str] | None = None,
        paced: bool = True,
    ) -> requests.Response:
        if any(f"/{key}_" in url for key in self.dead_sources):
            raise FetchError(f"GET {url} failed: 404", url=url, status_code=404)
        return cast(requests.Response, FakeResponse(b"x"))

    def stream_video(
        self,
        url: str,
        *,
        cookies: Mapping[str, str],
        headers: Mapping[str, str] | None = None,
        page_url: str | None = None,
    ) -> VideoStreamResponse:
        self.stream_calls.append((url, dict(headers or {}), page_url))
        if self.denials > 0:
            self.denials -= 1
            return DeniedMidStreamResponse(self.denial_headers)
        if headers and "Range" in headers:
            return ResumedResponse()
        return VideoResponse()

    def close(self) -> None:
        self.closed = True
