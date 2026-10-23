import unittest

from agecat.errors import ParseError
from agecat.extractor import (
    AgeExtractor,
    PlaylistEpisode,
    PlaylistSource,
    matching_episode,
    order_sources,
    parse_detail_page,
    parse_episode_number,
    parse_play_page,
    select_source,
)

from .fakes import SITE, FakeAgeClient, FakeSource, detail_html, play_html

SOURCES = (
    FakeSource("xigua", "西瓜", 3, vip=True),
    FakeSource("ffm3u8", "非凡", 3),
    FakeSource("hnm3u8", "红牛", 2, label_format="第{number}集"),
    FakeSource("mystery", "神秘", 3),
)


def source(key: str, label: str = "", episodes: int = 0) -> PlaylistSource:
    return PlaylistSource(
        key=key,
        label=label or key,
        episodes=[
            PlaylistEpisode(
                label=f"第{number:02d}集", url=f"{SITE}/play/1/1/{number}", number=number
            )
            for number in range(1, episodes + 1)
        ],
    )


class ParsingTests(unittest.TestCase):
    def test_parse_episode_number_handles_padding_and_bare_numbers(self):
        self.assertEqual(parse_episode_number("第01集"), 1)
        self.assertEqual(parse_episode_number("第12集(完)"), 12)
        self.assertEqual(parse_episode_number("第3话"), 3)
        self.assertEqual(parse_episode_number("07"), 7)
        self.assertIsNone(parse_episode_number("SP"))
        self.assertIsNone(parse_episode_number("剧场版"))

    def test_parse_detail_page_extracts_name_and_every_mirror_in_tab_order(self):
        detail = parse_detail_page(detail_html("欺诈游戏", "20260096", SOURCES), SITE)

        self.assertEqual(detail.anime_name, "欺诈游戏")
        self.assertEqual(
            [item.key for item in detail.sources], ["xigua", "ffm3u8", "hnm3u8", "mystery"]
        )
        self.assertEqual(detail.sources[0].label, "西瓜")
        self.assertTrue(detail.sources[0].vip)
        self.assertFalse(detail.sources[1].vip)
        self.assertEqual(
            detail.sources[1].episodes[0],
            PlaylistEpisode(label="第01集", url=f"{SITE}/play/20260096/2/1", number=1),
        )
        self.assertEqual([episode.number for episode in detail.sources[2].episodes], [1, 2])

    def test_parse_detail_page_falls_back_to_document_title(self):
        html = "<html><head><title>欺诈游戏 - AGE动漫</title></head></html>"

        self.assertEqual(parse_detail_page(html, SITE).anime_name, "欺诈游戏")

    def test_parse_play_page_extracts_title_player_and_playlists(self):
        page = parse_play_page(play_html("欺诈游戏", "20260096", 2, 3, SOURCES), SITE)

        self.assertEqual(page.page_title, "欺诈游戏 第03集")
        self.assertEqual(page.player_url, "https://jx.example.com:8443/m3u8/?url=age_ffm3u8_3")
        self.assertEqual(len(page.sources), 4)

    def test_matching_episode_matches_by_number_across_label_styles(self):
        wanted = PlaylistEpisode(label="第02集", url=f"{SITE}/play/1/1/2", number=2)
        other = PlaylistSource(
            key="hnm3u8",
            label="红牛",
            episodes=[
                PlaylistEpisode(label="第1集", url=f"{SITE}/play/1/3/1", number=1),
                PlaylistEpisode(label="第2集", url=f"{SITE}/play/1/3/2", number=2),
            ],
        )

        match = matching_episode(other, wanted)

        assert match is not None
        self.assertEqual(match.url, f"{SITE}/play/1/3/2")

    def test_matching_episode_returns_none_when_a_mirror_lacks_the_episode(self):
        wanted = PlaylistEpisode(label="第09集", url=f"{SITE}/play/1/1/9", number=9)

        self.assertIsNone(matching_episode(source("other", episodes=2), wanted))

    def test_matching_episode_falls_back_to_slot_for_unnumbered_labels(self):
        wanted = PlaylistEpisode(label="SP", url=f"{SITE}/play/1/1/2", number=None)
        other = PlaylistSource(
            key="b",
            label="b",
            episodes=[
                PlaylistEpisode(label="特典1", url=f"{SITE}/play/1/2/1"),
                PlaylistEpisode(label="特典2", url=f"{SITE}/play/1/2/2"),
            ],
        )

        match = matching_episode(other, wanted)

        assert match is not None
        self.assertEqual(match.label, "特典2")


class SourceOrderTests(unittest.TestCase):
    def test_default_order_follows_priority_then_page_order(self):
        sources = [source("mystery"), source("wjm3u8"), source("xigua"), source("ffm3u8")]

        ordered = order_sources(sources)

        self.assertEqual([item.key for item in ordered], ["xigua", "ffm3u8", "wjm3u8", "mystery"])

    def test_preferred_sources_restrict_and_order(self):
        sources = [source("xigua", "西瓜"), source("ffm3u8", "非凡"), source("wjm3u8", "无尽")]

        ordered = order_sources(sources, ["无尽", "1", "missing"])

        self.assertEqual([item.key for item in ordered], ["wjm3u8", "xigua"])

    def test_select_source_accepts_key_label_or_tab_number(self):
        sources = [source("xigua", "西瓜"), source("ffm3u8", "非凡")]

        self.assertEqual(select_source(sources, "FFM3U8"), sources[1])
        self.assertEqual(select_source(sources, "西瓜"), sources[0])
        self.assertEqual(select_source(sources, "2"), sources[1])
        self.assertIsNone(select_source(sources, "3"))


class ExtractorTests(unittest.TestCase):
    def test_season_episode_urls_uses_most_complete_mirror(self):
        client = FakeAgeClient(name="欺诈游戏", anime_id="20260096", sources=SOURCES)

        season = AgeExtractor(client).season_episode_urls(f"{SITE}/detail/20260096")

        self.assertEqual(season.anime_name, "欺诈游戏")
        self.assertEqual(
            season.episode_urls, [f"{SITE}/play/20260096/1/{number}" for number in (1, 2, 3)]
        )
        self.assertEqual(season.episode_numbers, [1, 2, 3])

    def test_season_rejects_detail_page_without_playlists(self):
        client = FakeAgeClient(sources=())

        with self.assertRaisesRegex(ParseError, "no playlist"):
            AgeExtractor(client).season_episode_urls(f"{SITE}/detail/20260001")

    def test_episode_page_builds_title_and_ordered_candidates(self):
        client = FakeAgeClient(name="欺诈游戏", anime_id="20260096", sources=SOURCES)

        page = AgeExtractor(client).episode_page(f"{SITE}/play/20260096/2/2")

        self.assertEqual(page.title, "欺诈游戏 第02集")
        self.assertEqual(page.anime_name, "欺诈游戏")
        self.assertEqual(
            [(item.key, item.play_url) for item in page.candidates],
            [
                ("xigua", f"{SITE}/play/20260096/1/2"),
                ("ffm3u8", f"{SITE}/play/20260096/2/2"),
                ("hnm3u8", f"{SITE}/play/20260096/3/2"),
                ("mystery", f"{SITE}/play/20260096/4/2"),
            ],
        )
        # Only the page we already fetched has its player URL in hand.
        self.assertIsNone(page.candidates[0].player_url)
        self.assertIsNotNone(page.candidates[1].player_url)

    def test_episode_page_skips_mirrors_missing_the_episode(self):
        client = FakeAgeClient(sources=SOURCES)

        page = AgeExtractor(client).episode_page(f"{SITE}/play/20260001/1/3")

        self.assertNotIn("hnm3u8", [item.key for item in page.candidates])

    def test_resolve_stream_falls_back_past_dead_mirrors(self):
        client = FakeAgeClient(sources=SOURCES, dead_sources=frozenset({"xigua", "ffm3u8"}))
        extractor = AgeExtractor(client)

        episode = extractor.resolve_stream(extractor.episode_page(f"{SITE}/play/20260001/1/1"))

        self.assertEqual(episode.source, "红牛")
        self.assertEqual(episode.kind, "mp4")
        self.assertTrue(episode.stream_url.startswith("https://cdn.example/hnm3u8_1.mp4"))

    def test_resolve_stream_honors_source_priority(self):
        client = FakeAgeClient(sources=SOURCES)
        extractor = AgeExtractor(client, source_priority=["mystery"])

        episode = extractor.episode(f"{SITE}/play/20260001/1/1")

        self.assertEqual(episode.source, "神秘")

    def test_resolve_stream_lists_every_failure(self):
        client = FakeAgeClient(sources=SOURCES[:2], dead_sources=frozenset({"xigua", "ffm3u8"}))
        extractor = AgeExtractor(client)

        with self.assertRaisesRegex(ParseError, "西瓜: .*; 非凡: "):
            extractor.episode(f"{SITE}/play/20260001/1/1")


if __name__ == "__main__":
    unittest.main()
