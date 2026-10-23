import io
import unittest
from collections.abc import Iterable
from pathlib import Path
from tempfile import TemporaryDirectory

from rich.console import Console

from agecat.account import AccountIO, ensure_login, interactive_login
from agecat.auth import LoginError, NotLoggedInError, SessionStore, is_logged_in

from .account_fakes import SITE, FakeAccountClient, FakeAccountSite


class ScriptedIO:
    """Answers prompts from a script and records what was asked."""

    def __init__(self, answers: Iterable[str]) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []
        self.output = io.StringIO()

    def ask(self, label: str) -> str:
        self.asked.append(label)
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {label}")
        return self.answers.pop(0)

    def account_io(self, *, interactive: bool = True) -> AccountIO:
        console = Console(file=self.output, width=60, force_terminal=False, color_system=None)
        return AccountIO(prompt=self.ask, secret=self.ask, console=console, interactive=interactive)


class InteractiveLoginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.store = SessionStore(Path(self.directory.name))
        self.site = FakeAccountSite()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_first_login_asks_everything_and_can_save_the_password(self):
        script = ScriptedIO(["alec", "correct horse", "4809", "y"])
        client = FakeAccountClient(self.site)

        interactive_login(client, SITE, self.store, script.account_io())

        saved = self.store.load()
        self.assertEqual((saved.username, saved.password), ("alec", "correct horse"))
        self.assertTrue(saved.has_cookies)
        self.assertTrue(self.store.captcha_path.exists())
        self.assertEqual(len(script.asked), 4)
        # The captcha was drawn in the terminal.
        self.assertIn("▀", script.output.getvalue())

    def test_declining_keeps_only_the_session_and_username(self):
        script = ScriptedIO(["alec", "correct horse", "4809", ""])

        interactive_login(FakeAccountClient(self.site), SITE, self.store, script.account_io())

        saved = self.store.load()
        self.assertEqual(saved.username, "alec")
        self.assertIsNone(saved.password)

    def test_saved_password_means_only_the_captcha_is_asked(self):
        interactive_login(
            FakeAccountClient(self.site),
            SITE,
            self.store,
            ScriptedIO(["alec", "correct horse", "4809"]).account_io(),
            save_password=True,
        )
        script = ScriptedIO(["4809"])

        interactive_login(FakeAccountClient(self.site), SITE, self.store, script.account_io())

        self.assertEqual(len(script.asked), 1)
        self.assertIn("验证码", script.asked[0])
        self.assertEqual(self.store.load().password, "correct horse")

    def test_wrong_captcha_retries_with_a_fresh_image(self):
        script = ScriptedIO(["alec", "correct horse", "1111", "4809", ""])
        client = FakeAccountClient(self.site)

        interactive_login(client, SITE, self.store, script.account_io())

        self.assertEqual(self.site.captchas_served, 2)
        self.assertIn("验证码错误", script.output.getvalue())
        self.assertTrue(is_logged_in(client, SITE))

    def test_empty_captcha_answer_fetches_another_image(self):
        script = ScriptedIO(["alec", "correct horse", "", "4809", ""])

        interactive_login(FakeAccountClient(self.site), SITE, self.store, script.account_io())

        self.assertEqual(self.site.captchas_served, 2)
        self.assertEqual(len(self.site.login_attempts), 1)

    def test_stale_saved_password_is_asked_again(self):
        interactive_login(
            FakeAccountClient(self.site),
            SITE,
            self.store,
            ScriptedIO(["alec", "correct horse", "4809"]).account_io(),
            save_password=True,
        )
        self.site.password = "new password!"
        # Captcha, then (wrong password) keep username, new password, captcha, save again.
        script = ScriptedIO(["4809", "", "new password!", "4809", "y"])

        interactive_login(FakeAccountClient(self.site), SITE, self.store, script.account_io())

        self.assertEqual(self.store.load().password, "new password!")

    def test_giving_up_after_repeated_failures(self):
        script = ScriptedIO(["alec", "correct horse", *(["0000"] * 5)])

        with self.assertRaises(LoginError):
            interactive_login(FakeAccountClient(self.site), SITE, self.store, script.account_io())
        self.assertFalse(self.store.path.exists())


class EnsureLoginTests(unittest.TestCase):
    def test_valid_saved_session_needs_no_prompt(self):
        site = FakeAccountSite()
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            interactive_login(
                FakeAccountClient(site),
                SITE,
                store,
                ScriptedIO(["alec", "correct horse", "4809", ""]).account_io(),
            )
            client = FakeAccountClient(site)

            ensure_login(client, SITE, store, ScriptedIO([]).account_io())

            self.assertTrue(is_logged_in(client, SITE))

    def test_expired_session_logs_in_again_interactively(self):
        site = FakeAccountSite()
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            interactive_login(
                FakeAccountClient(site),
                SITE,
                store,
                ScriptedIO(["alec", "correct horse", "4809", "y"]).account_io(),
            )
            site.valid_tokens.clear()
            script = ScriptedIO(["4809"])
            client = FakeAccountClient(site)

            ensure_login(client, SITE, store, script.account_io())

            self.assertTrue(is_logged_in(client, SITE))
            self.assertIn("过期", script.output.getvalue())

    def test_non_interactive_runs_fail_with_a_hint(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))

            with self.assertRaisesRegex(NotLoggedInError, "agecat login"):
                ensure_login(
                    FakeAccountClient(FakeAccountSite()),
                    SITE,
                    store,
                    ScriptedIO([]).account_io(interactive=False),
                )


if __name__ == "__main__":
    unittest.main()
