import unittest

from agecat.errors import AgeCatError
from agecat.urls import (
    PlayRef,
    anime_id_from_url,
    dedupe,
    ensure_supported_url,
    is_age_url,
    is_episode_url,
    is_season_url,
    parse_episode_selector,
    parse_play_url,
    rebase_url,
    split_urls,
)


class UrlTests(unittest.TestCase):
    def test_split_urls_accepts_commas_and_whitespace(self):
        self.assertEqual(split_urls(["a, b", " c\nd "]), ["a", "b", "c", "d"])

    def test_every_known_age_domain_is_accepted(self):
        for url in (
            "https://www.agedm.io/",
            "https://www.agedm.vip/",
            "https://agedm.live/",
            "https://www.agefans.la/",
            "http://agefans.com/",
            "http://www.age.tv/",
        ):
            with self.subTest(url=url):
                self.assertTrue(is_age_url(url))

    def test_other_hosts_are_rejected_unless_listed(self):
        self.assertFalse(is_age_url("https://anime1.me/"))
        self.assertFalse(is_age_url("https://agedm.io.evil.example/"))
        self.assertFalse(is_age_url("ftp://www.agedm.io/"))
        self.assertTrue(
            is_age_url("https://age-mirror.example/", extra_hosts=["age-mirror.example"])
        )

    def test_detail_and_play_shapes(self):
        self.assertTrue(is_season_url("https://www.agedm.io/detail/20260096"))
        self.assertTrue(is_season_url("https://www.agedm.io/detail/20260096/"))
        self.assertFalse(is_season_url("https://www.agedm.io/play/20260096/1/1"))
        self.assertTrue(is_episode_url("https://www.agedm.io/play/20260096/1/1"))
        self.assertFalse(is_episode_url("https://www.agedm.io/play/20260096/1"))
        self.assertFalse(is_episode_url("https://www.agedm.io/rank"))

    def test_ensure_supported_url_rejects_other_pages(self):
        ensure_supported_url("https://www.agedm.io/detail/1")
        with self.assertRaises(AgeCatError):
            ensure_supported_url("https://www.agedm.io/catalog/all-all-all-all-all-time-1")

    def test_parse_play_url(self):
        self.assertEqual(
            parse_play_url("https://www.agedm.io/play/20260096/2/13"),
            PlayRef(anime_id="20260096", source_index=2, episode_index=13),
        )
        with self.assertRaises(AgeCatError):
            parse_play_url("https://www.agedm.io/detail/20260096")

    def test_anime_id_from_url(self):
        self.assertEqual(anime_id_from_url("https://www.agedm.io/detail/7"), "7")
        self.assertEqual(anime_id_from_url("https://www.agedm.io/play/7/1/2"), "7")
        self.assertIsNone(anime_id_from_url("https://www.agedm.io/"))

    def test_rebase_url_moves_to_the_live_origin(self):
        self.assertEqual(
            rebase_url("http://www.agefans.la/play/1/2/3?x=1#frag", "https://www.agedm.io"),
            "https://www.agedm.io/play/1/2/3?x=1",
        )

    def test_parse_episode_selector(self):
        self.assertEqual(parse_episode_selector("1,3-5"), frozenset({1, 3, 4, 5}))
        for spec in ("", "0", "5-3", "a", "1,,2"):
            with self.subTest(spec=spec), self.assertRaises(AgeCatError):
                parse_episode_selector(spec)

    def test_dedupe_preserves_order(self):
        self.assertEqual(dedupe(["b", "a", "b"]), ["b", "a"])


if __name__ == "__main__":
    unittest.main()
