"""Local-time -> UTC resolution against pinned timezone data."""
import unittest
from datetime import datetime, timedelta, timezone

from tests.pinned_tz import FALL_BACK_DAY, SPRING_FORWARD_DAY, ZONES
from recurring_reminders.tzresolve import (
    AMBIGUOUS,
    GAP_SHIFTED,
    GAP_SKIPPED,
    NORMAL,
    resolve,
)

UTC = timezone.utc
Z = ZONES["Test/USlike"]


class PinnedDataSanityTest(unittest.TestCase):
    """The pinned TZif must behave like the intended rules."""

    def test_offsets_and_abbreviations(self):
        self.assertEqual(datetime(2026, 1, 15, 12, tzinfo=Z).utcoffset(), timedelta(hours=-5))
        self.assertEqual(datetime(2026, 1, 15, 12, tzinfo=Z).tzname(), "EST")
        self.assertEqual(datetime(2026, 7, 15, 12, tzinfo=Z).utcoffset(), timedelta(hours=-4))
        self.assertEqual(datetime(2026, 7, 15, 12, tzinfo=Z).tzname(), "EDT")

    def test_spring_forward_instant(self):
        before = datetime(2026, 3, 8, 6, 59, 59, tzinfo=UTC).astimezone(Z)
        after = datetime(2026, 3, 8, 7, 0, 0, tzinfo=UTC).astimezone(Z)
        self.assertEqual((before.hour, before.minute), (1, 59))
        self.assertEqual((after.hour, after.minute), (3, 0))

    def test_fall_back_instant(self):
        first = datetime(2026, 11, 1, 5, 30, tzinfo=UTC).astimezone(Z)
        second = datetime(2026, 11, 1, 6, 30, tzinfo=UTC).astimezone(Z)
        self.assertEqual((first.hour, first.minute, first.utcoffset()), (1, 30, timedelta(hours=-4)))
        self.assertEqual((second.hour, second.minute, second.utcoffset()), (1, 30, timedelta(hours=-5)))

    def test_fixed_east_zone(self):
        e = ZONES["Test/East"]
        self.assertEqual(datetime(2026, 4, 10, 0, 10, tzinfo=e).utcoffset(), timedelta(hours=10))


class ResolveTest(unittest.TestCase):
    def test_normal_time(self):
        res = resolve(datetime(2026, 1, 15, 9, 0), Z, "skip", "first")
        self.assertEqual(res.kind, NORMAL)
        self.assertEqual(res.utc, datetime(2026, 1, 15, 14, 0, tzinfo=UTC))
        self.assertEqual(res.actual_local.utcoffset(), timedelta(hours=-5))

    def test_same_local_time_maps_to_different_utc_across_dst(self):
        # proves resolution is not a fixed UTC offset
        winter = resolve(datetime(2026, 1, 15, 9, 0), Z, "skip", "first")
        summer = resolve(datetime(2026, 7, 15, 9, 0), Z, "skip", "first")
        self.assertEqual(winter.utc, datetime(2026, 1, 15, 14, 0, tzinfo=UTC))
        self.assertEqual(summer.utc, datetime(2026, 7, 15, 13, 0, tzinfo=UTC))

    def test_gap_skip(self):
        res = resolve(datetime(2026, 3, 8, 2, 30), Z, "skip", "first")
        self.assertEqual(res.kind, GAP_SKIPPED)
        self.assertIsNone(res.utc)
        self.assertIn("does not exist", res.reason)
        self.assertIn("2026-03-08 02:30", res.reason)

    def test_gap_shift_moves_to_next_valid_moment(self):
        res = resolve(datetime(2026, 3, 8, 2, 30), Z, "shift", "first")
        self.assertEqual(res.kind, GAP_SHIFTED)
        self.assertEqual(res.utc, datetime(2026, 3, 8, 7, 30, tzinfo=UTC))
        self.assertEqual(
            res.actual_local.replace(tzinfo=None), datetime(2026, 3, 8, 3, 30)
        )
        self.assertEqual(res.actual_local.utcoffset(), timedelta(hours=-4))
        self.assertIn("03:30", res.reason)

    def test_gap_edge_times(self):
        # 02:00 exactly is the first nonexistent minute; 03:00 is valid again
        shifted = resolve(datetime(2026, 3, 8, 2, 0), Z, "shift", "first")
        self.assertEqual(shifted.actual_local.replace(tzinfo=None), datetime(2026, 3, 8, 3, 0))
        normal = resolve(datetime(2026, 3, 8, 3, 0), Z, "skip", "first")
        self.assertEqual(normal.kind, NORMAL)

    def test_ambiguous_first_and_second(self):
        naive = datetime(2026, 11, 1, 1, 30)
        first = resolve(naive, Z, "skip", "first")
        second = resolve(naive, Z, "skip", "second")
        self.assertEqual(first.kind, AMBIGUOUS)
        self.assertEqual(second.kind, AMBIGUOUS)
        self.assertEqual(first.utc, datetime(2026, 11, 1, 5, 30, tzinfo=UTC))   # 01:30 EDT
        self.assertEqual(second.utc, datetime(2026, 11, 1, 6, 30, tzinfo=UTC))  # 01:30 EST
        self.assertEqual(second.utc - first.utc, timedelta(hours=1))
        self.assertEqual(first.actual_local.utcoffset(), timedelta(hours=-4))
        self.assertEqual(second.actual_local.utcoffset(), timedelta(hours=-5))

    def test_naive_required(self):
        with self.assertRaises(ValueError):
            resolve(datetime(2026, 1, 1, tzinfo=UTC), Z, "skip", "first")


if __name__ == "__main__":
    unittest.main()
