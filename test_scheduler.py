import datetime as dt
import unittest

from scheduler import due, next_run, healthy

UTC = dt.timezone.utc


class SchedulerTests(unittest.TestCase):
    def test_weekday_0940_only(self):
        monday = dt.datetime(2026, 11, 9, 1, 40, 25, tzinfo=UTC)
        self.assertTrue(due(monday, None))
        self.assertFalse(due(monday, '2026-11-09'))
        self.assertFalse(due(monday + dt.timedelta(minutes=1), None))
        self.assertFalse(due(monday - dt.timedelta(days=1), None))

    def test_restart_after_schedule_does_not_catch_up(self):
        now = dt.datetime(2026, 11, 9, 2, tzinfo=UTC)
        self.assertEqual(next_run(now, None), '2026-11-10T09:40:00+08:00')

    def test_friday_rolls_to_monday(self):
        now = dt.datetime(2026, 11, 13, 1, 40, tzinfo=UTC)
        self.assertEqual(next_run(now, '2026-11-13'), '2026-11-16T09:40:00+08:00')

    def test_still_runs_when_started_during_target_minute(self):
        now = dt.datetime(2026, 11, 9, 1, 40, 45, tzinfo=UTC)
        self.assertEqual(next_run(now, None), '2026-11-09T09:40:00+08:00')

    def test_health_distinguishes_idle_running_and_stalled(self):
        now = dt.datetime(2026, 11, 9, 1, 45, tzinfo=UTC)
        self.assertTrue(healthy({'next_run': '2026-11-10T09:40:00+08:00'}, now))
        self.assertFalse(healthy({'next_run': '2026-11-09T09:40:00+08:00'}, now))
        self.assertTrue(healthy({'running_since': (now-dt.timedelta(seconds=50)).isoformat()}, now))
        self.assertFalse(healthy({'running_since': (now-dt.timedelta(seconds=650)).isoformat()}, now))


if __name__ == '__main__':
    unittest.main()
