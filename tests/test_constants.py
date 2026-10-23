import unittest
from pathlib import Path

from agecat.cli import build_parser
from agecat.constants import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CIRCUIT_BREAKER_COOLDOWN,
    DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
    DEFAULT_CONCURRENCY,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_HOST_INTERVAL,
    DEFAULT_MAX_DELAY,
    DEFAULT_MIN_DELAY,
    DEFAULT_OUTPUT_DIR_NAME,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_RESOLVE_ATTEMPTS,
    DEFAULT_RETRIES,
    DEFAULT_RETRY_BUDGET,
    DEFAULT_STAGGER,
)
from agecat.options import DownloadOptions


class ConstantsTests(unittest.TestCase):
    def test_cli_defaults_match_shared_constants(self):
        args = build_parser().parse_args([])

        self.assertEqual(args.output.name, DEFAULT_OUTPUT_DIR_NAME)
        self.assertEqual(args.concurrency, DEFAULT_CONCURRENCY)
        self.assertEqual(args.timeout, DEFAULT_READ_TIMEOUT)
        self.assertEqual(args.connect_timeout, DEFAULT_CONNECT_TIMEOUT)
        self.assertEqual(args.retries, DEFAULT_RETRIES)
        self.assertEqual(args.chunk_size, DEFAULT_CHUNK_SIZE)
        self.assertEqual(args.min_delay, DEFAULT_MIN_DELAY)
        self.assertEqual(args.max_delay, DEFAULT_MAX_DELAY)
        self.assertEqual(args.stagger_start, DEFAULT_STAGGER)
        self.assertEqual(args.host_interval, DEFAULT_HOST_INTERVAL)
        self.assertEqual(args.resolve_attempts, DEFAULT_RESOLVE_ATTEMPTS)
        self.assertEqual(args.retry_budget, DEFAULT_RETRY_BUDGET)
        self.assertEqual(args.circuit_breaker_threshold, DEFAULT_CIRCUIT_BREAKER_THRESHOLD)
        self.assertEqual(args.circuit_breaker_cooldown, DEFAULT_CIRCUIT_BREAKER_COOLDOWN)

    def test_options_defaults_match_shared_constants(self):
        options = DownloadOptions(output_dir=Path("unused"))

        self.assertEqual(options.concurrency, DEFAULT_CONCURRENCY)
        self.assertEqual(options.timeout, DEFAULT_READ_TIMEOUT)
        self.assertEqual(options.connect_timeout, DEFAULT_CONNECT_TIMEOUT)
        self.assertEqual(options.retries, DEFAULT_RETRIES)
        self.assertEqual(options.chunk_size, DEFAULT_CHUNK_SIZE)
        self.assertEqual(options.min_delay, DEFAULT_MIN_DELAY)
        self.assertEqual(options.max_delay, DEFAULT_MAX_DELAY)
        self.assertEqual(options.stagger, DEFAULT_STAGGER)
        self.assertEqual(options.host_interval, DEFAULT_HOST_INTERVAL)
        self.assertEqual(options.resolve_attempts, DEFAULT_RESOLVE_ATTEMPTS)
        self.assertEqual(options.retry_budget, DEFAULT_RETRY_BUDGET)
        self.assertEqual(options.circuit_breaker_threshold, DEFAULT_CIRCUIT_BREAKER_THRESHOLD)
        self.assertEqual(options.circuit_breaker_cooldown, DEFAULT_CIRCUIT_BREAKER_COOLDOWN)


if __name__ == "__main__":
    unittest.main()
