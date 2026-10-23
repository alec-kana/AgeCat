import os
import unittest

from agecat.client import AgeClient
from agecat.extractor import AgeExtractor
from agecat.hls import fetch_media_playlist
from agecat.search import search_anime
from agecat.site import resolve_site_url
from agecat.urls import anime_id_from_url, rebase_url

RUN_INTEGRATION = os.environ.get("AGECAT_RUN_INTEGRATION") == "1"
SMOKE_URL = os.environ.get("AGECAT_SMOKE_URL", "https://www.agedm.io/play/20260096/1/1")
SMOKE_DETAIL_URL = os.environ.get("AGECAT_SMOKE_DETAIL_URL", "https://www.agedm.io/detail/20260096")
SMOKE_SEARCH_KEYWORD = os.environ.get("AGECAT_SMOKE_SEARCH_KEYWORD", "欺诈游戏")
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

    def test_detail_page_lists_named_numbered_episodes(self):
        season = AgeExtractor(self.client).season_episode_urls(
            rebase_url(SMOKE_DETAIL_URL, self.url)
        )

        self.assertTrue(season.anime_name)
        self.assertGreater(len(season.episode_urls), 1)
        self.assertEqual(season.episode_numbers[:2], [1, 2])

    def test_search_finds_the_smoke_anime(self):
        site = rebase_url("/", self.url).rstrip("/")
        outcome = search_anime(self.client, site, SMOKE_SEARCH_KEYWORD)

        self.assertGreater(outcome.total, 0)
        self.assertIn(
            anime_id_from_url(SMOKE_DETAIL_URL), [result.anime_id for result in outcome.results]
        )

    def test_some_hls_mirror_serves_a_media_playlist(self):
        episode = AgeExtractor(self.client, source_priority=HLS_SOURCES).episode(self.url)
        self.assertEqual(episode.kind, "hls")

        playlist = fetch_media_playlist(self.client, episode)

        self.assertGreater(len(playlist.segments), 10)


if __name__ == "__main__":
    unittest.main()
