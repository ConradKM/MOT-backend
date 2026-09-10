"""How far a tenant has got with setting itself up.

Entirely **derived** - there is no onboarding-progress column anywhere. Each
step is a question answered from the tenant's own data ("has it defined any
services?", "has it taken a booking?"), so progress can never drift out of
sync with reality, and a tenant that did the work before Platform Admin
existed already shows as complete.

Alongside the per-step breakdown there is a single derived **stage** - the
one-word answer the Businesses list shows per row (:data:`STAGES`). It reads
the same completed-step map, so it can no more drift than the steps can.

Two entry points, same definitions:

* :func:`onboarding_progress` - the full per-step breakdown for one tenant's
  detail page.
* :func:`onboarding_progress_bulk` - the same answers for a page of tenants in
  a fixed number of grouped queries, so the tenant list stays one screenful of
  SQL rather than eight queries per row.

``required=False`` steps (a team beyond the owner, live communications) are
reported but left out of the percentage: plenty of healthy single-operator
garages will never do them, and an onboarding score that can't reach 100% is
worse than useless for spotting the tenants that are actually stuck.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select

from app.extensions import db
from app.garages.schedule.defaults import DEFAULT_OPENING_HOURS
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.garage import Garage
from app.models.garage_schedule import GarageOpeningHours


@dataclass(frozen=True)
class Step:
    key: str
    label: str
    description: str
    required: bool = True


STEPS: tuple[Step, ...] = (
    Step(
        key="business_details",
        label="Business details",
        description="Contact email, phone and address on the tenant record.",
    ),
    Step(
        key="services",
        label="Services",
        description="At least one active appointment type customers can book.",
    ),
    Step(
        key="opening_hours",
        label="Opening hours",
        description="Opening hours reviewed - they differ from the seeded Mon-Fri 09:00-17:00.",
    ),
    Step(
        key="customers",
        label="First customer",
        description="At least one customer record exists.",
    ),
    Step(
        key="appointments",
        label="First appointment",
        description="At least one appointment has been booked.",
    ),
    Step(
        key="booking_requests",
        label="First online booking",
        description="At least one public booking request has been received.",
    ),
    Step(
        key="team",
        label="Team",
        description="A second staff login beyond the owner.",
        required=False,
    ),
    Step(
        key="communications",
        label="Communications",
        description="A voice number or WhatsApp sender is configured.",
        required=False,
    ),
)

REQUIRED_STEP_COUNT = sum(1 for step in STEPS if step.required)

#: The steps that make a business *usable* - it can be found, something can be
#: booked, and the availability engine knows when it is open. Everything else
#: in STEPS is evidence of a tenant working, not of it being set up.
CORE_STEP_KEYS = ("business_details", "services", "opening_hours")

STAGE_NOT_STARTED = "not_started"
STAGE_IN_PROGRESS = "in_progress"
STAGE_CORE_COMPLETE = "core_setup_complete"
STAGE_COMMUNICATIONS_PENDING = "communications_pending"
STAGE_READY_FOR_LAUNCH = "ready_for_launch"

#: stage key -> the label Platform Admin renders. Ordered as a tenant moves
#: through them, so the list view can sort or group on the index.
STAGES: dict[str, str] = {
    STAGE_NOT_STARTED: "Not started",
    STAGE_IN_PROGRESS: "In progress",
    STAGE_CORE_COMPLETE: "Core setup complete",
    STAGE_COMMUNICATIONS_PENDING: "Communications pending",
    STAGE_READY_FOR_LAUNCH: "Ready for launch",
}

STAGE_KEYS = tuple(STAGES)

#: Communications columns that mean "somebody has started wiring Twilio up".
#: Distinct from the `communications` step, which asks the stricter question
#: "can this tenant actually send and receive?".
_COMMS_STARTED_FIELDS = (
    "twilio_subaccount_sid",
    "voice_phone_number",
    "whatsapp_sender",
    "messaging_service_sid",
)


def _details_complete(garage: Garage) -> bool:
    return bool(garage.email and garage.phone and garage.address)


def _hours_customised(rows: list[GarageOpeningHours]) -> bool:
    """True if any opening-hours row differs from what onboarding seeded."""
    for row in rows:
        default = DEFAULT_OPENING_HOURS.get(row.weekday)
        if default is None:
            return True
        opens_at, closes_at, is_closed = default
        if (row.opens_at, row.closes_at, row.is_closed) != (opens_at, closes_at, is_closed):
            return True
    return False


def _comms_started(settings) -> bool:
    """True once any Twilio wiring exists for this tenant, live or not."""
    if settings is None:
        return False
    return bool(settings.communications_enabled) or any(
        getattr(settings, field, None) for field in _COMMS_STARTED_FIELDS
    )


def _stage(completed: dict[str, bool], *, comms_started: bool) -> str:
    """Where this tenant is, from the same answers the steps are built from.

    "Ready for launch" is deliberately the *communications* line rather than a
    button an admin presses: a tenant whose WhatsApp and phone number are live
    is ready whether or not anyone remembered to tick something, and one whose
    aren't is not ready however many times they do.
    """
    core_done = sum(1 for key in CORE_STEP_KEYS if completed.get(key))
    if core_done == 0:
        return STAGE_NOT_STARTED
    if core_done < len(CORE_STEP_KEYS):
        return STAGE_IN_PROGRESS
    if completed.get("communications"):
        return STAGE_READY_FOR_LAUNCH
    return STAGE_COMMUNICATIONS_PENDING if comms_started else STAGE_CORE_COMPLETE


def _summarise(completed: dict[str, bool], *, comms_started: bool = False) -> dict:
    steps = [
        {
            "key": step.key,
            "label": step.label,
            "description": step.description,
            "required": step.required,
            "complete": completed.get(step.key, False),
        }
        for step in STEPS
    ]
    done = sum(1 for step in STEPS if step.required and completed.get(step.key))
    stage = _stage(completed, comms_started=comms_started)
    return {
        "steps": steps,
        "stage": stage,
        "stage_label": STAGES[stage],
        "completed_required": done,
        "total_required": REQUIRED_STEP_COUNT,
        "percent_complete": round(100 * done / REQUIRED_STEP_COUNT) if REQUIRED_STEP_COUNT else 100,
        "complete": done == REQUIRED_STEP_COUNT,
    }


def onboarding_progress(garage: Garage) -> dict:
    """The full per-step breakdown for one tenant."""
    settings = garage.communication_settings

    completed = {
        "business_details": _details_complete(garage),
        "services": _count(
            GarageAppointmentType.garage_id == garage.id,
            GarageAppointmentType.status == "ACTIVE",
        )
        > 0,
        "opening_hours": _hours_customised(
            GarageOpeningHours.query.filter_by(garage_id=garage.id).all()
        ),
        "customers": _count(Customer.garage_id == garage.id) > 0,
        "appointments": _count(Appointment.garage_id == garage.id) > 0,
        "booking_requests": _count(BookingRequest.garage_id == garage.id) > 0,
        "team": _count(Employee.garage_id == garage.id) > 1,
        "communications": bool(
            settings and (settings.voice_phone_number or settings.whatsapp_sender)
        ),
    }
    return _summarise(completed, comms_started=_comms_started(settings))


def _count(*where) -> int:
    """A scalar COUNT that is an ``int``, never ``None`` - COUNT always returns
    a row, but ``Session.scalar`` is typed as optional."""
    return db.session.scalar(select(func.count()).where(*where)) or 0


def _counts_by_garage(model, garage_ids, *extra_filters) -> dict[uuid.UUID, int]:
    rows = db.session.execute(
        select(model.garage_id, func.count())
        .where(model.garage_id.in_(garage_ids), *extra_filters)
        .group_by(model.garage_id)
    ).all()
    return {row[0]: row[1] for row in rows}


def onboarding_progress_bulk(garages: list[Garage]) -> dict[uuid.UUID, dict]:
    """The same answers as :func:`onboarding_progress`, for many tenants at
    once - one grouped query per step rather than one per tenant."""
    if not garages:
        return {}

    garage_ids = [garage.id for garage in garages]

    services = _counts_by_garage(
        GarageAppointmentType, garage_ids, GarageAppointmentType.status == "ACTIVE"
    )
    customers = _counts_by_garage(Customer, garage_ids)
    appointments = _counts_by_garage(Appointment, garage_ids)
    booking_requests = _counts_by_garage(BookingRequest, garage_ids)
    employees = _counts_by_garage(Employee, garage_ids)

    hours_rows: dict[uuid.UUID, list[GarageOpeningHours]] = {}
    for row in GarageOpeningHours.query.filter(GarageOpeningHours.garage_id.in_(garage_ids)).all():
        hours_rows.setdefault(row.garage_id, []).append(row)

    comms_settings = {
        row.garage_id: row
        for row in db.session.execute(
            select(GarageCommunicationSettings).where(
                GarageCommunicationSettings.garage_id.in_(garage_ids)
            )
        )
        .scalars()
        .all()
    }
    configured_comms = {
        garage_id
        for garage_id, row in comms_settings.items()
        if row.voice_phone_number or row.whatsapp_sender
    }

    return {
        garage.id: _summarise(
            {
                "business_details": _details_complete(garage),
                "services": services.get(garage.id, 0) > 0,
                "opening_hours": _hours_customised(hours_rows.get(garage.id, [])),
                "customers": customers.get(garage.id, 0) > 0,
                "appointments": appointments.get(garage.id, 0) > 0,
                "booking_requests": booking_requests.get(garage.id, 0) > 0,
                "team": employees.get(garage.id, 0) > 1,
                "communications": garage.id in configured_comms,
            },
            comms_started=_comms_started(comms_settings.get(garage.id)),
        )
        for garage in garages
    }
