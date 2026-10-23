"""Find AGE's current domain.

AGE changes its main domain every few months. Its "easy to remember" domains
(``age.tv`` and friends) stay put and redirect to whatever is live, so the
redirect target is the current site.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

from .constants import DEFAULT_SITE_URL, DOMAIN_RESOLVER_URLS
from .errors import AgeCatError
from .headers import origin
from .urls import is_age_url

LOGGER = logging.getLogger(__name__)


class RedirectSource(Protocol):
    """Minimal HTTP dependency required to follow a domain redirect."""

    def final_url(self, url: str) -> str:
        """Return the URL a GET request ends up at after redirects."""

        ...


def resolve_site_url(
    client: RedirectSource,
    *,
    resolver_urls: Sequence[str] = DOMAIN_RESOLVER_URLS,
    fallback: str = DEFAULT_SITE_URL,
) -> str:
    """Return the live AGE origin, or ``fallback`` when no resolver answers."""

    for resolver_url in resolver_urls:
        try:
            final = client.final_url(resolver_url)
        except AgeCatError as error:
            LOGGER.debug("Domain resolver %s failed: %s", resolver_url, error)
            continue

        site = origin(final)
        if site and is_age_url(final) and origin(resolver_url) != site:
            LOGGER.info("Current AGE site: %s", site)
            return site
        LOGGER.debug("Domain resolver %s did not redirect to an AGE site: %s", resolver_url, final)

    LOGGER.warning("Could not resolve the current AGE domain; using %s", fallback)
    return fallback
