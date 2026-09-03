"""The built-in appointment status set.

Every garage gets these seeded (at registration, and backfilled for garages
that already existed). A garage with no rows in `garage_appointment_statuses`
also resolves to exactly this set, so the two paths agree.
"""

from app.models.appointments.appointment_status import GarageAppointmentStatus

# key, label, color, sort_order, is_terminal
DEFAULT_APPOINTMENT_STATUSES = (
    ("REQUESTED", "Requested", "violet", 10, False),
    ("BOOKED", "Booked", "blue", 20, False),
    ("IN_PROGRESS", "In progress", "sky", 30, False),
    ("ACTION_NEEDED", "Action needed", "red", 40, False),
    ("COMPLETED", "Completed", "emerald", 50, True),
    ("CANCELLED", "Cancelled", "slate", 60, True),
    ("NO_SHOW", "No-show", "amber", 70, True),
)

DEFAULT_STATUS_KEYS = tuple(row[0] for row in DEFAULT_APPOINTMENT_STATUSES)


def seed_default_statuses(garage_id, session) -> None:
    """Add the built-in status rows for `garage_id` (no-op if any already exist)."""
    existing = session.query(GarageAppointmentStatus.id).filter_by(garage_id=garage_id).first()
    if existing is not None:
        return

    for key, label, color, sort_order, is_terminal in DEFAULT_APPOINTMENT_STATUSES:
        session.add(
            GarageAppointmentStatus(
                garage_id=garage_id,
                key=key,
                label=label,
                color=color,
                sort_order=sort_order,
                is_terminal=is_terminal,
                is_system=True,
            )
        )
