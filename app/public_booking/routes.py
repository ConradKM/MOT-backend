from datetime import UTC, date, datetime

from flask import current_app
from flask.views import MethodView
from flask_smorest import Blueprint, abort

from app.booking_requests.reference import unique_booking_reference
from app.booking_requests.service import resolve_customer_and_vehicle
from app.communications.events import BOOKING_REQUEST_CREATED, emit_event
from app.extensions import db, limiter
from app.garages.logo import logo_public_url
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.garage import Garage
from app.payments.money import DepositConfigError, minor_to_decimal
from app.payments.providers.base import PaymentProviderError
from app.payments.service import (
    PaymentUnavailableError,
    create_deposit_hold,
    expire_stale_payment_holds,
    payment_hold_deadline,
)

from .availability import availability_range, single_day, validate_slot
from .captcha import verify_captcha
from .schemas import (
    AvailabilityQueryArgsSchema,
    AvailabilityRangeSchema,
    BookingRequestCreatedSchema,
    BookingRequestCreateSchema,
    DayAvailabilityQueryArgsSchema,
    DaySlotsSchema,
    DepositIntentCreatedSchema,
    DepositStatusSchema,
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
        garage = _get_garage_by_slug(slug)

        return {
            "id": garage.id,
            "name": garage.name,
            "slug": garage.slug,
            "logo_url": logo_public_url(garage),
            "appointment_types": [t for t in garage.appointment_types if t.status == "ACTIVE"],
        }


@public_booking_blp.route("/<slug>/availability")
class PublicGarageAvailability(MethodView):
    @limiter.limit(lambda: current_app.config["PUBLIC_AVAILABILITY_RATELIMIT"])
    @public_booking_blp.arguments(AvailabilityQueryArgsSchema, location="query")
    @public_booking_blp.response(200, AvailabilityRangeSchema)
    def get(self, args, slug):
        garage = _get_garage_by_slug(slug)
        return availability_range(garage, args.get("from_"), args.get("to"), datetime.now(UTC))


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


_SLOT_REJECTIONS = {
    "past": "That appointment time is in the past.",
    "closed": "The garage is not open at that time.",
    "outside_hours": "The garage is not open at that time.",
    "too_soon": "That time is inside the garage's minimum booking notice. Please pick a later slot.",
    "out_of_window": "That date is too far ahead to book.",
    "full": "This time is no longer available. Please select another time.",
}


def _lock_and_validate_slot(garage, data, appt_type):
    """Re-check the picked slot server-side against the same rules the
    calendar uses (opening hours, closures, minimum notice, not in the past,
    capacity, and - critically - the *selected type's* full duration) so a
    direct POST can't bypass it and a slot that looked free for a shorter
    service can't be claimed for a longer one that no longer fits. Only
    meaningful when a specific time was chosen - date-only requests carry no
    slot. Takes a per-garage row lock first so two concurrent submits (plain
    or deposit) can't both pass the capacity check. Returns the validated
    ``preferred_time`` (possibly None)."""
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
            abort(
                409,
                message=_SLOT_REJECTIONS.get(
                    reason, "This time is no longer available. Please select another time."
                ),
            )
    return preferred_time


def _build_booking_request(garage, data, appt_type, preferred_time, *, status):
    """Construct (not yet added/committed) a BookingRequest snapshotting the
    public form's submission - shared by the plain submit path (status
    PENDING) and the deposit-intent path (status AWAITING_PAYMENT)."""
    customer, vehicle = resolve_customer_and_vehicle(
        garage.id,
        customer_id=None,
        customer_email=data["customer_email"],
        first_name=data["customer_first_name"],
        last_name=data["customer_last_name"],
        phone=data.get("customer_phone"),
        vehicle_registration=data["vehicle_registration"],
        vehicle_make=data.get("vehicle_make"),
        vehicle_model=data.get("vehicle_model"),
        vehicle_year=data.get("vehicle_year"),
        vehicle_mileage=data.get("vehicle_mileage"),
    )

    return BookingRequest(
        garage_id=garage.id,
        status=status,
        booking_reference=unique_booking_reference(db.session),
        customer_id=customer.id,
        vehicle_id=vehicle.id,
        customer_first_name=data["customer_first_name"],
        customer_last_name=data["customer_last_name"],
        customer_email=data["customer_email"],
        customer_phone=data.get("customer_phone"),
        vehicle_registration=data["vehicle_registration"],
        vehicle_make=data.get("vehicle_make"),
        vehicle_model=data.get("vehicle_model"),
        vehicle_year=data.get("vehicle_year"),
        vehicle_mileage=data.get("vehicle_mileage"),
        appointment_type_id=data.get("appointment_type_id"),
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

        appt_type = _get_active_appointment_type(garage, data.get("appointment_type_id"))

        if appt_type is not None and appt_type.deposit_required:
            abort(
                422,
                message="This service requires a deposit - start the deposit payment "
                "flow instead of submitting directly.",
            )

        preferred_time = _lock_and_validate_slot(garage, data, appt_type)

        # Create (or match, by email) the customer's account + vehicle right
        # away, rather than waiting for staff to approve the request - see
        # app/booking_requests/service.py::resolve_customer_and_vehicle. The
        # appointment itself still isn't created until a staff member
        # approves and assigns it a slot/employee (see
        # app/booking_requests/routes.py::BookingRequestApprove).
        booking_request = _build_booking_request(
            garage, data, appt_type, preferred_time, status="PENDING"
        )

        db.session.add(booking_request)
        db.session.commit()

        emit_event(BOOKING_REQUEST_CREATED, garage=garage, booking_request=booking_request)

        return booking_request


@public_booking_blp.route("/<slug>/booking-requests/deposit-intent")
class DepositIntentCreate(MethodView):
    """Start a deposit-required booking: creates a short-lived
    AWAITING_PAYMENT booking request (reserving the slot exactly like a
    PENDING one - see app/public_booking/availability.py) plus a provider
    payment intent. The booking only becomes a normal, staff-visible PENDING
    request once the provider webhook confirms the deposit succeeded (see
    app/payments/service.py) - never from the browser's own say-so.
    """

    @limiter.limit(lambda: current_app.config["PUBLIC_BOOKING_RATELIMIT"])
    @public_booking_blp.arguments(BookingRequestCreateSchema)
    @public_booking_blp.response(201, DepositIntentCreatedSchema)
    def post(self, data, slug):
        garage = _get_garage_by_slug(slug)

        if not verify_captcha(data.get("captcha_token")):
            abort(400, message="CAPTCHA verification failed.")

        appt_type = _get_active_appointment_type(garage, data.get("appointment_type_id"))
        if appt_type is None or not appt_type.deposit_required:
            abort(422, message="This service does not require a deposit.")

        # Release any expired holds first so their capacity is genuinely
        # free before this one is validated against it.
        expire_stale_payment_holds(garage_id=garage.id)

        preferred_time = _lock_and_validate_slot(garage, data, appt_type)

        booking_request = _build_booking_request(
            garage, data, appt_type, preferred_time, status="AWAITING_PAYMENT"
        )
        booking_request.payment_hold_expires_at = payment_hold_deadline()

        db.session.add(booking_request)
        db.session.flush()

        try:
            payment, client_secret = create_deposit_hold(
                garage=garage, appointment_type=appt_type, booking_request=booking_request
            )
        except PaymentUnavailableError:
            db.session.rollback()
            abort(
                503,
                message="Deposit payments aren't available for this business right now. "
                "Please contact them directly to book.",
            )
        except DepositConfigError as exc:
            db.session.rollback()
            abort(422, message=str(exc))
        except PaymentProviderError:
            db.session.rollback()
            abort(502, message="Could not start the deposit payment. Please try again.")

        db.session.commit()

        base_price = appt_type.base_price
        deposit_amount = minor_to_decimal(payment.amount_minor)
        remaining_balance = (base_price - deposit_amount) if base_price is not None else None

        return {
            "booking_request_id": booking_request.id,
            "booking_reference": booking_request.booking_reference,
            "status": booking_request.status,
            "payment_status": payment.status,
            "currency": payment.currency,
            "service_total": base_price,
            "deposit_amount": deposit_amount,
            "remaining_balance": remaining_balance,
            "client_secret": client_secret,
            "publishable_key": current_app.config.get("STRIPE_PUBLISHABLE_KEY") or None,
            "provider": payment.provider,
            "hold_expires_at": booking_request.payment_hold_expires_at,
        }


@public_booking_blp.route("/<slug>/booking-requests/<reference>/payment-status")
class DepositStatus(MethodView):
    """Polling endpoint for the deposit step while waiting for the provider
    webhook to land - never itself a source of truth the frontend can use to
    declare success; it only reflects what the webhook has already recorded
    server-side (see app/payments/service.py)."""

    @limiter.limit(lambda: current_app.config["PUBLIC_AVAILABILITY_RATELIMIT"])
    @public_booking_blp.response(200, DepositStatusSchema)
    def get(self, slug, reference):
        garage = _get_garage_by_slug(slug)
        booking_request = BookingRequest.query.filter_by(
            garage_id=garage.id, booking_reference=reference
        ).first()
        if booking_request is None:
            abort(404, message="Booking not found.")

        expire_stale_payment_holds(garage_id=garage.id)
        db.session.refresh(booking_request)

        payment = booking_request.active_payment
        base_price = (
            booking_request.appointment_type.base_price
            if booking_request.appointment_type
            else None
        )
        deposit_amount = minor_to_decimal(payment.amount_minor) if payment else None
        remaining_balance = (
            (base_price - deposit_amount)
            if base_price is not None and deposit_amount is not None
            else None
        )

        return {
            "booking_request_id": booking_request.id,
            "booking_reference": booking_request.booking_reference,
            "status": booking_request.status,
            "payment_status": payment.status if payment else None,
            "currency": payment.currency if payment else None,
            "service_total": base_price,
            "deposit_amount": deposit_amount,
            "remaining_balance": remaining_balance,
            "hold_expires_at": booking_request.payment_hold_expires_at,
        }
