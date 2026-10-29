from __future__ import annotations

from datetime import datetime, timezone
import unittest
from zoneinfo import ZoneInfo

from saas_sentry.scheduling import next_run, run_with_retry


class SchedulingTests(unittest.TestCase):
    def test_next_run_uses_configured_timezone_and_rolls_to_next_day(self) -> None:
        now = datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc)
        scheduled = next_run(now, "09:30", "Asia/Kolkata")
        self.assertEqual(scheduled, datetime(2026, 10, 30, 4, 0, tzinfo=timezone.utc))

    def test_next_run_rejects_unknown_timezone_or_invalid_time(self) -> None:
        with self.assertRaises(ValueError):
            next_run(datetime.now(timezone.utc), "25:00", "UTC")
        with self.assertRaises(ValueError):
            next_run(datetime.now(timezone.utc), "09:00", "Mars/Olympus")

    def test_next_run_moves_forward_when_dst_skips_the_requested_wall_time(self) -> None:
        now = datetime(2026, 3, 7, 18, 0, tzinfo=timezone.utc)
        scheduled = next_run(now, "02:30", "America/New_York")
        local = scheduled.astimezone(ZoneInfo("America/New_York"))
        self.assertEqual((local.hour, local.minute), (3, 0))

    def test_retry_uses_exponential_backoff_then_returns_result(self) -> None:
        attempts = []
        delays = []

        def operation():
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("temporary")
            return "ok"

        self.assertEqual(run_with_retry(operation, retries=2, base_delay=0.5, sleep=delays.append), "ok")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(delays, [0.5, 1.0])

    def test_retry_raises_after_configured_attempts(self) -> None:
        attempts = []

        def operation():
            attempts.append(1)
            raise RuntimeError("offline")

        with self.assertRaisesRegex(RuntimeError, "offline"):
            run_with_retry(operation, retries=1, base_delay=0, sleep=lambda _delay: None)
        self.assertEqual(len(attempts), 2)


if __name__ == "__main__":
    unittest.main()
