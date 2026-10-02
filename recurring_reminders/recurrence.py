"""Date expansion for daily/weekly recurrence rules (all in local dates)."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterator


def matches(rule, day: date) -> bool:
    """True if `day` is a recurrence day for the given version rule.

    `rule` needs: freq ("daily"|"weekly"), interval (int), byweekday
    (tuple of ints, Monday=0), start_date, until (date or None).
    """
    if day < rule.start_date:
        return False
    if rule.until is not None and day > rule.until:
        return False
    if rule.freq == "daily":
        return (day - rule.start_date).days % rule.interval == 0
    # weekly: weeks are anchored on the Monday of the week containing start_date
    anchor = rule.start_date - timedelta(days=rule.start_date.weekday())
    week_index = ((day - timedelta(days=day.weekday())) - anchor).days // 7
    return day.weekday() in rule.byweekday and week_index % rule.interval == 0


def dates(rule, lo: date, hi: date) -> Iterator[date]:
    """Yield recurrence days for `rule` within [lo, hi] inclusive."""
    day = max(lo, rule.start_date)
    while day <= hi:
        if matches(rule, day):
            yield day
        day += timedelta(days=1)
