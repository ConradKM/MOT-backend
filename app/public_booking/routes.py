from datetime import UTC, date, datetime

from flask import current_app
from flask.views import MethodView
from flask_smorest import Blueprint, abort

from app.booking_flow.answers import (
    AnswerError,
    bound_values,
    persist_answers,
    tracked_item_kwargs,
    validate_answers,
)
from app.booking_flow.resolve import resolve_sections
from app.booking_requests.reference import unique_booking_reference
from app.booking_requests.service import resolve_customer_and_vehicle
from app.communications.events import BOOKING_REQUEST_CREATED, emit_event
from app.extensions import db, limiter
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.garage import Garage

from .availability import availability_range, single_day, validate_slot
from .captcha import verify_captcha
from .payload import public_garage_payload
from .schemas import (
    AvailabilityQueryArgsSchema,
    AvailabilityRangeSchema,
    BookingFlowQueryArgsSchema,
    BookingRequestCreatedSchema,
    BookingRequestCreateSchema,
    DayAvailabilityQueryArgsSchema,
    DaySlotsSchema,
    PublicBookingFlowSchema,
    PublicGarageDetailSchema,
)

public_booking_blp = Blueprint(
    "public_booking",
    "public_booking",
    url_prefix="/api/public",
    description="Unauthenticated customer booking - business lookup by slug, "
    "server-computed availability, and booking-request submission",
)


def _get_garage_by_slug(slug):
    garage = Garage.query.filter_by(slug=slug).first()
    if garage is None:
        abort(404, message="Garage not found")
    return garage


def _get_active_appointment_type(garage, appointment_type_id):
    """Resolve + validate a customer-selected appointment type, or None if
    they haven't chosen one (a garage with none configured, or a step the
    customer hasn't reached yet)."""
    if appointment_type_id is None:
        return None
    appt_type = GarageAppointmentType.query.filter_by(
        id=appointment_type_id, garage_id=garage.id
    ).first()
    if appt_type is None or appt_type.status != "ACTIVE":
        abort(422, message="appointment_type_id is not an active type for this business.")
    return appt_type


@public_booking_blp.route("/<slug>")
class PublicGarageBySlug(MethodView):
    @public_booking_blp.response(200, PublicGarageDetailSchema)
    def get(self, slug):
        return public_garage_payload(_get_garage_by_slug(slug))


@public_booking_blp.route("/<slug>/availability")
class PublicGarageAvailability(MethodView):
    @limiter.limit(lambda: current_app.config["PUBLIC_AVAILABILITY_RATELIMIT"])
    @public_booking_blp.arguments(AvailabilityQueryArgsSchema, location="query")
    @public_booking_blp.response(200, AvailabilityRangeSchema)
    def get(self, args, slug):
        garage = _get_garage_by_slug(slug)
        appt_type = _get_active_appointment_type(garage, args.get("appointment_type_id"))
        return availability_range(
            garage,
            args.get("from_"),
            args.get("to"),
            datetime.now(UTC),
            appointment_type=appt_type,
        )


@public_booking_blp.route("/<slug>/availability/<day>")
class PublicGarageDayAvailability(MethodView):
    @limiter.limit(lambda: current_app.config["PUBLIC_AVAILABILITY_RATELIMIT"])
    @public_booking_blp.arguments(DayAvailabilityQueryArgsSchema, location="query")
    @public_booking_blp.response(200, DaySlotsSchema)
    def get(self, args, slug, day):
        garage = _get_garage_by_slug(slug)
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            abort(422, message="day must be an ISO date (YYYY-MM-DD).")
        appt_type = _get_active_appointment_type(garage, args.get("appointment_type_id"))
        return single_day(garage, parsed, datetime.now(UTC), appointment_type=appt_type)


@public_booking_blp.route("/<slug>/booking-requests")
class BookingRequestSubmit(MethodView):
    # Rate-limit before parsing anything. Storage / on-off / the limit string
    # itself are all config-driven (see app/config.py + app/extensions.py).
    @limiter.limit(lambda: current_app.config["PUBLIC_BOOKING_RATELIMIT"])
    @public_booking_blp.arguments(BookingRequestCreateSchema)
    @public_booking_blp.response(201, BookingRequestCreatedSchema)
    def post(self, data, slug):
        garage = _get_garage_by_slug(slug)

        if not verify_captcha(data.get("captcha_token")):
            abort(400, message="CAPTCHA verification failed.")

        appointment_type_id = data.get("appointment_type_id")
        appt_type = _get_active_appointment_type(garage, appointment_type_id)

        # Re-check the picked slot server-side against the same rules the
        # calendar uses (opening hours, closures, minimum notice, not in the
        # past, capacity, and - critically - the *selected type's* full
        # duration) so a direct POST can't bypass it and a slot that looked
        # free for a shorter service can't be claimed for a longer one that
        # no longer fits. Only meaningful when a specific time was chosen -
        # date-only requests carry no slot. Take a per-garage row lock first
        # so two concurrent submits can't both pass the capacity check.
        preferred_time = data.get("preferred_time")
        if preferred_time is not None:
            db.session.query(Garage).filter_by(id=garage.id).with_for_update().one()
            reason = validate_slot(
                garage,
                data["preferred_date"],
                preferred_time,
                datetime.now(UTC),
                appointment_type=appt_type,
            )
            if reason is not None:
                _SLOT_REJECTIONS = {
                    "past": "That appointment time is in the past.",
                    "closed": "The garage is not open at that time.",
                    "outside_hours": "The garage is not open at that time.",
                    "too_soon": "That time is inside the garage's minimum "
                    "booking notice. Please pick a later slot.",
                    "out_of_window": "That date is too far ahead to book.",
                    "full": "This time is no longer available. Please select another time.",
                }
                abort(
                    409,
                    message=_SLOT_REJECTIONS.get(
                        reason,
                        "This time is no longer available. Please select another time.",
                    ),
                )

        # Validate the business's own configured questions before anything
        # is created. The client renders the form from this same
        # configuration, but that is a convenience: a direct POST must not be
        # able to skip a required field or invent one. Raises a 422 with
        # per-field messages.
        try:
            resolved_answers = validate_answers(
                garage.id, appointment_type_id, data.get("answers") or []
            )
        except AnswerError as exc:
            # Reported in the same shape flask-smorest uses for schema-level
            # failures, so the client has one error format to handle rather
            # than two. Keyed by field id, which is what the form renders from.
            abort(
                422, message="Please check the highlighted answers.", errors={"json": exc.messages}
            )
        item = tracked_item_kwargs(bound_values(resolved_answers))

        # A business that tracks the thing it books in binds a field to the
        # item's reference (see app/models/booking_flow/field.py); one that
        # tracks nothing collects none and `item` is empty. The top-level
        # vehicle_* keys are the pre-workflow client's shape, still accepted
        # so it keeps working - a binding wins wherever both are present.
        reference = item.get("reference") or data.get("vehicle_registration")

        # Create (or match, by email) the customer's account - and the tracked
        # item, when there is one - right away, rather than waiting for staff
        # to approve the request; see
        # app/booking_requests/service.py::resolve_customer_and_vehicle. The
        # appointment itself still isn't created until a staff member
        # approves and assigns it a slot/employee (see
        # app/booking_requests/routes.py::BookingRequestApprove).
        customer, vehicle = resolve_customer_and_vehicle(
            garage.id,
            customer_id=None,
            customer_email=data["customer_email"],
            first_name=data["customer_first_name"],
            last_name=data["customer_last_name"],
            phone=data.get("customer_phone"),
            vehicle_registration=reference,
            vehicle_make=item.get("make") or data.get("vehicle_make"),
            vehicle_model=item.get("model") or data.get("vehicle_model"),
            vehicle_year=item.get("year") or data.get("vehicle_year"),
            vehicle_mileage=item.get("usage") or data.get("vehicle_mileage"),
        )

        booking_request = BookingRequest(
            garage_id=garage.id,
            status="PENDING",
            booking_reference=unique_booking_reference(db.session),
            customer_id=customer.id,
            vehicle_id=None if vehicle is None else vehicle.id,
            customer_first_name=data["customer_first_name"],
            customer_last_name=data["customer_last_name"],
            customer_email=data["customer_email"],
            customer_phone=data.get("customer_phone"),
            # Denormalised onto the request as well as the item record, so the
            # staff review screen reads what was actually submitted even if
            # the item is later edited. All None for a business that tracks
            # no item.
            vehicle_registration=reference,
            vehicle_make=item.get("make") or data.get("vehicle_make"),
            vehicle_model=item.get("model") or data.get("vehicle_model"),
            vehicle_year=item.get("year") or data.get("vehicle_year"),
            vehicle_mileage=item.get("usage") or data.get("vehicle_mileage"),
            appointment_type_id=appointment_type_id,
            # Snapshot what the customer actually saw/chose, so staff review
            # (and history, if the type is edited or removed later) reflects
            # the real request rather than the type's current configuration.
            requested_duration_minutes=(
                appt_type.default_duration_minutes if appt_type is not None else None
            ),
            requested_price=appt_type.base_price if appt_type is not None else None,
            preferred_date=data["preferred_date"],
            preferred_time=preferred_time,
            preferred_employee_note=data.get("preferred_employee_note"),
            notes=data.get("notes"),
        )

        db.session.add(booking_request)
        db.session.flush()
        persist_answers(booking_request, resolved_answers)
        db.session.commit()

        emit_event(BOOKING_REQUEST_CREATED, garage=garage, booking_request=booking_request)

        return booking_request


@public_booking_blp.route("/<slug>/booking-flow")
class PublicBookingFlow(MethodView):
    @limiter.limit(lambda: current_app.config["PUBLIC_AVAILABILITY_RATELIMIT"])
    @public_booking_blp.arguments(BookingFlowQueryArgsSchema, location="query")
    @public_booking_blp.response(200, PublicBookingFlowSchema)
    def get(self, args, slug):
        """The questions this business asks for the chosen service.

        Resolved server-side (business default, or the service's own override)
        so the booking page and the submission validator can never disagree
        about what the customer was asked - see app/booking_flow/resolve.py.
        """
        garage = _get_garage_by_slug(slug)
        appointment_type_id = args.get("appointment_type_id")
        # Validated even though only its id is used: a customer must not be
        # able to probe which services exist by watching the form change.
        _get_active_appointment_type(garage, appointment_type_id)

        return {
            "appointment_type_id": appointment_type_id,
            "sections": resolve_sections(garage.id, appointment_type_id),
        }
