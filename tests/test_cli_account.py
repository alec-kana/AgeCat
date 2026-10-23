import io
import os
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from rich.console import Console

from agecat.account import AccountIO
from agecat.auth import SessionStore
from agecat.cli import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_USAGE,
    collects_main,
    login_main,
    logout_main,
    main,
)

from .account_fakes import SITE, FakeAccountClient, FakeAccountSite, favorite_card


class AccountCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.store = SessionStore(Path(self.directory.name))
        self.site = FakeAccountSite(
            favorites=[[favorite_card(20260096, "欺诈游戏"), favorite_card(20240004, "大欺诈师")]]
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def run_cli(self, command, argv, answers=(), *, interactive=True):
        """Run one account subcommand against the fake site with scripted answers."""

        answers = list(answers)
        asked: list[str] = []

        def prompt(label: str) -> str:
            asked.append(label)
            if not answers:
                raise AssertionError(f"unexpected prompt: {label}")
            return answers.pop(0)

        console = Console(file=io.StringIO(), width=80, color_system=None)
        account_io = AccountIO(
            prompt=prompt, secret=prompt, console=console, interactive=interactive
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"COLUMNS": "200"}))
            stack.enter_context(patch("agecat.cli.AgeClient", lambda: FakeAccountClient(self.site)))
            stack.enter_context(patch("agecat.cli.resolve_site_url", return_value=SITE))
            stack.enter_context(patch("agecat.cli.SessionStore", lambda: self.store))
            stack.enter_context(patch("agecat.cli.account_io", lambda: account_io))
            stack.enter_context(patch("sys.stdin.isatty", return_value=interactive))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            code = command(argv)
        return code, stdout.getvalue(), stderr.getvalue(), asked

    def test_login_then_reuse_without_any_prompt(self):
        code, _, _, asked = self.run_cli(login_main, [], ["alec", "correct horse", "4809", "y"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(asked), 4)

        code, stdout, _, asked = self.run_cli(login_main, [])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("already logged in as alec", stdout)
        self.assertEqual(asked, [])

    def test_login_flags_choose_password_saving_without_asking(self):
        self.run_cli(login_main, ["--no-save-password"], ["alec", "correct horse", "4809"])

        self.assertIsNone(self.store.load().password)

    def test_login_needs_a_terminal(self):
        code, _, stderr, _ = self.run_cli(login_main, [], interactive=False)

        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("terminal", stderr)

    def test_logout_forgets_everything(self):
        self.run_cli(login_main, ["--save-password"], ["alec", "correct horse", "4809"])

        code, stdout, _, _ = self.run_cli(logout_main, [])

        self.assertEqual(code, EXIT_OK)
        self.assertIn("logged out", stdout)
        self.assertFalse(self.store.path.exists())
        _, stdout, _, _ = self.run_cli(logout_main, [])
        self.assertIn("no saved session", stdout)

    def test_collects_logs_in_on_first_use_and_lists_favorites(self):
        code, stdout, _, asked = self.run_cli(
            collects_main, [], ["alec", "correct horse", "4809", ""]
        )

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(asked), 4)
        self.assertIn("欺诈游戏", stdout)
        self.assertIn("https://www.agedm.io/detail/20240004", stdout)
        self.assertIn("2 favorite(s)", stdout)

        # Second run: the saved session is reused silently.
        code, _, _, asked = self.run_cli(collects_main, [])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(asked, [])

    def test_collects_without_session_in_a_pipe_explains_how_to_log_in(self):
        code, _, stderr, _ = self.run_cli(collects_main, [], interactive=False)

        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("agecat login", stderr)

    def test_collects_download_passes_options_and_detail_urls(self):
        self.run_cli(login_main, [], ["alec", "correct horse", "4809", ""])

        with patch("agecat.cli.main", return_value=EXIT_OK) as download:
            code, _, _, _ = self.run_cli(
                collects_main, ["-d", "--pick", "2", "-o", "out", "--source", "无尽"]
            )

        self.assertEqual(code, EXIT_OK)
        download.assert_called_once_with(
            [
                "-o",
                "out",
                "--source",
                "无尽",
                "--base-url",
                SITE,
                "https://www.agedm.io/detail/20240004",
            ]
        )

    def test_collects_rejects_download_options_without_download(self):
        code, _, stderr, _ = self.run_cli(collects_main, ["-o", "out"])

        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("unrecognized arguments", stderr)

    def test_collects_pick_needs_download(self):
        code, _, stderr, _ = self.run_cli(collects_main, ["--pick", "1"])

        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--download", stderr)

    def test_collects_pick_out_of_range(self):
        self.run_cli(login_main, [], ["alec", "correct horse", "4809", ""])

        code, _, stderr, _ = self.run_cli(collects_main, ["-d", "--pick", "7"])

        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no favorite numbered 7 (there are 2)", stderr)

    def test_main_dispatches_account_subcommands(self):
        with patch("agecat.cli.logout_main", return_value=EXIT_OK) as logout:
            self.assertEqual(main(["logout"]), EXIT_OK)
        logout.assert_called_once_with([])


if __name__ == "__main__":
    unittest.main()
