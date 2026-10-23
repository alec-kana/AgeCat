from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .constants import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CIRCUIT_BREAKER_COOLDOWN,
    DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
    DEFAULT_CONCURRENCY,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_HOST_INTERVAL,
    DEFAULT_MAX_DELAY,
    DEFAULT_MIN_DELAY,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_RESOLVE_ATTEMPTS,
    DEFAULT_RETRIES,
    DEFAULT_RETRY_BUDGET,
    DEFAULT_SEGMENT_CONCURRENCY,
    DEFAULT_STAGGER,
)


@dataclass(frozen=True)
class DownloadOptions:
    """Runtime options shared by CLI and service orchestration."""

    output_dir: Path
    concurrency: int = DEFAULT_CONCURRENCY
    timeout: float = DEFAULT_READ_TIMEOUT
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    retries: int = DEFAULT_RETRIES
    chunk_size: int = DEFAULT_CHUNK_SIZE
    resume: bool = True
    overwrite: bool = False
    # Without this, an episode that does not finish leaves no partial files behind.
    keep_partial: bool = False
    progress: bool = True
    plain_progress: bool = False
    min_delay: float = DEFAULT_MIN_DELAY
    max_delay: float = DEFAULT_MAX_DELAY
    stagger: float = DEFAULT_STAGGER
    host_interval: float = DEFAULT_HOST_INTERVAL
    resolve_attempts: int = DEFAULT_RESOLVE_ATTEMPTS
    retry_budget: float = DEFAULT_RETRY_BUDGET
    circuit_breaker_threshold: int = DEFAULT_CIRCUIT_BREAKER_THRESHOLD
    circuit_breaker_cooldown: float = DEFAULT_CIRCUIT_BREAKER_COOLDOWN
    # None means "resolve the live AGE domain at startup".
    base_url: str | None = None
    # None means the default mirror priority; otherwise only these mirrors, in order.
    sources: tuple[str, ...] | None = None
    segment_concurrency: int = DEFAULT_SEGMENT_CONCURRENCY
    filter_ads: bool = True
    ffmpeg: str | None = None

    def __post_init__(self) -> None:
        """Validate runtime options early instead of silently coercing bad values."""

        if self.concurrency < 1:
            raise ValueError("concurrency must be greater than or equal to 1")
        if self.timeout <= 0:
            raise ValueError("timeout must be greater than 0")
        if self.connect_timeout <= 0:
            raise ValueError("connect_timeout must be greater than 0")
        if self.retries < 0:
            raise ValueError("retries must be greater than or equal to 0")
        if self.chunk_size <= 0:
            raise ValueError("chunk_size must be greater than 0")
        if self.min_delay < 0:
            raise ValueError("min_delay must be greater than or equal to 0")
        if self.max_delay < self.min_delay:
            raise ValueError("max_delay must be greater than or equal to min_delay")
        if self.stagger < 0:
            raise ValueError("stagger must be greater than or equal to 0")
        if self.host_interval < 0:
            raise ValueError("host_interval must be greater than or equal to 0")
        if self.resolve_attempts < 1:
            raise ValueError("resolve_attempts must be greater than or equal to 1")
        if self.retry_budget <= 0:
            raise ValueError("retry_budget must be greater than 0")
        if self.circuit_breaker_threshold < 0:
            raise ValueError("circuit_breaker_threshold must be greater than or equal to 0")
        if self.circuit_breaker_cooldown < 0:
            raise ValueError("circuit_breaker_cooldown must be greater than or equal to 0")
        if self.segment_concurrency < 1:
            raise ValueError("segment_concurrency must be greater than or equal to 1")
        if self.base_url is not None and not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        if self.sources is not None and not self.sources:
            raise ValueError("sources must name at least one source")

    @property
    def worker_count(self) -> int:
        """Return a safe worker count for ThreadPoolExecutor."""

        return self.concurrency

    @property
    def safe_chunk_size(self) -> int:
        """Return the validated chunk size used by streaming downloads."""

        return self.chunk_size

    @property
    def request_timeout(self) -> tuple[float, float]:
        """Return connect/read timeout tuple used by requests."""

        return (self.connect_timeout, self.timeout)
