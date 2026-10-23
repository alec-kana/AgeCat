from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Any

from rich import box
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__
from .client import AgeClient
from .constants import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CIRCUIT_BREAKER_COOLDOWN,
    DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
    DEFAULT_CONCURRENCY,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_HOST_INTERVAL,
    DEFAULT_MAX_DELAY,
    DEFAULT_MIN_DELAY,
    DEFAULT_OUTPUT_DIR_NAME,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_RESOLVE_ATTEMPTS,
    DEFAULT_RETRIES,
    DEFAULT_RETRY_BUDGET,
    DEFAULT_SEGMENT_CONCURRENCY,
    DEFAULT_SOURCE_PRIORITY,
    DEFAULT_STAGGER,
    FFMPEG_ENV_VAR,
    KEEP_PARTIAL_ENV_VAR,
    PLAIN_PROGRESS_ENV_VAR,
)
from .errors import AgeCatError
from .logging_config import configure_logging
from .models import EpisodeJob, JobReport
from .options import DownloadOptions
from .progress import (
    format_size,
    plain_download_progress,
    rich_download_progress,
    supports_rich_live,
)
from .search import DEFAULT_SEARCH_LIMIT, SearchResult, search_anime
from .service import AgeCatService
from .site import resolve_site_url
from .urls import parse_episode_selector, split_urls

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130
SEARCH_COMMAND = "search"
# Handled like Ctrl-C so the run stops cleanly instead of dying mid-write.
TERMINATION_SIGNALS = ("SIGTERM", "SIGHUP")
# Below this terminal width the search table drops its metadata column.
SEARCH_META_MIN_WIDTH = 80
# Below this width there is no room for a URL column: results print as blocks.
SEARCH_TABLE_MIN_WIDTH = 56

_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def env_flag(name: str) -> bool:
    """Read a boolean CLI-flag default from an environment variable."""

    return os.environ.get(name, "").strip().lower() in _TRUTHY_ENV_VALUES


def ensure_utf8_output() -> None:
    """Print Chinese titles even where stdout defaults to a legacy code page.

    Windows pipes and redirects default to cp1252/cp936, which cannot encode
    every title AGE uses.
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if callable(reconfigure) and encoding != "utf8":
            reconfigure(encoding="utf-8", errors="replace")


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""

    parser = argparse.ArgumentParser(
        prog="agecat",
        description="Download AGE (agedm) episodes from detail or play URLs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Commands:\n"
            "  search <keyword>  Search AGE and list matching detail URLs\n"
            "\n"
            "Exit codes:\n"
            "  0  All downloads completed or were skipped\n"
            "  1  At least one URL failed, no episode was found, or nothing matched\n"
            "  2  Invalid CLI usage or options\n"
            "  130  Interrupted (Ctrl-C, SIGTERM or SIGHUP); unfinished downloads were stopped"
        ),
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help=(
            "AGE detail URLs (/detail/<id>) or play URLs (/play/<id>/<source>/<ep>), "
            "on any AGE domain. Whitespace and commas are accepted."
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path.cwd() / DEFAULT_OUTPUT_DIR_NAME,
        help=f"Output directory. Default: ./{DEFAULT_OUTPUT_DIR_NAME}",
    )
    parser.add_argument(
        "-e",
        "--episodes",
        type=str,
        default=None,
        metavar="SPEC",
        help=(
            "Restrict detail URLs to these episode numbers, e.g. '15-17' "
            "or '1,3,5-8'. Explicit play URLs are never filtered."
        ),
    )
    parser.add_argument(
        "-s",
        "--source",
        type=str,
        default=None,
        metavar="LIST",
        help=(
            "Only use these mirrors, in this order: keys (xigua, ffm3u8), tab "
            "labels (西瓜, 非凡), or tab numbers (1, 2), comma-separated. "
            f"Default: try {', '.join(DEFAULT_SOURCE_PRIORITY)}."
        ),
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=None,
        metavar="URL",
        help=(
            "Use this AGE site instead of resolving the current domain via age.tv, "
            "e.g. https://www.agedm.io"
        ),
    )
    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=(
            f"Concurrent episode downloads. Default: {DEFAULT_CONCURRENCY}. "
            "Lower this first if a source starts returning 403."
        ),
    )
    parser.add_argument(
        "--segment-concurrency",
        type=int,
        default=DEFAULT_SEGMENT_CONCURRENCY,
        metavar="N",
        help=(
            "Concurrent HLS segment downloads per episode (m3u8 mirrors only). "
            f"Default: {DEFAULT_SEGMENT_CONCURRENCY}"
        ),
    )
    parser.add_argument(
        "--keep-ads",
        action="store_true",
        help="Keep ad breaks spliced into m3u8 mirror playlists instead of dropping them.",
    )
    parser.add_argument(
        "--ffmpeg",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "ffmpeg used to remux HLS downloads into MP4. Default: "
            f"${FFMPEG_ENV_VAR}, then ffmpeg on PATH; without it HLS episodes stay .ts."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_READ_TIMEOUT,
        help=f"HTTP read timeout in seconds. Default: {DEFAULT_READ_TIMEOUT:g}",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=DEFAULT_CONNECT_TIMEOUT,
        help=f"HTTP connect timeout in seconds. Default: {DEFAULT_CONNECT_TIMEOUT:g}",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"HTTP and stream retry count. Default: {DEFAULT_RETRIES}",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"Download chunk size in bytes. Default: {DEFAULT_CHUNK_SIZE}",
    )
    pacing_group = parser.add_argument_group(
        "pacing",
        "Randomized request pacing. Trades throughput for a request cadence that "
        "does not look automated. Set the delays to 0 to download as fast as possible.",
    )
    pacing_group.add_argument(
        "--no-pacing",
        action="store_true",
        help=(
            "Disable every pacing delay at once, overriding the four flags below. "
            "Pacing is already off by default; this undoes delays set by those flags."
        ),
    )
    pacing_group.add_argument(
        "--min-delay",
        type=float,
        default=DEFAULT_MIN_DELAY,
        metavar="SECONDS",
        help=f"Minimum wait between episodes on one worker. Default: {DEFAULT_MIN_DELAY:g}",
    )
    pacing_group.add_argument(
        "--max-delay",
        type=float,
        default=DEFAULT_MAX_DELAY,
        metavar="SECONDS",
        help=f"Maximum wait between episodes on one worker. Default: {DEFAULT_MAX_DELAY:g}",
    )
    pacing_group.add_argument(
        "--stagger-start",
        type=float,
        default=DEFAULT_STAGGER,
        metavar="SECONDS",
        help=(
            "Maximum random wait before each worker's first request, so a batch does "
            f"not open every connection at once. Default: {DEFAULT_STAGGER:g}"
        ),
    )
    pacing_group.add_argument(
        "--host-interval",
        type=float,
        default=DEFAULT_HOST_INTERVAL,
        metavar="SECONDS",
        help=(
            "Minimum gap between requests to one host across all workers. "
            f"Default: {DEFAULT_HOST_INTERVAL:g}"
        ),
    )
    recovery_group = parser.add_argument_group(
        "recovery",
        "How hard to work at recovering from HTTP 403 denials before giving up.",
    )
    recovery_group.add_argument(
        "--resolve-attempts",
        type=int,
        default=DEFAULT_RESOLVE_ATTEMPTS,
        metavar="N",
        help=(
            "Times to re-resolve an episode for fresh access credentials after a 403. "
            f"Default: {DEFAULT_RESOLVE_ATTEMPTS}"
        ),
    )
    recovery_group.add_argument(
        "--retry-budget",
        type=float,
        default=DEFAULT_RETRY_BUDGET,
        metavar="SECONDS",
        help=f"Total wall-clock recovery time per episode. Default: {DEFAULT_RETRY_BUDGET:g}",
    )
    recovery_group.add_argument(
        "--circuit-breaker-threshold",
        type=int,
        default=DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
        metavar="N",
        help=(
            "Consecutive denials from one host before pausing all workers. "
            f"0 disables. Default: {DEFAULT_CIRCUIT_BREAKER_THRESHOLD}"
        ),
    )
    recovery_group.add_argument(
        "--circuit-breaker-cooldown",
        type=float,
        default=DEFAULT_CIRCUIT_BREAKER_COOLDOWN,
        metavar="SECONDS",
        help=(
            f"Pause duration once the breaker trips. Default: {DEFAULT_CIRCUIT_BREAKER_COOLDOWN:g}"
        ),
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Discard existing .part files instead of resuming.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing completed files.",
    )
    parser.add_argument(
        "--keep-partial",
        action="store_true",
        default=env_flag(KEEP_PARTIAL_ENV_VAR),
        help=(
            "Keep partial downloads (.mp4.part files, .hls.part segment folders) of "
            "interrupted or failed episodes so a later run can resume them. By default "
            "they are deleted, along with leftovers in the output folder (in anime "
            "folders this run does not use, only ones untouched for 30 minutes). "
            f"Defaults to on if the {KEEP_PARTIAL_ENV_VAR} environment variable is set "
            "to a truthy value."
        ),
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable progress bar.",
    )
    parser.add_argument(
        "--plain-progress",
        action="store_true",
        default=env_flag(PLAIN_PROGRESS_ENV_VAR),
        help=(
            "Force line-based progress output instead of the Rich live bar. Use this on "
            "phone terminal apps (e.g. a-shell) where the live bar reports itself as a "
            f"compatible terminal but never actually redraws in place. Defaults to on if "
            f"the {PLAIN_PROGRESS_ENV_VAR} environment variable is set to a truthy value."
        ),
    )
    verbosity_group = parser.add_mutually_exclusive_group()
    verbosity_group.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Show diagnostic logs. Use -vv for HTTP-level debug details.",
    )
    verbosity_group.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress diagnostic logs except errors. Summary output is unchanged.",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show version and exit.",
    )
    return parser


def build_search_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the search subcommand."""

    parser = argparse.ArgumentParser(
        prog="agecat search",
        description="Search AGE and print matching detail URLs.",
    )
    parser.add_argument(
        "keyword",
        nargs="?",
        help="Anime title keyword, searched by the AGE site itself (Simplified Chinese).",
    )
    parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=DEFAULT_SEARCH_LIMIT,
        metavar="N",
        help=f"Show at most N results. Default: {DEFAULT_SEARCH_LIMIT}",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=None,
        metavar="URL",
        help="Search this AGE site instead of resolving the current domain via age.tv.",
    )
    verbosity_group = parser.add_mutually_exclusive_group()
    verbosity_group.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Show diagnostic logs. Use -vv for HTTP-level debug details.",
    )
    verbosity_group.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress diagnostic logs except errors.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the AgeCat CLI and return a process exit code."""

    ensure_utf8_output()
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Dispatched before the download parser so that plain `agecat <url>` keeps
    # working without requiring a subcommand.
    if arguments and arguments[0] == SEARCH_COMMAND:
        return search_main(arguments[1:])

    parser = build_parser()
    args = parser.parse_args(arguments)
    configure_logging(verbose=args.verbose, quiet=args.quiet)
    try:
        options = options_from_args(args)
    except ValueError as error:
        print(f"argument error: {error}", file=sys.stderr)
        return EXIT_USAGE

    episodes = None
    if args.episodes is not None:
        try:
            episodes = parse_episode_selector(args.episodes)
        except AgeCatError as error:
            print(f"argument error: {error}", file=sys.stderr)
            return EXIT_USAGE

    input_urls = split_urls(args.urls) or split_urls([prompt_for("? AGE 链接：")])
    if not input_urls:
        print("No URL provided.", file=sys.stderr)
        return EXIT_USAGE

    service = AgeCatService(options)

    try:
        with termination_as_interrupt():
            return download_and_report(service, options, input_urls, episodes)
    except KeyboardInterrupt:
        # download_many has already stopped every worker and closed its files.
        print("- interrupted; unfinished downloads were stopped.", file=sys.stderr)
        print_removed_partials(service)
        return EXIT_INTERRUPTED


def download_and_report(
    service: AgeCatService,
    options: DownloadOptions,
    input_urls: list[str],
    episodes: frozenset[int] | None,
) -> int:
    """Collect episodes, download them, and print the per-episode summary."""

    try:
        episode_jobs = service.collect_episode_urls(input_urls, episodes=episodes)
    except AgeCatError as error:
        print(f"- {error}", file=sys.stderr)
        return EXIT_FAILURE

    if not episode_jobs:
        print("- No episode found.", file=sys.stderr)
        return EXIT_FAILURE

    started_at = time.perf_counter()
    reports = run_downloads(service, options, episode_jobs)
    elapsed = time.perf_counter() - started_at

    downloaded = [item for item in reports if item.result and item.result.status == "downloaded"]
    skipped = [item for item in reports if item.result and item.result.status == "skipped"]
    failed = [item for item in reports if item.error]

    for item in reports:
        if item.result:
            marker = "+" if item.result.status == "downloaded" else "="
            size = format_size(item.result.bytes_written)
            print(f"{marker} {item.result.status}: {item.result.episode.title} [{size}]")
            print(f"  -> {item.result.path}")
        else:
            print(f"- failed: {item.url}")
            print(f"  {item.error}")

    print(
        f"+ done in {elapsed:.2f}s: "
        f"{len(downloaded)} downloaded, {len(skipped)} skipped, {len(failed)} failed"
    )
    print_removed_partials(service)
    return EXIT_FAILURE if failed else EXIT_OK


def print_removed_partials(service: AgeCatService) -> None:
    """Report how much storage the run's partial-download cleanup released."""

    removed = service.removed_partials
    if not removed:
        return
    freed = sum(size for _path, size in removed)
    print(f"= removed {len(removed)} partial download(s), freeing {format_size(freed)}")


@contextmanager
def termination_as_interrupt() -> Iterator[None]:
    """Raise KeyboardInterrupt on SIGTERM/SIGHUP so they get the Ctrl-C cleanup.

    Their default action kills the process mid-write, skipping it entirely.
    """

    def interrupt(signum: int, frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    previous: dict[int, Any] = {}
    for name in TERMINATION_SIGNALS:
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            previous[signum] = signal.signal(signum, interrupt)
        except (OSError, ValueError):
            # Not the main thread, or the host does not allow the signal.
            continue
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def prompt_for(label: str) -> str:
    """Ask for one missing argument, returning empty when input is unavailable."""

    # Piped or redirected input must fail with usage instead of blocking.
    if not sys.stdin.isatty():
        return ""
    try:
        return input(label).strip()
    except EOFError:
        return ""


def search_main(argv: Sequence[str]) -> int:
    """Run the search subcommand and return a process exit code."""

    parser = build_search_parser()
    args = parser.parse_args(argv)
    configure_logging(verbose=args.verbose, quiet=args.quiet)
    if args.limit < 1:
        print("argument error: --limit must be at least 1", file=sys.stderr)
        return EXIT_USAGE

    keyword = (args.keyword or "").strip() or prompt_for("? 搜索关键词：")
    if not keyword:
        print("No keyword provided.", file=sys.stderr)
        return EXIT_USAGE

    client = AgeClient()
    try:
        site_url = args.base_url.rstrip("/") if args.base_url else resolve_site_url(client)
        outcome = search_anime(client, site_url, keyword, limit=args.limit)
    except AgeCatError as error:
        print(f"- {error}", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        client.close()

    if not outcome.results:
        print(f"- No search result for {keyword!r}.", file=sys.stderr)
        return EXIT_FAILURE

    render_search_results(outcome.results, outcome.total)
    return EXIT_OK


def render_search_results(results: Sequence[SearchResult], total: int) -> None:
    """Render search matches, keeping the URL column intact for copy/paste."""

    # Rich's automatic highlighter recolours numbers and stray words in the
    # summary line, which fights the styling the table already applies.
    console = Console(highlight=False)
    if console.width < SEARCH_TABLE_MIN_WIDTH:
        # Too narrow for a URL column at all: one block per result instead.
        for result in results:
            line = f"[bold]{escape(result.title)}[/bold]"
            if meta := format_result_meta(result):
                line += f"  [dim]{escape(meta)}[/dim]"
            console.print(line)
            # soft_wrap keeps the URL one unbroken string, so copying it never picks up newlines.
            console.print(f"  [cyan]{escape(result.url)}[/cyan]", soft_wrap=True)
        console.print(search_summary(len(results), total))
        return

    # A phone held upright leaves too little room for three columns; the
    # metadata column goes first so titles stay readable next to the URL.
    show_meta = console.width >= SEARCH_META_MIN_WIDTH

    # The URL is the payload of this command, so pin its column to the widest
    # value. Narrow terminals then shrink the title and metadata instead of
    # cropping a URL into something that cannot be pasted back.
    table = Table(box=box.SIMPLE_HEAD, header_style="bold cyan", pad_edge=False)
    table.add_column("Title", style="bold", overflow="fold")
    if show_meta:
        table.add_column("Status · Premiere", style="dim")
    table.add_column(
        "URL",
        style="cyan",
        no_wrap=True,
        width=max(len(result.url) for result in results),
    )
    for result in results:
        if show_meta:
            table.add_row(result.title, format_result_meta(result), result.url)
        else:
            table.add_row(result.title, result.url)

    console.print(table)
    console.print(search_summary(len(results), total))


def search_summary(shown: int, total: int) -> str:
    """Return the result-count line, pointing at --limit when matches were cut off."""

    summary = f"[green]+[/green] {shown} result(s)"
    if total > shown:
        summary += f" of {total}; use --limit to see more or refine the keyword"
    return summary


def format_result_meta(result: SearchResult) -> str:
    """Join the episode status, type, and premiere date of one result."""

    parts = (result.status, result.kind, result.premiere)
    return " · ".join(part for part in parts if part)


def options_from_args(args: argparse.Namespace) -> DownloadOptions:
    """Convert parsed CLI arguments into service runtime options."""

    return DownloadOptions(
        output_dir=args.output,
        concurrency=args.concurrency,
        timeout=args.timeout,
        connect_timeout=args.connect_timeout,
        retries=args.retries,
        chunk_size=args.chunk_size,
        resume=not args.no_resume,
        overwrite=args.overwrite,
        keep_partial=args.keep_partial,
        progress=not args.no_progress,
        plain_progress=args.plain_progress,
        min_delay=0.0 if args.no_pacing else args.min_delay,
        max_delay=0.0 if args.no_pacing else args.max_delay,
        stagger=0.0 if args.no_pacing else args.stagger_start,
        host_interval=0.0 if args.no_pacing else args.host_interval,
        resolve_attempts=args.resolve_attempts,
        retry_budget=args.retry_budget,
        circuit_breaker_threshold=args.circuit_breaker_threshold,
        circuit_breaker_cooldown=args.circuit_breaker_cooldown,
        base_url=args.base_url,
        sources=parse_sources(args.source),
        segment_concurrency=args.segment_concurrency,
        filter_ads=not args.keep_ads,
        ffmpeg=args.ffmpeg,
    )


def parse_sources(value: str | None) -> tuple[str, ...] | None:
    """Split a ``--source`` list; ``None`` keeps the default mirror priority."""

    if value is None:
        return None
    sources = tuple(item.strip() for item in value.split(",") if item.strip())
    if not sources:
        raise ValueError("--source must name at least one source")
    return sources


def run_downloads(
    service: AgeCatService,
    options: DownloadOptions,
    episode_jobs: list[EpisodeJob],
) -> list[JobReport]:
    """Run downloads with or without Rich progress rendering."""

    if not options.progress:
        return service.download_many(episode_jobs)

    use_rich = not options.plain_progress and supports_rich_live()
    progress_factory = rich_download_progress if use_rich else plain_download_progress
    with progress_factory(len(episode_jobs)) as progress:
        return service.download_many(
            episode_jobs,
            on_progress=progress.on_progress,
            on_done=progress.on_done,
        )
