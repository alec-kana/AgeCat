from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from threading import Event, Lock, local
from typing import Protocol

from .client import AgeClient
from .constants import DEFAULT_BLOCKED_BACKOFF, DEFAULT_MAX_BACKOFF, STALE_PARTIAL_AGE
from .downloader import (
    VideoSource,
    discard_partial,
    download_episode,
    existing_download,
    partial_folders,
    partial_paths,
    sanitize_filename,
    skipped_result,
    stale_partials,
)
from .errors import AccessDeniedError, AgeCatError, DownloadCancelled
from .extractor import AgeExtractor, EpisodeSource
from .headers import host
from .hls import SegmentSource, download_hls_episode
from .models import DownloadProgressEvent, EpisodeJob, JobReport
from .options import DownloadOptions
from .pacing import CircuitBreaker, RateLimiter, full_jitter, jittered
from .site import RedirectSource, resolve_site_url
from .urls import (
    ensure_supported_url,
    is_episode_url,
    is_season_url,
    normalized_host,
    rebase_url,
)

DownloadProgress = Callable[[DownloadProgressEvent], None]
JobDone = Callable[["JobReport"], None]
Sleeper = Callable[[float], None]
Clock = Callable[[], float]
LOGGER = logging.getLogger(__name__)
# How often the coordinating thread wakes up to notice Ctrl-C while workers run.
WAIT_POLL_INTERVAL = 0.5

BLOCKED_HINT = (
    "blocked by anti-bot protection; try a lower --concurrency, "
    "a larger --min-delay/--max-delay, or retry later"
)


class AgeCatClient(EpisodeSource, VideoSource, SegmentSource, RedirectSource, Protocol):
    """Combined client protocol required by extraction and downloading."""

    ...


class WorkerClientState(local):
    """Thread-local storage for one reusable client per worker thread."""

    client: AgeCatClient | None = None
    started: bool = False


class AgeCatService:
    """Application service that coordinates URL expansion and downloads."""

    def __init__(
        self,
        options: DownloadOptions,
        *,
        client_factory: Callable[[], AgeCatClient] | None = None,
        sleeper: Sleeper | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        self.options = options
        self.client_factory = client_factory or self._default_client
        self._cancelled = Event()
        # Every pause (pacing, rate limiting, backoff, circuit breaker) goes
        # through this, so cancelling wakes a sleeping worker immediately.
        self.sleeper = sleeper or self._interruptible_sleep
        self.clock = clock
        self._partials_lock = Lock()
        self._touched_dirs: set[Path] = set()
        self.removed_partials: list[tuple[Path, int]] = []
        # Shared across worker threads, not per-worker.
        self.rate_limiter = RateLimiter(
            options.host_interval,
            jitter=options.host_interval,
            sleeper=self.sleeper,
            clock=clock,
        )
        self.circuit_breaker = CircuitBreaker(
            options.circuit_breaker_threshold,
            options.circuit_breaker_cooldown,
            sleeper=self.sleeper,
            clock=clock,
        )
        self.site_url: str | None = options.base_url.rstrip("/") if options.base_url else None

    @property
    def cancelled(self) -> bool:
        """Return whether the current download run has been cancelled."""

        return self._cancelled.is_set()

    def cancel(self) -> None:
        """Stop the current run: queued episodes never start, running ones stop soon."""

        self._cancelled.set()

    def collect_episode_urls(
        self,
        input_urls: list[str],
        *,
        episodes: frozenset[int] | None = None,
    ) -> list[EpisodeJob]:
        """Expand supported input URLs into a de-duplicated episode job list.

        ``episodes``, when given, restricts season/category expansions to the
        matching episode numbers; explicit episode URLs are never filtered.
        """

        LOGGER.info("Collecting episodes from %d input URL(s)", len(input_urls))
        client = self.client_factory()
        try:
            extractor = self._extractor(client)
            site_url = self._site_url(client)
            extra_hosts = (normalized_host(site_url),)
            jobs: list[EpisodeJob] = []

            for input_url in input_urls:
                ensure_supported_url(input_url, extra_hosts=extra_hosts)
                # Links copied from a dead domain still work on the live one.
                url = rebase_url(input_url, site_url)
                if is_season_url(url, extra_hosts=extra_hosts):
                    LOGGER.debug("Expanding season URL: %s", url)
                    season = extractor.season_episode_urls(url)
                    matched_numbers: set[int] = set()
                    for episode_url, number in zip(
                        season.episode_urls, season.episode_numbers, strict=True
                    ):
                        if episodes is not None and number not in episodes:
                            continue
                        if number is not None:
                            matched_numbers.add(number)
                        jobs.append(EpisodeJob(url=episode_url, anime_name=season.anime_name))

                    if episodes is not None:
                        missing = sorted(episodes - matched_numbers)
                        if missing:
                            LOGGER.warning(
                                "Requested episode(s) not found in %s: %s",
                                url,
                                ", ".join(str(number) for number in missing),
                            )
                elif is_episode_url(url, extra_hosts=extra_hosts):
                    LOGGER.debug("Adding episode URL: %s", url)
                    jobs.append(EpisodeJob(url=url))

            deduped_jobs = dedupe_jobs(jobs)
            LOGGER.info("Collected %d episode URL(s)", len(deduped_jobs))
            return deduped_jobs
        finally:
            close_client(client)

    def download_many(
        self,
        episode_jobs: list[EpisodeJob],
        *,
        on_progress: DownloadProgress | None = None,
        on_done: JobDone | None = None,
    ) -> list[JobReport]:
        """Download multiple episode jobs concurrently and return job reports.

        If the caller is interrupted (Ctrl-C, a termination signal raised as
        ``KeyboardInterrupt``, or a failing callback), the run is cancelled
        and this waits for every worker to close its stream and partial
        files before re-raising, so no thread keeps writing in the background.
        """

        self._cancelled.clear()
        with self._partials_lock:
            self._touched_dirs.clear()
            self.removed_partials.clear()
        LOGGER.info(
            "Downloading %d episode(s) with %d worker(s)",
            len(episode_jobs),
            self.options.worker_count,
        )
        reports: list[JobReport] = []
        worker_clients: list[AgeCatClient] = []
        worker_clients_lock = Lock()
        worker_state = WorkerClientState()

        def worker_client() -> AgeCatClient:
            """Return the current worker thread client, creating it on first use."""

            client = worker_state.client
            if client is None:
                client = self.client_factory()
                worker_state.client = client
                with worker_clients_lock:
                    worker_clients.append(client)
            return client

        def download_with_worker_client(job: EpisodeJob) -> JobReport:
            """Download one job using the session owned by the current worker thread."""

            try:
                self._pace_worker(worker_state)
            except DownloadCancelled as error:
                return JobReport(url=job.url, error=str(error))
            return self._download_one_with_client(
                worker_client(),
                job.url,
                anime_name=job.anime_name,
                on_progress=on_progress,
            )

        # Managed by hand: the context manager's exit waits for every queued
        # job, so an interrupted batch would keep downloading to the end.
        executor = ThreadPoolExecutor(max_workers=self.options.worker_count)
        try:
            # Each worker owns one reusable HTTP session across its assigned jobs.
            futures = {
                executor.submit(download_with_worker_client, job): job for job in episode_jobs
            }

            pending = set(futures)
            while pending:
                # Wake up regularly instead of blocking until a job finishes: a
                # plain lock wait cannot be interrupted on Windows, so Ctrl-C
                # would otherwise only land once the current episode completed.
                done, pending = wait(
                    pending, timeout=WAIT_POLL_INTERVAL, return_when=FIRST_COMPLETED
                )
                for future in done:
                    report = future.result()
                    reports.append(report)
                    if on_done:
                        on_done(report)
        except BaseException:
            LOGGER.warning("Interrupted; stopping active downloads")
            self.cancel()
            shutdown_until_idle(executor)
            raise
        else:
            executor.shutdown(wait=True)
        finally:
            for client in worker_clients:
                close_client(client)
            if not self.options.keep_partial:
                self._sweep_partials()

        return reports

    def download_one(
        self,
        url: str,
        *,
        anime_name: str | None = None,
        on_progress: DownloadProgress | None = None,
    ) -> JobReport:
        """Resolve and download one episode URL, isolating recoverable failures."""

        client = self.client_factory()

        try:
            return self._download_one_with_client(
                client,
                url,
                anime_name=anime_name,
                on_progress=on_progress,
            )
        finally:
            close_client(client)

    def _pace_worker(self, worker_state: WorkerClientState) -> None:
        """Spread out a worker's first request, then pace the jobs after it."""

        if not worker_state.started:
            worker_state.started = True
            delay = jittered(0.0, self.options.stagger)
        else:
            delay = jittered(self.options.min_delay, self.options.max_delay)

        if delay > 0:
            LOGGER.debug("Pacing worker for %.2fs before next episode", delay)
            self.sleeper(delay)

    def _download_one_with_client(
        self,
        client: AgeCatClient,
        url: str,
        *,
        anime_name: str | None = None,
        on_progress: DownloadProgress | None = None,
    ) -> JobReport:
        """Resolve and download one episode URL, removing its partials if it does not finish."""

        # Filled once the episode's output paths are known.
        partials: set[Path] = set()
        report: JobReport | None = None
        try:
            report = self._download_with_recovery(
                client,
                url,
                anime_name=anime_name,
                on_progress=on_progress,
                partials=partials,
            )
            return report
        except DownloadCancelled as error:
            LOGGER.info("Episode cancelled: %s", url)
            report = JobReport(url=url, error=str(error))
            return report
        finally:
            # Also runs when an unexpected exception escapes, so a crash
            # never strands a half-written file.
            if not self.options.keep_partial and (report is None or report.error):
                for part in partials:
                    self._remove_partial(part)

    def _download_with_recovery(
        self,
        client: AgeCatClient,
        url: str,
        *,
        anime_name: str | None,
        on_progress: DownloadProgress | None,
        partials: set[Path],
    ) -> JobReport:
        """Resolve and download one episode URL, re-resolving on denied access.

        Each attempt mints fresh credentials and re-enters the download, which
        picks the existing ``.part`` file back up at its current byte offset.
        """

        extractor = self._extractor(client)
        target_host = host(url)
        deadline = self.clock() + self.options.retry_budget
        overwrite = self.options.overwrite
        attempts = self.options.resolve_attempts

        for attempt in range(attempts):
            try:
                self.circuit_breaker.wait(target_host)
                return self._resolve_and_download(
                    client,
                    extractor,
                    url,
                    anime_name=anime_name,
                    on_progress=on_progress,
                    overwrite=overwrite,
                    partials=partials,
                )
            except AccessDeniedError as error:
                self.circuit_breaker.record_block(target_host)
                # Keep whatever bytes already landed for the next attempt.
                overwrite = False
                delay = self._denial_backoff(error, attempt)
                if attempt + 1 >= attempts or self.clock() + delay >= deadline:
                    LOGGER.warning("Episode permanently denied: %s (%s)", url, error)
                    return JobReport(url=url, error=describe_denial(error))
                LOGGER.warning(
                    "Access denied for %s (%s); re-resolving in %.1fs (attempt %d/%d)",
                    url,
                    "bot mitigation" if error.bot_mitigation else "expired credentials",
                    delay,
                    attempt + 2,
                    attempts,
                )
                self.sleeper(delay)
            except DownloadCancelled:
                raise
            except AgeCatError as error:
                LOGGER.warning("Episode failed with recoverable error: %s", error)
                return JobReport(url=url, error=str(error))
            except OSError as error:
                LOGGER.warning("Episode failed with file system error: %s", error)
                return JobReport(url=url, error=str(error))

        return JobReport(url=url, error=f"exhausted {attempts} resolve attempts for {url}")

    def _resolve_and_download(
        self,
        client: AgeCatClient,
        extractor: AgeExtractor,
        url: str,
        *,
        anime_name: str | None,
        on_progress: DownloadProgress | None,
        overwrite: bool,
        partials: set[Path],
    ) -> JobReport:
        """Resolve fresh episode credentials and run one full download attempt."""

        self._raise_if_cancelled()
        LOGGER.info("Resolving episode: %s", url)
        page = extractor.episode_page(url)
        output_dir = self._episode_output_dir(page.anime_name, anime_name)
        partials.update(partial_paths(output_dir, page.title))
        with self._partials_lock:
            self._touched_dirs.add(output_dir)

        # Stop before the stream request mints credentials we would discard.
        existing = existing_download(output_dir, page.title, overwrite=overwrite)
        if existing is not None:
            self.circuit_breaker.record_success(host(url))
            return JobReport(url=url, result=skipped_result(page.unresolved_episode(), existing))

        self._raise_if_cancelled()
        episode = extractor.resolve_stream(page)
        if episode.kind == "hls":
            result = download_hls_episode(
                client,
                episode,
                output_dir,
                resume=self.options.resume,
                overwrite=overwrite,
                progress=on_progress,
                segment_concurrency=self.options.segment_concurrency,
                ffmpeg=self.options.ffmpeg,
                filter_ads=self.options.filter_ads,
                should_stop=self._cancelled.is_set,
            )
        else:
            result = download_episode(
                client,
                episode,
                output_dir,
                chunk_size=self.options.safe_chunk_size,
                resume=self.options.resume,
                overwrite=overwrite,
                progress=on_progress,
                stream_retries=self.options.retries,
                should_stop=self._cancelled.is_set,
            )
        self.circuit_breaker.record_success(host(url))
        LOGGER.info("%s episode: %s", result.status.title(), result.episode.title)
        return JobReport(url=url, result=result)

    def _episode_output_dir(self, resolved_anime_name: str | None, anime_name: str | None) -> Path:
        """Return the per-anime output directory for an episode."""

        effective_anime_name = anime_name or resolved_anime_name
        output_dir = self.options.output_dir
        if effective_anime_name:
            output_dir = output_dir / sanitize_filename(effective_anime_name)
        return output_dir

    def _denial_backoff(self, error: AccessDeniedError, attempt: int) -> float:
        """Return how long to wait before re-resolving after a denial."""

        # An edge block is not about our credentials, so it waits far longer
        # than an expired cookie, which re-resolving fixes immediately.
        if error.retry_after is not None:
            return error.retry_after
        base = DEFAULT_BLOCKED_BACKOFF if error.bot_mitigation else self.options.min_delay
        return full_jitter(base, attempt, cap=DEFAULT_MAX_BACKOFF)

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep, but wake and abort the episode as soon as the run is cancelled."""

        if self._cancelled.wait(seconds):
            raise DownloadCancelled("download run cancelled")

    def _raise_if_cancelled(self) -> None:
        """Abort before starting more network work for a cancelled run."""

        if self._cancelled.is_set():
            raise DownloadCancelled("download run cancelled")

    def _remove_partial(self, part: Path) -> None:
        """Delete one partial download and remember it for the run summary."""

        try:
            freed = discard_partial(part)
        except OSError as error:
            LOGGER.warning("Could not remove partial download %s: %s", part, error)
            return
        if freed is not None:
            with self._partials_lock:
                self.removed_partials.append((part, freed))

    def _sweep_partials(self) -> None:
        """Remove partial downloads left in the output folder, e.g. by an earlier killed run.

        Folders this run used are cleared outright; other anime folders only lose
        partials untouched for STALE_PARTIAL_AGE, since another run may be writing there.
        """

        with self._partials_lock:
            touched = set(self._touched_dirs)
        cutoff = time.time() - STALE_PARTIAL_AGE
        for directory in sorted(touched.union(partial_folders(self.options.output_dir))):
            modified_before = None if directory in touched else cutoff
            for part in stale_partials(directory, modified_before=modified_before):
                self._remove_partial(part)

    def _extractor(self, client: AgeCatClient) -> AgeExtractor:
        """Create an extractor that honors the configured mirror priority."""

        return AgeExtractor(client, source_priority=self.options.sources)

    def _site_url(self, client: AgeCatClient) -> str:
        """Return the live AGE origin, resolving it once per service."""

        if self.site_url is None:
            self.site_url = resolve_site_url(client)
        return self.site_url

    def _default_client(self) -> AgeCatClient:
        """Create the default HTTP client for one worker."""

        return AgeClient(
            timeout=self.options.request_timeout,
            retries=self.options.retries,
            rate_limiter=self.rate_limiter,
            sleeper=self.sleeper,
        )


def describe_denial(error: AccessDeniedError) -> str:
    """Return a user-facing message that names the actual cause of a 403."""

    if error.bot_mitigation:
        return f"{error}: {BLOCKED_HINT}"
    return f"{error}: access credentials were rejected after re-resolving the episode"


def dedupe_jobs(jobs: list[EpisodeJob]) -> list[EpisodeJob]:
    """Remove duplicate episode URLs while preserving first-seen order and anime name."""

    seen: set[str] = set()
    result: list[EpisodeJob] = []
    for job in jobs:
        if job.url in seen:
            continue
        seen.add(job.url)
        result.append(job)
    return result


def shutdown_until_idle(executor: ThreadPoolExecutor) -> None:
    """Drop queued jobs and wait for running ones to stop, even through repeated Ctrl-C.

    Returning early would leave worker threads writing in the background; on
    hosts like a-Shell they live inside the app, holding deleted files' space.
    """

    while True:
        try:
            executor.shutdown(wait=True, cancel_futures=True)
            return
        except KeyboardInterrupt:
            LOGGER.warning("Still closing active downloads; please wait")


def close_client(client: object) -> None:
    """Close clients that expose a close method without requiring it in tests."""

    close = getattr(client, "close", None)
    if callable(close):
        close()
