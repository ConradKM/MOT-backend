"""Walk-in ETA: when will each person in the queue be seen?

Pure functions over plain values - no database, no clock - so the part of this
feature most prone to subtle bugs can be tested against fabricated
appointments and queue states (see tests/test_queue_eta.py). The caller
(app/queueing/service.py::queue_snapshot) turns real rows into the inputs.

The model
---------
The business has ``capacity`` bays that each serve one customer at a time.
Every appointment still due today, every customer currently being served, and
every customer who has been called forward occupies one bay for an interval
(:class:`Busy`). WAITING walk-ins are then placed, strictly in queue order,
at the earliest instant a bay is free for their *whole* expected service -
not just at the instant they start. That is what makes a queue sitting in
front of a fully booked afternoon report an honest wait: a 45-minute walk-in
cannot squeeze into the 30 free minutes before a 14:00 booking takes the last
bay, so it is placed after that booking instead.

Order is FIFO on *start* times: nobody is estimated to start before someone
ahead of them, even if a shorter job would technically fit in an earlier gap.
The queue is a promise about order; gap-filling is a staff decision, and if
staff do it the estimate simply refreshes on the next read.

Closing time
------------
A walk-in whose service would not *finish* by closing time is reported with
``start=None`` / ``fits_today=False`` - the same "the whole job fits before
closing" rule the public booking calendar applies to slots (see
app/public_booking/availability.py::day_slots). Because the order is FIFO,
everyone behind that person is also reported as not fitting today. New joins
are refused once a hypothetical new last-in-line walk-in would not fit (see
app/queueing/service.py::join_refusal_reason) - people already in the line
are never removed for it; staff decide what to tell them.

Reserved walk-in windows need no special case here: they act by keeping
public bookings *out* of the protected bays, so their effect already shows up
as fewer ``Busy`` appointment intervals during the window.
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

# When a service has already run past its expected end, we can't know how
# much longer it will take - assume it is about to finish, but not *already*
# finished, so an overrunning job keeps its bay slightly into the future
# rather than the estimate pretending the bay is free right now.
OVERRUN_GRACE = timedelta(minutes=5)


@dataclass(frozen=True)
class Busy:
    """One bay occupied over ``[start, end)``."""

    start: datetime
    end: datetime


@dataclass(frozen=True)
class QueuedJob:
    """A WAITING walk-in, in queue order."""

    key: Hashable
    minutes: int


@dataclass(frozen=True)
class Estimate:
    key: Hashable
    # 1-based place among WAITING entries.
    position: int
    # None when the service can't finish before closing today.
    start: datetime | None

    @property
    def fits_today(self) -> bool:
        return self.start is not None


def ongoing(start: datetime, expected_end: datetime, now: datetime) -> Busy:
    """The interval for a service that is *in progress* right now.

    Its bay stays busy until its expected end - or, if it has already overrun,
    until shortly after ``now`` (see OVERRUN_GRACE). This is how "a service
    runs long" pushes everyone's estimate back on the next refresh.
    """
    return Busy(start, max(expected_end, now + OVERRUN_GRACE))


def wait_minutes(start: datetime | None, now: datetime) -> int | None:
    """Whole minutes from ``now`` until ``start``, rounded up; 0 if due."""
    if start is None:
        return None
    return max(0, math.ceil((start - now).total_seconds() / 60))


def _usage_at(intervals: Sequence[Busy], instant: datetime) -> int:
    return sum(1 for b in intervals if b.start <= instant < b.end)


def _fits(intervals: Sequence[Busy], start: datetime, end: datetime, capacity: int) -> bool:
    """True if a bay is free for the whole of ``[start, end)``.

    Occupancy only rises at an interval's start, so checking ``start`` itself
    plus every interval start inside the window covers every peak.
    """
    checkpoints = [start] + [b.start for b in intervals if start < b.start < end]
    return all(_usage_at(intervals, t) < capacity for t in checkpoints)


def _earliest_start(
    intervals: Sequence[Busy], not_before: datetime, minutes: int, capacity: int
) -> datetime:
    """The earliest ``t >= not_before`` at which a ``minutes``-long job fits.

    Occupancy only falls at an interval's end, so if the job doesn't fit at
    ``not_before`` the next moment it could is one of those ends. The latest
    end always works (everything else has finished by then), so this always
    returns.
    """
    duration = timedelta(minutes=minutes)
    candidates = sorted({not_before, *(b.end for b in intervals if b.end > not_before)})
    for t in candidates:
        if _fits(intervals, t, t + duration, capacity):
            return t
    # Unreachable when capacity >= 1 - kept so the type is honest.
    return candidates[-1]


def estimate_queue(
    *,
    now: datetime,
    capacity: int,
    opens_at: datetime | None,
    closes_at: datetime | None,
    busy: Iterable[Busy],
    queue: Sequence[QueuedJob],
) -> list[Estimate]:
    """Estimated start for every job in ``queue`` (already in queue order).

    ``opens_at``/``closes_at`` are today's effective hours after schedule
    exceptions, or ``None`` when the business is closed today - in which case
    nobody fits. ``busy`` is every bay-occupying interval that isn't a
    WAITING walk-in; intervals entirely in the past are ignored.
    """
    if opens_at is None or closes_at is None or capacity < 1:
        return [Estimate(job.key, i + 1, None) for i, job in enumerate(queue)]

    floor = max(now, opens_at)
    intervals = [b for b in busy if b.end > floor and b.end > b.start]
    estimates: list[Estimate] = []
    blocked = False
    for i, job in enumerate(queue):
        if blocked:
            estimates.append(Estimate(job.key, i + 1, None))
            continue
        start = _earliest_start(intervals, floor, job.minutes, capacity)
        end = start + timedelta(minutes=job.minutes)
        if end > closes_at:
            # FIFO: if this person can't be seen today, nobody behind them
            # is estimated to be either (see module docs).
            blocked = True
            estimates.append(Estimate(job.key, i + 1, None))
            continue
        estimates.append(Estimate(job.key, i + 1, start))
        intervals.append(Busy(start, end))
        floor = start
    return estimates
