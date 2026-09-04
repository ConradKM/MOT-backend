"""Checklist-instance snapshotting, shared by every place an appointment can
come into existence.

An appointment's checklist is snapshotted from its appointment type's current
ChecklistTemplate *once*, at the earliest point the appointment exists - not
lazily whenever staff first open the checklist page. Snapshotting eagerly at
creation time (see callers in app/appointments/routes.py and
app/booking_requests/routes.py) closes a real gap: with lazy-on-first-open, an
appointment booked before a template edit could still end up snapshotting the
*post-edit* template, if nobody happened to open its checklist until after the
edit landed - defeating the historical-stability guarantee the snapshot design
exists for in the first place.

`app/appointments/checklists/routes.py::AppointmentChecklistResource.post`
remains as a manual fallback for the one legitimate remaining case: an
appointment created before its type had any checklist template, which later
gains one.
"""

from __future__ import annotations

from app.extensions import db
from app.models.appointments.appointment_checklist import AppointmentChecklist
from app.models.appointments.appointment_checklist_item import AppointmentChecklistItem


def snapshot_checklist_for_appointment(appointment) -> AppointmentChecklist | None:
    """Create and return the checklist instance for ``appointment``, or None
    if its appointment type has no template (or one already exists).

    Does not commit - callers already have their own commit as part of
    creating the appointment; flushes only so the returned object's id/items
    are usable immediately.
    """
    if appointment.checklist is not None:
        return appointment.checklist

    template = appointment.appointment_type.checklist_template
    if template is None:
        return None

    checklist = AppointmentChecklist(
        garage_id=appointment.garage_id,
        appointment_id=appointment.id,
        checklist_template_id=template.id,
    )
    db.session.add(checklist)
    db.session.flush()

    for template_item in template.items:
        db.session.add(
            AppointmentChecklistItem(
                garage_id=appointment.garage_id,
                appointment_checklist_id=checklist.id,
                checklist_template_item_id=template_item.id,
                order=template_item.order,
                label=template_item.label,
                description=template_item.description,
                is_compulsory=template_item.is_compulsory,
                media_type=template_item.media_type,
                media_required_for_statuses=list(template_item.media_required_for_statuses),
                result_options=list(template_item.result_options),
                visible_to_customer=template_item.visible_to_customer,
            )
        )

    db.session.flush()
    return checklist
