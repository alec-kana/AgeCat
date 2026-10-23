import os
import threading
import time
import unittest
from collections.abc import Iterator, Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar

from agecat.errors import DownloadCancelled
from agecat.models import DownloadProgressEvent, EpisodeJob, JobReport, VideoStreamResponse
from agecat.service import AgeCatService

from .fakes import FakeAgeClient, FakeSource, VideoResponse
from .test_service import EPISODE_URL, download_options


class PartialCleanupTests(unittest.TestCase):
    def test_failed_episode_removes_its_partial_file_by_default(self):
        client = FakeAgeClient(denials=99)

        with TemporaryDirectory() as directory:
            service = AgeCatService(
                download_options(output_dir=Path(directory), chunk_size=3, resolve_attempts=2),
                client_factory=lambda: client,
            )

            report = service.download_one(EPISODE_URL)

            self.assertIsNotNone(report.error)
            self.assertEqual(list(Path(directory).rglob("*.part")), [])
            self.assertEqual(len(service.removed_partials), 1)

    def test_keep_partial_leaves_a_failed_episode_resumable(self):
        client = FakeAgeClient(denials=99)

        with TemporaryDirectory() as directory:
            service = AgeCatService(
                download_options(
                    output_dir=Path(directory),
                    chunk_size=3,
                    resolve_attempts=2,
                    keep_partial=True,
                ),
                client_factory=lambda: client,
            )

            service.download_one(EPISODE_URL)

            parts = list(Path(directory).rglob("*.part"))
            self.assertEqual([part.read_bytes() for part in parts], [b"abc"])
            self.assertEqual(service.removed_partials, [])

    def test_batch_sweeps_leftovers_from_an_earlier_run(self):
        with TemporaryDirectory() as directory:
            anime_dir = Path(directory) / "Demo Anime"
            anime_dir.mkdir()
            leftover = anime_dir / "Killed Earlier.mp4.part"
            leftover.write_bytes(b"x" * 10)
            leftover_hls = anime_dir / "Killed HLS.hls.part"
            leftover_hls.mkdir()
            (leftover_hls / "00000.ts").write_bytes(b"y" * 5)
            service = AgeCatService(
                download_options(output_dir=Path(directory), chunk_size=1024),
                client_factory=FakeAgeClient,
            )

            reports = service.download_many([EpisodeJob(url=EPISODE_URL)])

            self.assertIsNotNone(reports[0].result)
            self.assertFalse(leftover.exists())
            self.assertFalse(leftover_hls.exists())
            self.assertTrue((anime_dir / "Demo Anime 第01集.mp4").exists())
            self.assertEqual(
                sorted(service.removed_partials), sorted([(leftover, 10), (leftover_hls, 5)])
            )

    def test_batch_sweeps_old_leftovers_from_other_anime_folders(self):
        with TemporaryDirectory() as directory:
            other_dir = Path(directory) / "Other Show"
            other_dir.mkdir()
            killed = other_dir / "Killed Earlier.mp4.part"
            killed.write_bytes(b"x" * 10)
            os.utime(killed, (1000, 1000))
            service = AgeCatService(
                download_options(output_dir=Path(directory), chunk_size=1024),
                client_factory=FakeAgeClient,
            )

            service.download_many([EpisodeJob(url=EPISODE_URL)])

            self.assertFalse(killed.exists())
            self.assertEqual(service.removed_partials, [(killed, 10)])

    def test_batch_keeps_recent_partials_in_other_anime_folders(self):
        with TemporaryDirectory() as directory:
            other_dir = Path(directory) / "Other Show"
            other_dir.mkdir()
            # Possibly still being written by another run in the same output folder.
            live = other_dir / "Another Run.mp4.part"
            live.write_bytes(b"x")
            live_hls = other_dir / "Another Run.hls.part"
            live_hls.mkdir()
            service = AgeCatService(
                download_options(output_dir=Path(directory), chunk_size=1024),
                client_factory=FakeAgeClient,
            )

            service.download_many([EpisodeJob(url=EPISODE_URL)])

            self.assertTrue(live.exists())
            self.assertTrue(live_hls.exists())

    def test_keep_partial_skips_the_leftover_sweep(self):
        with TemporaryDirectory() as directory:
            anime_dir = Path(directory) / "Demo Anime"
            anime_dir.mkdir()
            leftover = anime_dir / "Killed Earlier.mp4.part"
            leftover.write_bytes(b"x")
            service = AgeCatService(
                download_options(output_dir=Path(directory), chunk_size=1024, keep_partial=True),
                client_factory=FakeAgeClient,
            )

            service.download_many([EpisodeJob(url=EPISODE_URL)])

            self.assertTrue(leftover.exists())


class CancellationTests(unittest.TestCase):
    def test_cancel_wakes_a_sleeping_worker(self):
        service = AgeCatService(download_options(output_dir=Path("unused")))
        service.cancel()

        started = time.monotonic()
        with self.assertRaises(DownloadCancelled):
            service.sleeper(30.0)

        self.assertLess(time.monotonic() - started, 1.0)

    def test_cancellation_is_not_mistaken_for_a_dead_source(self):
        client = CancellingClient()
        service = AgeCatService(
            download_options(output_dir=Path("unused")), client_factory=lambda: client
        )

        report = service.download_one(EPISODE_URL)

        # A cancelled source stops the episode instead of moving on to the next mirror.
        error = report.error
        assert error is not None
        self.assertIn("cancelled", error)
        self.assertNotIn("no playable source", error)
        self.assertEqual(client.frames, 1)

    def test_interrupted_batch_stops_workers_and_leaves_no_partial_files(self):
        EndlessClient.opened.clear()
        both_streaming = threading.Event()
        streaming: set[str] = set()
        lock = threading.Lock()

        def on_progress(event: DownloadProgressEvent) -> None:
            with lock:
                if not event.episode.title.endswith("第01集"):
                    streaming.add(event.episode.title)
                if len(streaming) == 2:
                    both_streaming.set()

        def on_done(report: JobReport) -> None:
            # Runs on the calling thread, exactly where Ctrl-C lands in the CLI.
            self.assertTrue(both_streaming.wait(10))
            raise KeyboardInterrupt

        with TemporaryDirectory() as directory:
            service = AgeCatService(
                download_options(
                    output_dir=Path(directory),
                    concurrency=3,
                    chunk_size=4,
                    # The worker that finishes the quick episode then waits
                    # here, which the cancellation has to cut short.
                    min_delay=30.0,
                    max_delay=30.0,
                ),
                client_factory=EndlessClient,
            )
            jobs = [
                EpisodeJob(url=f"https://www.agedm.io/play/20260001/1/{number}")
                for number in range(1, 6)
            ]

            started = time.monotonic()
            with self.assertRaises(KeyboardInterrupt):
                service.download_many(jobs, on_progress=on_progress, on_done=on_done)
            elapsed = time.monotonic() - started

            leftovers = list(Path(directory).rglob("*.part"))
            removed = len(service.removed_partials)

        # Queued episodes never started, and both running streams were closed
        # before download_many returned control, well before they would end.
        self.assertLess(elapsed, 5.0)
        self.assertEqual(len(EndlessClient.opened), 2)
        self.assertTrue(all(response.closed for response in EndlessClient.opened))
        self.assertEqual(leftovers, [])
        self.assertEqual(removed, 2)
        self.assertTrue(service.cancelled)


class CancellingClient(FakeAgeClient):
    """Every player page request is interrupted, as if Ctrl-C landed mid-resolve."""

    def __init__(self) -> None:
        super().__init__()
        self.frames = 0

    def get_frame(self, url: str, *, page_url: str) -> str:
        self.frames += 1
        raise DownloadCancelled("download run cancelled")


class EndlessResponse:
    """A stream that never ends on its own, like a large file on a slow link."""

    status_code = 200

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.closed = False

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        # Bounded so a regression fails the test instead of hanging it.
        for _ in range(5000):
            time.sleep(0.002)
            yield b"x" * chunk_size

    def close(self) -> None:
        self.closed = True


class EndlessClient(FakeAgeClient):
    """Episode 1 finishes at once; every other episode streams until stopped."""

    opened: ClassVar[list[EndlessResponse]] = []

    def __init__(self) -> None:
        super().__init__(
            sources=(FakeSource("xigua", "西瓜", 5, vip=True), FakeSource("ffm3u8", "非凡", 5))
        )

    def stream_video(
        self,
        url: str,
        *,
        cookies: Mapping[str, str],
        headers: Mapping[str, str] | None = None,
        page_url: str | None = None,
    ) -> VideoStreamResponse:
        if "_1.mp4" in url:
            return VideoResponse()
        response = EndlessResponse()
        self.opened.append(response)
        return response


if __name__ == "__main__":
    unittest.main()
