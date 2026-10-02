"""Recurrence date expansion (daily/weekly, intervals, until)."""
import unittest
from datetime import date

from recurring_reminders.recurrence import dates, matches
from recurring_reminders.service import VersionRule


def rule(**kw):
    base = dict(
        series_id=1, version=1, effective_from=date(2026, 1, 1), tz="Test/USlike",
        freq="daily", interval=1, byweekday=(), time_of_day=None,
        start_date=date(2026, 1, 1), until=None, on_gap="skip", on_ambiguous="first",
    )
    base.update(kw)
    return VersionRule(**base)


class DailyTest(unittest.TestCase):
    def test_every_day(self):
        got = list(dates(rule(), date(2026, 1, 1), date(2026, 1, 5)))
        self.assertEqual(got, [date(2026, 1, d) for d in range(1, 6)])

    def test_interval_anchored_at_start(self):
        r = rule(interval=3, start_date=date(2026, 1, 2))
        got = list(dates(r, date(2026, 1, 1), date(2026, 1, 12)))
        self.assertEqual(got, [date(2026, 1, 2), date(2026, 1, 5),
                               date(2026, 1, 8), date(2026, 1, 11)])

    def test_until_inclusive(self):
        r = rule(until=date(2026, 1, 3))
        got = list(dates(r, date(2026, 1, 1), date(2026, 1, 10)))
        self.assertEqual(got, [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)])


class WeeklyTest(unittest.TestCase):
    def test_byweekday(self):
        # 2026-03-02 is a Monday
        r = rule(freq="weekly", byweekday=(0, 2, 4), start_date=date(2026, 3, 2))
        got = list(dates(r, date(2026, 3, 1), date(2026, 3, 15)))
        self.assertEqual(got, [date(2026, 3, 2), date(2026, 3, 4), date(2026, 3, 6),
                               date(2026, 3, 9), date(2026, 3, 11), date(2026, 3, 13)])

    def test_biweekly_anchored_on_start_week(self):
        r = rule(freq="weekly", interval=2, byweekday=(2,), start_date=date(2026, 3, 4))
        got = list(dates(r, date(2026, 3, 1), date(2026, 4, 5)))
        self.assertEqual(got, [date(2026, 3, 4), date(2026, 3, 18), date(2026, 4, 1)])

    def test_start_date_not_on_byweekday(self):
        # start Wednesday but rule fires Mondays: first hit is next Monday
        r = rule(freq="weekly", byweekday=(0,), start_date=date(2026, 3, 4))
        got = list(dates(r, date(2026, 3, 1), date(2026, 3, 20)))
        self.assertEqual(got, [date(2026, 3, 9), date(2026, 3, 16)])

    def test_matches_respects_start_and_until(self):
        r = rule(start_date=date(2026, 2, 10), until=date(2026, 2, 12))
        self.assertFalse(matches(r, date(2026, 2, 9)))
        self.assertTrue(matches(r, date(2026, 2, 10)))
        self.assertTrue(matches(r, date(2026, 2, 12)))
        self.assertFalse(matches(r, date(2026, 2, 13)))


if __name__ == "__main__":
    unittest.main()
