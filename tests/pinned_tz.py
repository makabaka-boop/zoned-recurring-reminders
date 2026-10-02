"""Pinned timezone data for tests.

Builds self-contained TZif (v2) files and loads them with
zoneinfo.ZoneInfo.from_file, so tests do not depend on the host's tzdata
version.  Two zones are provided:

  Test/USlike  - US-style DST, pinned: springs forward 02:00 -> 03:00 on the
                 second Sunday of March (offset -5 -> -4, EST -> EDT), falls
                 back 02:00 -> 01:00 on the first Sunday of November.
                 Transitions are pinned for 2025..2030 plus a POSIX footer.
  Test/East    - fixed UTC+10, no DST (used for cross-day reminder tests).
"""
from __future__ import annotations

import io
import struct
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th weekday (Monday=0) of the given month, 1-based."""
    first = date(year, month, 1)
    return first + timedelta(days=((weekday - first.weekday()) % 7) + 7 * (n - 1))


def _build_tzif(transitions, ttinfos, abbrs, footer: str = "") -> bytes:
    """Serialize a TZif v2 file.

    transitions: sorted list of (unix_seconds:int, type_index:int)
    ttinfos:     list of (gmtoff_seconds:int, isdst:int, abbr_index:int)
    abbrs:       list of abbreviation strings; abbr_index refers into the
                 concatenated, NUL-separated char array
    """
    chars = b""
    offsets = []
    for a in abbrs:
        offsets.append(len(chars))
        chars += a.encode() + b"\0"

    def header() -> bytes:
        return (
            b"TZif" + b"2" + b"\0" * 15
            + struct.pack(">6I", 0, 0, 0, len(transitions), len(ttinfos), len(chars))
        )

    def body(time_fmt: str) -> bytes:
        out = b""
        if transitions:
            out += struct.pack(f">{len(transitions)}{time_fmt}", *[t for t, _ in transitions])
            out += bytes(idx for _, idx in transitions)
        for gmtoff, isdst, abbr_idx in ttinfos:
            out += struct.pack(">iBB", gmtoff, isdst, abbr_idx)
        return out + chars

    data = header() + body("i") + header() + body("q")
    # zoneinfo requires the (possibly empty) newline-delimited POSIX footer
    data += b"\n" + footer.encode() + b"\n"
    return data


def _uslike_tzif() -> bytes:
    est, edt = 0, 1  # type indices
    transitions = []
    for year in range(2025, 2031):
        spring = _nth_weekday(year, 3, 6, 2)   # 2nd Sunday of March, 02:00 EST
        fall = _nth_weekday(year, 11, 6, 1)    # 1st Sunday of November, 02:00 EDT
        spring_utc = datetime(year, 3, spring.day, 7, 0, tzinfo=UTC)    # 02:00 -0500
        fall_utc = datetime(year, 11, fall.day, 6, 0, tzinfo=UTC)       # 02:00 -0400
        transitions.append((int(spring_utc.timestamp()), edt))
        transitions.append((int(fall_utc.timestamp()), est))
    transitions.sort()
    ttinfos = [(-5 * 3600, 0, 0), (-4 * 3600, 1, 4)]  # EST, EDT
    return _build_tzif(transitions, ttinfos, ["EST", "EDT"],
                       footer="EST5EDT,M3.2.0/2,M11.1.0/2")


def _east_tzif() -> bytes:
    return _build_tzif([], [(10 * 3600, 0, 0)], ["AET"])


def load_zones() -> dict:
    return {
        "Test/USlike": ZoneInfo.from_file(io.BytesIO(_uslike_tzif()), key="Test/USlike"),
        "Test/East": ZoneInfo.from_file(io.BytesIO(_east_tzif()), key="Test/East"),
    }


ZONES = load_zones()

# Well-known pinned instants in Test/USlike (2026):
SPRING_FORWARD_DAY = date(2026, 3, 8)   # 02:00 -> 03:00 local
FALL_BACK_DAY = date(2026, 11, 1)       # 02:00 -> 01:00 local
