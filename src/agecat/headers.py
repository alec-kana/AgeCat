"""Referer/Origin/Sec-Fetch headers derived from the URL pair being fetched."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

FetchDest = Literal["document", "iframe", "empty", "video"]
FetchMode = Literal["navigate", "cors", "no-cors"]
FetchSite = Literal["same-origin", "same-site", "cross-site", "none"]


def header_value(headers: Mapping[str, str], name: str) -> str | None:
    """Return a header value case-insensitively."""

    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def origin(url: str) -> str | None:
    """Return the scheme://host[:port] origin of a URL, or None when it has none."""

    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def host(url: str) -> str:
    """Return the lowercase hostname of a URL, or an empty string when absent."""

    return (urlsplit(url).hostname or "").lower()


def registrable_domain(hostname: str) -> str:
    """Return the last two labels of a hostname as an approximate site key."""

    labels = hostname.lower().rstrip(".").split(".")
    return ".".join(labels[-2:]) if len(labels) > 2 else hostname.lower().rstrip(".")


def fetch_site(target_url: str, page_url: str | None) -> FetchSite:
    """Return the ``Sec-Fetch-Site`` relationship between a request and its page."""

    if not page_url:
        return "none"

    target_origin = origin(target_url)
    page_origin = origin(page_url)
    if target_origin is None or page_origin is None:
        return "none"
    if target_origin == page_origin:
        return "same-origin"
    if registrable_domain(host(target_url)) == registrable_domain(host(page_url)):
        return "same-site"
    return "cross-site"


def referer_for(target_url: str, page_url: str | None) -> str | None:
    """Return the ``Referer`` for ``strict-origin-when-cross-origin``, the browser default."""

    if not page_url:
        return None

    page_parts = urlsplit(page_url)
    page_origin = origin(page_url)
    if page_origin is None:
        return None

    target_parts = urlsplit(target_url)
    if page_parts.scheme.lower() == "https" and target_parts.scheme.lower() != "https":
        return None

    if fetch_site(target_url, page_url) == "same-origin":
        # Browsers strip credentials and the fragment, but keep path and query.
        return urlunsplit(
            (
                page_parts.scheme.lower(),
                page_parts.netloc.lower(),
                page_parts.path,
                page_parts.query,
                "",
            )
        )
    return f"{page_origin}/"


def identity_headers(
    target_url: str,
    *,
    page_url: str | None,
    dest: FetchDest,
    mode: FetchMode,
    send_origin: bool = False,
) -> dict[str, str]:
    """Build a mutually consistent ``Referer``/``Origin``/``Sec-Fetch-*`` header set."""

    site = fetch_site(target_url, page_url)
    headers: dict[str, str] = {
        "Sec-Fetch-Dest": dest,
        "Sec-Fetch-Mode": mode,
        "Sec-Fetch-Site": site,
    }

    referer = referer_for(target_url, page_url)
    if referer:
        headers["Referer"] = referer

    if send_origin and site != "none":
        page_origin = origin(page_url) if page_url else None
        if page_origin:
            headers["Origin"] = page_origin

    return headers


def site_root(url: str) -> str | None:
    """Return the site root page URL used as the ``Referer`` for page requests."""

    page_origin = origin(url)
    return f"{page_origin}/" if page_origin else None
