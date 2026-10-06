from datetime import time, timedelta

from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.appointments.add_ons.service import (
    duration_delta,
    price_delta,
    resolve_selection,
    snapshot_kwargs,
    total_price,
)
from app.appointments.checklists.service import snapshot_checklist_for_appointment
from app.appointments.statuses.defaults import DEFAULT_STATUS_KEYS
from app.auth.utils import get_current_employee
from app.communications.events import (
    APPOINTMENT_CANCELLED,
    APPOINTMENT_COMPLETED,
    APPOINTMENT_CREATED,
    APPOINTMENT_RESCHEDULED,
    emit_event,
)
from app.extensions import db
from app.garages.timezones import local_slot_as_utc
from app.models.appointments.applied_add_on import AppointmentAddOn
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_status import GarageAppointmentStatus
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.garage import Garage
from app.models.vehicle import Vehicle
from app.public_booking.availability import slot_capacity_usage

from .schemas import AppointmentQueryArgsSchema, AppointmentSchema, AppointmentUpdateSchema

appointments_blp = Blueprint(
    "appointments",
    "appointments",
    url_prefix="/api/appointments",
    description="Business appointment calendar. Appointments are scoped to the "
    "authenticated employee's business and assigned to an individual employee - "
    "multiple employees in the same business may have appointments at the same "
    "time, but a single employee cannot be double-booked.",
)

_AUTH_DOC: dict[str, list[dict[str, list[str]]]] = {"security": [{"bearerAuth": []}]}


def _get_owned_employee(employee_id, garage_id):
    """Look up an employee to assign to an appointment. Only called when an
    appointment's employee is being set/changed (create, or an explicit
    employee_id on PATCH) - an existing appointment keeps its employee even if
    that employee is later deactivated, but a deactivated employee can't be
    newly assigned to one."""
    employee = Employee.query.filter_by(id=employee_id, garage_id=garage_id).first()

    if not employee:
        abort(422, message="employee_id does not belong to your business.")

    if not employee.is_active:
        abort(422, message="This employee's account is deactivated.")

    return employee


def _get_owned_customer(customer_id, garage_id):
    customer = Customer.query.filter_by(id=customer_id, garage_id=garage_id).first()

    if not customer:
        abort(422, message="customer_id does not belong to your business.")

    # An archived customer (see app/customers/routes.py) is still a real,
    # reachable record - booking them again just means they're active again.
    if not customer.is_active:
        customer.is_active = True

    return customer


def _get_owned_appointment_type(appointment_type_id, garage_id):
    appointment_type = GarageAppointmentType.query.filter_by(
        id=appointment_type_id, garage_id=garage_id
    ).first()

    if not appointment_type:
        abort(422, message="appointment_type_id does not belong to your business.")

    # Only blocks *assigning* a hidden/deprecated type to an appointment
    # (here, on create or on an explicit type change) - an appointment that
    # already uses a type before it was hidden/deprecated is unaffected,
    # since this is only called when appointment_type_id is being set.
    if appointment_type.status != "ACTIVE":
        abort(
            422,
            message="This appointment type is not active and cannot be used for new bookings.",
        )

    return appointment_type


def _get_owned_vehicle(vehicle_id, garage_id, customer_id):
    vehicle = Vehicle.query.filter_by(id=vehicle_id, garage_id=garage_id).first()

    if not vehicle:
        abort(422, message="vehicle_id does not belong to your business.")

    if vehicle.customer_id != customer_id:
        abort(422, message="vehicle_id does not belong to the specified customer.")

    if not vehicle.is_active:
        vehicle.is_active = True

    return vehicle


def _allowed_statuses(garage_id):
    """The garage's configured status keys, or the built-in default set if it
    hasn't customised them."""
    configured = {
        key
        for (key,) in db.session.query(GarageAppointmentStatus.key)
        .filter_by(garage_id=garage_id)
        .all()
    }
    return configured or set(DEFAULT_STATUS_KEYS)


def _validate_status(status, garage_id):
    if status is not None and status not in _allowed_statuses(garage_id):
        abort(422, message="Not a valid appointment status for this business.")


def _is_terminal_status(status, garage_id):
    """Whether a configured status is terminal.

    Existing garages without seeded configuration still use the same terminal
    semantics as the built-in status set.  ``CANCELLED`` is deliberately
    handled separately by the transition guard because staff have an existing,
    capacity-checked reactivation workflow for a cancelled appointment.
    """
    configured = GarageAppointmentStatus.query.filter_by(garage_id=garage_id, key=status).first()
    if configured is not None:
        return configured.is_terminal
    return status in {"COMPLETED", "CANCELLED", "NO_SHOW"}


def _validate_status_transition(current_status, requested_status, garage_id):
    """Reject stale or impossible terminal-state transitions.

    Completion and no-show are historical outcomes, so a delayed form submit
    cannot silently make either live again.  Cancellation is the one explicit
    exception: the existing staff workflow permits a cancelled appointment to
    be reactivated after conflict/capacity revalidation.
    """
    if requested_status == current_status:
        return

    if current_status == "CANCELLED":
        if _is_terminal_status(requested_status, garage_id):
            abort(
                409,
                message="A cancelled appointment must be reactivated before it can change state.",
            )
        return

    if _is_terminal_status(current_status, garage_id):
        abort(409, message="A completed or no-show appointment cannot be moved to another status.")


def _validate_time_range(start_time, end_time):
    if start_time >= end_time:
        abort(422, message="start_time must be before end_time.")


def _resolve_end_time(start_time, end_time, appointment_type, add_on_minutes=0):
    # An explicit end_time is staff's own override and wins outright, add-ons
    # included - the form shows the derived value but lets staff edit it.
    if end_time is not None:
        return end_time

    if appointment_type.default_duration_minutes is None:
        abort(
            422,
            message="end_time is required - this appointment type has no default duration set.",
        )

    return start_time + timedelta(
        minutes=appointment_type.default_duration_minutes + add_on_minutes
    )


def _plan_add_ons(appointment, appointment_type, type_changed, selection):
    """The add-on rows an edited appointment should end up with, as
    transient AppointmentAddOn objects (assigned in one go once every other
    check has passed).

    ``selection`` is the full new selection. Add-ons that stay selected keep
    their snapshotted price and duration - re-saving an appointment must not
    silently re-price it from today's catalogue - and only newly selected
    ones take current values. A type change drops the old type's add-ons,
    since they can't apply to the new service.
    """
    existing = [] if type_changed else list(appointment.add_ons)
    by_add_on = {r.add_on_id: r for r in existing if r.add_on_id is not None}
    resolved = resolve_selection(
        appointment_type,
        selection,
        already_applied={add_on_id: r.quantity for add_on_id, r in by_add_on.items()},
    )

    planned = []
    for row in resolved:
        kept = by_add_on.get(row.add_on.id)
        if kept is not None:
            planned.append(
                AppointmentAddOn(
                    garage_id=kept.garage_id,
                    add_on_id=kept.add_on_id,
                    name=kept.name,
                    quantity=row.quantity,
                    price_delta=kept.price_delta,
                    duration_delta_minutes=kept.duration_delta_minutes,
                )
            )
        else:
            planned.append(AppointmentAddOn(**snapshot_kwargs(row, appointment.garage_id)))
    return planned


def _check_for_conflict(employee_id, start_time, end_time, exclude_appointment_id=None):
    query = Appointment.query.filter(
        Appointment.employee_id == employee_id,
        Appointment.status != "CANCELLED",
        Appointment.start_time < end_time,
        Appointment.end_time > start_time,
    )

    if exclude_appointment_id is not None:
        query = query.filter(Appointment.id != exclude_appointment_id)

    if query.first() is not None:
        abort(
            409,
            message="The selected employee already has an appointment during this time.",
        )


def _check_capacity(garage, start_time, end_time, exclude_appointment_id=None):
    """Apply the garage-wide capacity contract to staff-created schedules.

    Employee conflict checks alone are insufficient when a business has set
    ``capacity_per_slot`` below its number of staff.  This deliberately uses
    the same reservation accounting as public booking and request approval,
    including pending customer requests, so staff actions cannot silently
    take a slot those flows have already reserved.
    """
    duration_minutes = int((end_time - start_time).total_seconds() // 60)
    used, capacity = slot_capacity_usage(
        garage,
        start_time.date(),
        start_time,
        duration_minutes,
        exclude_appointment_id=exclude_appointment_id,
        # Walk-in reserved windows protect bays from *public* booking only.
        include_walkin_reservations=False,
    )
    if used >= capacity:
        abort(
            409,
            message="This time is no longer available - capacity has already been taken by another appointment or request.",
        )


def _day_bounds(garage, day):
    return (
        local_slot_as_utc(garage, day, time.min),
        local_slot_as_utc(garage, day, time.max),
    )


@appointments_blp.route("/")
class AppointmentList(MethodView):
    @jwt_required()
    @appointments_blp.doc(**_AUTH_DOC)
    @appointments_blp.arguments(AppointmentQueryArgsSchema, location="query")
    @appointments_blp.response(200, AppointmentSchema(many=True))
    def get(self, args):
        garage_id = get_current_employee().garage_id
        garage = Garage.query.filter_by(id=garage_id).one()

        query = Appointment.query.filter_by(garage_id=garage_id)

        if args.get("employee_id") is not None:
            query = query.filter(Appointment.employee_id == args["employee_id"])

        if args.get("customer_id") is not None:
            query = query.filter(Appointment.customer_id == args["customer_id"])

        if args.get("vehicle_id") is not None:
            query = query.filter(Appointment.vehicle_id == args["vehicle_id"])

        if args.get("status") is not None:
            query = query.filter(Appointment.status == args["status"])

        if args.get("appointment_type_id") is not None:
            query = query.filter(Appointment.appointment_type_id == args["appointment_type_id"])

        # Date query values name the business calendar day; convert its local
        # boundary to UTC before filtering persisted appointment instants.
        if args.get("date") is not None:
            day_start, day_end = _day_bounds(garage, args["date"])
            query = query.filter(
                Appointment.start_time >= day_start, Appointment.start_time <= day_end
            )
        else:
            if args.get("start_date") is not None:
                query = query.filter(
                    Appointment.start_time >= _day_bounds(garage, args["start_date"])[0]
                )
            if args.get("end_date") is not None:
                query = query.filter(
                    Appointment.start_time <= _day_bounds(garage, args["end_date"])[1]
                )

        return query.order_by(Appointment.start_time).all()

    @jwt_required()
    @appointments_blp.doc(**_AUTH_DOC)
    @appointments_blp.arguments(AppointmentSchema)
    @appointments_blp.response(201, AppointmentSchema)
    def post(self, data):
        garage_id = get_current_employee().garage_id
        garage = db.session.query(Garage).filter_by(id=garage_id).with_for_update().one()

        _get_owned_employee(data["employee_id"], garage_id)
        _get_owned_customer(data["customer_id"], garage_id)
        appointment_type = _get_owned_appointment_type(data["appointment_type_id"], garage_id)

        vehicle_id = data.get("vehicle_id")
        if vehicle_id is not None:
            _get_owned_vehicle(vehicle_id, garage_id, data["customer_id"])

        add_ons = resolve_selection(appointment_type, data.get("add_ons"))
        end_time = _resolve_end_time(
            data["start_time"], data["end_time"], appointment_type, duration_delta(add_ons)
        )
        price_at_booking = total_price(appointment_type.base_price, price_delta(add_ons))

        _validate_time_range(data["start_time"], end_time)
        _validate_status(data.get("status"), garage_id)
        _check_for_conflict(data["employee_id"], data["start_time"], end_time)
        if data.get("status") != "CANCELLED":
            _check_capacity(garage, data["start_time"], end_time)

        appointment = Appointment(
            garage_id=garage_id,
            employee_id=data["employee_id"],
            customer_id=data["customer_id"],
            vehicle_id=vehicle_id,
            start_time=data["start_time"],
            end_time=end_time,
            appointment_type_id=data["appointment_type_id"],
            status=data.get("status") or "BOOKED",
            notes=data.get("notes"),
            price_at_booking=price_at_booking,
            appointment_type_name_at_booking=appointment_type.name,
            add_ons=[AppointmentAddOn(**snapshot_kwargs(row, garage_id)) for row in add_ons],
        )

        db.session.add(appointment)
        db.session.flush()
        snapshot_checklist_for_appointment(appointment)
        db.session.commit()

        # Only fires once the booking has actually been persisted - a
        # validation abort() above or a failed commit never reaches here, so
        # a confirmation email never goes out for a booking that didn't happen.
        emit_event(APPOINTMENT_CREATED, garage=appointment.garage, appointment=appointment)

        return appointment


@appointments_blp.route("/<uuid:appointment_id>")
class AppointmentResource(MethodView):
    @jwt_required()
    @appointments_blp.doc(**_AUTH_DOC)
    @appointments_blp.response(200, AppointmentSchema)
    def get(self, appointment_id):
        garage_id = get_current_employee().garage_id

        appointment = Appointment.query.filter_by(id=appointment_id, garage_id=garage_id).first()

        if not appointment:
            abort(404, message="Appointment not found")

        return appointment

    @jwt_required()
    @appointments_blp.doc(**_AUTH_DOC)
    @appointments_blp.arguments(AppointmentUpdateSchema)
    @appointments_blp.response(200, AppointmentSchema)
    def patch(self, data, appointment_id):
        garage_id = get_current_employee().garage_id
        garage = db.session.query(Garage).filter_by(id=garage_id).with_for_update().one()

        appointment = Appointment.query.filter_by(id=appointment_id, garage_id=garage_id).first()

        if not appointment:
            abort(404, message="Appointment not found")

        if "employee_id" in data:
            _get_owned_employee(data["employee_id"], garage_id)

        # Re-sending the current type (as the staff form does on every save)
        # is not a change of service: it must neither re-price the booking
        # from today's catalogue nor trip the "type is no longer active" check.
        if data.get("appointment_type_id") == appointment.appointment_type_id:
            del data["appointment_type_id"]

        replacement_appointment_type = None
        if "appointment_type_id" in data:
            replacement_appointment_type = _get_owned_appointment_type(
                data["appointment_type_id"], garage_id
            )

        effective_customer_id = data.get("customer_id", appointment.customer_id)
        if "customer_id" in data:
            _get_owned_customer(data["customer_id"], garage_id)

        if "vehicle_id" in data and data["vehicle_id"] is not None:
            _get_owned_vehicle(data["vehicle_id"], garage_id, effective_customer_id)
        elif "customer_id" in data and appointment.vehicle_id is not None:
            # A vehicle is owned by a customer, not merely by the garage.  A
            # customer-only PATCH must therefore not leave the appointment
            # pointing at a vehicle belonging to its previous customer.
            # Callers can explicitly detach the vehicle (vehicle_id: null) or
            # select one owned by the new customer in the same update.
            _get_owned_vehicle(appointment.vehicle_id, garage_id, effective_customer_id)

        if "status" in data:
            _validate_status(data["status"], garage_id)
            _validate_status_transition(appointment.status, data["status"], garage_id)

        # None = leave the add-ons exactly as they are (snapshots, and rows
        # whose catalogue add-on has since been deleted, included).
        add_on_selection = data.pop("add_ons", None)
        planned_add_ons = None
        if add_on_selection is not None or replacement_appointment_type is not None:
            planned_add_ons = _plan_add_ons(
                appointment,
                replacement_appointment_type or appointment.appointment_type,
                replacement_appointment_type is not None,
                add_on_selection or [],
            )

        effective_start = data.get("start_time", appointment.start_time)
        effective_end = data.get("end_time", appointment.end_time)
        if planned_add_ons is not None and "end_time" not in data:
            # No explicit end_time: carry the add-ons' change in duration
            # through, rather than leaving the old end in place.
            shift = duration_delta(planned_add_ons) - duration_delta(appointment.add_ons)
            if shift:
                effective_end = effective_end + timedelta(minutes=shift)
                data["end_time"] = effective_end
        _validate_time_range(effective_start, effective_end)

        will_be_live = data.get("status", appointment.status) != "CANCELLED"
        scheduling_changed = (
            "employee_id" in data
            or "start_time" in data
            or "end_time" in data
            or (appointment.status == "CANCELLED" and will_be_live)
        )
        if scheduling_changed and will_be_live:
            effective_employee_id = data.get("employee_id", appointment.employee_id)
            _check_for_conflict(
                effective_employee_id,
                effective_start,
                effective_end,
                exclude_appointment_id=appointment.id,
            )
            _check_capacity(
                garage,
                effective_start,
                effective_end,
                exclude_appointment_id=appointment.id,
            )

        new_price = None
        if planned_add_ons is not None:
            if replacement_appointment_type is not None:
                base = replacement_appointment_type.base_price
            elif appointment.price_at_booking is not None:
                # Recover the booked base from the existing snapshot rather
                # than today's catalogue price - see _plan_add_ons.
                base = appointment.price_at_booking - price_delta(appointment.add_ons)
            else:
                base = None
            new_price = total_price(base, price_delta(planned_add_ons))

        previous_status = appointment.status
        previous_start = appointment.start_time
        previous_end = appointment.end_time

        for field, value in data.items():
            setattr(appointment, field, value)

        if replacement_appointment_type is not None:
            # This is an explicit change to what the customer is booked for,
            # not a later catalogue edit.  Carry the replacement service's
            # immutable customer-facing values with it, otherwise the
            # appointment would point at one type while displaying the old
            # type's name and price.
            appointment.appointment_type_name_at_booking = replacement_appointment_type.name

        if planned_add_ons is not None:
            appointment.add_ons = planned_add_ons
            appointment.price_at_booking = new_price

        db.session.commit()

        # Precise, low-risk signals only: a real transition into CANCELLED or
        # COMPLETED, or a real time change on a still-live appointment - never
        # fired just because *some* field on this general-purpose PATCH changed.
        if appointment.status == "CANCELLED" and previous_status != "CANCELLED":
            emit_event(APPOINTMENT_CANCELLED, garage=appointment.garage, appointment=appointment)
        elif appointment.status == "COMPLETED" and previous_status != "COMPLETED":
            emit_event(APPOINTMENT_COMPLETED, garage=appointment.garage, appointment=appointment)
        elif appointment.status != "CANCELLED" and (
            appointment.start_time != previous_start or appointment.end_time != previous_end
        ):
            emit_event(
                APPOINTMENT_RESCHEDULED,
                garage=appointment.garage,
                appointment=appointment,
                previous_start_time=previous_start,
                previous_end_time=previous_end,
            )

        return appointment

    @jwt_required()
    @appointments_blp.doc(**_AUTH_DOC)
    @appointments_blp.response(204)
    def delete(self, appointment_id):
        garage_id = get_current_employee().garage_id
        # Keep cancellation in the same per-tenant mutation critical section
        # as PATCH/reschedule/create.  In particular, a completion racing a
        # deletion must not let the stale DELETE overwrite a terminal outcome.
        db.session.query(Garage).filter_by(id=garage_id).with_for_update().one()

        appointment = (
            Appointment.query.filter_by(id=appointment_id, garage_id=garage_id)
            .with_for_update()
            .first()
        )

        if not appointment:
            abort(404, message="Appointment not found")

        # Appointments are historical scheduling records, so deletion cancels
        # rather than hard-deletes - the booking stays visible in the
        # customer/employee's history instead of disappearing outright.
        was_cancelled = appointment.status == "CANCELLED"
        _validate_status_transition(appointment.status, "CANCELLED", garage_id)
        appointment.status = "CANCELLED"
        db.session.commit()
        if not was_cancelled:
            emit_event(APPOINTMENT_CANCELLED, garage=appointment.garage, appointment=appointment)

        return ""
