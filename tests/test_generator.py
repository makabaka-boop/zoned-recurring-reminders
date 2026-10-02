"""Reminder generator: 15-minute lead, idempotency, DST, cross-day, restarts."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from tests.pinned_tz import ZONES
from recurring_reminders import ManualClock, Service

UTC = timezone.utc


class GeneratorTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = f"{self.tmp.name}/test.db"
        self.clock = ManualClock(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
        self.svc = Service(self.db, clock=self.clock, tz_resolver=ZONES.__getitem__)

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def daily(self, time_of_day="09:00", start="2026-01-01", **kw):
        defaults = dict(
            name="s", tz="Test/USlike", freq="daily",
            time_of_day=time_of_day, start_date=start,
        )
        defaults.update(kw)
        return self.svc.create_series(**defaults)

    def reopen(self):
        """Simulate a process restart: new Service on the same database."""
        self.svc.close()
        self.svc = Service(self.db, clock=self.clock, tz_resolver=ZONES.__getitem__)


class LeadBoundaryTest(GeneratorTestCase):
    def test_reminder_written_exactly_15_minutes_before(self):
        self.daily()  # 09:00 EST = 14:00 UTC, due 13:45 UTC
        self.clock.set(datetime(2026, 1, 1, 13, 44, 59, tzinfo=UTC))
        self.assertEqual(self.svc.tick()["inserted"], 0)
        self.clock.set(datetime(2026, 1, 1, 13, 45, 0, tzinfo=UTC))
        result = self.svc.tick()
        self.assertEqual(result["inserted"], 1)
        rem = result["reminders"][0]
        self.assertEqual(rem["local_date"], "2026-01-01")
        self.assertEqual(rem["due_utc"], "2026-01-01T13:45:00+00:00")
        self.assertEqual(rem["event_utc"], "2026-01-01T14:00:00+00:00")
        self.assertEqual(rem["version"], 1)

    def test_repeated_ticks_are_idempotent(self):
        self.daily()
        self.clock.set(datetime(2026, 1, 1, 13, 45, tzinfo=UTC))
        self.assertEqual(self.svc.tick()["inserted"], 1)
        for _ in range(3):
            self.assertEqual(self.svc.tick()["inserted"], 0)
        self.assertEqual(len(self.svc.list_reminders()), 1)
        # every tick is audited
        self.assertEqual(len(self.svc.generator_runs()), 4)


class RestartAndJumpTest(GeneratorTestCase):
    def test_restart_is_idempotent(self):
        self.daily()
        self.clock.set(datetime(2026, 1, 1, 13, 45, tzinfo=UTC))
        self.assertEqual(self.svc.tick()["inserted"], 1)

        self.reopen()
        self.assertEqual(self.svc.tick()["inserted"], 0)          # same instant
        self.clock.advance(timedelta(days=1))                     # next day's due
        self.assertEqual(self.svc.tick()["inserted"], 1)
        self.assertEqual(len(self.svc.list_reminders()), 2)

        self.reopen()
        self.assertEqual(self.svc.tick()["inserted"], 0)          # still no dupes
        self.assertEqual(len(self.svc.list_reminders()), 2)

    def test_clock_jump_emits_all_overdue_reminders_exactly_once(self):
        self.daily()  # 09:00 EST = 14:00 UTC
        self.clock.set(datetime(2026, 1, 6, 0, 0, tzinfo=UTC))    # 5-day jump
        result = self.svc.tick()
        # Jan 1..5 at 14:00 UTC are all <= horizon (Jan 6 00:15 UTC); Jan 6 is not
        self.assertEqual(result["inserted"], 5)
        dates = sorted(r["local_date"] for r in result["reminders"])
        self.assertEqual(dates, [f"2026-01-0{d}" for d in range(1, 6)])

        self.reopen()
        self.assertEqual(self.svc.tick()["inserted"], 0)          # nothing re-emitted
        self.clock.set(datetime(2026, 1, 6, 13, 45, tzinfo=UTC))
        self.assertEqual(self.svc.tick()["inserted"], 1)          # Jan 6 on time
        self.assertEqual(len(self.svc.list_reminders()), 6)


class DstTest(GeneratorTestCase):
    def test_gap_skip_policy_records_reason_and_sends_no_reminder(self):
        sid = self.daily(time_of_day="02:30", start="2026-03-07", on_gap="skip")
        self.clock.set(datetime(2026, 3, 10, 12, 0, tzinfo=UTC))  # after the gap day
        self.svc.tick()
        by_date = {r["local_date"]: r for r in self.svc.list_reminders(sid)}
        self.assertIn("2026-03-07", by_date)
        self.assertIn("2026-03-09", by_date)
        self.assertNotIn("2026-03-08", by_date)  # skipped: no reminder

        (occ,) = self.svc.list_occurrences(sid, "2026-03-08", "2026-03-08")
        self.assertEqual(occ["status"], "skipped")
        self.assertEqual(occ["kind"], "gap-skipped")
        self.assertIsNone(occ["utc"])
        self.assertIn("does not exist", occ["skip_reason"])
        self.assertEqual(occ["version"], 1)

    def test_gap_shift_policy_reminds_at_shifted_instant(self):
        sid = self.daily(time_of_day="02:30", start="2026-03-08", on_gap="shift")
        self.clock.set(datetime(2026, 3, 8, 7, 14, 59, tzinfo=UTC))
        self.assertEqual(self.svc.tick()["inserted"], 0)
        self.clock.set(datetime(2026, 3, 8, 7, 15, 0, tzinfo=UTC))  # 03:30 EDT - 15m
        result = self.svc.tick()
        self.assertEqual(result["inserted"], 1)
        rem = result["reminders"][0]
        self.assertEqual(rem["event_utc"], "2026-03-08T07:30:00+00:00")
        (occ,) = self.svc.list_occurrences(sid, "2026-03-08", "2026-03-08")
        self.assertEqual(occ["kind"], "gap-shifted")
        self.assertEqual(occ["actual_local"], "2026-03-08T03:30")
        self.assertIn("moved to next valid local time", occ["skip_reason"])

    def test_ambiguous_first_vs_second(self):
        first_sid = self.daily(time_of_day="01:30", start="2026-11-01",
                               until="2026-11-01", on_ambiguous="first")
        second_sid = self.daily(time_of_day="01:30", start="2026-11-01",
                                until="2026-11-01", on_ambiguous="second")
        self.clock.set(datetime(2026, 11, 1, 5, 15, tzinfo=UTC))  # first occurrence - 15m
        result = self.svc.tick()
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["reminders"][0]["series_id"], first_sid)
        self.assertEqual(result["reminders"][0]["event_utc"], "2026-11-01T05:30:00+00:00")

        self.clock.set(datetime(2026, 11, 1, 6, 15, tzinfo=UTC))  # second occurrence - 15m
        result = self.svc.tick()
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["reminders"][0]["series_id"], second_sid)
        self.assertEqual(result["reminders"][0]["event_utc"], "2026-11-01T06:30:00+00:00")
        self.assertEqual(len(self.svc.list_reminders()), 2)

    def test_no_fixed_offset_across_dst_boundary(self):
        sid = self.daily(time_of_day="09:00", start="2026-03-07", until="2026-03-09")
        occs = {o["local_date"]: o for o in self.svc.list_occurrences(sid, "2026-03-07", "2026-03-09")}
        self.assertEqual(occs["2026-03-07"]["utc"], "2026-03-07T14:00:00+00:00")  # EST
        self.assertEqual(occs["2026-03-09"]["utc"], "2026-03-09T13:00:00+00:00")  # EDT


class CrossDayTest(GeneratorTestCase):
    def test_reminder_due_on_previous_utc_day(self):
        sid = self.svc.create_series(
            name="midnight-snack", tz="Test/East", freq="daily",
            time_of_day="00:10", start_date="2026-04-10",
        )
        # 2026-04-10 00:10 +10:00 == 2026-04-09T14:10Z; due 2026-04-09T13:55Z
        self.clock.set(datetime(2026, 4, 9, 13, 54, tzinfo=UTC))
        self.assertEqual(self.svc.tick()["inserted"], 0)
        self.clock.set(datetime(2026, 4, 9, 13, 55, tzinfo=UTC))
        result = self.svc.tick()
        self.assertEqual(result["inserted"], 1)
        rem = result["reminders"][0]
        self.assertEqual(rem["local_date"], "2026-04-10")            # local "tomorrow"
        self.assertEqual(rem["due_utc"], "2026-04-09T13:55:00+00:00")  # UTC "yesterday"
        self.assertEqual(rem["event_utc"], "2026-04-09T14:10:00+00:00")
        self.assertEqual(rem["created_at"], "2026-04-09T13:55:00+00:00")


class InterleavingTest(GeneratorTestCase):
    def test_series_modification_between_ticks(self):
        sid = self.daily(time_of_day="09:00", start="2026-04-10")
        # 09:00 EDT = 13:00 UTC, due 12:45 UTC
        self.clock.set(datetime(2026, 4, 10, 12, 45, tzinfo=UTC))
        self.assertEqual(self.svc.tick()["inserted"], 1)  # Apr 10, v1

        v2 = self.svc.modify_series(sid, effective_from="2026-04-12", time_of_day="10:00")
        self.assertEqual(v2, 2)

        self.clock.set(datetime(2026, 4, 11, 12, 45, tzinfo=UTC))
        result = self.svc.tick()
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["reminders"][0]["local_date"], "2026-04-11")
        self.assertEqual(result["reminders"][0]["version"], 1)  # before effective date

        # Apr 12 at 10:00 EDT = 14:00 UTC, due 13:45 UTC
        self.clock.set(datetime(2026, 4, 12, 13, 45, tzinfo=UTC))
        result = self.svc.tick()
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["reminders"][0]["local_date"], "2026-04-12")
        self.assertEqual(result["reminders"][0]["version"], 2)
        self.assertEqual(result["reminders"][0]["event_utc"], "2026-04-12T14:00:00+00:00")

        # replaying the whole window after a restart adds nothing
        self.reopen()
        self.assertEqual(self.svc.tick()["inserted"], 0)
        self.assertEqual(len(self.svc.list_reminders(sid)), 3)

    def test_confirmed_instance_survives_later_modification(self):
        sid = self.daily(time_of_day="09:00", start="2026-04-10")
        self.clock.set(datetime(2026, 4, 10, 12, 45, tzinfo=UTC))
        self.svc.tick()  # confirms Apr 10 under v1
        self.svc.modify_series(sid, effective_from="2026-04-10", time_of_day="23:00")
        self.reopen()
        self.clock.set(datetime(2026, 4, 10, 23, 0, tzinfo=UTC))
        self.assertEqual(self.svc.tick()["inserted"], 0)  # Apr 10 already reminded
        (occ,) = self.svc.list_occurrences(sid, "2026-04-10", "2026-04-10")
        self.assertEqual(occ["version"], 1)
        self.assertEqual(occ["intended_local"], "2026-04-10T09:00")
        self.assertTrue(occ["confirmed"])

    def test_exceptions_interleaved_with_ticks(self):
        sid = self.daily(time_of_day="09:00", start="2026-05-01")
        # May 1 reminder
        self.clock.set(datetime(2026, 5, 1, 12, 45, tzinfo=UTC))
        self.assertEqual(self.svc.tick()["inserted"], 1)

        # cancel May 2, move May 3 to 18:30 (22:30 UTC, due 22:15 UTC)
        self.svc.add_exception(sid, date="2026-05-02", action="cancel")
        self.svc.add_exception(sid, date="2026-05-03", action="move", override_time="18:30")

        self.clock.set(datetime(2026, 5, 3, 0, 0, tzinfo=UTC))
        result = self.svc.tick()
        self.assertEqual(result["inserted"], 0)  # May 2 cancelled, May 3 not due yet

        self.clock.set(datetime(2026, 5, 3, 22, 15, tzinfo=UTC))
        result = self.svc.tick()
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["reminders"][0]["local_date"], "2026-05-03")
        self.assertEqual(result["reminders"][0]["event_utc"], "2026-05-03T22:30:00+00:00")

        # interleave a series edit, restart, and re-run: still consistent
        self.svc.modify_series(sid, effective_from="2026-05-04", time_of_day="08:00")
        self.reopen()
        self.assertEqual(self.svc.tick()["inserted"], 0)
        # May 4 at 08:00 EDT = 12:00 UTC, due 11:45 UTC
        self.clock.set(datetime(2026, 5, 4, 11, 45, tzinfo=UTC))
        result = self.svc.tick()
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["reminders"][0]["version"], 2)

        reminders = self.svc.list_reminders(sid)
        self.assertEqual(len(reminders), 3)
        self.assertEqual(
            sorted(r["local_date"] for r in reminders),
            ["2026-05-01", "2026-05-03", "2026-05-04"],
        )

    def test_exception_added_after_materialization_but_before_due(self):
        sid = self.daily(time_of_day="09:00", start="2026-06-01")
        # run the generator while June 2 is already inside the horizon window
        self.clock.set(datetime(2026, 6, 1, 12, 50, tzinfo=UTC))  # June 1 reminded
        self.assertEqual(self.svc.tick()["inserted"], 1)
        # cancel June 2 before its reminder is due
        self.svc.add_exception(sid, date="2026-06-02", action="cancel")
        self.clock.set(datetime(2026, 6, 3, 0, 0, tzinfo=UTC))
        self.assertEqual(self.svc.tick()["inserted"], 0)
        self.assertEqual(
            [r["local_date"] for r in self.svc.list_reminders(sid)], ["2026-06-01"]
        )
        (occ,) = self.svc.list_occurrences(sid, "2026-06-02", "2026-06-02")
        self.assertEqual(occ["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
