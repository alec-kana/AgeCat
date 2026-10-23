from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import Any, cast

import requests

from .constants import (
    DEFAULT_BACKOFF,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_RETRIES,
    FORBIDDEN_STATUS_CODE,
    RETRY_STATUS_CODES,
)
from .errors import AccessDeniedError, FetchError
from .headers import header_value, host, identity_headers, site_root
from .models import VideoStreamResponse
from .pacing import RateLimiter, full_jitter, retry_after_seconds

DEFAULT_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "DNT": "1",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
}

# Enough to tell an edge block apart from an expired signed cookie.
DIAGNOSTIC_HEADERS = ("server", "cf-ray", "cf-mitigated", "content-type", "retry-after")
LOGGER = logging.getLogger(__name__)


class AgeClient:
    """HTTP client wrapper for AGE pages, player APIs, and video requests."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float | tuple[float, float] = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
        retries: int = DEFAULT_RETRIES,
        backoff: float = DEFAULT_BACKOFF,
        headers: Mapping[str, str] | None = None,
        rate_limiter: RateLimiter | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = max(0.0, backoff)
        self.headers = {**DEFAULT_HEADERS, **(headers or {})}
        self.rate_limiter = rate_limiter
        self.sleeper = sleeper

    def __enter__(self) -> AgeClient:
        """Return this client so callers can manage it with a context manager."""

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the underlying HTTP session when leaving a context manager."""

        self.close()

    def close(self) -> None:
        """Close the underlying HTTP session and release pooled connections."""

        self.session.close()

    def get_page(self, url: str, *, page_url: str | None = None) -> str:
        """Fetch an HTML page the way a browser navigation would."""

        return self.request("GET", url, page_url=page_url or site_root(url), dest="document").text

    def final_url(self, url: str) -> str:
        """Return where a page navigation to ``url`` lands after redirects."""

        response = self.request("GET", url, dest="document", allow_redirects=True)
        response.close()
        return response.url

    def get_frame(self, url: str, *, page_url: str) -> str:
        """Fetch an embedded player page as the iframe inside ``page_url`` would."""

        return self.request("GET", url, page_url=page_url, dest="iframe").text

    def post_form(
        self,
        url: str,
        data: Mapping[str, str],
        *,
        headers: Mapping[str, str] | None = None,
        page_url: str | None = None,
    ) -> requests.Response:
        """Submit a form-encoded XHR the way the embedded player script does."""

        return self.request(
            "POST",
            url,
            data=dict(data),
            headers=headers,
            page_url=page_url,
            dest="empty",
        )

    def get_resource(
        self,
        url: str,
        *,
        page_url: str | None = None,
        headers: Mapping[str, str] | None = None,
        paced: bool = True,
    ) -> requests.Response:
        """Fetch a playlist, key, or media segment as an hls.js XHR would.

        Media segments pass ``paced=False``: they are hundreds of requests to a
        CDN, not page loads, and the per-host interval would crawl.
        """

        return self.request(
            "GET", url, headers=headers, page_url=page_url, dest="empty", paced=paced
        )

    def stream_video(
        self,
        url: str,
        *,
        cookies: Mapping[str, str],
        headers: Mapping[str, str] | None = None,
        page_url: str | None = None,
    ) -> VideoStreamResponse:
        """Open a streaming response for an episode video file.

        CDN hotlink protection checks the ``Referer`` derived from ``page_url``.
        """

        request_headers = {
            "Accept-Encoding": "identity;q=1, *;q=0",
            **(headers or {}),
        }
        return cast(
            VideoStreamResponse,
            self.request(
                "GET",
                url,
                headers=request_headers,
                cookies=dict(cookies),
                stream=True,
                page_url=page_url,
                dest="video",
            ),
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | tuple[float, float] | None = None,
        page_url: str | None = None,
        dest: str = "empty",
        paced: bool = True,
        **kwargs: Any,
    ) -> requests.Response:
        """Send an HTTP request with retry/backoff and normalized errors."""

        request_headers = {
            **self.headers,
            **self._identity_headers(method, url, page_url, dest),
            **(headers or {}),
        }
        target_host = host(url)
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            attempt_number = attempt + 1
            if paced and self.rate_limiter is not None:
                self.rate_limiter.acquire(target_host)
            try:
                LOGGER.debug(
                    "HTTP %s %s attempt %d/%d",
                    method,
                    url,
                    attempt_number,
                    self.retries + 1,
                )
                response = self.session.request(
                    method,
                    url,
                    headers=request_headers,
                    timeout=timeout if timeout is not None else self.timeout,
                    **kwargs,
                )
                if response.status_code >= 400:
                    self._log_rejection(method, url, response, request_headers, kwargs)
                # Resending the same signed request cannot fix a 403.
                if response.status_code == FORBIDDEN_STATUS_CODE:
                    raise self._access_denied(method, url, response)
                # Retry only transient upstream/server throttling errors.
                if response.status_code in RETRY_STATUS_CODES and attempt < self.retries:
                    retry_after = retry_after_seconds(response.headers)
                    response.close()
                    LOGGER.warning(
                        "HTTP %s %s returned %d; retrying attempt %d/%d",
                        method,
                        url,
                        response.status_code,
                        attempt_number + 1,
                        self.retries + 1,
                    )
                    self._sleep(attempt, retry_after)
                    continue
                response.raise_for_status()
                LOGGER.debug("HTTP %s %s completed with %d", method, url, response.status_code)
                return response
            except requests.RequestException as error:
                last_error = error
                if attempt >= self.retries:
                    break
                LOGGER.warning(
                    "HTTP %s %s failed: %s; retrying attempt %d/%d",
                    method,
                    url,
                    error,
                    attempt_number + 1,
                    self.retries + 1,
                )
                self._sleep(attempt)

        LOGGER.debug("HTTP %s %s exhausted retries: %s", method, url, last_error)
        raise FetchError(
            f"{method} {url} failed: {last_error}",
            url=url,
            status_code=_error_status_code(last_error),
        ) from last_error

    def _identity_headers(
        self,
        method: str,
        url: str,
        page_url: str | None,
        dest: str,
    ) -> dict[str, str]:
        """Return browser-consistent Referer/Origin/Sec-Fetch headers for one request."""

        if dest in {"document", "iframe"}:
            mode = "navigate"
        elif dest == "video":
            mode = "no-cors"
        else:
            mode = "cors"

        return identity_headers(
            url,
            page_url=page_url,
            dest=cast(Any, dest),
            mode=cast(Any, mode),
            # Browsers attach Origin to every non-GET request, and to any CORS one.
            send_origin=method.upper() not in {"GET", "HEAD"} or mode == "cors",
        )

    def _access_denied(
        self,
        method: str,
        url: str,
        response: requests.Response,
    ) -> AccessDeniedError:
        """Build a classified 403 error and release the rejected response."""

        response_headers = dict(response.headers)
        retry_after = retry_after_seconds(response_headers)
        response.close()
        error = AccessDeniedError(
            f"{method} {url} was denied with 403",
            url=url,
            status_code=FORBIDDEN_STATUS_CODE,
            headers=response_headers,
            retry_after=retry_after,
        )
        LOGGER.warning(
            "HTTP %s %s denied with 403 (%s)",
            method,
            url,
            "bot mitigation" if error.bot_mitigation else "expired or invalid access credentials",
        )
        return error

    def _log_rejection(
        self,
        method: str,
        url: str,
        response: requests.Response,
        request_headers: Mapping[str, str],
        kwargs: Mapping[str, Any],
    ) -> None:
        """Log request/response context for a rejection, redacting signed values."""

        if not LOGGER.isEnabledFor(logging.INFO):
            return

        diagnostics = {
            name: value
            for name in DIAGNOSTIC_HEADERS
            if (value := header_value(response.headers, name)) is not None
        }
        cookies = kwargs.get("cookies") or {}
        LOGGER.info(
            "HTTP %s %s rejected with %d (referer=%s, cookies=[%s], response=%s)",
            method,
            url,
            response.status_code,
            request_headers.get("Referer", "<none>"),
            ",".join(sorted(cookies)) if isinstance(cookies, Mapping) else "<opaque>",
            diagnostics or "<no diagnostic headers>",
        )

    def _sleep(self, attempt: int, retry_after: float | None = None) -> None:
        """Sleep before the next retry, honoring Retry-After over jittered backoff."""

        delay = retry_after if retry_after is not None else full_jitter(self.backoff, attempt)
        if delay > 0:
            self.sleeper(delay)


def _error_status_code(error: Exception | None) -> int | None:
    """Return the HTTP status carried by a requests error, when it has one."""

    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code if isinstance(status_code, int) else None
