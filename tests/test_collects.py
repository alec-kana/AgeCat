import unittest

from agecat.auth import NotLoggedInError, fetch_captcha, login
from agecat.collects import collects_url, fetch_collects, parse_collects_page

from .account_fakes import SITE, FakeAccountClient, FakeAccountSite, collects_page, favorite_card


def logged_in(site: FakeAccountSite) -> FakeAccountClient:
    client = FakeAccountClient(site)
    fetch_captcha(client, SITE)
    login(client, SITE, site.username, site.password, site.captcha_answer)
    return client


class ParseCollectsTests(unittest.TestCase):
    def test_cards_yield_one_entry_each_with_title_and_status(self):
        html = collects_page(
            [
                favorite_card(20260096, "欺诈游戏", "第26集(完结)"),
                favorite_card(20240004, "大欺诈师"),
            ],
            total=2,
        )

        entries, total = parse_collects_page(html, SITE)

        self.assertEqual(total, 2)
        self.assertEqual(
            [(entry.anime_id, entry.title, entry.status, entry.url) for entry in entries],
            [
                ("20260096", "欺诈游戏", "第26集(完结)", "https://www.agedm.io/detail/20260096"),
                ("20240004", "大欺诈师", "第12集", "https://www.agedm.io/detail/20240004"),
            ],
        )

    def test_real_site_card_markup(self):
        # Copied from the home page's list cards, which the favorites page reuses.
        card = (
            '<div class="col g-2 position-relative"><div class="video_item">'
            '<div class="video_item--image position-relative"><img src="x.jpg">'
            '<span class="video_item--info rounded-1 text-truncate">第3集</span></div>'
            '<div class="video_item-title text-truncate text-center py-2">'
            '<a href="http://www.agedm.io/detail/20260286" '
            'class="link-light text-decoration-none stretched-link">'
            "冰剑的魔术师将要统一世界 第二季</a>"
            "</div></div></div>"
        )

        entries, _ = parse_collects_page(collects_page([card]), SITE)

        self.assertEqual(
            [(entry.anime_id, entry.title, entry.status) for entry in entries],
            [("20260286", "冰剑的魔术师将要统一世界 第二季", "第3集")],
        )

    def test_header_and_footer_links_are_ignored(self):
        entries, _ = parse_collects_page(collects_page([favorite_card(1, "A")]), SITE)

        self.assertEqual([entry.anime_id for entry in entries], ["1"])

    def test_without_cards_any_detail_link_counts(self):
        html = (
            "<html><body><header><a href='/detail/9'>nav</a></header>"
            "<main><a href='/detail/5'><img alt='动画五'></a>"
            "<a href='/detail/5'>资源详情</a><a href='/play/5/1/1'>播放</a>"
            "<a href='/detail/6' title='动画六'>6</a></main></body></html>"
        )

        entries, total = parse_collects_page(html, SITE)

        self.assertIsNone(total)
        self.assertEqual(
            [(entry.anime_id, entry.title) for entry in entries],
            [("5", "资源详情"), ("6", "动画六")],
        )

    def test_collects_url(self):
        self.assertEqual(collects_url(SITE), "https://www.agedm.io/my/collects")
        self.assertEqual(collects_url(SITE, 2), "https://www.agedm.io/my/collects?page=2")


class FetchCollectsTests(unittest.TestCase):
    def test_follows_pages_until_every_favorite_is_loaded(self):
        site = FakeAccountSite(
            favorites=[
                [favorite_card(1, "A"), favorite_card(2, "B")],
                [favorite_card(3, "C")],
            ]
        )

        entries = fetch_collects(logged_in(site), SITE)

        self.assertEqual([entry.title for entry in entries], ["A", "B", "C"])
        self.assertEqual(len(site.pages_served), 2)

    def test_stops_when_a_page_repeats(self):
        # Sites often serve the last page again for out-of-range page numbers.
        site = FakeAccountSite(favorites=[[favorite_card(1, "A")], [favorite_card(1, "A")]])
        site_without_total = site

        entries = fetch_collects(logged_in(site_without_total), SITE)

        self.assertEqual([entry.anime_id for entry in entries], ["1"])

    def test_logged_out_redirect_raises(self):
        with self.assertRaises(NotLoggedInError):
            fetch_collects(FakeAccountClient(FakeAccountSite()), SITE)


if __name__ == "__main__":
    unittest.main()
