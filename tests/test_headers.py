import unittest

from agecat.headers import (
    fetch_site,
    header_value,
    host,
    identity_headers,
    origin,
    referer_for,
    registrable_domain,
    site_root,
)

EPISODE_URL = "https://www.agedm.io/29592"
API_URL = "https://v.agedm.io/api"
CDN_URL = "https://v.agedm.io/2024/demo.mp4"


class HeaderHelperTests(unittest.TestCase):
    def test_header_value_is_case_insensitive(self):
        self.assertEqual(
            header_value({"Content-Range": "bytes 0-1/2"}, "content-range"), "bytes 0-1/2"
        )
        self.assertIsNone(header_value({"Content-Range": "x"}, "etag"))

    def test_origin_and_host_normalize_case(self):
        self.assertEqual(origin("HTTPS://WWW.AGEDM.IO/play/1/1/1"), "https://www.agedm.io")
        self.assertEqual(host("HTTPS://WWW.AGEDM.IO/play/1/1/1"), "www.agedm.io")

    def test_origin_returns_none_without_scheme_or_host(self):
        self.assertIsNone(origin("/29592"))

    def test_registrable_domain_folds_subdomains(self):
        self.assertEqual(registrable_domain("v.agedm.io"), "agedm.io")
        self.assertEqual(registrable_domain("agedm.io"), "agedm.io")

    def test_site_root_is_the_domain_root_page(self):
        self.assertEqual(site_root(EPISODE_URL), "https://www.agedm.io/")


class FetchSiteTests(unittest.TestCase):
    def test_same_host_is_same_origin(self):
        self.assertEqual(fetch_site(EPISODE_URL, "https://www.agedm.io/"), "same-origin")

    def test_sibling_subdomain_is_same_site(self):
        self.assertEqual(fetch_site(API_URL, EPISODE_URL), "same-site")

    def test_unrelated_host_is_cross_site(self):
        self.assertEqual(
            fetch_site("https://pwvideo.example/6.mp4", "https://agedm.io/349"), "cross-site"
        )

    def test_missing_page_is_none(self):
        self.assertEqual(fetch_site(EPISODE_URL, None), "none")


class RefererTests(unittest.TestCase):
    def test_same_origin_request_keeps_the_full_page_url(self):
        self.assertEqual(
            referer_for("https://www.agedm.io/wp-json", "https://www.agedm.io/29592?cat=1921"),
            "https://www.agedm.io/29592?cat=1921",
        )

    def test_cross_origin_request_is_trimmed_to_the_page_origin(self):
        self.assertEqual(referer_for(API_URL, EPISODE_URL), "https://www.agedm.io/")

    def test_https_to_http_downgrade_sends_no_referer(self):
        self.assertIsNone(referer_for("http://cdn.example/demo.mp4", EPISODE_URL))

    def test_no_page_means_no_referer(self):
        self.assertIsNone(referer_for(API_URL, None))


class IdentityHeaderTests(unittest.TestCase):
    def test_cdn_video_request_carries_the_episode_page_referer(self):
        headers = identity_headers(CDN_URL, page_url=EPISODE_URL, dest="video", mode="no-cors")

        self.assertEqual(headers["Referer"], "https://www.agedm.io/")
        self.assertEqual(headers["Sec-Fetch-Dest"], "video")
        self.assertEqual(headers["Sec-Fetch-Mode"], "no-cors")
        self.assertEqual(headers["Sec-Fetch-Site"], "same-site")
        self.assertNotIn("Origin", headers)

    def test_api_request_sends_the_page_origin(self):
        headers = identity_headers(
            API_URL,
            page_url=EPISODE_URL,
            dest="empty",
            mode="cors",
            send_origin=True,
        )

        self.assertEqual(headers["Origin"], "https://www.agedm.io")
        self.assertEqual(headers["Referer"], "https://www.agedm.io/")

    def test_sec_fetch_site_is_never_claimed_without_a_backing_referer(self):
        headers = identity_headers(CDN_URL, page_url=None, dest="video", mode="no-cors")

        self.assertEqual(headers["Sec-Fetch-Site"], "none")
        self.assertNotIn("Referer", headers)
        self.assertNotIn("Origin", headers)


if __name__ == "__main__":
    unittest.main()
