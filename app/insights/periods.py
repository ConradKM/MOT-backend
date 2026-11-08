"""Reporting periods in the business's own timezone.

A business's "this month" is its local calendar month. The period's bounds
are the UTC instants of local midnight at each end, computed per date, so a
period spanning a DST change is still exactly the local days it names - one
day of it is 23 or 25 hours long, which is correct.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from app.garages.timezones import timezone_for

Granularity = Literal["day", "week", "month"]


def local_midnight_utc(garage, day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone_for(garage)).astimezone(UTC)


@dataclass(frozen=True)
class Period:
    """Local dates ``start_date``..``end_date`` inclusive, and the half-open
    UTC window ``start <= t < end`` they cover."""

    start_date: date
    end_date: date
    start: datetime
    end: datetime

    @classmethod
    def for_dates(cls, garage, start_date: date, end_date: date) -> Period:
        return cls(
            start_date=start_date,
            end_date=end_date,
            start=local_midnight_utc(garage, start_date),
            end=local_midnight_utc(garage, end_date + timedelta(days=1)),
        )

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    def previous(self, garage) -> Period:
        """The period of equal length immediately before this one."""
        end_date = self.start_date - timedelta(days=1)
        return Period.for_dates(garage, end_date - timedelta(days=self.days - 1), end_date)

    @property
    def granularity(self) -> Granularity:
        """Trend bucket size: enough points to show a shape, few enough to read."""
        if self.days <= 31:
            return "day"
        if self.days <= 182:
            return "week"
        return "month"

    def buckets(self) -> list[date]:
        """Every bucket start the trend covers, so quiet days still get a
        point instead of the x-axis skipping them."""
        granularity = self.granularity
        if granularity == "day":
            return [self.start_date + timedelta(days=i) for i in range(self.days)]
        if granularity == "week":
            # date_trunc('week') starts on Monday, as ISO weeks do.
            cursor = self.start_date - timedelta(days=self.start_date.weekday())
            step = timedelta(days=7)
            points = []
            while cursor <= self.end_date:
                points.append(cursor)
                cursor += step
            return points
        cursor = self.start_date.replace(day=1)
        points = []
        while cursor <= self.end_date:
            points.append(cursor)
            cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        return points
