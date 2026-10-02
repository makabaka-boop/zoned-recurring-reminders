"""Series CRUD, rule versioning, exceptions, confirmed-instance evidence."""
import tempfile
import unittest
from datetime import datetime, timezone

from tests.pinned_tz import ZONES
from recurring_reminders import ManualClock, Service

UTC = timezone.utc


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = f"{self.tmp.name}/test.db"
        self.clock = ManualClock(datetime(2026, 3, 1, 0, 0, tzinfo=UTC))
        self.svc = Service(self.db, clock=self.clock, tz_resolver=ZONES.__getitem__)

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def daily(self, **kw):
        defaults = dict(
            name="standup", tz="Test/USlike", freq="daily",
            time_of_day="09:00", start_date="2026-03-01",
        )
        defaults.update(kw)
        return self.svc.create_series(**defaults)


class CreateAndListTest(ServiceTestCase):
    def test_daily_occurrences_carry_full_api_fields(self):
        sid = self.daily()
        occs = self.svc.list_occurrences(sid, "2026-03-01", "2026-03-03")
        self.assertEqual([o["local_date"] for o in occs],
                         ["2026-03-01", "2026-03-02", "2026-03-03"])
        first = occs[0]
        self.assertEqual(first["version"], 1)
        self.assertEqual(first["intended_local"], "2026-03-01T09:00")
        self.assertEqual(first["effective_local"], "2026-03-01T09:00")
        self.assertEqual(first["actual_local"], "2026-03-01T09:00")
        self.assertEqual(first["utc"], "2026-03-01T14:00:00+00:00")  # EST
        self.assertEqual(first["status"], "scheduled")
        self.assertEqual(first["kind"], "normal")
        self.assertIsNone(first["skip_reason"])
        self.assertFalse(first["confirmed"])
        self.assertEqual(first["tz"], "Test/USlike")

    def test_weekly_series(self):
        sid = self.svc.create_series(
            name="gym", tz="Test/USlike", freq="weekly", byweekday=[0, 4],
            time_of_day="18:00", start_date="2026-03-02",
        )
        occs = self.svc.list_occurrences(sid, "2026-03-01", "2026-03-15")
        self.assertEqual([o["local_date"] for o in occs],
                         ["2026-03-02", "2026-03-06", "2026-03-09", "2026-03-13"])

    def test_invalid_inputs_rejected(self):
        with self.assertRaises(ValueError):
            self.daily(tz="No/SuchZone")
        with self.assertRaises(ValueError):
            self.daily(time_of_day="25:00")
        with self.assertRaises(ValueError):
            self.daily(on_gap="explode")
        with self.assertRaises(ValueError):
            self.daily(on_ambiguous="third")
        with self.assertRaises(ValueError):
            self.daily(freq="hourly")
        with self.assertRaises(ValueError):
            self.daily(interval=0)


class VersioningTest(ServiceTestCase):
    def test_modify_only_affects_instances_on_or_after_effective_date(self):
        sid = self.daily()
        v2 = self.svc.modify_series(sid, effective_from="2026-03-10", time_of_day="10:00")
        self.assertEqual(v2, 2)
        occs = self.svc.list_occurrences(sid, "2026-03-08", "2026-03-12")
        by_date = {o["local_date"]: o for o in occs}
        self.assertEqual(by_date["2026-03-09"]["version"], 1)
        self.assertEqual(by_date["2026-03-09"]["intended_local"], "2026-03-09T09:00")
        self.assertEqual(by_date["2026-03-10"]["version"], 2)
        self.assertEqual(by_date["2026-03-10"]["intended_local"], "2026-03-10T10:00")
        self.assertEqual(by_date["2026-03-11"]["version"], 2)
        # rule history is preserved as evidence
        series = self.svc.get_series(sid)
        self.assertEqual([v["version"] for v in series["versions"]], [1, 2])
        self.assertEqual(series["versions"][1]["effective_from"], "2026-03-10")

    def test_confirmed_instance_keeps_original_version_evidence(self):
        sid = self.daily()
        # confirm a future instance under version 1
        confirmed = self.svc.confirm_occurrence(sid, "2026-03-20")
        self.assertTrue(confirmed["confirmed"])
        self.assertEqual(confirmed["version"], 1)
        self.assertEqual(confirmed["utc"], "2026-03-20T13:00:00+00:00")  # EDT by then

        # now change the series effective before that date
        self.svc.modify_series(sid, effective_from="2026-03-10", time_of_day="07:30")

        occs = self.svc.list_occurrences(sid, "2026-03-19", "2026-03-21")
        by_date = {o["local_date"]: o for o in occs}
        frozen = by_date["2026-03-20"]
        self.assertTrue(frozen["confirmed"])
        self.assertEqual(frozen["version"], 1)                      # old version kept
        self.assertEqual(frozen["intended_local"], "2026-03-20T09:00")  # old time kept
        self.assertEqual(frozen["utc"], "2026-03-20T13:00:00+00:00")
        # neighbours are unconfirmed and follow the new version
        self.assertEqual(by_date["2026-03-21"]["version"], 2)
        self.assertEqual(by_date["2026-03-21"]["intended_local"], "2026-03-21T07:30")

    def test_confirm_is_idempotent(self):
        sid = self.daily()
        a = self.svc.confirm_occurrence(sid, "2026-03-05")
        b = self.svc.confirm_occurrence(sid, "2026-03-05")
        self.assertEqual(a, b)

    def test_confirm_non_occurrence_rejected(self):
        sid = self.daily(until="2026-03-05")
        with self.assertRaises(ValueError):
            self.svc.confirm_occurrence(sid, "2026-03-09")


class ExceptionTest(ServiceTestCase):
    def test_cancel_exception(self):
        sid = self.daily()
        self.svc.add_exception(sid, date="2026-03-03", action="cancel")
        occs = self.svc.list_occurrences(sid, "2026-03-02", "2026-03-04")
        by_date = {o["local_date"]: o for o in occs}
        cancelled = by_date["2026-03-03"]
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(cancelled["utc"])
        self.assertIn("cancelled by exception", cancelled["skip_reason"])
        self.assertEqual(cancelled["exception"], {"action": "cancel", "override_time": None})
        self.assertEqual(by_date["2026-03-02"]["status"], "scheduled")
        self.assertEqual(by_date["2026-03-04"]["status"], "scheduled")

    def test_move_exception_overrides_time(self):
        sid = self.daily()
        self.svc.add_exception(sid, date="2026-03-03", action="move", override_time="18:30")
        (occ,) = self.svc.list_occurrences(sid, "2026-03-03", "2026-03-03")
        self.assertEqual(occ["intended_local"], "2026-03-03T09:00")   # rule's time kept
        self.assertEqual(occ["effective_local"], "2026-03-03T18:30")  # override applied
        self.assertEqual(occ["utc"], "2026-03-03T23:30:00+00:00")     # 18:30 EST
        self.assertEqual(occ["exception"], {"action": "move", "override_time": "18:30"})

    def test_exception_upsert_replaces(self):
        sid = self.daily()
        self.svc.add_exception(sid, date="2026-03-03", action="cancel")
        self.svc.add_exception(sid, date="2026-03-03", action="move", override_time="20:00")
        (occ,) = self.svc.list_occurrences(sid, "2026-03-03", "2026-03-03")
        self.assertEqual(occ["status"], "scheduled")
        self.assertEqual(occ["effective_local"], "2026-03-03T20:00")
        self.assertEqual(len(self.svc.get_series(sid)["exceptions"]), 1)

    def test_exception_does_not_rewrite_confirmed_instance(self):
        sid = self.daily()
        self.svc.confirm_occurrence(sid, "2026-03-06")
        self.svc.add_exception(sid, date="2026-03-06", action="cancel")
        (occ,) = self.svc.list_occurrences(sid, "2026-03-06", "2026-03-06")
        # confirmed evidence is immutable
        self.assertEqual(occ["status"], "scheduled")
        self.assertEqual(occ["utc"], "2026-03-06T14:00:00+00:00")
        self.assertTrue(occ["confirmed"])

    def test_move_requires_time_and_valid_action(self):
        sid = self.daily()
        with self.assertRaises(ValueError):
            self.svc.add_exception(sid, date="2026-03-03", action="move")
        with self.assertRaises(ValueError):
            self.svc.add_exception(sid, date="2026-03-03", action="explode")


if __name__ == "__main__":
    unittest.main()
