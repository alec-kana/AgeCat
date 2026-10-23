import unittest

from agecat.errors import FetchError
from agecat.site import resolve_site_url


class FakeRedirects:
    def __init__(self, answers: dict[str, str | Exception]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def final_url(self, url: str) -> str:
        self.calls.append(url)
        answer = self.answers[url]
        if isinstance(answer, Exception):
            raise answer
        return answer


class SiteTests(unittest.TestCase):
    def test_follows_the_first_resolver_that_redirects_to_age(self):
        client = FakeRedirects(
            {
                "http://www.age.tv": FetchError("down"),
                "http://www.agedm.com": "https://www.agedm.xyz/?ref=agedm.com",
            }
        )

        site = resolve_site_url(client, resolver_urls=("http://www.age.tv", "http://www.agedm.com"))

        self.assertEqual(site, "https://www.agedm.xyz")
        self.assertEqual(client.calls, ["http://www.age.tv", "http://www.agedm.com"])

    def test_ignores_redirects_to_non_age_sites_and_falls_back(self):
        client = FakeRedirects(
            {
                "http://www.age.tv": "https://parking.example/",
                "http://agefans.com": "http://agefans.com/",
            }
        )

        with self.assertLogs("agecat.site", level="WARNING"):
            site = resolve_site_url(
                client,
                resolver_urls=("http://www.age.tv", "http://agefans.com"),
                fallback="https://www.agedm.io",
            )

        self.assertEqual(site, "https://www.agedm.io")


if __name__ == "__main__":
    unittest.main()
