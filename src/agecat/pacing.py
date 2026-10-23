"""Request pacing primitives: jittered backoff, host gating, circuit breaking."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Mapping
from email.utils import parsedate_to_datetime
from threading import Lock

from .constants import DEFAULT_MAX_BACKOFF, MAX_RETRY_AFTER
from .headers import header_value

Sleeper = Callable[[float], None]
Clock = Callable[[], float]
LOGGER = logging.getLogger(__name__)


def full_jitter(base: float, attempt: int, *, cap: float = DEFAULT_MAX_BACKOFF) -> float:
    """Return a backoff delay drawn uniformly from ``[0, base * 2**attempt]``."""

    if base <= 0:
        return 0.0
    return random.uniform(0.0, min(cap, base * (2**attempt)))


def jittered(minimum: float, maximum: float) -> float:
    """Return a uniform delay in ``[minimum, maximum]``, tolerating an inverted range."""

    low = max(0.0, minimum)
    high = max(low, maximum)
    return random.uniform(low, high) if high > low else low


def retry_after_seconds(
    headers: Mapping[str, str],
    *,
    now: float | None = None,
    cap: float = MAX_RETRY_AFTER,
) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds or HTTP-date) into clamped seconds."""

    raw = header_value(headers, "Retry-After")
    if raw is None:
        return None

    raw = raw.strip()
    if not raw:
        return None

    if raw.isdigit():
        return min(float(raw), cap)

    try:
        target = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None

    delta = target.timestamp() - (now if now is not None else time.time())
    return min(max(delta, 0.0), cap)


class RateLimiter:
    """Minimum-interval gate shared by every worker thread, keyed by host.

    A worker-count cap alone cannot stop two workers hitting one host at once.
    """

    def __init__(
        self,
        min_interval: float,
        *,
        jitter: float = 0.0,
        sleeper: Sleeper = time.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self.min_interval = max(0.0, min_interval)
        self.jitter = max(0.0, jitter)
        self._sleeper = sleeper
        self._clock = clock
        self._lock = Lock()
        self._next_allowed: dict[str, float] = {}

    def acquire(self, host: str) -> float:
        """Block until this host may be contacted again and return the waited seconds."""

        if self.min_interval <= 0 and self.jitter <= 0:
            return 0.0

        with self._lock:
            now = self._clock()
            slot = max(now, self._next_allowed.get(host, 0.0))
            self._next_allowed[host] = slot + self.min_interval + random.uniform(0.0, self.jitter)
            wait = slot - now

        if wait > 0:
            LOGGER.debug("Rate limiter holding %s for %.2fs", host, wait)
            self._sleeper(wait)
        return max(0.0, wait)


class CircuitBreaker:
    """Pause every worker for a cooldown after repeated blocks from one host."""

    def __init__(
        self,
        threshold: int,
        cooldown: float,
        *,
        sleeper: Sleeper = time.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self.threshold = max(0, threshold)
        self.cooldown = max(0.0, cooldown)
        self._sleeper = sleeper
        self._clock = clock
        self._lock = Lock()
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        """Return whether the breaker can trip with its current configuration."""

        return self.threshold > 0 and self.cooldown > 0

    def wait(self, host: str) -> float:
        """Block while this host is in cooldown and return the waited seconds."""

        if not self.enabled:
            return 0.0

        with self._lock:
            wait = self._open_until.get(host, 0.0) - self._clock()

        if wait > 0:
            LOGGER.warning("Circuit breaker open for %s; pausing %.1fs", host, wait)
            self._sleeper(wait)
            return wait
        return 0.0

    def record_success(self, host: str) -> None:
        """Clear the consecutive-failure streak for a host."""

        if not self.enabled:
            return
        with self._lock:
            self._failures.pop(host, None)

    def record_block(self, host: str) -> bool:
        """Record one blocked request and return whether the breaker tripped."""

        if not self.enabled:
            return False

        with self._lock:
            failures = self._failures.get(host, 0) + 1
            self._failures[host] = failures
            if failures < self.threshold:
                return False
            self._failures[host] = 0
            self._open_until[host] = self._clock() + self.cooldown

        LOGGER.warning(
            "Circuit breaker tripped for %s after %d consecutive blocks; cooling down %.1fs",
            host,
            self.threshold,
            self.cooldown,
        )
        return True
