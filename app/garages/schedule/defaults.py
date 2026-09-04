"""Built-in scheduling defaults.

Every new garage gets a settings row plus one opening-hours row per weekday
seeded at registration (and backfilled for pre-existing garages in the
migration). A garage with *no* rows also resolves to exactly these values via
``resolve_settings`` / ``resolve_opening_hours`` in
app/public_booking/availability.py, so the seeded path and the fallback path
always agree - the same design as app/appointments/statuses/defaults.py.
"""

from datetime import time

from app.models.garage_schedule import GarageOpeningHours, GarageScheduleSettings

# Attribute name -> default, for both the seed and the in-code fallback.
# min_lead_time_hours defaults to 2 so same-day bookings work out of the box
# (a garage can raise it in Settings > Availability).
DEFAULT_SETTINGS = {
    "slot_interval_minutes": 30,
    "default_appointment_minutes": 60,
    "min_lead_time_hours": 2,
    "max_advance_days": 60,
    "capacity_per_slot": None,
    "limited_threshold_ratio": 0.5,
}

_WEEKDAY_OPEN = (time(9, 0), time(17, 0))

# weekday (0 = Mon ... 6 = Sun) -> (opens_at, closes_at, is_closed)
DEFAULT_OPENING_HOURS = {
    0: (*_WEEKDAY_OPEN, False),
    1: (*_WEEKDAY_OPEN, False),
    2: (*_WEEKDAY_OPEN, False),
    3: (*_WEEKDAY_OPEN, False),
    4: (*_WEEKDAY_OPEN, False),
    5: (time(9, 0), time(17, 0), True),
    6: (time(9, 0), time(17, 0), True),
}


def seed_default_schedule(garage_id, session) -> None:
    """Add the default settings + 7 opening-hours rows for ``garage_id``.

    No-op if a settings row already exists, so it is safe to call more than once.
    """
    existing = (
        session.query(GarageScheduleSettings.id)
        .filter_by(garage_id=garage_id)
        .first()
    )
    if existing is not None:
        return

    session.add(
        GarageScheduleSettings(garage_id=garage_id, **DEFAULT_SETTINGS)
    )
    for weekday, (opens_at, closes_at, is_closed) in DEFAULT_OPENING_HOURS.items():
        session.add(
            GarageOpeningHours(
                garage_id=garage_id,
                weekday=weekday,
                opens_at=opens_at,
                closes_at=closes_at,
                is_closed=is_closed,
            )
        )
