"""Recurring-event reminder service.

Semantics
---------
* An *instance* of a series is identified by (series_id, local_date).
* Instances are computed lazily from the rule version whose effective_from
  is the greatest one <= the instance's local date.  Editing a series only
  inserts a new version row, so instances before the effective date keep
  using the old version automatically.
* An instance becomes *confirmed* when it is explicitly confirmed via the
  API or when its reminder is generated.  A confirmed instance is frozen
  evidence: version, intended/effective/actual local time, UTC instant,
  status and skip reason are stored and never rewritten, even if the
  series is edited or exceptions are added later.
* Exceptions (cancel / move) apply to instances that are not yet
  confirmed; they are evaluated at computation time.
* The generator is idempotent: reminders are UNIQUE per
  (series, local_date) and inserted with INSERT OR IGNORE inside a single
  transaction, so restarts, repeated clock advances and interleaved edits
  can neither duplicate nor lose reminders.  If the clock jumps forward,
  all overdue reminders are emitted (late) rather than dropped.
"""
from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Dict, Iterator, List, Optional
from zoneinfo import ZoneInfo

from . import recurrence, store
from .clock import SystemClock
from .tzresolve import resolve

UTC = timezone.utc
DEFAULT_LEAD = timedelta(minutes=15)

FREQS = ("daily", "weekly")
GAP_POLICIES = ("skip", "shift")
AMBIGUOUS_POLICIES = ("first", "second")
EXCEPTION_ACTIONS = ("cancel", "move")

RULE_FIELDS = (
    "tz", "freq", "interval", "byweekday", "time_of_day",
    "start_date", "until", "on_gap", "on_ambiguous",
)

_TOD_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# ---------- parsing / formatting helpers ----------

def parse_date(s) -> date:
    if isinstance(s, date):
        return s
    return date.fromisoformat(s)


def parse_tod(s) -> time:
    if isinstance(s, time):
        return s.replace(second=0, microsecond=0)
    m = _TOD_RE.match(s)
    if not m:
        raise ValueError(f"invalid time_of_day {s!r}; expected HH:MM")
    return time(int(m.group(1)), int(m.group(2)))


def parse_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def fmt_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def fmt_local(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def fmt_tod(t: time) -> str:
    return t.strftime("%H:%M")


@contextmanager
def _tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


@dataclass(frozen=True)
class VersionRule:
    series_id: int
    version: int
    effective_from: date
    tz: str
    freq: str
    interval: int
    byweekday: tuple
    time_of_day: time
    start_date: date
    until: Optional[date]
    on_gap: str
    on_ambiguous: str


def _row_to_rule(row) -> VersionRule:
    return VersionRule(
        series_id=row["series_id"],
        version=row["version"],
        effective_from=date.fromisoformat(row["effective_from"]),
        tz=row["tz"],
        freq=row["freq"],
        interval=row["interval"],
        byweekday=tuple(json.loads(row["byweekday"])),
        time_of_day=parse_tod(row["time_of_day"]),
        start_date=date.fromisoformat(row["start_date"]),
        until=date.fromisoformat(row["until"]) if row["until"] else None,
        on_gap=row["on_gap"],
        on_ambiguous=row["on_ambiguous"],
    )


class Service:
    def __init__(
        self,
        db_path: str,
        *,
        clock=None,
        tz_resolver: Optional[Callable[[str], object]] = None,
        lead: timedelta = DEFAULT_LEAD,
    ):
        self.conn = store.connect(db_path)
        self.clock = clock or SystemClock()
        self.tz_resolver = tz_resolver or ZoneInfo
        self.lead = lead
        self._lock = threading.RLock()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ utils

    def _tz(self, key: str):
        try:
            return self.tz_resolver(key)
        except Exception as exc:
            raise ValueError(f"unknown timezone: {key!r}") from exc

    def _now(self) -> datetime:
        now = self.clock.now()
        if now.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now.astimezone(UTC).replace(microsecond=0)

    @staticmethod
    def _validate_fields(f: dict) -> None:
        if f["freq"] not in FREQS:
            raise ValueError(f"freq must be one of {FREQS}")
        if not isinstance(f["interval"], int) or f["interval"] < 1:
            raise ValueError("interval must be a positive integer")
        if f["on_gap"] not in GAP_POLICIES:
            raise ValueError(f"on_gap must be one of {GAP_POLICIES}")
        if f["on_ambiguous"] not in AMBIGUOUS_POLICIES:
            raise ValueError(f"on_ambiguous must be one of {AMBIGUOUS_POLICIES}")
        if not isinstance(f["time_of_day"], time):
            raise ValueError("time_of_day must be a time")
        if f["freq"] == "weekly":
            if not f["byweekday"]:
                raise ValueError("weekly series needs a non-empty byweekday")
            if any(d < 0 or d > 6 for d in f["byweekday"]):
                raise ValueError("byweekday entries must be in 0..6 (Monday=0)")
        if f["until"] is not None and f["until"] < f["start_date"]:
            raise ValueError("until must not be before start_date")

    def _coerce_field(self, key: str, value):
        if key == "time_of_day":
            return parse_tod(value)
        if key in ("start_date", "until"):
            if value is None and key == "until":
                return None
            return parse_date(value)
        if key == "byweekday":
            return tuple(sorted(int(x) for x in value)) if value else ()
        if key == "interval":
            return int(value)
        return value

    # ------------------------------------------------------------- series CRUD

    def create_series(
        self,
        *,
        name: str,
        tz: str,
        freq: str,
        time_of_day,
        start_date,
        interval: int = 1,
        byweekday=None,
        until=None,
        on_gap: str = "skip",
        on_ambiguous: str = "first",
    ) -> int:
        self._tz(tz)  # validates the key early
        fields = {
            "tz": tz,
            "freq": freq,
            "interval": int(interval),
            "byweekday": tuple(sorted(byweekday)) if byweekday else (),
            "time_of_day": parse_tod(time_of_day),
            "start_date": parse_date(start_date),
            "until": parse_date(until) if until else None,
            "on_gap": on_gap,
            "on_ambiguous": on_ambiguous,
        }
        if freq == "weekly" and not fields["byweekday"]:
            fields["byweekday"] = (fields["start_date"].weekday(),)
        self._validate_fields(fields)

        with self._lock, _tx(self.conn):
            cur = self.conn.execute(
                "INSERT INTO series(name, created_at) VALUES (?, ?)",
                (name, fmt_utc(self._now())),
            )
            series_id = cur.lastrowid
            self._insert_version(series_id, 1, fields["start_date"], fields)
        return series_id

    def modify_series(self, series_id: int, *, effective_from, **changes) -> int:
        """Create a new rule version applying to local dates >= effective_from.

        Unspecified fields are inherited from the latest version.  Instances
        before effective_from (and all confirmed instances) are unaffected.
        Returns the new version number.
        """
        eff = parse_date(effective_from)
        with self._lock, _tx(self.conn):
            versions = self._versions(series_id)
            if not versions:
                raise ValueError(f"unknown series {series_id}")
            latest = versions[-1]
            fields = {k: getattr(latest, k) for k in RULE_FIELDS}
            for key, value in changes.items():
                if key not in RULE_FIELDS:
                    raise ValueError(f"cannot modify field {key!r}")
                fields[key] = self._coerce_field(key, value)
            if fields["freq"] == "weekly" and not fields["byweekday"]:
                fields["byweekday"] = (fields["start_date"].weekday(),)
            self._tz(fields["tz"])
            self._validate_fields(fields)
            new_version = latest.version + 1
            self._insert_version(series_id, new_version, eff, fields)
            return new_version

    def _insert_version(self, series_id: int, version: int, effective_from: date, f: dict) -> None:
        self.conn.execute(
            "INSERT INTO series_versions(series_id, version, effective_from, tz, freq,"
            " interval, byweekday, time_of_day, start_date, until, on_gap,"
            " on_ambiguous, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                series_id, version, effective_from.isoformat(), f["tz"], f["freq"],
                f["interval"], json.dumps(list(f["byweekday"])), fmt_tod(f["time_of_day"]),
                f["start_date"].isoformat(),
                f["until"].isoformat() if f["until"] else None,
                f["on_gap"], f["on_ambiguous"], fmt_utc(self._now()),
            ),
        )

    def add_exception(self, series_id: int, *, date, action: str, override_time=None) -> int:
        """Upsert a single-day exception: action='cancel' or 'move' (with override_time).

        Exceptions apply to instances that are not yet confirmed; confirmed
        instances keep their stored evidence.
        """
        if action not in EXCEPTION_ACTIONS:
            raise ValueError(f"action must be one of {EXCEPTION_ACTIONS}")
        day = parse_date(date)
        odt = parse_tod(override_time) if override_time else None
        if action == "move" and odt is None:
            raise ValueError("action='move' requires override_time")
        with self._lock, _tx(self.conn):
            self._require_series(series_id)
            cur = self.conn.execute(
                "INSERT OR REPLACE INTO exceptions(series_id, ex_date, action,"
                " override_time, created_at) VALUES (?,?,?,?,?)",
                (series_id, day.isoformat(), action,
                 fmt_tod(odt) if odt else None, fmt_utc(self._now())),
            )
            return cur.lastrowid

    # --------------------------------------------------------------- internals

    def _require_series(self, series_id: int) -> None:
        row = self.conn.execute("SELECT id FROM series WHERE id=?", (series_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown series {series_id}")

    def _versions(self, series_id: int) -> List[VersionRule]:
        rows = self.conn.execute(
            "SELECT * FROM series_versions WHERE series_id=?"
            " ORDER BY effective_from, version",
            (series_id,),
        ).fetchall()
        return [_row_to_rule(r) for r in rows]

    @staticmethod
    def _version_for(versions: List[VersionRule], day: date) -> Optional[VersionRule]:
        chosen = None
        for v in versions:  # sorted by (effective_from, version)
            if v.effective_from <= day:
                chosen = v
            else:
                break
        return chosen

    def _exceptions(self, series_id: int) -> Dict[date, dict]:
        rows = self.conn.execute(
            "SELECT * FROM exceptions WHERE series_id=?", (series_id,)
        ).fetchall()
        return {
            date.fromisoformat(r["ex_date"]): {
                "id": r["id"],
                "action": r["action"],
                "override_time": parse_tod(r["override_time"]) if r["override_time"] else None,
            }
            for r in rows
        }

    def _compute_occurrence(self, rule: VersionRule, day: date, ex: Optional[dict]) -> Optional[dict]:
        """Live-compute the (unconfirmed) instance of `rule` on `day`."""
        if not recurrence.matches(rule, day):
            return None
        intended = datetime.combine(day, rule.time_of_day)
        effective = intended
        if ex and ex["action"] == "move" and ex["override_time"]:
            effective = datetime.combine(day, ex["override_time"])
        base = {
            "series_id": rule.series_id,
            "local_date": day,
            "version": rule.version,
            "tz": rule.tz,
            "intended_local": intended,
            "effective_local": effective,
            "exception": (
                {"action": ex["action"],
                 "override_time": fmt_tod(ex["override_time"]) if ex["override_time"] else None}
                if ex else None
            ),
            "confirmed": False,
        }
        if ex and ex["action"] == "cancel":
            return {**base, "status": "cancelled", "kind": "cancelled",
                    "actual_local": None, "utc_start": None,
                    "skip_reason": f"cancelled by exception for {day.isoformat()}"}
        res = resolve(effective, self._tz(rule.tz), rule.on_gap, rule.on_ambiguous)
        if res.utc is None:
            return {**base, "status": "skipped", "kind": res.kind,
                    "actual_local": None, "utc_start": None, "skip_reason": res.reason}
        return {**base, "status": "scheduled", "kind": res.kind,
                "actual_local": res.actual_local, "utc_start": res.utc,
                "skip_reason": res.reason}

    def _row_to_occurrence(self, row) -> dict:
        return {
            "series_id": row["series_id"],
            "local_date": date.fromisoformat(row["local_date"]),
            "version": row["version"],
            "tz": row["tz"],
            "intended_local": datetime.fromisoformat(row["intended_local"]),
            "effective_local": datetime.fromisoformat(row["effective_local"]),
            "actual_local": datetime.fromisoformat(row["actual_local"]) if row["actual_local"] else None,
            "utc_start": parse_utc(row["utc_start"]) if row["utc_start"] else None,
            "status": row["status"],
            "kind": row["kind"],
            "skip_reason": row["skip_reason"],
            "exception": None,  # evidence of the effect is in status/kind/reason
            "confirmed": True,
        }

    def _occurrence_for(self, series_id: int, day: date, rule: VersionRule, ex) -> Optional[dict]:
        """Confirmed evidence if it exists, otherwise a live computation."""
        row = self.conn.execute(
            "SELECT * FROM occurrences WHERE series_id=? AND local_date=?",
            (series_id, day.isoformat()),
        ).fetchone()
        if row is not None:
            return self._row_to_occurrence(row)
        return self._compute_occurrence(rule, day, ex)

    def _insert_occurrence(self, occ: dict, now: datetime) -> None:
        """Freeze an instance as confirmed evidence (idempotent)."""
        self.conn.execute(
            "INSERT OR IGNORE INTO occurrences(series_id, local_date, version, tz,"
            " intended_local, effective_local, actual_local, utc_start, status,"
            " kind, skip_reason, confirmed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                occ["series_id"], occ["local_date"].isoformat(), occ["version"], occ["tz"],
                fmt_local(occ["intended_local"]), fmt_local(occ["effective_local"]),
                fmt_local(occ["actual_local"]) if occ["actual_local"] else None,
                fmt_utc(occ["utc_start"]) if occ["utc_start"] else None,
                occ["status"], occ["kind"], occ["skip_reason"], fmt_utc(now),
            ),
        )

    # ------------------------------------------------------------------- query

    def get_series(self, series_id: int) -> dict:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM series WHERE id=?", (series_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown series {series_id}")
            versions = self._versions(series_id)
            exceptions = self._exceptions(series_id)
            return {
                "id": row["id"],
                "name": row["name"],
                "versions": [
                    {
                        "version": v.version,
                        "effective_from": v.effective_from.isoformat(),
                        "tz": v.tz,
                        "freq": v.freq,
                        "interval": v.interval,
                        "byweekday": list(v.byweekday),
                        "time_of_day": fmt_tod(v.time_of_day),
                        "start_date": v.start_date.isoformat(),
                        "until": v.until.isoformat() if v.until else None,
                        "on_gap": v.on_gap,
                        "on_ambiguous": v.on_ambiguous,
                    }
                    for v in versions
                ],
                "exceptions": [
                    {"date": d.isoformat(), "action": e["action"],
                     "override_time": fmt_tod(e["override_time"]) if e["override_time"] else None}
                    for d, e in sorted(exceptions.items())
                ],
            }

    @staticmethod
    def _serialize_occurrence(occ: dict) -> dict:
        return {
            "series_id": occ["series_id"],
            "local_date": occ["local_date"].isoformat(),
            "tz": occ["tz"],
            "version": occ["version"],
            "intended_local": fmt_local(occ["intended_local"]),
            "effective_local": fmt_local(occ["effective_local"]),
            "actual_local": fmt_local(occ["actual_local"]) if occ["actual_local"] else None,
            "utc": fmt_utc(occ["utc_start"]) if occ["utc_start"] else None,
            "status": occ["status"],
            "kind": occ["kind"],
            "skip_reason": occ["skip_reason"],
            "exception": occ["exception"],
            "confirmed": occ["confirmed"],
        }

    def list_occurrences(self, series_id: int, from_date, to_date) -> List[dict]:
        """Occurrences in [from_date, to_date] (local dates).

        Confirmed instances are returned as stored evidence; unconfirmed
        ones are computed live from the applicable rule version and any
        exceptions.
        """
        frm, to = parse_date(from_date), parse_date(to_date)
        if to < frm:
            raise ValueError("to_date must not be before from_date")
        with self._lock:
            versions = self._versions(series_id)
            if not versions:
                raise ValueError(f"unknown series {series_id}")
            exceptions = self._exceptions(series_id)
            out = []
            day = frm
            while day <= to:
                rule = self._version_for(versions, day)
                if rule is not None:
                    occ = self._occurrence_for(series_id, day, rule, exceptions.get(day))
                    if occ is not None:
                        out.append(self._serialize_occurrence(occ))
                day += timedelta(days=1)
            return out

    def confirm_occurrence(self, series_id: int, date) -> dict:
        """Freeze the instance for `date` as confirmed evidence (idempotent)."""
        day = parse_date(date)
        with self._lock, _tx(self.conn):
            versions = self._versions(series_id)
            if not versions:
                raise ValueError(f"unknown series {series_id}")
            rule = self._version_for(versions, day)
            if rule is None:
                raise ValueError(f"no rule version covers {day.isoformat()}")
            occ = self._occurrence_for(series_id, day, rule, self._exceptions(series_id).get(day))
            if occ is None:
                raise ValueError(f"series {series_id} has no occurrence on {day.isoformat()}")
            if not occ["confirmed"]:
                self._insert_occurrence(occ, self._now())
            row = self.conn.execute(
                "SELECT * FROM occurrences WHERE series_id=? AND local_date=?",
                (series_id, day.isoformat()),
            ).fetchone()
            return self._serialize_occurrence(self._row_to_occurrence(row))

    def list_reminders(self, series_id: Optional[int] = None) -> List[dict]:
        with self._lock:
            if series_id is None:
                rows = self.conn.execute(
                    "SELECT * FROM reminders ORDER BY event_utc, id"
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM reminders WHERE series_id=? ORDER BY event_utc, id",
                    (series_id,),
                ).fetchall()
            return [dict(r) for r in rows]

    def generator_runs(self) -> List[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM generator_runs ORDER BY id"
            ).fetchall()]

    # --------------------------------------------------------------- generator

    def tick(self) -> dict:
        """Run the reminder generator once against the injected clock.

        Emits a unique reminder for every scheduled instance whose event
        starts within [anything, now + lead] and that has no reminder yet.
        Safe to call repeatedly, after restarts, and across clock jumps.
        """
        with self._lock:
            now = self._now()
            horizon = now + self.lead
            inserted = []
            with _tx(self.conn):
                series_ids = [r["id"] for r in self.conn.execute("SELECT id FROM series")]
                for sid in series_ids:
                    versions = self._versions(sid)
                    exceptions = self._exceptions(sid)
                    tz = self._tz(versions[-1].tz)
                    # Any instance with utc_start <= horizon has a local date
                    # at most one day after the horizon's local date
                    # (|utcoffset| < 24h for every real zone).
                    hi = horizon.astimezone(tz).date() + timedelta(days=1)
                    day = min(v.start_date for v in versions)
                    while day <= hi:
                        rule = self._version_for(versions, day)
                        if rule is not None:
                            occ = self._occurrence_for(sid, day, rule, exceptions.get(day))
                            if (occ is not None and occ["status"] == "scheduled"
                                    and occ["utc_start"] <= horizon):
                                rem = self._record_reminder(occ, now)
                                if rem is not None:
                                    inserted.append(rem)
                        day += timedelta(days=1)
                self.conn.execute(
                    "INSERT INTO generator_runs(run_at, horizon, inserted) VALUES (?,?,?)",
                    (fmt_utc(now), fmt_utc(horizon), len(inserted)),
                )
            return {
                "run_at": fmt_utc(now),
                "horizon": fmt_utc(horizon),
                "inserted": len(inserted),
                "reminders": inserted,
            }

    def _record_reminder(self, occ: dict, now: datetime) -> Optional[dict]:
        """Freeze evidence and insert the reminder; None if it already existed."""
        self._insert_occurrence(occ, now)
        occ_id = self.conn.execute(
            "SELECT id FROM occurrences WHERE series_id=? AND local_date=?",
            (occ["series_id"], occ["local_date"].isoformat()),
        ).fetchone()["id"]
        due = occ["utc_start"] - self.lead
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO reminders(series_id, local_date, occurrence_id,"
            " version, due_utc, event_utc, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                occ["series_id"], occ["local_date"].isoformat(), occ_id, occ["version"],
                fmt_utc(due), fmt_utc(occ["utc_start"]), fmt_utc(now),
            ),
        )
        if cur.rowcount == 0:
            return None
        return {
            "id": cur.lastrowid,
            "series_id": occ["series_id"],
            "local_date": occ["local_date"].isoformat(),
            "occurrence_id": occ_id,
            "version": occ["version"],
            "due_utc": fmt_utc(due),
            "event_utc": fmt_utc(occ["utc_start"]),
            "created_at": fmt_utc(now),
        }
