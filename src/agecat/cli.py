from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from . import __version__
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
from .service import AgeCatService
from .urls import parse_episode_selector, split_urls

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

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
            "Exit codes:\n"
            "  0  All downloads completed or were skipped\n"
            "  1  At least one URL failed or no episode was found\n"
            "  2  Invalid CLI usage or options"
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
            "Fastest, but restores the burst pattern that draws 403s."
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


def main(argv: Sequence[str] | None = None) -> int:
    """Run the AgeCat CLI and return a process exit code."""

    ensure_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
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

    input_urls = split_urls(args.urls)
    if not input_urls:
        if not sys.stdin.isatty():
            print("No URL provided.", file=sys.stderr)
            return EXIT_USAGE
        try:
            input_urls = split_urls([input("? AGE 链接：")])
        except EOFError:
            print("No URL provided.", file=sys.stderr)
            return EXIT_USAGE
    if not input_urls:
        print("No URL provided.", file=sys.stderr)
        return EXIT_USAGE

    service = AgeCatService(options)

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
    return EXIT_FAILURE if failed else EXIT_OK


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
