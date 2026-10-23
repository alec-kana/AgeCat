import os
import unittest

from agecat.client import AgeClient
from agecat.extractor import AgeExtractor
from agecat.hls import fetch_media_playlist
from agecat.site import resolve_site_url
from agecat.urls import rebase_url

RUN_INTEGRATION = os.environ.get("AGECAT_RUN_INTEGRATION") == "1"
SMOKE_URL = os.environ.get("AGECAT_SMOKE_URL", "https://www.agedm.io/play/20260096/1/1")
HLS_SOURCES = os.environ.get("AGECAT_SMOKE_HLS_SOURCES", "wjm3u8,hnm3u8,ffm3u8").split(",")


@unittest.skipUnless(RUN_INTEGRATION, "set AGECAT_RUN_INTEGRATION=1 to run AGE smoke tests")
class AgeSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = AgeClient(timeout=(10.0, 30.0), retries=1)
        self.url = rebase_url(SMOKE_URL, resolve_site_url(self.client))

    def tearDown(self) -> None:
        self.client.close()

    def test_vip_source_resolves_to_a_resumable_mp4(self):
        episode = AgeExtractor(self.client, source_priority=["xigua"]).episode(self.url)
        self.assertEqual(episode.kind, "mp4")

        response = self.client.stream_video(
            episode.stream_url, cookies={}, headers={"Range": "bytes=0-1"}
        )
        try:
            body = next(iter(response.iter_content(chunk_size=2)), b"")

            self.assertEqual(response.status_code, 206)
            self.assertRegex(response.headers.get("Content-Range", ""), r"^bytes 0-1/\d+$")
            self.assertEqual(len(body), 2)
        finally:
            response.close()

    def test_some_hls_mirror_serves_a_media_playlist(self):
        episode = AgeExtractor(self.client, source_priority=HLS_SOURCES).episode(self.url)
        self.assertEqual(episode.kind, "hls")

        playlist = fetch_media_playlist(self.client, episode)

        self.assertGreater(len(playlist.segments), 10)


if __name__ == "__main__":
    unittest.main()
