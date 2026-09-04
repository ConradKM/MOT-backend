from datetime import UTC, datetime, time, timedelta

from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.appointments.checklists.service import snapshot_checklist_for_appointment
from app.appointments.statuses.defaults import DEFAULT_STATUS_KEYS
from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_status import GarageAppointmentStatus
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.vehicle import Vehicle

from .schemas import AppointmentQueryArgsSchema, AppointmentSchema, AppointmentUpdateSchema

appointments_blp = Blueprint(
    "appointments",
    "appointments",
    url_prefix="/api/appointments",
    description="Garage appointment calendar. Appointments are scoped to the "
    "authenticated employee's garage and assigned to an individual employee - "
    "multiple employees in the same garage may have appointments at the same "
    "time, but a single employee cannot be double-booked.",
)

_AUTH_DOC = {"security": [{"bearerAuth": []}]}


def _get_owned_employee(employee_id, garage_id):
    """Look up an employee to assign to an appointment. Only called when an
    appointment's employee is being set/changed (create, or an explicit
    employee_id on PATCH) - an existing appointment keeps its employee even if
    that employee is later deactivated, but a deactivated employee can't be
    newly assigned to one."""
    employee = Employee.query.filter_by(id=employee_id, garage_id=garage_id).first()

    if not employee:
        abort(422, message="employee_id does not belong to your garage.")

    if not employee.is_active:
        abort(422, message="This employee's account is deactivated.")

    return employee


def _get_owned_customer(customer_id, garage_id):
    customer = Customer.query.filter_by(id=customer_id, garage_id=garage_id).first()

    if not customer:
        abort(422, message="customer_id does not belong to your garage.")

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
        abort(422, message="appointment_type_id does not belong to your garage.")

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
        abort(422, message="vehicle_id does not belong to your garage.")

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
        abort(422, message="Not a valid appointment status for this garage.")


def _validate_time_range(start_time, end_time):
    if start_time >= end_time:
        abort(422, message="start_time must be before end_time.")


def _resolve_end_time(start_time, end_time, appointment_type):
    if end_time is not None:
        return end_time

    if appointment_type.default_duration_minutes is None:
        abort(
            422,
            message="end_time is required - this appointment type has no default "
            "duration set.",
        )

    return start_time + timedelta(minutes=appointment_type.default_duration_minutes)


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


def _day_bounds(day):
    return (
        datetime.combine(day, time.min, tzinfo=UTC),
        datetime.combine(day, time.max, tzinfo=UTC),
    )


@appointments_blp.route("/")
class AppointmentList(MethodView):

    @jwt_required()
    @appointments_blp.doc(**_AUTH_DOC)
    @appointments_blp.arguments(AppointmentQueryArgsSchema, location="query")
    @appointments_blp.response(200, AppointmentSchema(many=True))
    def get(self, args):
        garage_id = get_current_employee().garage_id

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

        # Calendar-day filters compare against UTC day boundaries, since
        # start_time is always stored and compared as an absolute instant.
        if args.get("date") is not None:
            day_start, day_end = _day_bounds(args["date"])
            query = query.filter(Appointment.start_time >= day_start, Appointment.start_time <= day_end)
        else:
            if args.get("start_date") is not None:
                query = query.filter(Appointment.start_time >= _day_bounds(args["start_date"])[0])
            if args.get("end_date") is not None:
                query = query.filter(Appointment.start_time <= _day_bounds(args["end_date"])[1])

        return query.order_by(Appointment.start_time).all()

    @jwt_required()
    @appointments_blp.doc(**_AUTH_DOC)
    @appointments_blp.arguments(AppointmentSchema)
    @appointments_blp.response(201, AppointmentSchema)
    def post(self, data):
        garage_id = get_current_employee().garage_id

        _get_owned_employee(data["employee_id"], garage_id)
        _get_owned_customer(data["customer_id"], garage_id)
        appointment_type = _get_owned_appointment_type(data["appointment_type_id"], garage_id)

        vehicle_id = data.get("vehicle_id")
        if vehicle_id is not None:
            _get_owned_vehicle(vehicle_id, garage_id, data["customer_id"])

        end_time = _resolve_end_time(data["start_time"], data["end_time"], appointment_type)

        _validate_time_range(data["start_time"], end_time)
        _validate_status(data.get("status"), garage_id)
        _check_for_conflict(data["employee_id"], data["start_time"], end_time)

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
            price_at_booking=appointment_type.base_price,
        )

        db.session.add(appointment)
        db.session.flush()
        snapshot_checklist_for_appointment(appointment)
        db.session.commit()

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

        appointment = Appointment.query.filter_by(id=appointment_id, garage_id=garage_id).first()

        if not appointment:
            abort(404, message="Appointment not found")

        if "employee_id" in data:
            _get_owned_employee(data["employee_id"], garage_id)

        if "appointment_type_id" in data:
            _get_owned_appointment_type(data["appointment_type_id"], garage_id)

        effective_customer_id = data.get("customer_id", appointment.customer_id)
        if "customer_id" in data:
            _get_owned_customer(data["customer_id"], garage_id)

        if "vehicle_id" in data and data["vehicle_id"] is not None:
            _get_owned_vehicle(data["vehicle_id"], garage_id, effective_customer_id)

        if "status" in data:
            _validate_status(data["status"], garage_id)

        effective_start = data.get("start_time", appointment.start_time)
        effective_end = data.get("end_time", appointment.end_time)
        _validate_time_range(effective_start, effective_end)

        if "employee_id" in data or "start_time" in data or "end_time" in data:
            effective_employee_id = data.get("employee_id", appointment.employee_id)
            _check_for_conflict(
                effective_employee_id,
                effective_start,
                effective_end,
                exclude_appointment_id=appointment.id,
            )

        for field, value in data.items():
            setattr(appointment, field, value)

        db.session.commit()

        return appointment

    @jwt_required()
    @appointments_blp.doc(**_AUTH_DOC)
    @appointments_blp.response(204)
    def delete(self, appointment_id):
        garage_id = get_current_employee().garage_id

        appointment = Appointment.query.filter_by(id=appointment_id, garage_id=garage_id).first()

        if not appointment:
            abort(404, message="Appointment not found")

        # Appointments are historical scheduling records, so deletion cancels
        # rather than hard-deletes - the booking stays visible in the
        # customer/employee's history instead of disappearing outright.
        appointment.status = "CANCELLED"
        db.session.commit()

        return ""
