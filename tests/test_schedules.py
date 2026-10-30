from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from saas_sentry.schedules import create_schedule, list_schedules, run_due_schedules, set_schedule_enabled


class DashboardScheduleStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "schedules.sqlite"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_due_schedule_runs_once_and_records_history(self) -> None:
        create_schedule(
            self.database, name="Daily spend", schedule_time="08:00", timezone_name="UTC",
            actor="admin", now=datetime(2026, 10, 29, 7, 0, tzinfo=timezone.utc),
        )
        runs = []
        now = datetime(2026, 10, 29, 8, 5, tzinfo=timezone.utc)
        self.assertEqual(run_due_schedules(self.database, lambda job, run_id: runs.append(job["name"]), now=now), 1)
        self.assertEqual(run_due_schedules(self.database, lambda *_args: self.fail("must run once"), now=now), 0)
        schedule, = list_schedules(self.database)
        self.assertEqual(runs, ["Daily spend"])
        self.assertEqual(schedule["last_result"], "succeeded")
        self.assertEqual(len(schedule["history"]), 1)

    def test_paused_schedule_does_not_run_until_resumed(self) -> None:
        schedule = create_schedule(
            self.database, name="Daily spend", schedule_time="08:00", timezone_name="UTC",
            actor="admin", now=datetime(2026, 10, 29, 7, 0, tzinfo=timezone.utc),
        )
        set_schedule_enabled(self.database, schedule["schedule_id"], enabled=False)
        self.assertEqual(run_due_schedules(
            self.database, lambda *_args: self.fail("paused schedule must not run"),
            now=datetime(2026, 10, 29, 9, 0, tzinfo=timezone.utc),
        ), 0)


if __name__ == "__main__":
    unittest.main()
