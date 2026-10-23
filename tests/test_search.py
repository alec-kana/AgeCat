import unittest
from urllib.parse import parse_qs, urlsplit

from agecat.errors import ParseError
from agecat.search import SearchResult, parse_search_page, search_anime, search_url

SITE = "https://www.agedm.io"


def card(anime_id: int, title: str, status: str = "第12集(完结)") -> str:
    return (
        '<div class="card cata_video_item py-4"><div class="d-flex">'
        '<div class="video_cover">'
        f'<a href="http://www.agedm.io/detail/{anime_id}" title="{title}"></a>'
        f'<span class="video_play_status rounded">{status}</span></div>'
        f'<div class="card-body"><h5 class="card-title">'
        f'<a href="http://www.agedm.io/detail/{anime_id}">{title}</a></h5>'
        '<div class="video_detail_info"><span>动画种类：</span>TV</div>'
        '<div class="video_detail_info"><span>首播时间：</span>2026-04-06</div>'
        '<div class="video_detail_info"><span>播放状态：</span>完结</div>'
        '<div class="video_detail_info desc"><span>简介：</span>略</div>'
        "</div></div></div>"
    )


def results_page(keyword: str, total: int, cards: list[str]) -> str:
    return (
        '<html><body><div id="search_results_wrapper">'
        f'<h6 class="search_keywords">关键词 <span class="text-danger">{keyword}</span> '
        f'搜索结果如下，共 <span class="text-danger">{total}</span> 条记录</h6>'
        f'<div id="cata_video_list" class="row"><div class="col-md-12">{"".join(cards)}</div></div>'
        "</div></body></html>"
    )


class FakeSearchClient:
    def __init__(self, pages: dict[int, str]) -> None:
        self.pages = pages
        self.requested: list[int] = []

    def get_page(self, url: str, *, page_url: str | None = None) -> str:
        page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
        self.requested.append(page)
        return self.pages[page]


class SearchTests(unittest.TestCase):
    def test_search_url_encodes_keyword_and_page(self):
        self.assertEqual(search_url(SITE, "欺诈"), f"{SITE}/search?query=%E6%AC%BA%E8%AF%88")
        self.assertEqual(search_url(SITE + "/", "a b", 3), f"{SITE}/search?query=a+b&page=3")

    def test_parse_search_page_extracts_cards_and_total(self):
        page = parse_search_page(results_page("欺诈", 3, [card(20260096, "欺诈游戏")]), SITE)

        self.assertEqual(page.total, 3)
        self.assertEqual(
            page.results,
            [
                SearchResult(
                    anime_id="20260096",
                    title="欺诈游戏",
                    # Rebased onto the live site's scheme and host.
                    url="https://www.agedm.io/detail/20260096",
                    status="第12集(完结)",
                    kind="TV",
                    premiere="2026-04-06",
                    play_state="完结",
                )
            ],
        )

    def test_parse_search_page_with_no_matches(self):
        page = parse_search_page(results_page("欺詐", 0, []), SITE)

        self.assertEqual((page.results, page.total), ([], 0))

    def test_parse_search_page_rejects_other_pages(self):
        with self.assertRaises(ParseError):
            parse_search_page("<html><body>blocked</body></html>", SITE)

    def test_search_follows_pages_until_the_limit(self):
        client = FakeSearchClient(
            {
                1: results_page("的", 5, [card(1, "A"), card(2, "B")]),
                2: results_page("的", 5, [card(3, "C"), card(4, "D")]),
                3: results_page("的", 5, [card(5, "E")]),
            }
        )

        outcome = search_anime(client, SITE, "的", limit=3)

        self.assertEqual([result.title for result in outcome.results], ["A", "B", "C"])
        self.assertEqual(outcome.total, 5)
        self.assertEqual(client.requested, [1, 2])

    def test_search_stops_once_every_match_is_collected(self):
        client = FakeSearchClient(
            {
                1: results_page("x", 3, [card(1, "A"), card(2, "B")]),
                2: results_page("x", 3, [card(3, "C")]),
            }
        )

        outcome = search_anime(client, SITE, "x", limit=50)

        self.assertEqual(len(outcome.results), 3)
        self.assertEqual(client.requested, [1, 2])

    def test_search_rejects_non_positive_limit(self):
        with self.assertRaises(ValueError):
            search_anime(FakeSearchClient({}), SITE, "x", limit=0)


if __name__ == "__main__":
    unittest.main()
