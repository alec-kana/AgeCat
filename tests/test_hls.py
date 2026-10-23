import unittest
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest import mock

import requests

from agecat.aes import cbc_encrypt
from agecat.errors import AccessDeniedError, DownloadError, ParseError
from agecat.hls import (
    Segment,
    Variant,
    best_variant,
    decrypt_segment,
    download_hls_episode,
    ffmpeg_runs,
    filter_ad_segments,
    find_ffmpeg,
    hls_work_dir,
    parse_master_playlist,
    parse_media_playlist,
    segment_path,
)
from agecat.models import DownloadProgressEvent, Episode

from .fakes import FakeResponse

BASE = "https://cdn.example/show/ep1/index.m3u8"
KEY = b"0123456789abcdef"

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=800000,RESOLUTION=854x480
480p/index.m3u8
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=1500000,RESOLUTION=1280x720
720p/index.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA-SEQUENCE:5
#EXTINF:4.0,
seg0.ts
#EXT-X-KEY:METHOD=AES-128,URI="enc.key",IV=0x000102030405060708090a0b0c0d0e0f
#EXTINF:4.0,
seg1.ts
#EXT-X-KEY:METHOD=AES-128,URI="enc.key"
#EXTINF:4.0,
https://other.example/seg2.ts
#EXT-X-KEY:METHOD=NONE
#EXTINF:2.5,
seg3.ts
#EXT-X-ENDLIST
"""


def encrypt(data: bytes, iv: bytes) -> bytes:
    return cbc_encrypt(KEY, iv, data)


def segment(uri: str, duration: float, group: int, sequence: int = 0) -> Segment:
    return Segment(uri=uri, duration=duration, sequence=sequence, group=group)


class FakeHlsClient:
    """Serves one playlist plus segments from a dict; records every request."""

    def __init__(self, files: dict[str, bytes], *, deny: frozenset[str] = frozenset()) -> None:
        self.files = files
        self.deny = deny
        self.requests: list[tuple[str, str | None, bool]] = []

    def get_resource(
        self,
        url: str,
        *,
        page_url: str | None = None,
        headers: Mapping[str, str] | None = None,
        paced: bool = True,
    ) -> requests.Response:
        self.requests.append((url, page_url, paced))
        if url in self.deny:
            raise AccessDeniedError(f"GET {url} was denied with 403", status_code=403)
        return cast(requests.Response, FakeResponse(self.files[url]))


def episode(source: str = "无尽") -> Episode:
    return Episode(
        page_url="https://www.agedm.io/play/1/4/1",
        title="Demo 第01集",
        stream_url=BASE,
        kind="hls",
        source=source,
        player_url="https://jx.example/m3u8/?url=x",
    )


def plain_playlist(count: int) -> str:
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:4"]
    for index in range(count):
        lines += ["#EXTINF:4.0,", f"s{index}.ts"]
    return "\n".join([*lines, "#EXT-X-ENDLIST"])


def plain_files(count: int) -> dict[str, bytes]:
    files = {BASE: plain_playlist(count).encode()}
    files.update(
        {
            f"https://cdn.example/show/ep1/s{index}.ts": f"<{index}>".encode()
            for index in range(count)
        }
    )
    return files


class PlaylistParsingTests(unittest.TestCase):
    def test_master_playlist_picks_highest_resolution(self):
        variants = parse_master_playlist(MASTER, BASE)

        self.assertEqual(
            variants,
            [
                Variant("https://cdn.example/show/ep1/480p/index.m3u8", 800000, 854 * 480),
                Variant("https://cdn.example/show/ep1/720p/index.m3u8", 1500000, 1280 * 720),
            ],
        )
        self.assertEqual(best_variant(variants).uri, "https://cdn.example/show/ep1/720p/index.m3u8")
        with self.assertRaises(ParseError):
            best_variant([])

    def test_media_playlist_tracks_keys_sequence_and_absolute_uris(self):
        playlist = parse_media_playlist(MEDIA, BASE)
        segments = playlist.segments

        self.assertEqual([item.sequence for item in segments], [5, 6, 7, 8])
        self.assertEqual(segments[2].uri, "https://other.example/seg2.ts")
        self.assertIsNone(segments[0].key)
        first_key = segments[1].key
        assert first_key is not None
        self.assertEqual(first_key.uri, "https://cdn.example/show/ep1/enc.key")
        self.assertEqual(first_key.iv, bytes(range(16)))
        second_key = segments[2].key
        assert second_key is not None
        self.assertIsNone(second_key.iv)
        self.assertIsNone(segments[3].key)
        self.assertEqual(segments[3].duration, 2.5)

    def test_media_playlist_rejects_bad_input(self):
        with self.assertRaisesRegex(ParseError, "not HLS"):
            parse_media_playlist("<html>", BASE)
        with self.assertRaisesRegex(ParseError, "no segments"):
            parse_media_playlist("#EXTM3U\n#EXT-X-ENDLIST", BASE)
        with self.assertRaisesRegex(ParseError, "SAMPLE-AES"):
            parse_media_playlist(
                '#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="k"\n#EXTINF:1,\na.ts', BASE
            )

    def test_discontinuities_split_groups(self):
        text = "#EXTM3U\n#EXTINF:1,\na.ts\n#EXT-X-DISCONTINUITY\n#EXTINF:1,\nb.ts\n"

        groups = [item.group for item in parse_media_playlist(text, BASE).segments]

        self.assertEqual(groups, [0, 1])


class AdFilterTests(unittest.TestCase):
    def test_short_group_from_another_path_is_dropped(self):
        segments = [
            *(segment(f"https://cdn.example/show/a{index}.ts", 10.0, 0) for index in range(30)),
            segment("https://cdn.example/ads/x1.ts", 5.0, 1),
            segment("https://cdn.example/ads/x2.ts", 5.0, 1),
            *(segment(f"https://cdn.example/show/b{index}.ts", 10.0, 2) for index in range(30)),
        ]

        kept = filter_ad_segments(segments)

        self.assertEqual(len(kept), 60)
        self.assertFalse(any("/ads/" in item.uri for item in kept))

    def test_long_groups_and_same_path_groups_are_kept(self):
        segments = [
            *(segment(f"https://cdn.example/show/a{index}.ts", 10.0, 0) for index in range(30)),
            segment("https://cdn.example/show/c.ts", 5.0, 1),
            *(segment(f"https://cdn.example/part2/b{index}.ts", 10.0, 2) for index in range(20)),
        ]

        self.assertEqual(filter_ad_segments(segments), segments)

    def test_single_group_is_untouched(self):
        segments = [segment("https://cdn.example/a.ts", 1.0, 0)]

        self.assertIs(filter_ad_segments(segments), segments)


class DecryptTests(unittest.TestCase):
    def test_decrypt_segment_round_trip_and_errors(self):
        iv = bytes(16)
        self.assertEqual(decrypt_segment(encrypt(b"payload", iv), KEY, iv), b"payload")
        with self.assertRaisesRegex(DownloadError, "block multiple"):
            decrypt_segment(b"abc", KEY, iv)
        with self.assertRaisesRegex(DownloadError, "decryption failed"):
            decrypt_segment(encrypt(b"payload", iv), b"fedcba9876543210", iv)


class DownloadTests(unittest.TestCase):
    def test_master_playlist_encrypted_segments_and_ts_fallback(self):
        variant = "https://cdn.example/show/ep1/720p/index.m3u8"
        media = (
            "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:3\n"
            '#EXT-X-KEY:METHOD=AES-128,URI="/keys/k.key"\n'
            "#EXTINF:4,\nseg0.ts\n#EXTINF:4,\nseg1.ts\n#EXT-X-ENDLIST\n"
        )
        files = {
            BASE: MASTER.encode(),
            variant: media.encode(),
            "https://cdn.example/keys/k.key": KEY,
            "https://cdn.example/show/ep1/720p/seg0.ts": encrypt(
                b"first-", (3).to_bytes(16, "big")
            ),
            "https://cdn.example/show/ep1/720p/seg1.ts": encrypt(
                b"second", (4).to_bytes(16, "big")
            ),
        }
        client = FakeHlsClient(files)
        events: list[DownloadProgressEvent] = []

        with (
            TemporaryDirectory() as directory,
            mock.patch("agecat.hls.find_ffmpeg", return_value=None),
        ):
            result = download_hls_episode(
                client, episode(), Path(directory), progress=events.append, segment_concurrency=1
            )

            self.assertEqual(result.path, Path(directory) / "Demo 第01集.ts")
            self.assertEqual(result.path.read_bytes(), b"first-second")
            self.assertFalse(hls_work_dir(Path(directory), "Demo 第01集").exists())

        # The key is fetched once; segments bypass pacing; the player is the Referer page.
        key_requests = [item for item in client.requests if item[0].endswith("k.key")]
        self.assertEqual(len(key_requests), 1)
        segment_requests = [item for item in client.requests if item[0].endswith(".ts")]
        self.assertTrue(all(not paced for _url, _page, paced in segment_requests))
        self.assertTrue(
            all(page == "https://jx.example/m3u8/?url=x" for _url, page, _ in client.requests)
        )
        self.assertEqual(events[0].phase, "started")
        self.assertEqual(events[-1].bytes_completed, 12)
        self.assertEqual(events[-1].total_bytes, 12)

    def test_resume_skips_segments_already_on_disk(self):
        client = FakeHlsClient(plain_files(3))

        with (
            TemporaryDirectory() as directory,
            mock.patch("agecat.hls.find_ffmpeg", return_value=None),
        ):
            output_dir = Path(directory)
            work_dir = hls_work_dir(output_dir, "Demo 第01集")
            work_dir.mkdir()
            (work_dir / "source.txt").write_text("无尽\n3\n", encoding="utf-8")
            segment_path(work_dir, 0).write_bytes(b"<0>")

            result = download_hls_episode(client, episode(), output_dir, segment_concurrency=2)

            self.assertEqual(result.path.read_bytes(), b"<0><1><2>")

        fetched = sorted(url for url, _page, _paced in client.requests if url.endswith(".ts"))
        self.assertEqual(
            fetched, ["https://cdn.example/show/ep1/s1.ts", "https://cdn.example/show/ep1/s2.ts"]
        )

    def test_segments_from_a_different_source_are_discarded(self):
        client = FakeHlsClient(plain_files(2))

        with (
            TemporaryDirectory() as directory,
            mock.patch("agecat.hls.find_ffmpeg", return_value=None),
        ):
            output_dir = Path(directory)
            work_dir = hls_work_dir(output_dir, "Demo 第01集")
            work_dir.mkdir()
            (work_dir / "source.txt").write_text("非凡\n2\n", encoding="utf-8")
            segment_path(work_dir, 0).write_bytes(b"stale")

            result = download_hls_episode(client, episode(), output_dir)

            self.assertEqual(result.path.read_bytes(), b"<0><1>")

    def test_denied_segment_propagates_and_keeps_finished_segments(self):
        files = plain_files(2)
        client = FakeHlsClient(files, deny=frozenset({"https://cdn.example/show/ep1/s1.ts"}))

        with TemporaryDirectory() as directory:
            output_dir = Path(directory)
            with self.assertRaises(AccessDeniedError):
                download_hls_episode(client, episode(), output_dir, segment_concurrency=1)

            work_dir = hls_work_dir(output_dir, "Demo 第01集")
            self.assertEqual(segment_path(work_dir, 0).read_bytes(), b"<0>")
            self.assertFalse(segment_path(work_dir, 1).exists())

    def test_existing_output_is_skipped(self):
        client = FakeHlsClient({})

        with TemporaryDirectory() as directory:
            (Path(directory) / "Demo 第01集.mp4").write_bytes(b"done")

            result = download_hls_episode(client, episode(), Path(directory))

        self.assertEqual(result.status, "skipped")
        self.assertEqual(client.requests, [])

    def test_broken_ffmpeg_falls_back_to_ts(self):
        client = FakeHlsClient(plain_files(1))

        with TemporaryDirectory() as directory:
            with self.assertLogs("agecat.hls", level="WARNING") as logs:
                result = download_hls_episode(
                    client, episode(), Path(directory), ffmpeg="definitely-not-ffmpeg-binary"
                )

            self.assertEqual(result.path.suffix, ".ts")
            self.assertEqual(result.path.read_bytes(), b"<0>")
        self.assertIn("remux failed", "".join(logs.output))


class FindFfmpegTests(unittest.TestCase):
    def setUp(self) -> None:
        ffmpeg_runs.cache_clear()

    def tearDown(self) -> None:
        ffmpeg_runs.cache_clear()

    def test_environment_variable_wins(self):
        with mock.patch.dict("os.environ", {"AGECAT_FFMPEG": "/opt/ffmpeg"}):
            self.assertEqual(find_ffmpeg(), "/opt/ffmpeg")

    def test_path_lookup_comes_next(self):
        with (
            mock.patch.dict("os.environ", {"AGECAT_FFMPEG": ""}),
            mock.patch("agecat.hls.shutil.which", return_value="/usr/bin/ffmpeg"),
        ):
            self.assertEqual(find_ffmpeg(), "/usr/bin/ffmpeg")

    def test_bare_command_is_used_when_it_runs(self):
        # a-Shell: ffmpeg is a built-in command that which() cannot see.
        with (
            mock.patch.dict("os.environ", {"AGECAT_FFMPEG": ""}),
            mock.patch("agecat.hls.shutil.which", return_value=None),
            mock.patch("agecat.hls.subprocess.run") as run,
        ):
            run.return_value.returncode = 0
            self.assertEqual(find_ffmpeg(), "ffmpeg")

    def test_unavailable_or_unsupported_subprocess_means_no_ffmpeg(self):
        for error in (FileNotFoundError("ffmpeg"), NotImplementedError()):
            ffmpeg_runs.cache_clear()
            with (
                self.subTest(error=type(error).__name__),
                mock.patch.dict("os.environ", {"AGECAT_FFMPEG": ""}),
                mock.patch("agecat.hls.shutil.which", return_value=None),
                mock.patch("agecat.hls.subprocess.run", side_effect=error),
            ):
                self.assertIsNone(find_ffmpeg())


if __name__ == "__main__":
    unittest.main()
