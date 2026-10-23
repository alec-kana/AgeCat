import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock

from agecat.models import DownloadProgressEvent, DownloadResult, Episode, JobReport
from agecat.progress import (
    format_size,
    plain_download_progress,
    plain_progress_line,
    supports_rich_live,
)

EPISODE = Episode(
    page_url="https://www.agedm.io/play/1/1/1",
    title="Test Episode",
    stream_url="https://example.com/1.mp4",
)


class SupportsRichLiveTests(unittest.TestCase):
    def test_false_when_console_is_not_a_terminal(self):
        console = MagicMock(is_terminal=False, is_dumb_terminal=False)

        self.assertFalse(supports_rich_live(console))

    def test_false_when_terminal_is_dumb(self):
        console = MagicMock(is_terminal=True, is_dumb_terminal=True)

        self.assertFalse(supports_rich_live(console))

    def test_true_for_a_real_interactive_terminal(self):
        console = MagicMock(is_terminal=True, is_dumb_terminal=False)

        self.assertTrue(supports_rich_live(console))


class PlainProgressLineTests(unittest.TestCase):
    def test_includes_percentage_when_total_is_known(self):
        event = DownloadProgressEvent(
            episode=EPISODE, phase="advanced", bytes_delta=100, bytes_completed=50, total_bytes=200
        )

        line = plain_progress_line(event, completed_jobs=1, total_jobs=3)

        self.assertEqual(line, "[1/3] Test Episode: 50 B/200 B (25%)")

    def test_omits_percentage_when_total_is_unknown(self):
        event = DownloadProgressEvent(
            episode=EPISODE, phase="started", bytes_delta=0, bytes_completed=0, total_bytes=None
        )

        line = plain_progress_line(event, completed_jobs=0, total_jobs=1)

        self.assertEqual(line, "[0/1] Test Episode: 0 B")


class PlainDownloadProgressTests(unittest.TestCase):
    def test_prints_a_line_on_every_started_event_regardless_of_throttle(self):
        stdout = StringIO()

        with redirect_stdout(stdout), plain_download_progress(1, min_interval=999) as progress:
            progress.on_progress(
                DownloadProgressEvent(
                    episode=EPISODE,
                    phase="started",
                    bytes_delta=0,
                    bytes_completed=0,
                    total_bytes=10,
                )
            )
            progress.on_progress(
                DownloadProgressEvent(
                    episode=EPISODE,
                    phase="advanced",
                    bytes_delta=5,
                    bytes_completed=5,
                    total_bytes=10,
                )
            )

        output = stdout.getvalue()
        # The throttled "advanced" event should not add a second line.
        self.assertEqual(output.count("Test Episode"), 1)

    def test_on_done_prints_summary_and_bypasses_throttle_for_next_start(self):
        stdout = StringIO()
        result = DownloadResult(
            episode=EPISODE, path=Path("out.mp4"), status="downloaded", bytes_written=10
        )

        with redirect_stdout(stdout), plain_download_progress(1) as progress:
            progress.on_done(JobReport(url=EPISODE.page_url, result=result))

        self.assertIn("+ downloaded: Test Episode", stdout.getvalue())
        self.assertIn("(1/1)", stdout.getvalue())

    def test_on_done_prints_failure_summary(self):
        stdout = StringIO()

        with redirect_stdout(stdout), plain_download_progress(1) as progress:
            progress.on_done(JobReport(url="https://www.agedm.io/play/1/1/1", error="boom"))

        self.assertIn("- failed: https://www.agedm.io/play/1/1/1", stdout.getvalue())


class FormatSizeTests(unittest.TestCase):
    def test_formats_bytes_without_decimals(self):
        self.assertEqual(format_size(512), "512 B")

    def test_formats_larger_units_with_decimals(self):
        self.assertEqual(format_size(1536), "1.50 KB")


if __name__ == "__main__":
    unittest.main()
