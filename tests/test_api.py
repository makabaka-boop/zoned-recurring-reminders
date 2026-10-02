"""End-to-end HTTP API test over a real socket (stdlib http.server)."""
import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone

from tests.pinned_tz import ZONES
from recurring_reminders import ManualClock, Service
from recurring_reminders.api import make_server

UTC = timezone.utc


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.clock = ManualClock(datetime(2026, 3, 1, 0, 0, tzinfo=UTC))
        cls.svc = Service(f"{cls.tmp.name}/api.db", clock=cls.clock,
                          tz_resolver=ZONES.__getitem__)
        cls.server = make_server(cls.svc, port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.svc.close()
        cls.tmp.cleanup()

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        payload = json.dumps(body) if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def test_full_flow(self):
        # create a daily 02:30 series with gap-shift policy
        status, series = self.request("POST", "/series", {
            "name": "night-job", "tz": "Test/USlike", "freq": "daily",
            "time_of_day": "02:30", "start_date": "2026-03-07", "on_gap": "shift",
        })
        self.assertEqual(status, 201)
        sid = series["id"]
        self.assertEqual(series["versions"][0]["version"], 1)

        # occurrences around the spring-forward gap carry all API fields
        status, data = self.request(
            "GET", f"/series/{sid}/occurrences?from=2026-03-07&to=2026-03-09")
        self.assertEqual(status, 200)
        occs = {o["local_date"]: o for o in data["occurrences"]}
        gap = occs["2026-03-08"]
        self.assertEqual(gap["kind"], "gap-shifted")
        self.assertEqual(gap["actual_local"], "2026-03-08T03:30")
        self.assertEqual(gap["utc"], "2026-03-08T07:30:00+00:00")
        self.assertIn("does not exist", gap["skip_reason"])
        self.assertEqual(gap["version"], 1)

        # modify the series effective 2026-03-09
        status, mod = self.request("POST", f"/series/{sid}/versions", {
            "effective_from": "2026-03-09", "time_of_day": "04:00",
        })
        self.assertEqual(status, 201)
        self.assertEqual(mod["version"], 2)

        # add an exception for 2026-03-10
        status, _ = self.request("POST", f"/series/{sid}/exceptions", {
            "date": "2026-03-10", "action": "cancel",
        })
        self.assertEqual(status, 201)

        # drive the controllable clock through the API and generate reminders
        status, tick1 = self.request("POST", "/tick", {"now": "2026-03-08T07:15:00+00:00"})
        self.assertEqual(status, 200)
        self.assertEqual(tick1["inserted"], 2)  # Mar 7 (late) + Mar 8 (shifted)

        # idempotent replay at the same instant
        status, tick2 = self.request("POST", "/tick", {"now": "2026-03-08T07:15:00+00:00"})
        self.assertEqual(tick2["inserted"], 0)

        # Mar 9 uses v2 (04:00 EDT = 08:00 UTC, due 07:45 UTC); Mar 10 cancelled
        status, tick3 = self.request("POST", "/tick", {"now": "2026-03-11T00:00:00+00:00"})
        self.assertEqual(tick3["inserted"], 1)

        status, rems = self.request("GET", f"/reminders?series_id={sid}")
        self.assertEqual(status, 200)
        by_date = {r["local_date"]: r for r in rems["reminders"]}
        self.assertEqual(sorted(by_date), ["2026-03-07", "2026-03-08", "2026-03-09"])
        self.assertEqual(by_date["2026-03-09"]["version"], 2)
        self.assertEqual(by_date["2026-03-09"]["event_utc"], "2026-03-09T08:00:00+00:00")
        self.assertNotIn("2026-03-10", by_date)  # cancelled

        # series view exposes rule versions and exceptions as evidence
        status, series = self.request("GET", f"/series/{sid}")
        self.assertEqual([v["version"] for v in series["versions"]], [1, 2])
        self.assertEqual(series["exceptions"][0]["action"], "cancel")

    def test_errors(self):
        status, err = self.request("POST", "/series", {
            "name": "bad", "tz": "Test/USlike", "freq": "daily",
            "time_of_day": "99:00", "start_date": "2026-01-01",
        })
        self.assertEqual(status, 400)
        self.assertIn("error", err)

        status, err = self.request("GET", "/nope")
        self.assertEqual(status, 404)

        status, err = self.request("GET", "/series/999")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
