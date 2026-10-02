"""Resolution of local wall-clock times to UTC instants.

Every local time is resolved through the tz database (a zoneinfo-style
tzinfo), never through a fixed UTC offset, so DST gaps and repeated times
are handled per the series' configured policy:

  * gap (nonexistent local time, e.g. spring forward):
      on_gap="skip"  -> the instance is dropped, with a recorded reason
      on_gap="shift" -> the instance moves to the next valid local moment
  * ambiguous (repeated local time, e.g. fall back):
      on_ambiguous="first"  -> the first  (earlier UTC) occurrence
      on_ambiguous="second" -> the second (later  UTC) occurrence

Classification is done by round-tripping through UTC, so it does not
depend on any particular fold/offset convention of the tz database.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import Optional

UTC = timezone.utc

NORMAL = "normal"
AMBIGUOUS = "ambiguous"
GAP_SHIFTED = "gap-shifted"
GAP_SKIPPED = "gap-skipped"


@dataclass(frozen=True)
class Resolution:
    kind: str                          # NORMAL | AMBIGUOUS | GAP_SHIFTED | GAP_SKIPPED
    utc: Optional[datetime]            # chosen UTC instant; None when skipped
    actual_local: Optional[datetime]   # aware local time actually used; None when skipped
    reason: Optional[str]              # human-readable note for skip/shift


def _tzkey(tz: tzinfo) -> str:
    return getattr(tz, "key", None) or str(tz)


def _roundtrips(naive: datetime, aware: datetime) -> bool:
    """True if `aware` maps to UTC and back to exactly `naive` local time."""
    back = aware.astimezone(UTC).astimezone(aware.tzinfo)
    return back.replace(tzinfo=None) == naive


def resolve(naive: datetime, tz: tzinfo, on_gap: str, on_ambiguous: str) -> Resolution:
    """Resolve a naive local datetime to a UTC instant under the given policies."""
    if naive.tzinfo is not None:
        raise ValueError("resolve() expects a naive datetime")
    key = _tzkey(tz)

    f0 = naive.replace(tzinfo=tz, fold=0)
    f1 = naive.replace(tzinfo=tz, fold=1)

    if f0.utcoffset() == f1.utcoffset():
        utc = f0.astimezone(UTC)
        return Resolution(NORMAL, utc, utc.astimezone(tz), None)

    if _roundtrips(naive, f0) and _roundtrips(naive, f1):
        # Repeated local time: fold=0 is the first (earlier-UTC) occurrence,
        # fold=1 the second.
        chosen = f0 if on_ambiguous == "first" else f1
        utc = chosen.astimezone(UTC)
        return Resolution(AMBIGUOUS, utc, utc.astimezone(tz), None)

    # Nonexistent local time (spring-forward gap).
    if on_gap == "skip":
        return Resolution(
            GAP_SKIPPED,
            None,
            None,
            f"local time {naive:%Y-%m-%d %H:%M} does not exist in {key} "
            f"(clocks jump forward); skipped per series policy on_gap=skip",
        )

    # on_gap == "shift": move to the next valid local moment.  Both fold
    # interpretations are candidates; pick the one whose round-tripped local
    # time is the smallest local time >= the requested one.
    candidates = []
    for aware in (f0, f1):
        utc = aware.astimezone(UTC)
        back = utc.astimezone(tz)
        candidates.append((back.replace(tzinfo=None), utc, back))
    forward = [c for c in candidates if c[0] >= naive]
    _, utc, back = min(forward or candidates, key=lambda c: c[0])
    return Resolution(
        GAP_SHIFTED,
        utc,
        back,
        f"local time {naive:%Y-%m-%d %H:%M} does not exist in {key} "
        f"(clocks jump forward); moved to next valid local time "
        f"{back:%Y-%m-%d %H:%M} per series policy on_gap=shift",
    )
