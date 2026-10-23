import os
import stat
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from requests.cookies import RequestsCookieJar

from agecat.auth import (
    LoginError,
    SavedSession,
    SessionStore,
    agecat_home,
    apply_cookies,
    export_cookies,
    fetch_captcha,
    is_logged_in,
    login,
    refresh_session,
    remember_session,
    restore_session,
)

from .account_fakes import SESSION_COOKIE, SITE, FakeAccountClient, FakeAccountSite, far_future


def logged_in_client(site: FakeAccountSite) -> FakeAccountClient:
    client = FakeAccountClient(site)
    fetch_captcha(client, SITE)
    login(client, SITE, site.username, site.password, site.captcha_answer)
    return client


class SessionStoreTests(unittest.TestCase):
    def test_home_defaults_to_dot_agecat_and_honors_env(self):
        with patch.dict(os.environ, {"AGECAT_HOME": ""}):
            self.assertEqual(agecat_home(), Path.home() / ".agecat")
        with patch.dict(os.environ, {"AGECAT_HOME": "/tmp/agecat-test"}):
            self.assertEqual(agecat_home(), Path("/tmp/agecat-test"))

    def test_round_trip_and_clear(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "nested")
            saved = SavedSession(
                cookies={"a": {"value": "1", "expires": None}},
                username="alec",
                password="pw",
                saved_at=123.0,
            )

            store.save(saved)

            self.assertEqual(store.load(), saved)
            self.assertTrue(store.clear())
            self.assertFalse(store.clear())
            self.assertEqual(store.load(), SavedSession())

    @unittest.skipIf(sys.platform == "win32", "POSIX permissions only")
    def test_session_file_is_private(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.save(SavedSession(username="alec"))

            self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)

    def test_corrupt_file_is_ignored(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.path.write_text("{not json", encoding="utf-8")

            with self.assertLogs("agecat.auth", level="WARNING"):
                self.assertEqual(store.load(), SavedSession())


class CookieTests(unittest.TestCase):
    def test_export_skips_captcha_and_expired_cookies(self):
        jar = RequestsCookieJar()
        jar.set("age_captcha", "x", domain="www.agedm.io")
        jar.set("old", "x", domain="www.agedm.io", expires=int(time.time()) - 10)
        jar.set(SESSION_COOKIE, "t", domain="www.agedm.io")

        self.assertEqual(export_cookies(jar), {SESSION_COOKIE: {"value": "t", "expires": None}})

    def test_apply_replays_cookies_onto_a_new_domain(self):
        jar = RequestsCookieJar()

        applied = apply_cookies(
            jar,
            {
                SESSION_COOKIE: {"value": "t", "expires": far_future()},
                "stale": {"value": "x", "expires": time.time() - 10},
                "broken": {"value": None},
            },
            "https://www.agedm.xyz",
        )

        self.assertEqual(applied, 1)
        cookie = next(iter(jar))
        self.assertEqual((cookie.name, cookie.domain), (SESSION_COOKIE, ".agedm.xyz"))


class LoginTests(unittest.TestCase):
    def test_login_sends_the_site_form_and_sets_the_session(self):
        site = FakeAccountSite()
        client = logged_in_client(site)

        self.assertEqual(
            site.login_attempts,
            [
                {
                    "username": "alec",
                    "password": "correct horse",
                    "captcha": "4809",
                    "action": "login",
                }
            ],
        )
        self.assertTrue(is_logged_in(client, SITE))

    def test_login_failure_raises_the_site_message(self):
        site = FakeAccountSite()
        client = FakeAccountClient(site)
        fetch_captcha(client, SITE)

        with self.assertRaisesRegex(LoginError, "验证码错误"):
            login(client, SITE, "alec", "correct horse", "0000")
        self.assertFalse(is_logged_in(client, SITE))

    def test_saved_session_is_restored_by_a_new_client(self):
        site = FakeAccountSite(session_expires=far_future())
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            remember_session(logged_in_client(site), store, username="alec", password=None)

            fresh = FakeAccountClient(site)
            self.assertTrue(restore_session(fresh, SITE, store))
            self.assertTrue(is_logged_in(fresh, SITE))

            saved = store.load()
            self.assertEqual(saved.username, "alec")
            self.assertIsNone(saved.password)
            self.assertNotIn("age_captcha", saved.cookies)

    def test_restore_reports_an_expired_server_session(self):
        site = FakeAccountSite()
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            remember_session(logged_in_client(site), store, username="alec", password=None)
            site.valid_tokens.clear()

            with self.assertLogs("agecat.auth", level="INFO"):
                self.assertFalse(restore_session(FakeAccountClient(site), SITE, store))

    def test_restore_without_a_saved_session_makes_no_request(self):
        with TemporaryDirectory() as directory:
            client = FakeAccountClient(FakeAccountSite())
            client.get_resource = None  # type: ignore[assignment]

            self.assertFalse(restore_session(client, SITE, SessionStore(Path(directory))))

    def test_refresh_keeps_credentials_and_updates_cookies(self):
        site = FakeAccountSite()
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            client = logged_in_client(site)
            remember_session(client, store, username="alec", password="correct horse")
            client.session.cookies.set(SESSION_COOKIE, "renewed", domain="www.agedm.io")

            refresh_session(client, store)

            saved = store.load()
            self.assertEqual(saved.cookies[SESSION_COOKIE]["value"], "renewed")
            self.assertEqual((saved.username, saved.password), ("alec", "correct horse"))


if __name__ == "__main__":
    unittest.main()
