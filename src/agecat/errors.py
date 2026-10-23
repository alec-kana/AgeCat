from __future__ import annotations

from collections.abc import Mapping

# Only set when an edge layer, not the origin application, produced the response.
BOT_MITIGATION_HEADERS = ("cf-mitigated", "x-anubis", "x-anubis-challenge")
BOT_MITIGATION_SERVERS = ("cloudflare", "anubis")


class AgeCatError(Exception):
    """Base error for recoverable AgeCat failures."""


class FetchError(AgeCatError):
    """HTTP/network failure."""

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        status_code: int | None = None,
        headers: Mapping[str, str] | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code
        self.headers: dict[str, str] = {
            key.lower(): value for key, value in (headers or {}).items()
        }
        self.retry_after = retry_after


class AccessDeniedError(FetchError):
    """HTTP 403: signed access credentials were rejected, or the client was blocked.

    Resending the identical request cannot help, so callers re-resolve for
    fresh credentials or back off hard depending on :attr:`bot_mitigation`.
    """

    @property
    def bot_mitigation(self) -> bool:
        """Return whether an edge protection layer, not the origin, denied the request."""

        return is_bot_mitigation(self.headers)


class ParseError(AgeCatError):
    """Unexpected upstream HTML/API shape."""


class DownloadError(AgeCatError):
    """Download/write failure."""


def is_bot_mitigation(headers: Mapping[str, str]) -> bool:
    """Classify a denial as edge bot-mitigation rather than expired credentials."""

    # Heuristic: expired signed-cookie denials come back as short non-HTML
    # bodies, so an HTML denial from a mitigation edge is a block.
    lowered = {key.lower(): value for key, value in headers.items()}
    if any(name in lowered for name in BOT_MITIGATION_HEADERS):
        return True

    server = lowered.get("server", "").lower()
    if not any(name in server for name in BOT_MITIGATION_SERVERS):
        return False
    return "text/html" in lowered.get("content-type", "").lower()
