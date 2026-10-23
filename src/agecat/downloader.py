from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import requests

from .constants import DEFAULT_CHUNK_SIZE, DEFAULT_MAX_FILENAME_STEM_LENGTH
from .errors import AccessDeniedError, DownloadCancelled, DownloadError, FetchError
from .headers import header_value
from .models import DownloadProgressEvent, DownloadResult, Episode, VideoStreamResponse

PARTIAL_SUFFIX = ".part"
# MP4 downloads (and the ffmpeg remux) write <title>.mp4.part files; HLS
# downloads keep their segments in a <title>.hls.part directory.
PARTIAL_FILE_GLOB = f"*.mp4{PARTIAL_SUFFIX}"
HLS_PARTIAL_GLOB = f"*.hls{PARTIAL_SUFFIX}"

INVALID_FILENAME_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    # Avoid names that are invalid on Windows even when they look like normal stems.
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

ProgressCallback = Callable[[DownloadProgressEvent], None]
StopCheck = Callable[[], bool]
WriteMode = Literal["ab", "wb"]
LOGGER = logging.getLogger(__name__)
CONTENT_RANGE_PATTERN = re.compile(r"^bytes (?P<start>\d+)-(?P<end>\d+)/(?P<total>\d+|\*)$")


@dataclass
class StreamState:
    """Mutable identity state collected across stream retry attempts."""

    total_bytes: int | None = None
    validator: str | None = None

    def reset(self) -> None:
        """Forget stream identity after the server forces a full restart."""

        self.total_bytes = None
        self.validator = None

    def observe(self, headers: Mapping[str, str], total_bytes: int | None) -> None:
        """Validate and remember stable stream identity across retry attempts."""

        if self.total_bytes is None:
            self.total_bytes = total_bytes
        elif total_bytes is not None and self.total_bytes != total_bytes:
            raise DownloadError(
                f"stream size changed during retry: {self.total_bytes} -> {total_bytes}"
            )

        if self.validator is None:
            self.validator = _stream_validator(headers)


class VideoSource(Protocol):
    """Minimal HTTP dependency required by the downloader."""

    def stream_video(
        self,
        url: str,
        *,
        cookies: Mapping[str, str],
        headers: Mapping[str, str] | None = None,
        page_url: str | None = None,
    ) -> VideoStreamResponse:
        """Return a streaming video response for the given CDN URL."""

        ...


def sanitize_filename(
    value: str,
    *,
    fallback: str = "episode",
    max_length: int = DEFAULT_MAX_FILENAME_STEM_LENGTH,
) -> str:
    """Return a cross-platform safe filename stem for an episode title."""

    cleaned = INVALID_FILENAME_PATTERN.sub("_", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    cleaned = cleaned[:max_length].strip(" .")
    if not cleaned or cleaned.upper() in WINDOWS_RESERVED_NAMES:
        return fallback
    return cleaned


def target_path(output_dir: Path, title: str) -> Path:
    """Build the final MP4 output path for an episode title."""

    return output_dir / f"{sanitize_filename(title)}.mp4"


def output_paths(output_dir: Path, title: str) -> tuple[Path, Path]:
    """Return the MP4 path and the MPEG-TS fallback path for an episode title.

    HLS episodes end up as ``.ts`` when ffmpeg is unavailable, and either
    file counts as the episode already being downloaded.
    """

    mp4_path = target_path(output_dir, title)
    return mp4_path, mp4_path.with_suffix(".ts")


def download_episode(
    client: VideoSource,
    episode: Episode,
    output_dir: Path,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    resume: bool = True,
    overwrite: bool = False,
    progress: ProgressCallback | None = None,
    stream_retries: int = 3,
    should_stop: StopCheck | None = None,
) -> DownloadResult:
    """Download one episode with atomic writes, resume support, and validation.

    ``should_stop`` is polled between chunks; once it returns True the stream
    and the ``.part`` file are closed and :class:`DownloadCancelled` is raised.
    """

    output_dir.mkdir(parents=True, exist_ok=True)
    path = target_path(output_dir, episode.title)
    part_path = _partial_path(path)

    skipped = _skipped_result(episode, path, overwrite=overwrite)
    if skipped is not None:
        return skipped

    resume_from = _prepare_partial_file(path, part_path, resume=resume, overwrite=overwrite)
    written, total_bytes = _download_stream(
        client,
        episode,
        part_path,
        initial_resume_from=resume_from,
        chunk_size=chunk_size,
        progress=progress,
        stream_retries=stream_retries,
        should_stop=should_stop,
    )
    part_path.replace(path)
    return _downloaded_result(episode, path, written, total_bytes)


def _partial_path(path: Path) -> Path:
    """Return the temporary .part path for a final video path."""

    return path.with_name(f"{path.name}{PARTIAL_SUFFIX}")


def partial_paths(output_dir: Path, title: str) -> list[Path]:
    """Return every temporary path an episode title may write: MP4 part and HLS work dir."""

    mp4_path = target_path(output_dir, title)
    return [_partial_path(mp4_path), mp4_path.with_suffix(f".hls{PARTIAL_SUFFIX}")]


def discard_partial(path: Path) -> int | None:
    """Delete a partial file or HLS work dir; return bytes freed, or None if absent."""

    try:
        if path.is_dir():
            size = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
            shutil.rmtree(path)
        else:
            size = path.stat().st_size
            path.unlink()
    except FileNotFoundError:
        return None
    LOGGER.info("Removed partial download: %s", path)
    return size


def stale_partials(directory: Path, *, modified_before: float | None = None) -> list[Path]:
    """Return partial downloads left in a directory, optionally only untouched ones."""

    if not directory.is_dir():
        return []
    found: list[Path] = []
    candidates = [
        *(path for path in directory.glob(PARTIAL_FILE_GLOB) if path.is_file()),
        *(path for path in directory.glob(HLS_PARTIAL_GLOB) if path.is_dir()),
    ]
    for path in sorted(candidates):
        try:
            # A segment landing in an HLS work dir bumps the dir's mtime too.
            if modified_before is not None and path.stat().st_mtime >= modified_before:
                continue
        except FileNotFoundError:
            continue
        found.append(path)
    return found


def partial_folders(output_dir: Path) -> list[Path]:
    """Return the output folder and its per-anime subfolders, where partials are written."""

    if not output_dir.is_dir():
        return []
    return [
        output_dir,
        *sorted(
            path
            for path in output_dir.iterdir()
            if path.is_dir() and not path.name.endswith(f".hls{PARTIAL_SUFFIX}")
        ),
    ]


def raise_if_stopped(should_stop: StopCheck | None, title: str) -> None:
    """Abort the download when the run has been cancelled."""

    if should_stop is not None and should_stop():
        raise DownloadCancelled(f"download cancelled: {title}")


def existing_download(output_dir: Path, title: str, *, overwrite: bool) -> Path | None:
    """Return the completed output path when this episode is already downloaded."""

    if overwrite:
        return None
    for path in output_paths(output_dir, title):
        if path.exists():
            return path
    return None


def skipped_result(episode: Episode, path: Path) -> DownloadResult:
    """Build the result for an episode whose output file already exists."""

    existing_size = path.stat().st_size
    LOGGER.info("Skipping existing file: %s", path)
    return DownloadResult(
        episode=episode,
        path=path,
        status="skipped",
        bytes_written=existing_size,
        total_bytes=existing_size,
    )


def _skipped_result(
    episode: Episode,
    path: Path,
    *,
    overwrite: bool,
) -> DownloadResult | None:
    """Return a skipped result when a completed file already exists."""

    if overwrite or not path.exists():
        return None
    return skipped_result(episode, path)


def _prepare_partial_file(
    path: Path,
    part_path: Path,
    *,
    resume: bool,
    overwrite: bool,
) -> int:
    """Prepare existing output files and return the byte offset to resume from."""

    if overwrite:
        LOGGER.debug("Discarding existing output before overwrite: %s", path)
        path.unlink(missing_ok=True)
        part_path.unlink(missing_ok=True)
        return 0

    if not resume:
        LOGGER.debug("Discarding partial file because resume is disabled: %s", part_path)
        part_path.unlink(missing_ok=True)
        return 0

    resume_from = part_path.stat().st_size if part_path.exists() else 0
    if resume_from:
        LOGGER.info("Resuming partial file from byte %d: %s", resume_from, part_path)
    return resume_from


def _open_video_stream(
    client: VideoSource,
    episode: Episode,
    resume_from: int,
    validator: str | None = None,
) -> VideoStreamResponse:
    """Open a video stream, adding a Range header when resuming a partial file."""

    return client.stream_video(
        episode.stream_url,
        cookies=episode.cookies,
        headers=_request_headers(resume_from, validator),
        # The Referer comes from the embedded player, not the AGE page.
        page_url=episode.player_url,
    )


def _download_stream(
    client: VideoSource,
    episode: Episode,
    part_path: Path,
    *,
    initial_resume_from: int,
    chunk_size: int,
    progress: ProgressCallback | None,
    stream_retries: int,
    should_stop: StopCheck | None = None,
) -> tuple[int, int | None]:
    """Download the response body, reopening the stream with Range after interruptions."""

    retry_limit = max(0, stream_retries)
    resume_from = initial_resume_from
    state = StreamState()

    for attempt in range(retry_limit + 1):
        try:
            return _download_stream_attempt(
                client,
                episode,
                part_path,
                resume_from=resume_from,
                chunk_size=chunk_size,
                progress=progress,
                state=state,
                should_stop=should_stop,
            )
        except (DownloadError, FetchError, requests.RequestException) as error:
            # Stale credentials are not a transport hiccup; only the caller can
            # fix them by re-resolving, and the .part file is left for it.
            if attempt >= retry_limit or isinstance(error, AccessDeniedError):
                raise

            resume_from = _current_partial_size(part_path)
            LOGGER.warning(
                "Stream interrupted for %s: %s; retrying from byte %d (%d/%d)",
                episode.title,
                error,
                resume_from,
                attempt + 2,
                retry_limit + 1,
            )

    raise DownloadError(f"stream retry loop exited unexpectedly for {episode.title}")


def _download_stream_attempt(
    client: VideoSource,
    episode: Episode,
    part_path: Path,
    *,
    resume_from: int,
    chunk_size: int,
    progress: ProgressCallback | None,
    state: StreamState,
    should_stop: StopCheck | None = None,
) -> tuple[int, int | None]:
    """Run one stream attempt and return written bytes with expected total size."""

    raise_if_stopped(should_stop, episode.title)
    response = _open_video_stream(client, episode, resume_from, state.validator)

    try:
        normalized_resume_from = _normalize_resume_state(response, part_path, resume_from)
        if normalized_resume_from < resume_from:
            state.reset()
            _emit_progress(
                progress,
                episode,
                phase="reset",
                bytes_delta=0,
                bytes_completed=0,
                total_bytes=None,
            )
        range_info = _validate_resume_response(
            response.headers,
            normalized_resume_from,
            response.status_code,
        )
        total_bytes = _resolve_total_bytes(
            response.headers,
            normalized_resume_from,
            response.status_code,
            range_info=range_info,
        )
        state.observe(response.headers, total_bytes)
        written = _write_response_body(
            response,
            part_path,
            episode,
            mode=_write_mode(normalized_resume_from),
            chunk_size=chunk_size,
            resume_from=normalized_resume_from,
            total_bytes=total_bytes,
            progress=progress,
            should_stop=should_stop,
        )
        _ensure_complete(episode, written, total_bytes)
        return written, total_bytes
    finally:
        response.close()


def _current_partial_size(part_path: Path) -> int:
    """Return the current .part file size after a failed stream attempt."""

    return part_path.stat().st_size if part_path.exists() else 0


def _request_headers(resume_from: int, validator: str | None) -> dict[str, str]:
    """Return HTTP headers for resumed downloads with optional If-Range protection."""

    if not resume_from:
        return {}

    headers = {"Range": f"bytes={resume_from}-"}
    if validator:
        headers["If-Range"] = validator
    return headers


def _normalize_resume_state(
    response: VideoStreamResponse,
    part_path: Path,
    resume_from: int,
) -> int:
    """Reset a partial file when the server ignores a resume Range request."""

    if resume_from and response.status_code == 200:
        LOGGER.warning("Server ignored Range request; restarting partial file: %s", part_path)
        part_path.unlink(missing_ok=True)
        return 0
    return resume_from


def _validate_resume_response(
    headers: Mapping[str, str],
    resume_from: int,
    status_code: int,
) -> tuple[int, int, int | None] | None:
    """Validate Content-Range before appending a resumed response body."""

    if not resume_from:
        return None
    if status_code != 206:
        raise DownloadError(f"resume request returned unexpected status {status_code}")

    range_info = _parse_content_range(header_value(headers, "Content-Range"))
    if range_info is None:
        raise DownloadError("resumed response is missing valid Content-Range")

    start, end, _total = range_info
    if start != resume_from:
        raise DownloadError(f"resumed response starts at {start}, expected {resume_from}")
    if end < start:
        raise DownloadError(f"invalid Content-Range end before start: {start}-{end}")

    return range_info


def _parse_content_range(value: str | None) -> tuple[int, int, int | None] | None:
    """Parse a Content-Range header into start, end, and total byte counts."""

    if value is None:
        return None

    match = CONTENT_RANGE_PATTERN.fullmatch(value.strip())
    if not match:
        return None

    total_value = match.group("total")
    return (
        int(match.group("start")),
        int(match.group("end")),
        None if total_value == "*" else int(total_value),
    )


def _resolve_total_bytes(
    headers: Mapping[str, str],
    resume_from: int,
    status_code: int,
    *,
    range_info: tuple[int, int, int | None] | None,
) -> int | None:
    """Return expected final size using validated range or content length metadata."""

    if status_code == 206 and range_info is not None:
        return range_info[2]

    content_length = header_value(headers, "Content-Length")
    if content_length and content_length.isdigit():
        return resume_from + int(content_length)
    return None


def _stream_validator(headers: Mapping[str, str]) -> str | None:
    """Return a strong-enough If-Range validator from response headers."""

    return header_value(headers, "ETag") or header_value(headers, "Last-Modified")


def _write_mode(resume_from: int) -> WriteMode:
    """Return append mode only when the server accepted a resumed download."""

    return "ab" if resume_from else "wb"


def _write_response_body(
    response: VideoStreamResponse,
    part_path: Path,
    episode: Episode,
    *,
    mode: WriteMode,
    chunk_size: int,
    resume_from: int,
    total_bytes: int | None,
    progress: ProgressCallback | None,
    should_stop: StopCheck | None = None,
) -> int:
    """Write a streaming response body to the .part file and emit progress."""

    written = resume_from
    _emit_progress(
        progress,
        episode,
        phase="started",
        bytes_delta=0,
        bytes_completed=written,
        total_bytes=total_bytes,
    )

    with part_path.open(mode) as file:
        LOGGER.debug("Writing response body to %s with mode=%s", part_path, mode)
        for chunk in response.iter_content(chunk_size=chunk_size):
            # Leaving through the with block closes the handle, so an
            # interrupted run never keeps a deleted file's space reserved.
            raise_if_stopped(should_stop, episode.title)
            if not chunk:
                continue
            file.write(chunk)
            written += len(chunk)
            _emit_progress(
                progress,
                episode,
                phase="advanced",
                bytes_delta=len(chunk),
                bytes_completed=written,
                total_bytes=total_bytes,
            )

    return written


def _emit_progress(
    progress: ProgressCallback | None,
    episode: Episode,
    *,
    phase: Literal["started", "advanced", "reset"],
    bytes_delta: int,
    bytes_completed: int,
    total_bytes: int | None,
) -> None:
    """Emit one progress event when a progress callback is configured."""

    if not progress:
        return

    progress(
        DownloadProgressEvent(
            episode=episode,
            phase=phase,
            bytes_delta=bytes_delta,
            bytes_completed=bytes_completed,
            total_bytes=total_bytes,
        )
    )


def _ensure_complete(
    episode: Episode,
    written: int,
    total_bytes: int | None,
) -> None:
    """Raise when the downloaded byte count does not match response metadata."""

    if total_bytes is not None and written != total_bytes:
        LOGGER.debug(
            "Incomplete download detected for %s: %d/%d bytes",
            episode.title,
            written,
            total_bytes,
        )
        raise DownloadError(
            f"incomplete download for {episode.title}: {written}/{total_bytes} bytes"
        )


def _downloaded_result(
    episode: Episode,
    path: Path,
    bytes_written: int,
    total_bytes: int | None,
) -> DownloadResult:
    """Build the successful download result after atomic promotion."""

    LOGGER.info("Downloaded file: %s", path)
    return DownloadResult(
        episode=episode,
        path=path,
        status="downloaded",
        bytes_written=bytes_written,
        total_bytes=total_bytes,
    )
