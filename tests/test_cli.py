import os
import signal
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from agecat import EpisodeJob, SearchResult, __version__
from agecat.cli import (
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    build_parser,
    env_flag,
    main,
    options_from_args,
    run_downloads,
    termination_as_interrupt,
)
from agecat.constants import KEEP_PARTIAL_ENV_VAR, PLAIN_PROGRESS_ENV_VAR
from agecat.search import SearchOutcome

PLAY_URL = "https://www.agedm.io/play/1/1/1"
RESULT = SearchResult(
    anime_id="20260096",
    title="欺诈游戏",
    url="https://www.agedm.io/detail/20260096",
    status="第26集(完结)",
    kind="TV",
    premiere="2026-04-06",
    play_state="完结",
)


class CliTests(unittest.TestCase):
    def test_missing_url_does_not_prompt_when_stdin_is_not_tty(self):
        with (
            patch("sys.stdin.isatty", return_value=False),
            patch("builtins.input", side_effect=AssertionError("input should not run")),
        ):
            self.assertEqual(main([]), EXIT_USAGE)

    def test_invalid_options_return_argument_error(self):
        self.assertEqual(main(["--timeout", "0", "https://www.agedm.io/play/1/1/1"]), EXIT_USAGE)

    def test_invalid_connect_timeout_returns_argument_error(self):
        self.assertEqual(
            main(["--connect-timeout", "0", "https://www.agedm.io/play/1/1/1"]), EXIT_USAGE
        )

    def test_invalid_episodes_selector_returns_argument_error(self):
        self.assertEqual(main(["--episodes", "abc", "https://www.agedm.io/play/1/1/1"]), EXIT_USAGE)

    def test_episodes_flag_is_parsed(self):
        args = build_parser().parse_args(["--episodes", "15-17", "https://www.agedm.io/play/1/1/1"])

        self.assertEqual(args.episodes, "15-17")

    def test_timeout_flags_build_request_timeout_tuple(self):
        args = build_parser().parse_args(
            [
                "--connect-timeout",
                "2",
                "--timeout",
                "9",
                "https://www.agedm.io/play/1/1/1",
            ]
        )

        self.assertEqual(options_from_args(args).request_timeout, (2, 9))

    def test_pacing_and_recovery_flags_reach_options(self):
        args = build_parser().parse_args(
            [
                "--min-delay",
                "2",
                "--max-delay",
                "6",
                "--stagger-start",
                "3",
                "--host-interval",
                "1.5",
                "--resolve-attempts",
                "5",
                "--retry-budget",
                "120",
                "--circuit-breaker-threshold",
                "8",
                "--circuit-breaker-cooldown",
                "90",
                "https://www.agedm.io/play/1/1/1",
            ]
        )

        options = options_from_args(args)

        self.assertEqual(options.min_delay, 2)
        self.assertEqual(options.max_delay, 6)
        self.assertEqual(options.stagger, 3)
        self.assertEqual(options.host_interval, 1.5)
        self.assertEqual(options.resolve_attempts, 5)
        self.assertEqual(options.retry_budget, 120)
        self.assertEqual(options.circuit_breaker_threshold, 8)
        self.assertEqual(options.circuit_breaker_cooldown, 90)

    def test_no_pacing_zeroes_every_delay_and_wins_over_explicit_flags(self):
        args = build_parser().parse_args(
            ["--no-pacing", "--min-delay", "5", "https://www.agedm.io/play/1/1/1"]
        )

        options = options_from_args(args)

        self.assertEqual(options.min_delay, 0.0)
        self.assertEqual(options.max_delay, 0.0)
        self.assertEqual(options.stagger, 0.0)
        self.assertEqual(options.host_interval, 0.0)

    def test_inverted_delay_range_returns_argument_error(self):
        self.assertEqual(
            main(["--min-delay", "9", "--max-delay", "1", "https://www.agedm.io/play/1/1/1"]),
            EXIT_USAGE,
        )

    def test_verbose_flag_counts_diagnostic_level(self):
        args = build_parser().parse_args(["-vv", "https://www.agedm.io/play/1/1/1"])

        self.assertEqual(args.verbose, 2)
        self.assertFalse(args.quiet)

    def test_verbose_and_quiet_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit), redirect_stderr(StringIO()):
            build_parser().parse_args(["-vv", "--quiet", "https://www.agedm.io/play/1/1/1"])

    def test_version_flag_prints_package_version(self):
        stdout = StringIO()

        with self.assertRaises(SystemExit) as error, redirect_stdout(stdout):
            build_parser().parse_args(["--version"])

        self.assertEqual(error.exception.code, 0)
        self.assertIn(__version__, stdout.getvalue())

    def test_quiet_flag_parse(self):
        args = build_parser().parse_args(["--quiet", "https://www.agedm.io/play/1/1/1"])

        self.assertEqual(args.verbose, 0)
        self.assertTrue(args.quiet)

    def test_plain_progress_flag_sets_options(self):
        args = build_parser().parse_args(["--plain-progress", "https://www.agedm.io/play/1/1/1"])

        self.assertTrue(options_from_args(args).plain_progress)

    def test_plain_progress_defaults_to_false(self):
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop(PLAIN_PROGRESS_ENV_VAR, None)
            args = build_parser().parse_args(["https://www.agedm.io/play/1/1/1"])

        self.assertFalse(options_from_args(args).plain_progress)

    def test_plain_progress_env_var_sets_default_on(self):
        with patch.dict("os.environ", {PLAIN_PROGRESS_ENV_VAR: "1"}):
            args = build_parser().parse_args(["https://www.agedm.io/play/1/1/1"])

        self.assertTrue(options_from_args(args).plain_progress)

    def test_env_flag_recognizes_common_truthy_spellings(self):
        for value in ("1", "true", "True", "yes", "on"):
            with patch.dict("os.environ", {PLAIN_PROGRESS_ENV_VAR: value}):
                self.assertTrue(env_flag(PLAIN_PROGRESS_ENV_VAR), msg=value)

    def test_env_flag_treats_unset_or_zero_as_false(self):
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop(PLAIN_PROGRESS_ENV_VAR, None)
            self.assertFalse(env_flag(PLAIN_PROGRESS_ENV_VAR))

        with patch.dict("os.environ", {PLAIN_PROGRESS_ENV_VAR: "0"}):
            self.assertFalse(env_flag(PLAIN_PROGRESS_ENV_VAR))

    def test_plain_progress_flag_forces_plain_renderer_even_on_a_live_capable_terminal(self):
        args = build_parser().parse_args(["--plain-progress", "https://www.agedm.io/play/1/1/1"])
        options = options_from_args(args)
        service = MagicMock()

        with (
            patch("agecat.cli.supports_rich_live", return_value=True),
            patch("agecat.cli.plain_download_progress") as plain_factory,
            patch("agecat.cli.rich_download_progress") as rich_factory,
        ):
            run_downloads(service, options, [])

        plain_factory.assert_called_once_with(0)
        rich_factory.assert_not_called()

    def test_exit_code_constants_match_documented_values(self):
        self.assertEqual(EXIT_OK, 0)
        self.assertEqual(EXIT_FAILURE, 1)
        self.assertEqual(EXIT_USAGE, 2)


class SearchCliTests(unittest.TestCase):
    def search(self, argv, outcome, *, extra=()):
        stdout, stderr = StringIO(), StringIO()
        with ExitStack() as stack:
            # Pin the render width so table wrapping cannot break assertions.
            stack.enter_context(patch.dict(os.environ, {"COLUMNS": "200"}))
            stack.enter_context(patch("agecat.cli.AgeClient"))
            stack.enter_context(
                patch("agecat.cli.resolve_site_url", return_value="https://www.agedm.io")
            )
            search = stack.enter_context(patch("agecat.cli.search_anime", return_value=outcome))
            for context in extra:
                stack.enter_context(context)
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue(), search

    def test_search_prints_titles_and_detail_urls(self):
        code, stdout, _, search = self.search(
            ["search", "欺诈"], SearchOutcome(results=[RESULT], total=1)
        )

        self.assertEqual(code, EXIT_OK)
        self.assertIn("欺诈游戏", stdout)
        self.assertIn("https://www.agedm.io/detail/20260096", stdout)
        self.assertIn("第26集(完结)", stdout)
        self.assertIn("1 result(s)", stdout)
        self.assertNotIn(" of ", stdout)
        self.assertEqual(search.call_args.args[1:], ("https://www.agedm.io", "欺诈"))

    def test_search_drops_metadata_column_on_phone_width(self):
        _, stdout, _, _ = self.search(
            ["search", "欺诈"],
            SearchOutcome(results=[RESULT], total=1),
            extra=(patch.dict(os.environ, {"COLUMNS": "60"}),),
        )

        self.assertIn("https://www.agedm.io/detail/20260096", stdout)
        self.assertNotIn("Status · Premiere", stdout)

    def test_search_prints_blocks_when_too_narrow_for_a_table(self):
        _, stdout, _, _ = self.search(
            ["search", "欺诈"],
            SearchOutcome(results=[RESULT], total=1),
            extra=(patch.dict(os.environ, {"COLUMNS": "50"}),),
        )

        lines = stdout.splitlines()
        self.assertNotIn("Title", stdout)
        self.assertIn("欺诈游戏  第26集(完结) · TV · 2026-04-06", lines)
        # The URL stays on one line even though it is wider than the terminal.
        self.assertIn("  https://www.agedm.io/detail/20260096", lines)

    def test_search_says_when_results_were_truncated(self):
        _, stdout, _, search = self.search(
            ["search", "的", "-n", "1"], SearchOutcome(results=[RESULT], total=1392)
        )

        self.assertIn("1 result(s) of 1392", stdout)
        self.assertEqual(search.call_args.kwargs["limit"], 1)

    def test_search_base_url_skips_domain_resolution(self):
        with patch("agecat.cli.resolve_site_url", side_effect=AssertionError("resolved")):
            stdout = StringIO()
            with (
                patch("agecat.cli.AgeClient"),
                patch("agecat.cli.search_anime", return_value=SearchOutcome([RESULT], 1)) as search,
                redirect_stdout(stdout),
            ):
                code = main(["search", "x", "--base-url", "https://age.example/"])

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(search.call_args.args[1], "https://age.example")

    def test_search_without_match_returns_failure(self):
        code, _, stderr, _ = self.search(["search", "欺詐"], SearchOutcome(results=[], total=0))

        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("No search result", stderr)

    def test_search_rejects_bad_limit(self):
        with redirect_stderr(StringIO()):
            self.assertEqual(main(["search", "x", "--limit", "0"]), EXIT_USAGE)

    def test_non_search_first_argument_still_uses_the_download_parser(self):
        with patch("agecat.cli.search_main", side_effect=AssertionError("search should not run")):
            self.assertEqual(main(["--timeout", "0", PLAY_URL]), EXIT_USAGE)

    def test_search_without_keyword_prompts_when_stdin_is_a_tty(self):
        code, _, _, search = self.search(
            ["search"],
            SearchOutcome(results=[RESULT], total=1),
            extra=(
                patch("sys.stdin.isatty", return_value=True),
                patch("builtins.input", return_value="  欺诈  "),
            ),
        )

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(search.call_args.args[2], "欺诈")

    def test_search_without_keyword_does_not_prompt_when_stdin_is_not_a_tty(self):
        with (
            patch("sys.stdin.isatty", return_value=False),
            patch("builtins.input", side_effect=AssertionError("input should not run")),
            redirect_stderr(StringIO()),
        ):
            self.assertEqual(main(["search"]), EXIT_USAGE)

    def test_search_with_blank_keyword_returns_usage_error(self):
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="   "),
            redirect_stderr(StringIO()),
        ):
            self.assertEqual(main(["search", "   "]), EXIT_USAGE)


class InterruptCliTests(unittest.TestCase):
    def test_keep_partial_defaults_to_false(self):
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop(KEEP_PARTIAL_ENV_VAR, None)
            args = build_parser().parse_args([PLAY_URL])

        self.assertFalse(options_from_args(args).keep_partial)

    def test_keep_partial_flag_and_env_var_set_options(self):
        args = build_parser().parse_args(["--keep-partial", PLAY_URL])
        self.assertTrue(options_from_args(args).keep_partial)

        with patch.dict("os.environ", {KEEP_PARTIAL_ENV_VAR: "1"}):
            args = build_parser().parse_args([PLAY_URL])
        self.assertTrue(options_from_args(args).keep_partial)

    def test_interrupted_download_exits_130_and_reports_cleanup(self):
        service = MagicMock()
        service.collect_episode_urls.return_value = [EpisodeJob(url=PLAY_URL)]
        service.removed_partials = [(Path("a.mp4.part"), 2048)]
        stdout = StringIO()
        stderr = StringIO()

        with (
            patch("agecat.cli.AgeCatService", return_value=service),
            patch("agecat.cli.run_downloads", side_effect=KeyboardInterrupt),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(main([PLAY_URL]), EXIT_INTERRUPTED)

        self.assertIn("interrupted", stderr.getvalue())
        self.assertIn("removed 1 partial download(s), freeing 2.00 KB", stdout.getvalue())
        self.assertEqual(EXIT_INTERRUPTED, 130)

    @unittest.skipUnless(hasattr(signal, "SIGTERM"), "platform has no SIGTERM")
    def test_sigterm_is_raised_as_keyboard_interrupt_then_restored(self):
        original = signal.getsignal(signal.SIGTERM)

        with self.assertRaises(KeyboardInterrupt), termination_as_interrupt():
            signal.raise_signal(signal.SIGTERM)

        self.assertIs(signal.getsignal(signal.SIGTERM), original)


if __name__ == "__main__":
    unittest.main()
