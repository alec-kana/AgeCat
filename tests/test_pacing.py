import unittest
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

from agecat.errors import AccessDeniedError, is_bot_mitigation
from agecat.pacing import CircuitBreaker, RateLimiter, full_jitter, jittered, retry_after_seconds


class FakeClock:
    """Manually advanced monotonic clock paired with a recording sleeper."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class BackoffTests(unittest.TestCase):
    def test_full_jitter_stays_within_the_exponential_bound(self):
        for attempt in range(5):
            for _ in range(50):
                delay = full_jitter(0.5, attempt, cap=30.0)
                self.assertGreaterEqual(delay, 0.0)
                self.assertLessEqual(delay, 0.5 * 2**attempt)

    def test_full_jitter_is_not_deterministic(self):
        delays = {full_jitter(1.0, 3) for _ in range(30)}

        self.assertGreater(len(delays), 1)

    def test_full_jitter_respects_the_cap(self):
        self.assertLessEqual(full_jitter(10.0, 10, cap=5.0), 5.0)

    def test_full_jitter_disabled_by_zero_base(self):
        self.assertEqual(full_jitter(0.0, 3), 0.0)

    def test_jittered_tolerates_an_inverted_range(self):
        self.assertEqual(jittered(4.0, 1.0), 4.0)


class RetryAfterTests(unittest.TestCase):
    def test_delta_seconds_are_parsed(self):
        self.assertEqual(retry_after_seconds({"Retry-After": "12"}), 12.0)

    def test_http_date_is_converted_to_a_delay(self):
        target = datetime.now(UTC) + timedelta(seconds=30)

        delay = retry_after_seconds({"retry-after": format_datetime(target)})

        assert delay is not None
        self.assertAlmostEqual(delay, 30.0, delta=2.0)

    def test_past_http_date_clamps_to_zero(self):
        target = datetime.now(UTC) - timedelta(seconds=30)

        self.assertEqual(retry_after_seconds({"Retry-After": format_datetime(target)}), 0.0)

    def test_value_is_capped(self):
        self.assertEqual(retry_after_seconds({"Retry-After": "99999"}, cap=60.0), 60.0)

    def test_missing_or_unparsable_values_return_none(self):
        self.assertIsNone(retry_after_seconds({}))
        self.assertIsNone(retry_after_seconds({"Retry-After": "  "}))
        self.assertIsNone(retry_after_seconds({"Retry-After": "soon"}))


class RateLimiterTests(unittest.TestCase):
    def test_requests_to_one_host_are_spaced_out(self):
        clock = FakeClock()
        limiter = RateLimiter(2.0, sleeper=clock.sleep, clock=clock)

        self.assertEqual(limiter.acquire("www.agedm.io"), 0.0)
        self.assertEqual(limiter.acquire("www.agedm.io"), 2.0)
        self.assertEqual(limiter.acquire("www.agedm.io"), 2.0)

    def test_separate_hosts_do_not_gate_each_other(self):
        clock = FakeClock()
        limiter = RateLimiter(2.0, sleeper=clock.sleep, clock=clock)

        limiter.acquire("www.agedm.io")

        self.assertEqual(limiter.acquire("v.agedm.io"), 0.0)

    def test_zero_interval_disables_the_gate(self):
        clock = FakeClock()
        limiter = RateLimiter(0.0, sleeper=clock.sleep, clock=clock)

        limiter.acquire("www.agedm.io")
        limiter.acquire("www.agedm.io")

        self.assertEqual(clock.slept, [])


class CircuitBreakerTests(unittest.TestCase):
    def test_breaker_trips_after_consecutive_blocks(self):
        clock = FakeClock()
        breaker = CircuitBreaker(3, 60.0, sleeper=clock.sleep, clock=clock)

        self.assertFalse(breaker.record_block("www.agedm.io"))
        self.assertFalse(breaker.record_block("www.agedm.io"))
        self.assertTrue(breaker.record_block("www.agedm.io"))
        self.assertEqual(breaker.wait("www.agedm.io"), 60.0)

    def test_success_clears_the_streak(self):
        clock = FakeClock()
        breaker = CircuitBreaker(2, 60.0, sleeper=clock.sleep, clock=clock)

        breaker.record_block("www.agedm.io")
        breaker.record_success("www.agedm.io")

        self.assertFalse(breaker.record_block("www.agedm.io"))
        self.assertEqual(clock.slept, [])

    def test_cooldown_expires(self):
        clock = FakeClock()
        breaker = CircuitBreaker(1, 60.0, sleeper=clock.sleep, clock=clock)

        breaker.record_block("www.agedm.io")
        breaker.wait("www.agedm.io")

        self.assertEqual(breaker.wait("www.agedm.io"), 0.0)

    def test_zero_threshold_disables_the_breaker(self):
        clock = FakeClock()
        breaker = CircuitBreaker(0, 60.0, sleeper=clock.sleep, clock=clock)

        self.assertFalse(breaker.enabled)
        self.assertFalse(breaker.record_block("www.agedm.io"))
        self.assertEqual(breaker.wait("www.agedm.io"), 0.0)


class DenialClassificationTests(unittest.TestCase):
    def test_expired_signed_cookie_denial_is_not_bot_mitigation(self):
        error = AccessDeniedError(
            "denied",
            status_code=403,
            headers={"Server": "nginx", "Content-Type": "application/json"},
        )

        self.assertFalse(error.bot_mitigation)

    def test_challenge_header_marks_bot_mitigation(self):
        error = AccessDeniedError("denied", status_code=403, headers={"cf-mitigated": "challenge"})

        self.assertTrue(error.bot_mitigation)

    def test_html_block_page_from_a_mitigation_edge_is_bot_mitigation(self):
        self.assertTrue(
            is_bot_mitigation({"server": "cloudflare", "content-type": "text/html; charset=UTF-8"})
        )

    def test_mitigation_edge_serving_a_plain_origin_denial_is_not_a_block(self):
        self.assertFalse(is_bot_mitigation({"server": "cloudflare", "content-type": "video/mp4"}))


if __name__ == "__main__":
    unittest.main()
