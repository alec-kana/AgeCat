from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from .errors import AgeCatError

# Every domain AGE has used so far is agedm.<tld> or agefans.<tld>, plus age.tv.
AGE_DOMAIN_PATTERN = re.compile(r"^(?:agedm|agefans)\.[a-z]{2,}$")
AGE_FIXED_DOMAINS = frozenset({"age.tv"})
DETAIL_PATH_PATTERN = re.compile(r"^/detail/(?P<anime_id>\d+)/?$")
PLAY_PATH_PATTERN = re.compile(r"^/play/(?P<anime_id>\d+)/(?P<source>\d+)/(?P<episode>\d+)/?$")
URL_SEPARATOR_PATTERN = re.compile(r"[,\s]+")
SUPPORTED_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True)
class PlayRef:
    """Identity of one play URL: which anime, which playlist tab, which episode slot."""

    anime_id: str
    source_index: int
    episode_index: int


def split_urls(values: Sequence[str]) -> list[str]:
    """Split CLI URL arguments that may contain comma or whitespace separators."""

    return [item for value in values for item in URL_SEPARATOR_PATTERN.split(value.strip()) if item]


def normalized_host(url: str) -> str:
    """Return the lowercase hostname of a URL without a leading ``www.``."""

    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def is_age_host(host: str, *, extra_hosts: Iterable[str] = ()) -> bool:
    """Return whether a hostname belongs to AGE (any of its past or present domains)."""

    host = host.lower().removeprefix("www.")
    if host in AGE_FIXED_DOMAINS or AGE_DOMAIN_PATTERN.fullmatch(host):
        return True
    return host in {extra.lower().removeprefix("www.") for extra in extra_hosts}


def is_age_url(url: str, *, extra_hosts: Iterable[str] = ()) -> bool:
    """Return whether a URL is an http(s) URL on an AGE domain."""

    if any(character.isspace() for character in url):
        return False
    parts = urlsplit(url)
    if parts.scheme.lower() not in SUPPORTED_SCHEMES:
        return False
    return is_age_host(parts.hostname or "", extra_hosts=extra_hosts)


def is_season_url(url: str, *, extra_hosts: Iterable[str] = ()) -> bool:
    """Return whether a URL points to an AGE anime detail page (all episodes)."""

    return is_age_url(url, extra_hosts=extra_hosts) and bool(
        DETAIL_PATH_PATTERN.fullmatch(urlsplit(url).path)
    )


def is_episode_url(url: str, *, extra_hosts: Iterable[str] = ()) -> bool:
    """Return whether a URL points to an AGE play page for one episode."""

    return is_age_url(url, extra_hosts=extra_hosts) and bool(
        PLAY_PATH_PATTERN.fullmatch(urlsplit(url).path)
    )


def ensure_supported_url(url: str, *, extra_hosts: Iterable[str] = ()) -> None:
    """Raise when a URL is outside the supported AGE URL shapes."""

    if not is_season_url(url, extra_hosts=extra_hosts) and not is_episode_url(
        url, extra_hosts=extra_hosts
    ):
        raise AgeCatError(f"unsupported AGE URL: {url}")


def parse_play_url(url: str) -> PlayRef:
    """Parse the anime id, source tab, and episode slot out of a play URL."""

    match = PLAY_PATH_PATTERN.fullmatch(urlsplit(url).path)
    if match is None:
        raise AgeCatError(f"not an AGE play URL: {url}")
    return PlayRef(
        anime_id=match.group("anime_id"),
        source_index=int(match.group("source")),
        episode_index=int(match.group("episode")),
    )


def anime_id_from_url(url: str) -> str | None:
    """Return the anime id of a detail or play URL."""

    path = urlsplit(url).path
    match = DETAIL_PATH_PATTERN.fullmatch(path) or PLAY_PATH_PATTERN.fullmatch(path)
    return match.group("anime_id") if match else None


def rebase_url(url: str, base_url: str) -> str:
    """Move a URL onto another origin, keeping its path and query.

    Used to rewrite links copied from a dead AGE domain onto the live one.
    """

    parts = urlsplit(url)
    base = urlsplit(base_url)
    return urlunsplit((base.scheme, base.netloc, parts.path, parts.query, ""))


def parse_episode_selector(spec: str) -> frozenset[int]:
    """Parse a comma-separated episode number/range selector like '15-17,20'."""

    numbers: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise AgeCatError(f"invalid episode selector: {spec!r}")

        if "-" in part:
            start_text, _, end_text = part.partition("-")
            start = _parse_episode_bound(start_text, spec)
            end = _parse_episode_bound(end_text, spec)
            if end < start:
                raise AgeCatError(f"invalid episode range {part!r} in selector: {spec!r}")
            numbers.update(range(start, end + 1))
        else:
            numbers.add(_parse_episode_bound(part, spec))

    return frozenset(numbers)


def _parse_episode_bound(value: str, spec: str) -> int:
    """Parse one episode selector bound, rejecting non-positive or non-numeric values."""

    value = value.strip()
    if not value.isdigit() or int(value) < 1:
        raise AgeCatError(f"invalid episode selector: {spec!r}")
    return int(value)


def dedupe(values: Iterable[str]) -> list[str]:
    """Remove duplicates while preserving the original order."""

    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
