from datetime import UTC, datetime, timedelta

from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.appointments.checklists.service import snapshot_checklist_for_appointment
from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.vehicle import Vehicle
from app.public_booking.availability import slot_capacity_usage

from .schemas import (
    BookingRequestApproveSchema,
    BookingRequestQueryArgsSchema,
    BookingRequestRejectSchema,
    BookingRequestSchema,
)
from .service import attach_review_context, expire_stale_booking_requests, is_request_stale

booking_requests_blp = Blueprint(
    "booking_requests",
    "booking_requests",
    url_prefix="/api/booking-requests",
    description="Staff review of public booking requests. Approving one creates "
    "the customer / vehicle / appointment; rejecting one just records the decision.",
)


def _get_owned_request(request_id):
    garage_id = get_current_employee().garage_id
    booking_request = BookingRequest.query.filter_by(
        id=request_id, garage_id=garage_id
    ).first()

    if booking_request is None:
        abort(404, message="Booking request not found")

    return booking_request


def _normalize_registration(value: str) -> str:
    # Mirrors app/models/vehicle.py::Vehicle.normalize_registration_number so a
    # lookup matches however the reg was originally stored.
    return value.strip().upper().replace(" ", "")


# Minimal re-implementation of the scheduling checks in
# app/appointments/routes.py (_resolve_end_time / _check_for_conflict): an
# approved request must place a real appointment under the same rules a staff
# member gets, without importing that module's private helpers.
def _resolve_appointment_slot(booking_request, data, appointment_type):
    start_time = data.get("start_time")
    if start_time is None and booking_request.preferred_time is not None:
        start_time = datetime.combine(
            booking_request.preferred_date, booking_request.preferred_time, tzinfo=UTC
        )
    if start_time is None:
        abort(
            422,
            message="start_time is required - the request has no preferred time to fall back on.",
        )

    end_time = data.get("end_time")
    if end_time is None:
        if appointment_type.default_duration_minutes is None:
            abort(
                422,
                message="end_time is required - this appointment type has no default duration.",
            )
        end_time = start_time + timedelta(
            minutes=appointment_type.default_duration_minutes
        )

    if start_time >= end_time:
        abort(422, message="start_time must be before end_time.")

    return start_time, end_time


def _assert_no_conflict(employee_id, start_time, end_time):
    clash = Appointment.query.filter(
        Appointment.employee_id == employee_id,
        Appointment.status != "CANCELLED",
        Appointment.start_time < end_time,
        Appointment.end_time > start_time,
    ).first()
    if clash is not None:
        abort(
            409,
            message="The selected employee already has an appointment during this time.",
        )


def _assert_capacity_available(garage, booking_request, start_time, end_time):
    """The general per-slot capacity check (see app/public_booking/availability.py)
    - not just "is this one employee free". A garage can configure
    capacity_per_slot below its employee count, so a specific employee having
    no conflict doesn't by itself guarantee the slot is still within capacity.
    Excludes this request's own reservation - approving it converts that
    reservation into the appointment, it doesn't add a new one."""
    duration_min = int((end_time - start_time).total_seconds() // 60)
    used, capacity = slot_capacity_usage(
        garage,
        start_time.date(),
        start_time,
        duration_min,
        exclude_request_id=booking_request.id,
    )
    if used >= capacity:
        abort(
            409,
            message="This time is no longer available - capacity has already "
            "been taken by another appointment or request.",
        )


@booking_requests_blp.route("/")
class BookingRequestList(MethodView):

    @jwt_required()
    @booking_requests_blp.arguments(BookingRequestQueryArgsSchema, location="query")
    @booking_requests_blp.response(200, BookingRequestSchema(many=True))
    def get(self, args):
        garage_id = get_current_employee().garage_id

        # A PENDING request whose preferred time has passed shouldn't stay
        # actionable - sweep before every read rather than relying on staff
        # to notice and reject it manually (see service.py).
        expire_stale_booking_requests(garage_id=garage_id)

        query = BookingRequest.query.filter_by(garage_id=garage_id)
        if args.get("status") is not None:
            query = query.filter(BookingRequest.status == args["status"])

        results = query.order_by(BookingRequest.created_at.desc()).all()
        attach_review_context(results)
        return results


@booking_requests_blp.route("/<uuid:request_id>")
class BookingRequestResource(MethodView):

    @jwt_required()
    @booking_requests_blp.response(200, BookingRequestSchema)
    def get(self, request_id):
        expire_stale_booking_requests(garage_id=get_current_employee().garage_id)
        booking_request = _get_owned_request(request_id)
        attach_review_context([booking_request])
        return booking_request


@booking_requests_blp.route("/<uuid:request_id>/approve")
class BookingRequestApprove(MethodView):

    @jwt_required()
    @booking_requests_blp.arguments(BookingRequestApproveSchema)
    @booking_requests_blp.response(200, BookingRequestSchema)
    def post(self, data, request_id):
        employee = get_current_employee()
        garage_id = employee.garage_id
        booking_request = _get_owned_request(request_id)

        if booking_request.status == "PENDING" and is_request_stale(booking_request):
            booking_request.status = "EXPIRED"
            db.session.commit()
            abort(
                409,
                message="This request's preferred time has already passed - "
                "it has expired and can no longer be approved.",
            )

        if booking_request.status != "PENDING":
            abort(
                409,
                message=f"This booking request has already been {booking_request.status.lower()}.",
            )

        # --- resolve the appointment type -------------------------------
        appointment_type_id = data.get("appointment_type_id") or booking_request.appointment_type_id
        if appointment_type_id is None:
            abort(422, message="appointment_type_id is required to create the appointment.")

        appointment_type = GarageAppointmentType.query.filter_by(
            id=appointment_type_id, garage_id=garage_id
        ).first()
        if appointment_type is None or appointment_type.status != "ACTIVE":
            abort(422, message="appointment_type_id is not an active type for this garage.")

        # --- resolve the assigned employee ----------------------------
        assigned_employee_id = data.get("employee_id")
        if assigned_employee_id is None:
            abort(422, message="employee_id is required to schedule the appointment.")
        assigned_employee = Employee.query.filter_by(
            id=assigned_employee_id, garage_id=garage_id
        ).first()
        if assigned_employee is None:
            abort(422, message="employee_id does not belong to your garage.")
        if not assigned_employee.is_active:
            abort(422, message="This employee's account is deactivated.")

        start_time, end_time = _resolve_appointment_slot(booking_request, data, appointment_type)
        _assert_no_conflict(assigned_employee_id, start_time, end_time)
        _assert_capacity_available(booking_request.garage, booking_request, start_time, end_time)

        # --- reuse-or-create the customer ------------------------------
        customer = (
            Customer.query.filter(
                Customer.garage_id == garage_id,
                Customer.email.ilike(booking_request.customer_email),
            ).first()
        )
        if customer is None:
            customer = Customer(
                garage_id=garage_id,
                first_name=booking_request.customer_first_name,
                last_name=booking_request.customer_last_name,
                email=booking_request.customer_email,
                phone=booking_request.customer_phone,
            )
            db.session.add(customer)
            db.session.flush()
        elif not customer.is_active:
            # An archived customer matched by email - bring them back rather
            # than silently creating a duplicate or letting the appointment
            # attach to a customer nobody can see in the normal list.
            customer.is_active = True

        # --- reuse-or-create the vehicle ------------------------------
        reg = _normalize_registration(booking_request.vehicle_registration)
        vehicle = Vehicle.query.filter_by(
            garage_id=garage_id, registration_number=reg
        ).first()
        if vehicle is None:
            vehicle = Vehicle(
                garage_id=garage_id,
                customer_id=customer.id,
                registration_number=booking_request.vehicle_registration,
                make=booking_request.vehicle_make,
                model=booking_request.vehicle_model,
                year=booking_request.vehicle_year,
                current_mileage=booking_request.vehicle_mileage,
            )
            db.session.add(vehicle)
            db.session.flush()
        elif vehicle.customer_id != customer.id:
            abort(
                409,
                message="A vehicle with this registration already exists for a different "
                "customer - resolve it manually before approving.",
            )
        elif not vehicle.is_active:
            vehicle.is_active = True

        # --- create the appointment ---------------------------------
        appointment = Appointment(
            garage_id=garage_id,
            employee_id=assigned_employee_id,
            customer_id=customer.id,
            vehicle_id=vehicle.id,
            appointment_type_id=appointment_type.id,
            start_time=start_time,
            end_time=end_time,
            status="BOOKED",
            notes=booking_request.notes,
            price_at_booking=appointment_type.base_price,
        )
        db.session.add(appointment)
        db.session.flush()
        snapshot_checklist_for_appointment(appointment)

        booking_request.status = "APPROVED"
        booking_request.appointment_type_id = appointment_type.id
        booking_request.customer_id = customer.id
        booking_request.vehicle_id = vehicle.id
        booking_request.appointment_id = appointment.id
        booking_request.reviewed_by_employee_id = employee.id
        booking_request.reviewed_at = datetime.now(UTC)
        if data.get("staff_notes") is not None:
            booking_request.staff_notes = data["staff_notes"]

        db.session.commit()

        attach_review_context([booking_request])
        return booking_request


@booking_requests_blp.route("/<uuid:request_id>/reject")
class BookingRequestReject(MethodView):

    @jwt_required()
    @booking_requests_blp.arguments(BookingRequestRejectSchema)
    @booking_requests_blp.response(200, BookingRequestSchema)
    def post(self, data, request_id):
        employee = get_current_employee()
        booking_request = _get_owned_request(request_id)

        if booking_request.status == "PENDING" and is_request_stale(booking_request):
            booking_request.status = "EXPIRED"
            db.session.commit()

        if booking_request.status != "PENDING":
            abort(
                409,
                message=f"This booking request has already been {booking_request.status.lower()}.",
            )

        booking_request.status = "REJECTED"
        booking_request.reviewed_by_employee_id = employee.id
        booking_request.reviewed_at = datetime.now(UTC)
        booking_request.staff_notes = data.get("staff_notes")

        db.session.commit()

        attach_review_context([booking_request])
        return booking_request
