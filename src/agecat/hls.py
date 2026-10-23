"""HLS (m3u8) episode downloads: segments in Python, then an ffmpeg remux to MP4.

Segments land one file each in a ``<title>.hls.part`` work directory, so an
interrupted run resumes at segment granularity. AES-128 segments are
decrypted as they arrive. Once every segment is present they are joined into
one MPEG-TS stream and remuxed (``-c copy``) into the final MP4; without
ffmpeg the joined ``.ts`` is kept as the output instead.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from threading import Lock
from typing import Literal, Protocol
from urllib.parse import urljoin, urlsplit

import requests

from .aes import BLOCK_SIZE, cbc_decrypt
from .constants import DEFAULT_SEGMENT_CONCURRENCY, FFMPEG_ENV_VAR
from .downloader import (
    StopCheck,
    output_paths,
    raise_if_stopped,
    sanitize_filename,
    skipped_result,
)
from .errors import DownloadError, ParseError
from .models import DownloadProgressEvent, DownloadResult, Episode

HLS_SIGNATURE = "#EXTM3U"
ATTRIBUTE_PATTERN = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
# Ad breaks spliced into mirror playlists are short and served from elsewhere.
AD_GROUP_MAX_DURATION = 120.0
MANIFEST_NAME = "source.txt"
FFMPEG_COMMAND = "ffmpeg"
JOINED_NAME = "joined.ts"
LOGGER = logging.getLogger(__name__)

ProgressCallback = Callable[[DownloadProgressEvent], None]


class SegmentSource(Protocol):
    """Minimal HTTP dependency required by the HLS downloader."""

    def get_resource(
        self,
        url: str,
        *,
        page_url: str | None = None,
        headers: Mapping[str, str] | None = None,
        paced: bool = True,
    ) -> requests.Response:
        """Return the raw response for a playlist, key, or segment URL."""

        ...


@dataclass(frozen=True)
class SegmentKey:
    """Decryption parameters attached to a run of segments by ``#EXT-X-KEY``."""

    method: str
    uri: str | None = None
    iv: bytes | None = None


@dataclass(frozen=True)
class Segment:
    """One media segment of a media playlist."""

    uri: str
    duration: float
    sequence: int
    group: int = 0
    key: SegmentKey | None = None


@dataclass(frozen=True)
class MediaPlaylist:
    """Parsed media playlist: ordered segments plus an optional init section."""

    segments: list[Segment]
    init_uri: str | None = None
    init_key: SegmentKey | None = None


@dataclass(frozen=True)
class Variant:
    """One ``#EXT-X-STREAM-INF`` entry of a master playlist."""

    uri: str
    bandwidth: int = 0
    pixels: int = 0


@dataclass
class SegmentProgress:
    """Thread-safe byte and segment counters for one episode."""

    total_segments: int
    done_segments: int = 0
    done_bytes: int = 0
    lock: Lock = field(default_factory=Lock)

    def add(self, size: int) -> tuple[int, int | None]:
        """Record one finished segment and return completed bytes and estimated total."""

        with self.lock:
            self.done_segments += 1
            self.done_bytes += size
            return self.done_bytes, self.estimated_total()

    def estimated_total(self) -> int | None:
        """Extrapolate the final size from the average finished segment."""

        if not self.done_segments:
            return None
        if self.done_segments >= self.total_segments:
            return self.done_bytes
        return self.done_bytes * self.total_segments // self.done_segments


def parse_attributes(text: str) -> dict[str, str]:
    """Parse an HLS attribute list (``A=1,B="x"``) into a dict."""

    return {name: value.strip('"') for name, value in ATTRIBUTE_PATTERN.findall(text)}


def is_master_playlist(text: str) -> bool:
    """Return whether a playlist lists variant streams instead of segments."""

    return "#EXT-X-STREAM-INF" in text


def parse_master_playlist(text: str, base_url: str) -> list[Variant]:
    """Parse the variant streams of a master playlist."""

    variants: list[Variant] = []
    pending: dict[str, str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = parse_attributes(line.partition(":")[2])
        elif line and not line.startswith("#") and pending is not None:
            width, _, height = pending.get("RESOLUTION", "").partition("x")
            variants.append(
                Variant(
                    uri=urljoin(base_url, line),
                    bandwidth=int(pending.get("BANDWIDTH", "0") or 0),
                    pixels=int(width) * int(height) if width.isdigit() and height.isdigit() else 0,
                )
            )
            pending = None
    return variants


def best_variant(variants: list[Variant]) -> Variant:
    """Pick the highest-resolution variant, using bandwidth to break ties."""

    if not variants:
        raise ParseError("master playlist has no variant streams")
    return max(variants, key=lambda variant: (variant.pixels, variant.bandwidth))


def parse_media_playlist(text: str, base_url: str) -> MediaPlaylist:
    """Parse segments, keys, discontinuities, and the init section of a media playlist."""

    if not text.lstrip().startswith(HLS_SIGNATURE):
        raise ParseError("playlist is not HLS")

    segments: list[Segment] = []
    sequence = 0
    group = 0
    key: SegmentKey | None = None
    duration: float | None = None
    init_uri: str | None = None
    init_key: SegmentKey | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            sequence = int(line.partition(":")[2] or 0)
        elif line.startswith("#EXT-X-KEY:"):
            key = parse_key(line.partition(":")[2], base_url)
        elif line.startswith("#EXT-X-MAP:"):
            attributes = parse_attributes(line.partition(":")[2])
            if "URI" in attributes:
                init_uri = urljoin(base_url, attributes["URI"])
                init_key = key
        elif line.startswith("#EXT-X-DISCONTINUITY") and not line.startswith(
            "#EXT-X-DISCONTINUITY-SEQUENCE"
        ):
            group += 1
        elif line.startswith("#EXT-X-BYTERANGE"):
            raise ParseError("EXT-X-BYTERANGE playlists are not supported")
        elif line.startswith("#EXTINF:"):
            duration = float(line.partition(":")[2].split(",", 1)[0] or 0)
        elif not line.startswith("#"):
            segments.append(
                Segment(
                    uri=urljoin(base_url, line),
                    duration=duration or 0.0,
                    sequence=sequence,
                    group=group,
                    key=key,
                )
            )
            sequence += 1
            duration = None

    if not segments:
        raise ParseError("media playlist has no segments")
    return MediaPlaylist(segments=segments, init_uri=init_uri, init_key=init_key)


def parse_key(text: str, base_url: str) -> SegmentKey | None:
    """Parse an ``#EXT-X-KEY`` attribute list; ``METHOD=NONE`` clears encryption."""

    attributes = parse_attributes(text)
    method = attributes.get("METHOD", "NONE").upper()
    if method == "NONE":
        return None
    if method != "AES-128":
        raise ParseError(f"unsupported HLS encryption method: {method}")
    uri = attributes.get("URI")
    if not uri:
        raise ParseError("AES-128 key is missing its URI")

    iv_text = attributes.get("IV")
    iv = bytes.fromhex(iv_text[2:]) if iv_text and iv_text.lower().startswith("0x") else None
    return SegmentKey(method=method, uri=urljoin(base_url, uri), iv=iv)


def filter_ad_segments(
    segments: list[Segment], *, max_ad_duration: float = AD_GROUP_MAX_DURATION
) -> list[Segment]:
    """Drop short discontinuity groups that are served from a different path.

    Mirrors splice ad breaks into playlists between ``#EXT-X-DISCONTINUITY``
    tags. The real episode is the path that carries the most playtime; a short
    group from any other path is treated as an ad.
    """

    groups: dict[int, list[Segment]] = {}
    for segment in segments:
        groups.setdefault(segment.group, []).append(segment)
    if len(groups) < 2:
        return segments

    playtime: dict[str, float] = {}
    for segment in segments:
        playtime[segment_origin(segment.uri)] = (
            playtime.get(segment_origin(segment.uri), 0.0) + segment.duration
        )
    main_origin = max(playtime, key=lambda origin: playtime[origin])

    dropped: set[int] = set()
    for group, members in groups.items():
        origins = {segment_origin(segment.uri) for segment in members}
        duration = sum(segment.duration for segment in members)
        if main_origin not in origins and duration <= max_ad_duration:
            dropped.add(group)

    if not dropped:
        return segments
    kept = [segment for segment in segments if segment.group not in dropped]
    LOGGER.info("Dropped %d ad segment(s) in %d break(s)", len(segments) - len(kept), len(dropped))
    return kept


def segment_origin(uri: str) -> str:
    """Return host plus parent directory, the identity used to spot spliced ads."""

    parts = urlsplit(uri)
    return f"{parts.netloc}{parts.path.rsplit('/', 1)[0]}"


def find_ffmpeg() -> str | None:
    """Return the ffmpeg to use: ``AGECAT_FFMPEG``, ``PATH``, or a runnable bare ``ffmpeg``.

    a-Shell on iOS ships ffmpeg as a built-in command rather than a file on
    ``PATH``, so the last resort is simply trying to run it.
    """

    configured = os.environ.get(FFMPEG_ENV_VAR, "").strip()
    if configured:
        return configured
    return shutil.which("ffmpeg") or (FFMPEG_COMMAND if ffmpeg_runs(FFMPEG_COMMAND) else None)


@cache
def ffmpeg_runs(command: str) -> bool:
    """Return whether ``command -version`` runs successfully (checked once per process)."""

    try:
        result = subprocess.run(
            [command, "-version"], capture_output=True, text=True, check=False, timeout=30
        )
    except (OSError, ValueError, subprocess.SubprocessError, NotImplementedError):
        return False
    return result.returncode == 0


def hls_work_dir(output_dir: Path, title: str) -> Path:
    """Return the per-episode segment work directory."""

    return output_dir / f"{sanitize_filename(title)}.hls.part"


def download_hls_episode(
    client: SegmentSource,
    episode: Episode,
    output_dir: Path,
    *,
    resume: bool = True,
    overwrite: bool = False,
    progress: ProgressCallback | None = None,
    segment_concurrency: int = DEFAULT_SEGMENT_CONCURRENCY,
    ffmpeg: str | None = None,
    filter_ads: bool = True,
    should_stop: StopCheck | None = None,
) -> DownloadResult:
    """Download one HLS episode with segment-level resume, then remux it to MP4.

    ``should_stop`` is polled before every segment; once it returns True,
    queued segments are dropped, in-flight ones finish, and
    :class:`DownloadCancelled` is raised with the work directory intact.
    """

    output_dir.mkdir(parents=True, exist_ok=True)
    mp4_path, ts_path = output_paths(output_dir, episode.title)

    if not overwrite:
        for existing in (mp4_path, ts_path):
            if existing.exists():
                return skipped_result(episode, existing)

    work_dir = hls_work_dir(output_dir, episode.title)
    if (overwrite or not resume) and work_dir.exists():
        shutil.rmtree(work_dir)

    raise_if_stopped(should_stop, episode.title)
    playlist = fetch_media_playlist(client, episode)
    segments = filter_ad_segments(playlist.segments) if filter_ads else playlist.segments
    prepare_work_dir(work_dir, episode, len(segments))

    keys = KeyCache(client, episode.player_url)
    counters = SegmentProgress(total_segments=len(segments) + (1 if playlist.init_uri else 0))
    pending: list[tuple[int, Segment]] = []
    for index, segment in enumerate(segments):
        path = segment_path(work_dir, index)
        if path.exists():
            counters.add(path.stat().st_size)
        else:
            pending.append((index, segment))

    if counters.done_segments:
        LOGGER.info(
            "Resuming %s from segment %d/%d", episode.title, counters.done_segments, len(segments)
        )
    _emit(progress, episode, "started", 0, counters.done_bytes, counters.estimated_total())

    if playlist.init_uri:
        init_path = work_dir / "init.mp4"
        if not init_path.exists():
            data = fetch_segment_bytes(
                client, playlist.init_uri, playlist.init_key, 0, keys, episode.player_url
            )
            write_atomic(init_path, data)
        done, total = counters.add(init_path.stat().st_size)
        _emit(progress, episode, "advanced", init_path.stat().st_size, done, total)

    def fetch(index: int, segment: Segment) -> int:
        """Download, decrypt, and store one segment; return its size."""

        raise_if_stopped(should_stop, episode.title)
        data = fetch_segment_bytes(
            client, segment.uri, segment.key, segment.sequence, keys, episode.player_url
        )
        write_atomic(segment_path(work_dir, index), data)
        return len(data)

    executor = ThreadPoolExecutor(max_workers=max(1, segment_concurrency))
    try:
        futures = [executor.submit(fetch, index, segment) for index, segment in pending]
        for future in as_completed(futures):
            size = future.result()
            done, total = counters.add(size)
            _emit(progress, episode, "advanced", size, done, total)
    finally:
        # Drop queued segments on any failure; wait only for the ones in flight,
        # so no thread keeps writing into the work dir after this returns.
        executor.shutdown(wait=True, cancel_futures=True)

    raise_if_stopped(should_stop, episode.title)
    joined = join_segments(work_dir, len(segments), has_init=playlist.init_uri is not None)
    final_path = finalize(joined, mp4_path, ts_path, ffmpeg=ffmpeg or find_ffmpeg())
    shutil.rmtree(work_dir, ignore_errors=True)

    size = final_path.stat().st_size
    LOGGER.info("Downloaded file: %s", final_path)
    return DownloadResult(
        episode=episode,
        path=final_path,
        status="downloaded",
        bytes_written=size,
        total_bytes=size,
    )


def fetch_media_playlist(client: SegmentSource, episode: Episode) -> MediaPlaylist:
    """Fetch the episode playlist, following a master playlist to its best variant."""

    url = episode.stream_url
    text = client.get_resource(url, page_url=episode.player_url).text
    if is_master_playlist(text):
        variant = best_variant(parse_master_playlist(text, url))
        LOGGER.debug("Following master playlist to variant %s", variant.uri)
        url = variant.uri
        text = client.get_resource(url, page_url=episode.player_url).text
    return parse_media_playlist(text, url)


def prepare_work_dir(work_dir: Path, episode: Episode, segment_count: int) -> None:
    """Create the work directory, discarding segments left by a different stream.

    Mirrors encode differently, so segments from one cannot be mixed with
    another's; the manifest records which mirror and layout the files belong to.
    """

    manifest = f"{episode.source or ''}\n{segment_count}\n"
    manifest_path = work_dir / MANIFEST_NAME
    if work_dir.exists():
        try:
            previous = manifest_path.read_text(encoding="utf-8")
        except OSError:
            previous = None
        if previous != manifest:
            LOGGER.info("Discarding segments from a different source: %s", work_dir)
            shutil.rmtree(work_dir)

    work_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(manifest, encoding="utf-8")


class KeyCache:
    """Fetch each AES-128 key once per episode, shared across segment workers."""

    def __init__(self, client: SegmentSource, page_url: str | None) -> None:
        self.client = client
        self.page_url = page_url
        self.keys: dict[str, bytes] = {}
        self.lock = Lock()

    def get(self, uri: str) -> bytes:
        """Return the 16-byte key at ``uri``."""

        with self.lock:
            key = self.keys.get(uri)
            if key is None:
                key = self.client.get_resource(uri, page_url=self.page_url).content
                if len(key) != 16:
                    raise DownloadError(f"AES-128 key has {len(key)} bytes, expected 16: {uri}")
                self.keys[uri] = key
            return key


def fetch_segment_bytes(
    client: SegmentSource,
    uri: str,
    key: SegmentKey | None,
    sequence: int,
    keys: KeyCache,
    page_url: str | None,
) -> bytes:
    """Download one segment and decrypt it when the playlist says so."""

    data = client.get_resource(uri, page_url=page_url, paced=False).content
    if key is None or key.uri is None:
        return data
    return decrypt_segment(data, keys.get(key.uri), key.iv or sequence.to_bytes(16, "big"))


def decrypt_segment(data: bytes, key: bytes, iv: bytes) -> bytes:
    """Decrypt an AES-128-CBC segment and strip its PKCS#7 padding."""

    if len(data) % BLOCK_SIZE:
        raise DownloadError(f"encrypted segment size {len(data)} is not a block multiple")
    try:
        return cbc_decrypt(key, iv, data)
    except ValueError as error:
        raise DownloadError("segment decryption failed (bad key or IV)") from error


def segment_path(work_dir: Path, index: int) -> Path:
    """Return the file path of one downloaded segment."""

    return work_dir / f"{index:05d}.ts"


def write_atomic(path: Path, data: bytes) -> None:
    """Write a file so that a crash never leaves a truncated segment behind."""

    temp = path.with_name(f"{path.name}.tmp")
    temp.write_bytes(data)
    temp.replace(path)


def join_segments(work_dir: Path, segment_count: int, *, has_init: bool) -> Path:
    """Concatenate segments (after the init section, if any) into one stream file."""

    joined = work_dir / JOINED_NAME
    with joined.open("wb") as output:
        if has_init:
            output.write((work_dir / "init.mp4").read_bytes())
        for index in range(segment_count):
            path = segment_path(work_dir, index)
            if not path.exists():
                raise DownloadError(f"segment {index} is missing from {work_dir}")
            with path.open("rb") as segment:
                shutil.copyfileobj(segment, output)
    return joined


def finalize(joined: Path, mp4_path: Path, ts_path: Path, *, ffmpeg: str | None) -> Path:
    """Remux the joined stream to MP4, or keep it as ``.ts`` without a working ffmpeg."""

    if ffmpeg:
        part = mp4_path.with_name(f"{mp4_path.name}.part")
        error = remux_to_mp4(ffmpeg, joined, part)
        if error is None:
            part.replace(mp4_path)
            return mp4_path
        part.unlink(missing_ok=True)
        LOGGER.warning("ffmpeg remux failed (%s); keeping MPEG-TS output instead", error)
    else:
        LOGGER.warning("ffmpeg not found; saving %s as MPEG-TS", ts_path.name)

    joined.replace(ts_path)
    return ts_path


def remux_to_mp4(ffmpeg: str, source: Path, target: Path) -> str | None:
    """Copy streams from ``source`` into an MP4 at ``target``; return an error or ``None``."""

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(target),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except (OSError, ValueError, subprocess.SubprocessError, NotImplementedError) as error:
        # Sandboxed Pythons (a-Shell) may not support subprocesses at all.
        return str(error) or type(error).__name__
    if result.returncode == 0:
        return None
    last_line = (result.stderr or "").strip().splitlines()[-1:]
    return last_line[0] if last_line else f"exit code {result.returncode}"


def _emit(
    progress: ProgressCallback | None,
    episode: Episode,
    phase: Literal["started", "advanced", "reset"],
    delta: int,
    completed: int,
    total: int | None,
) -> None:
    """Emit one progress event when a progress callback is configured."""

    if progress is not None:
        progress(
            DownloadProgressEvent(
                episode=episode,
                phase=phase,
                bytes_delta=delta,
                bytes_completed=completed,
                total_bytes=total,
            )
        )
