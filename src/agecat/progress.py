from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from .constants import PROGRESS_TITLE_MAX_LENGTH
from .models import DownloadProgressEvent, JobReport


@dataclass(frozen=True)
class ProgressCallbacks:
    """Callbacks passed from CLI UI into the service layer."""

    on_progress: Callable[[DownloadProgressEvent], None]
    on_done: Callable[[JobReport], None]


def supports_rich_live(console: Console | None = None) -> bool:
    """Return True if the terminal can render Rich's in-place Live updates.

    Rich only repositions the cursor when ``Console.is_terminal`` is true and
    the terminal isn't reported as "dumb". Some phone terminal apps (e.g.
    a-shell on iOS) capture stdout without exposing a real tty, so
    ``is_terminal`` is False; Rich's Live then withholds every intermediate
    frame and only prints once, at the very end, when the whole block exits.
    Callers should fall back to :func:`plain_download_progress` in that case.
    """

    console = console or Console()
    return console.is_terminal and not console.is_dumb_terminal


@contextmanager
def rich_download_progress(total_jobs: int) -> Iterator[ProgressCallbacks]:
    """Render overall and active per-file progress bars with Rich."""

    # Worker threads report progress concurrently; Rich updates must stay serialized.
    lock = Lock()
    active_tasks: dict[str, TaskID] = {}
    completed_jobs = 0
    failed_jobs = 0
    skipped_jobs = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        DownloadColumn(binary_units=True),
        TransferSpeedColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    ) as progress:
        # Overall size is unknown until every episode has resolved its stream response.
        overall_task = progress.add_task(
            overall_description(completed_jobs, total_jobs, failed_jobs, skipped_jobs),
            total=None,
        )

        def on_progress(event: DownloadProgressEvent) -> None:
            """Update overall and per-file progress for one downloader event."""

            key = event.episode.page_url
            with lock:
                task_id = active_tasks.get(key)
                if task_id is None:
                    # Create per-file tasks lazily when the stream headers are known.
                    task_id = progress.add_task(
                        trim_title(event.episode.title),
                        total=event.total_bytes,
                        completed=event.bytes_completed,
                    )
                    active_tasks[key] = task_id
                else:
                    progress.update(
                        task_id,
                        total=event.total_bytes,
                        completed=event.bytes_completed,
                    )

                if event.phase == "reset":
                    # Reset only affects the current file bar; cumulative transfer never rewinds.
                    return

                if event.bytes_delta:
                    progress.update(overall_task, advance=event.bytes_delta)

        def on_done(report: JobReport) -> None:
            """Update job counters and remove the finished per-file task."""

            nonlocal completed_jobs, failed_jobs, skipped_jobs

            key = report.result.episode.page_url if report.result else report.url
            with lock:
                completed_jobs += 1
                if report.error:
                    failed_jobs += 1
                elif report.result and report.result.status == "skipped":
                    skipped_jobs += 1

                task_id = active_tasks.pop(key, None)
                if task_id is not None:
                    # Keep the display focused on active downloads instead of completed rows.
                    progress.remove_task(task_id)

                progress.update(
                    overall_task,
                    description=overall_description(
                        completed_jobs,
                        total_jobs,
                        failed_jobs,
                        skipped_jobs,
                    ),
                )

        yield ProgressCallbacks(on_progress=on_progress, on_done=on_done)


@contextmanager
def plain_download_progress(
    total_jobs: int, *, min_interval: float = 1.0
) -> Iterator[ProgressCallbacks]:
    """Print a fresh status line per event instead of a Rich Live bar.

    This only relies on an ordinary newline, so it shows up as it happens
    even on terminal apps that don't give Rich a real tty (see
    :func:`supports_rich_live`). Per-file updates are throttled to
    ``min_interval`` seconds so a fast download doesn't flood the screen.
    """

    lock = Lock()
    completed_jobs = 0
    last_printed: dict[str, float] = {}

    def on_progress(event: DownloadProgressEvent) -> None:
        key = event.episode.page_url
        now = time.monotonic()
        with lock:
            if event.phase != "started" and now - last_printed.get(key, 0.0) < min_interval:
                return
            last_printed[key] = now
            print(f"  {plain_progress_line(event, completed_jobs, total_jobs)}", flush=True)

    def on_done(report: JobReport) -> None:
        nonlocal completed_jobs

        key = report.result.episode.page_url if report.result else report.url
        with lock:
            completed_jobs += 1
            last_printed.pop(key, None)
            if report.error:
                print(f"- failed: {report.url} ({completed_jobs}/{total_jobs})", flush=True)
            elif report.result:
                size = format_size(report.result.bytes_written)
                print(
                    f"+ {report.result.status}: {report.result.episode.title} "
                    f"[{size}] ({completed_jobs}/{total_jobs})",
                    flush=True,
                )

    yield ProgressCallbacks(on_progress=on_progress, on_done=on_done)


def plain_progress_line(event: DownloadProgressEvent, completed_jobs: int, total_jobs: int) -> str:
    """Build one status line for the plain-text progress fallback."""

    title = trim_title(event.episode.title)
    completed = format_size(event.bytes_completed)
    if event.total_bytes:
        pct = int(event.bytes_completed / event.total_bytes * 100)
        size_part = f"{completed}/{format_size(event.total_bytes)} ({pct}%)"
    else:
        size_part = completed
    return f"[{completed_jobs}/{total_jobs}] {title}: {size_part}"


def format_size(size: int) -> str:
    """Format a byte count for compact human-readable display."""

    units = ("B", "KB", "MB", "GB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def overall_description(
    completed_jobs: int,
    total_jobs: int,
    failed_jobs: int,
    skipped_jobs: int,
) -> str:
    """Build the overall progress task label."""

    parts = [f"Overall {completed_jobs}/{total_jobs} episodes"]
    if failed_jobs:
        parts.append(f"{failed_jobs} failed")
    if skipped_jobs:
        parts.append(f"{skipped_jobs} skipped")
    return " · ".join(parts)


def trim_title(title: str, *, max_length: int = PROGRESS_TITLE_MAX_LENGTH) -> str:
    """Normalize and truncate episode titles for compact terminal display."""

    normalized = " ".join(title.split())
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[: max_length - 1]}…"
