"""Recurring events and reminders with real timezone semantics.

Guarantees:
  * Local wall-clock times are resolved through the IANA tz database
    (zoneinfo), never through a fixed UTC offset.
  * Nonexistent local times (DST gaps) are skipped or shifted to the next
    valid moment, per the series' policy.  Repeated local times (DST
    overlaps) use the first or second occurrence, per the series' policy.
  * Editing a series creates a new rule version effective from a chosen
    date; already-confirmed instances keep their original version evidence.
  * The reminder generator writes one unique reminder per instance,
    15 minutes (configurable) before the event, and is idempotent across
    restarts, repeated clock advances, and interleaved edits/exceptions.
"""

from .clock import ManualClock, SystemClock
from .service import Service

__all__ = ["Service", "ManualClock", "SystemClock"]
