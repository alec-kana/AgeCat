import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, ClassVar, cast

from agecat.models import EpisodeJob
from agecat.options import DownloadOptions
from agecat.service import AgeCatService

from .fakes import FakeAgeClient, FakeSource, VideoResponse

EPISODE_URL = "https://www.agedm.io/play/20260001/1/1"
DETAIL_URL = "https://www.agedm.io/detail/20260001"


def download_options(**overrides: object) -> DownloadOptions:
    """Build options with pacing disabled and a fixed site so tests never sleep or resolve."""

    defaults: dict[str, object] = {
        "min_delay": 0.0,
        "max_delay": 0.0,
        "stagger": 0.0,
        "host_interval": 0.0,
        "base_url": "https://www.agedm.io",
    }
    return DownloadOptions(**cast(Any, {**defaults, **overrides}))


class TrackingClient(FakeAgeClient):
    instances: ClassVar[list["TrackingClient"]] = []

    def __init__(self) -> None:
        super().__init__()
        self.instances.append(self)


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        TrackingClient.instances.clear()

    def test_download_one_reports_when_no_source_is_playable(self):
        client = FakeAgeClient(dead_sources=frozenset({"xigua", "ffm3u8"}))
        service = AgeCatService(
            download_options(output_dir=Path("unused")), client_factory=lambda: client
        )

        report = service.download_one(EPISODE_URL)

        self.assertIsNone(report.result)
        error = report.error
        assert error is not None
        self.assertIn("no playable source", error)
        self.assertIn("西瓜", error)
        self.assertIn("非凡", error)

    def test_dead_source_falls_back_to_the_next_mirror(self):
        client = FakeAgeClient(dead_sources=frozenset({"xigua"}))

        with TemporaryDirectory() as directory:
            service = AgeCatService(
                download_options(output_dir=Path(directory), chunk_size=1024),
                client_factory=lambda: client,
            )

            report = service.download_one(EPISODE_URL)

            result = report.result
            assert result is not None
            self.assertEqual(result.episode.source, "非凡")
            self.assertEqual(result.path.read_bytes(), VideoResponse.content)

    def test_download_many_reports_chunk_progress(self):
        progress: list[tuple[str, int, int, int | None]] = []

        with TemporaryDirectory() as directory:
            service = AgeCatService(
                download_options(output_dir=Path(directory), concurrency=1, chunk_size=1024),
                client_factory=TrackingClient,
            )

            reports = service.download_many(
                [EpisodeJob(url=EPISODE_URL)],
                on_progress=lambda event: progress.append(
                    (event.phase, event.bytes_delta, event.bytes_completed, event.total_bytes)
                ),
            )

            self.assertEqual(
                progress,
                [
                    ("started", 0, 0, 2500),
                    ("advanced", 1024, 1024, 2500),
                    ("advanced", 1024, 2048, 2500),
                    ("advanced", 452, 2500, 2500),
                ],
            )
            result = reports[0].result
            assert result is not None
            self.assertEqual(result.path.name, "Demo Anime 第01集.mp4")
            self.assertEqual(result.path.parent, Path(directory) / "Demo Anime")
            self.assertEqual(len(TrackingClient.instances), 1)
            self.assertTrue(TrackingClient.instances[0].closed)

    def test_download_many_reuses_one_client_per_worker_thread(self):
        with TemporaryDirectory() as directory:
            service = AgeCatService(
                download_options(output_dir=Path(directory), concurrency=1, chunk_size=1024),
                client_factory=TrackingClient,
            )

            reports = service.download_many(
                [
                    EpisodeJob(url=EPISODE_URL),
                    EpisodeJob(url="https://www.agedm.io/play/20260001/1/2"),
                ]
            )

            self.assertEqual(len(reports), 2)
            self.assertEqual(len(TrackingClient.instances), 1)
            self.assertTrue(TrackingClient.instances[0].closed)

    def test_stream_referer_comes_from_the_embedded_player(self):
        client = FakeAgeClient()

        with TemporaryDirectory() as directory:
            service = AgeCatService(
                download_options(output_dir=Path(directory), chunk_size=1024),
                client_factory=lambda: client,
            )
            service.download_one(EPISODE_URL)

        # Fake players live under /m3u8/, so the player URL becomes the Referer page.
        self.assertTrue(client.stream_calls)
        _url, _headers, page_url = client.stream_calls[0]
        assert page_url is not None
        self.assertIn("/m3u8/", page_url)

    def test_collect_episode_urls_closes_client(self):
        service = AgeCatService(
            download_options(output_dir=Path("unused")), client_factory=TrackingClient
        )

        jobs = service.collect_episode_urls([EPISODE_URL])

        self.assertEqual(jobs, [EpisodeJob(url=EPISODE_URL)])
        self.assertTrue(TrackingClient.instances[0].closed)

    def test_collect_rebases_links_from_dead_domains(self):
        service = AgeCatService(
            download_options(output_dir=Path("unused")), client_factory=FakeAgeClient
        )

        jobs = service.collect_episode_urls(["https://www.agefans.la/play/20260001/1/1"])

        self.assertEqual(jobs, [EpisodeJob(url=EPISODE_URL)])

    def test_collect_resolves_the_live_domain_when_no_base_url_is_set(self):
        service = AgeCatService(
            download_options(output_dir=Path("unused"), base_url=None),
            client_factory=FakeAgeClient,
        )

        jobs = service.collect_episode_urls(["http://age.tv/play/20260001/1/1"])

        self.assertEqual(service.site_url, "https://www.agedm.io")
        self.assertEqual(jobs, [EpisodeJob(url=EPISODE_URL)])

    def test_collect_rejects_non_age_urls(self):
        service = AgeCatService(
            download_options(output_dir=Path("unused")), client_factory=FakeAgeClient
        )

        with self.assertRaisesRegex(Exception, "unsupported AGE URL"):
            service.collect_episode_urls(["https://anime1.me/1"])

    def test_detail_download_places_episodes_under_anime_name_subfolder(self):
        with TemporaryDirectory() as directory:
            service = AgeCatService(
                download_options(output_dir=Path(directory), chunk_size=1024),
                client_factory=FakeAgeClient,
            )

            jobs = service.collect_episode_urls([DETAIL_URL])
            self.assertEqual(
                jobs,
                [
                    EpisodeJob(url=EPISODE_URL, anime_name="Demo Anime"),
                    EpisodeJob(
                        url="https://www.agedm.io/play/20260001/1/2", anime_name="Demo Anime"
                    ),
                ],
            )

            reports = service.download_many(jobs)

            for report in reports:
                result = report.result
                assert result is not None
                self.assertEqual(result.path.parent, Path(directory) / "Demo Anime")

    def test_detail_uses_the_most_complete_mirror(self):
        sources = (
            FakeSource("xigua", "西瓜", 1, vip=True),
            FakeSource("ffm3u8", "非凡", 3),
        )
        service = AgeCatService(
            download_options(output_dir=Path("unused")),
            client_factory=lambda: FakeAgeClient(sources=sources),
        )

        jobs = service.collect_episode_urls([DETAIL_URL])

        self.assertEqual(
            [job.url for job in jobs],
            [f"https://www.agedm.io/play/20260001/2/{number}" for number in (1, 2, 3)],
        )

    def test_collect_episode_urls_filters_detail_by_episodes(self):
        service = AgeCatService(
            download_options(output_dir=Path("unused")), client_factory=FakeAgeClient
        )

        jobs = service.collect_episode_urls([DETAIL_URL], episodes=frozenset({2}))

        self.assertEqual(
            jobs,
            [EpisodeJob(url="https://www.agedm.io/play/20260001/1/2", anime_name="Demo Anime")],
        )

    def test_collect_episode_urls_warns_about_missing_requested_episodes(self):
        service = AgeCatService(
            download_options(output_dir=Path("unused")), client_factory=FakeAgeClient
        )

        with self.assertLogs("agecat.service", level="WARNING") as logs:
            jobs = service.collect_episode_urls([DETAIL_URL], episodes=frozenset({2, 5}))

        self.assertEqual(len(jobs), 1)
        self.assertIn("not found", logs.output[0])
        self.assertIn("5", logs.output[0])

    def test_collect_episode_urls_never_filters_explicit_episode_urls(self):
        service = AgeCatService(
            download_options(output_dir=Path("unused")), client_factory=FakeAgeClient
        )

        jobs = service.collect_episode_urls([EPISODE_URL], episodes=frozenset({99}))

        self.assertEqual(jobs, [EpisodeJob(url=EPISODE_URL)])

    def test_existing_file_is_skipped_without_resolving_a_stream(self):
        client = FakeAgeClient(denials=99)

        with TemporaryDirectory() as directory:
            output_dir = Path(directory) / "Demo Anime"
            output_dir.mkdir()
            (output_dir / "Demo Anime 第01集.mp4").write_bytes(b"already downloaded")

            service = AgeCatService(
                download_options(output_dir=Path(directory), chunk_size=3),
                client_factory=lambda: client,
            )

            report = service.download_one(EPISODE_URL)

            result = report.result
            assert result is not None
            self.assertEqual(result.status, "skipped")
            self.assertEqual(result.bytes_written, len(b"already downloaded"))

        # The page fetch names the file; no player is asked for a stream.
        self.assertEqual(client.resolve_calls, 0)
        self.assertEqual(client.stream_calls, [])

    def test_existing_ts_file_also_counts_as_downloaded(self):
        client = FakeAgeClient()

        with TemporaryDirectory() as directory:
            output_dir = Path(directory) / "Demo Anime"
            output_dir.mkdir()
            (output_dir / "Demo Anime 第01集.ts").write_bytes(b"ts")

            service = AgeCatService(
                download_options(output_dir=Path(directory)), client_factory=lambda: client
            )
            report = service.download_one(EPISODE_URL)

        result = report.result
        assert result is not None
        self.assertEqual(result.status, "skipped")
        self.assertEqual(client.resolve_calls, 0)

    def test_overwrite_still_resolves_an_existing_file(self):
        client = FakeAgeClient()

        with TemporaryDirectory() as directory:
            output_dir = Path(directory) / "Demo Anime"
            output_dir.mkdir()
            (output_dir / "Demo Anime 第01集.mp4").write_bytes(b"stale")

            service = AgeCatService(
                download_options(output_dir=Path(directory), chunk_size=3, overwrite=True),
                client_factory=lambda: client,
            )

            report = service.download_one(EPISODE_URL)

            result = report.result
            assert result is not None
            self.assertEqual(result.status, "downloaded")

        self.assertEqual(client.resolve_calls, 1)

    def test_expired_stream_is_re_resolved_and_the_download_resumes(self):
        client = FakeAgeClient(denials=1)

        with TemporaryDirectory() as directory:
            service = AgeCatService(
                download_options(output_dir=Path(directory), chunk_size=3),
                client_factory=lambda: client,
            )

            report = service.download_one(EPISODE_URL)

            result = report.result
            assert result is not None
            self.assertEqual(result.path.read_bytes(), b"abcdef")

        # A freshly signed URL, and a retry that resumes instead of restarting.
        self.assertEqual(client.resolve_calls, 2)
        first_url, first_headers, _ = client.stream_calls[0]
        second_url, second_headers, _ = client.stream_calls[1]
        self.assertNotEqual(first_url, second_url)
        self.assertEqual(first_headers, {})
        self.assertEqual(second_headers, {"Range": "bytes=3-"})

    def test_denial_that_survives_every_re_resolution_is_reported_as_failed(self):
        client = FakeAgeClient(denials=99)

        with TemporaryDirectory() as directory:
            service = AgeCatService(
                download_options(output_dir=Path(directory), chunk_size=3, resolve_attempts=2),
                client_factory=lambda: client,
            )

            report = service.download_one(EPISODE_URL)

        self.assertIsNone(report.result)
        error = report.error
        assert error is not None
        self.assertIn("credentials were rejected", error)
        self.assertEqual(client.resolve_calls, 2)

    def test_bot_mitigation_denial_is_reported_with_actionable_guidance(self):
        client = FakeAgeClient(denials=99, denial_headers={"cf-mitigated": "challenge"})
        sleeps: list[float] = []

        with TemporaryDirectory() as directory:
            service = AgeCatService(
                download_options(output_dir=Path(directory), chunk_size=3, resolve_attempts=2),
                client_factory=lambda: client,
                sleeper=sleeps.append,
            )

            report = service.download_one(EPISODE_URL)

        error = report.error
        assert error is not None
        self.assertIn("anti-bot protection", error)
        self.assertIn("--concurrency", error)
        self.assertTrue(any(delay > 0 for delay in sleeps))

    def test_retry_budget_stops_recovery_before_the_attempts_run_out(self):
        client = FakeAgeClient(denials=99)
        clock = iter([0.0, 0.0, 10_000.0, 10_000.0])

        with TemporaryDirectory() as directory:
            service = AgeCatService(
                download_options(
                    output_dir=Path(directory),
                    chunk_size=3,
                    resolve_attempts=10,
                    retry_budget=60.0,
                ),
                client_factory=lambda: client,
                clock=lambda: next(clock),
            )

            report = service.download_one(EPISODE_URL)

        self.assertIsNotNone(report.error)
        self.assertEqual(client.resolve_calls, 1)

    def test_circuit_breaker_pauses_workers_after_repeated_denials(self):
        client = FakeAgeClient(denials=99)
        sleeps: list[float] = []

        with TemporaryDirectory() as directory:
            service = AgeCatService(
                download_options(
                    output_dir=Path(directory),
                    chunk_size=3,
                    resolve_attempts=2,
                    circuit_breaker_threshold=2,
                    circuit_breaker_cooldown=30.0,
                ),
                client_factory=lambda: client,
                sleeper=sleeps.append,
            )

            with self.assertLogs("agecat.pacing", level="WARNING") as logs:
                service.download_many(
                    [
                        EpisodeJob(url=EPISODE_URL),
                        EpisodeJob(url="https://www.agedm.io/play/20260001/1/2"),
                    ]
                )

        output = "".join(logs.output)
        self.assertIn("Circuit breaker tripped", output)
        self.assertIn("Circuit breaker open", output)
        self.assertAlmostEqual(max(sleeps), 30.0, delta=1.0)

    def test_workers_are_staggered_and_paced_between_episodes(self):
        sleeps: list[float] = []

        with TemporaryDirectory() as directory:
            service = AgeCatService(
                download_options(
                    output_dir=Path(directory),
                    concurrency=1,
                    chunk_size=1024,
                    min_delay=2.0,
                    max_delay=2.0,
                    stagger=1.0,
                ),
                client_factory=FakeAgeClient,
                sleeper=sleeps.append,
            )

            service.download_many(
                [
                    EpisodeJob(url=EPISODE_URL),
                    EpisodeJob(url="https://www.agedm.io/play/20260001/1/2"),
                ]
            )

        self.assertEqual(len(sleeps), 2)
        self.assertLessEqual(sleeps[0], 1.0)
        self.assertEqual(sleeps[1], 2.0)


if __name__ == "__main__":
    unittest.main()
