from datetime import UTC, date, datetime
from hashlib import sha256
from secrets import token_urlsafe

from flask import current_app, g
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
from app.garages.timezones import local_slot_as_utc
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BOOKING_REQUEST_SOURCE_WEB, BookingRequest
from app.models.garage import GARAGE_STATUS_ACTIVE, GARAGE_STATUS_TRIAL, Garage
from app.payments.money import DepositConfigError, minor_to_decimal
from app.payments.providers import get_provider
from app.payments.providers.base import PaymentProviderError
from app.payments.service import (
    PaymentUnavailableError,
    create_deposit_hold,
    expire_stale_payment_holds,
    payment_hold_deadline,
    reconcile_public_payment_recovery,
)

from .availability import _type_duration as _slot_duration_for_type
from .availability import (
    availability_range,
    resolve_settings,
    single_day,
    slot_capacity_usage,
    validate_slot,
)
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
    DepositAttemptRecoveryResponseSchema,
    DepositAttemptRecoverySchema,
    DepositIntentCreatedSchema,
    DepositStatusSchema,
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
    # A public slug is deliberately stable, so suspending or archiving a
    # tenant must close *every* unauthenticated booking route that resolves
    # it.  Returning the same 404 as an unknown slug avoids advertising a
    # business's platform status while preventing new availability probes,
    # booking requests, payment holds, and status polling for an offline
    # tenant.  Trial tenants are intentionally public: they are live
    # businesses until platform administration says otherwise.
    if garage is None or garage.status not in (GARAGE_STATUS_ACTIVE, GARAGE_STATUS_TRIAL):
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
        # A hold whose 15-minute window has already passed still counts as
        # AWAITING_PAYMENT (and so still occupies capacity - see
        # availability.py's module docs) until something flips it to EXPIRED;
        # otherwise this endpoint would only ever release it once someone
        # happens to hit the deposit-intent/status-poll endpoints for this
        # garage, which could be long after this calendar is what a customer
        # is actually looking at right now.
        expire_stale_payment_holds(garage_id=garage.id)
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
        # See PublicGarageAvailability.get - same reasoning: release a
        # time-expired hold's capacity before computing what to show, rather
        # than only ever doing so as a side effect of a deposit attempt.
        expire_stale_payment_holds(garage_id=garage.id)
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
            # Full diagnostic context on every rejection - a byte-count in
            # the access log can't tell two different reasons apart, and an
            # advertised-then-rejected report is otherwise nearly
            # impossible to root-cause after the fact (the exact capacity
            # snapshot at rejection time is gone by the time anyone looks).
            extra = ""
            if reason == "full":
                duration = _slot_duration_for_type(appt_type, resolve_settings(garage))
                used, capacity = slot_capacity_usage(
                    garage,
                    data["preferred_date"],
                    local_slot_as_utc(garage, data["preferred_date"], preferred_time),
                    duration,
                )
                extra = f" used={used} capacity={capacity} duration={duration}"
            current_app.logger.warning(
                "AVAILABILITY_REJECTED request_id=%s garage=%s appointment_type=%s date=%s "
                "time=%s reason=%s%s",
                g.get("request_id"),
                garage.id,
                appt_type.id if appt_type is not None else None,
                data["preferred_date"],
                preferred_time,
                reason,
                extra,
            )
            abort(
                409,
                message=_SLOT_REJECTIONS.get(
                    reason, "This time is no longer available. Please select another time."
                ),
                # A machine-readable reason alongside the human message - see
                # MOT-frontend's DepositStep, which today maps every 409 on
                # this endpoint to one generic "slot unavailable" string.
                # That happens to be correct while this is the only 409
                # source in this call chain, but `errors.reason` lets a
                # future distinct conflict (or the frontend) tell them apart
                # without guessing from the message text.
                errors={"reason": reason},
            )
    return preferred_time


def _validate_answers_or_abort(garage, data, appointment_type_id):
    """Check the business's own configured questions before anything is
    created, so a rejected answer never leaves a half-built customer behind.

    The booking page renders from this same configuration, but that is a
    convenience: a direct POST must not be able to skip a required field,
    invent one, or answer a SELECT with something never on offer.
    """
    try:
        return validate_answers(garage.id, appointment_type_id, data.get("answers") or [])
    except AnswerError as exc:
        # Same shape flask-smorest uses for schema-level failures, keyed by
        # field id - which is what the form renders from.
        abort(422, message="Please check the highlighted answers.", errors={"json": exc.messages})


def _build_booking_request(garage, data, appt_type, preferred_time, *, status, answers):
    """Construct (not yet added/committed) a BookingRequest snapshotting the
    public form's submission - shared by the plain submit path (status
    PENDING) and the deposit-intent path (status AWAITING_PAYMENT)."""
    item = tracked_item_kwargs(bound_values(answers))

    # A business that tracks the thing it books in binds a field to the item's
    # reference (see app/models/booking_flow/field.py); one that tracks
    # nothing collects none and `item` is empty. The top-level vehicle_* keys
    # are the pre-workflow client's shape, still accepted so it keeps working -
    # a binding wins wherever both are present.
    reference = item.get("reference") or data.get("vehicle_registration")
    make = item.get("make") or data.get("vehicle_make")
    model = item.get("model") or data.get("vehicle_model")
    year = item.get("year") or data.get("vehicle_year")
    usage = item.get("usage") or data.get("vehicle_mileage")

    customer, vehicle = resolve_customer_and_vehicle(
        garage.id,
        customer_id=None,
        customer_email=data["customer_email"],
        first_name=data["customer_first_name"],
        last_name=data["customer_last_name"],
        phone=data.get("customer_phone"),
        vehicle_registration=reference,
        vehicle_make=make,
        vehicle_model=model,
        vehicle_year=year,
        vehicle_mileage=usage,
    )

    return BookingRequest(
        garage_id=garage.id,
        status=status,
        booking_reference=unique_booking_reference(db.session),
        # The browser-generated attempt id is an idempotency key for either
        # public submission mode.  Deposit flows additionally use it to
        # resume their provider session; plain bookings use it to make a
        # retried POST return the same pending request rather than reserve a
        # second slot.
        payment_attempt_id=data.get("payment_attempt_id"),
        customer_id=customer.id,
        # None when the business tracks no item - the column is nullable for
        # exactly that.
        vehicle_id=None if vehicle is None else vehicle.id,
        customer_first_name=data["customer_first_name"],
        customer_last_name=data["customer_last_name"],
        customer_email=data["customer_email"],
        customer_phone=data.get("customer_phone"),
        # Denormalised onto the request as well as the item record, so staff
        # review reads what was submitted even if the item is edited later.
        vehicle_registration=reference,
        vehicle_make=make,
        vehicle_model=model,
        vehicle_year=year,
        vehicle_mileage=usage,
        appointment_type_id=data.get("appointment_type_id"),
        # Snapshot what the customer actually saw/chose, so staff review
        # (and history, if the type is edited or removed later) reflects
        # the real request rather than the type's current configuration.
        requested_duration_minutes=(
            appt_type.default_duration_minutes if appt_type is not None else None
        ),
        requested_price=appt_type.base_price if appt_type is not None else None,
        requested_appointment_type_name=appt_type.name if appt_type is not None else None,
        preferred_date=data["preferred_date"],
        preferred_time=preferred_time,
        preferred_employee_note=data.get("preferred_employee_note"),
        notes=data.get("notes"),
    )


def _deposit_response(booking_request, payment, session, *, recovery_token=None):
    """Build the client-safe deposit envelope for a new or resumed hold."""
    # A retry must describe the immutable request/payment snapshot, not a
    # service record that staff may have edited after the customer began
    # checkout.  This is the same historical-price rule used by staff
    # approval and prevents a resumed attempt from displaying a different
    # total from the amount its provider intent actually represents.
    base_price = booking_request.requested_price
    deposit_amount = minor_to_decimal(payment.amount_minor)
    remaining_balance = (base_price - deposit_amount) if base_price is not None else None
    response = {
        "booking_request_id": booking_request.id,
        "booking_reference": booking_request.booking_reference,
        "status": booking_request.status,
        "payment_status": payment.status,
        "currency": payment.currency,
        "service_total": base_price,
        "deposit_amount": deposit_amount,
        "remaining_balance": remaining_balance,
        "provider": payment.provider,
        "checkout_mode": session.checkout_mode,
        "provider_data": session.provider_data,
        "hold_expires_at": booking_request.payment_hold_expires_at,
    }
    # This capability is emitted only when an attempt is first created.  It
    # must not be reconstructable from the client-generated idempotency UUID,
    # and must never be written to logs or persisted alongside provider data.
    if recovery_token is not None:
        response["recovery_token"] = recovery_token
    return response


def _recovery_response(booking_request, payment, session=None):
    """Return the immutable server snapshot for one bearer-authorised
    recovery attempt.  A normal booking reference/status endpoint cannot do
    this because it is intentionally guess-resistant only by reference and
    must never disclose customer data or a reusable client secret.
    """
    base_price = booking_request.requested_price
    deposit_amount = minor_to_decimal(payment.amount_minor) if payment else None
    remaining_balance = (
        base_price - deposit_amount
        if base_price is not None and deposit_amount is not None
        else None
    )
    appointment_type = booking_request.appointment_type
    return {
        "booking_reference": booking_request.booking_reference,
        "status": booking_request.status,
        "payment_status": payment.status if payment else None,
        "currency": payment.currency if payment else None,
        "service_total": base_price,
        "deposit_amount": deposit_amount,
        "remaining_balance": remaining_balance,
        "hold_expires_at": booking_request.payment_hold_expires_at,
        "appointment_type_id": booking_request.appointment_type_id,
        "appointment_type_name": (
            booking_request.requested_appointment_type_name
            or (appointment_type.name if appointment_type else None)
        ),
        "preferred_date": booking_request.preferred_date,
        "preferred_time": booking_request.preferred_time,
        "requested_duration_minutes": booking_request.requested_duration_minutes,
        "customer_first_name": booking_request.customer_first_name,
        "customer_last_name": booking_request.customer_last_name,
        "customer_email": booking_request.customer_email,
        "customer_phone": booking_request.customer_phone,
        "vehicle_registration": booking_request.vehicle_registration,
        "vehicle_make": booking_request.vehicle_make,
        "vehicle_model": booking_request.vehicle_model,
        "vehicle_year": booking_request.vehicle_year,
        "vehicle_mileage": booking_request.vehicle_mileage,
        "answers": [
            {
                "field_id": str(answer.booking_flow_field_id),
                "value": answer.value,
                "values": answer.value_list,
            }
            for answer in booking_request.answers
            if answer.booking_flow_field_id is not None
        ],
        # Do not make a previously-paid booking pay again.  A client-safe
        # provider session is only meaningful for the sole active checkout.
        "provider": payment.provider if session is not None else None,
        "checkout_mode": session.checkout_mode if session is not None else None,
        "provider_data": session.provider_data if session is not None else None,
    }


def _existing_deposit_attempt(garage, attempt_id, appt_type, data):
    """Resume an active attempt before normal capacity revalidation.

    The caller holds the garage row lock, so an initial request and its retry
    are serialised.  Other attempt ids still run normal validation and remain
    blocked by this request's AWAITING_PAYMENT hold.
    """
    if attempt_id is None:
        return None
    booking_request = BookingRequest.query.filter_by(
        garage_id=garage.id, payment_attempt_id=attempt_id
    ).first()
    if booking_request is None or booking_request.status != "AWAITING_PAYMENT":
        return None
    # The browser token is an idempotency key for one exact checkout intent,
    # not a reusable capability to mutate an existing reservation.  Resuming
    # it after a service/date/time change would otherwise return the old
    # payment session while the UI is showing the new selection.
    if (
        booking_request.appointment_type_id != appt_type.id
        or booking_request.preferred_date != data["preferred_date"]
        or booking_request.preferred_time != data.get("preferred_time")
    ):
        abort(
            409,
            message="This payment attempt belongs to a different service or appointment time. Please start a new payment attempt.",
            errors={"reason": "payment_attempt_mismatch"},
        )
    payment = booking_request.active_payment
    if payment is None or payment.provider_payment_id is None:
        return None
    try:
        session = get_provider(
            payment.provider, connected_account_id=payment.provider_account_id
        ).get_payment_status(payment.provider_payment_id)
    except PaymentProviderError:
        abort(502, message="Could not resume the deposit payment. Please try again.")
    return _deposit_response(booking_request, payment, session)


def _existing_plain_booking_attempt(garage, attempt_id, appt_type, data):
    """Return the one existing plain submission for a browser attempt.

    A single opaque attempt id is deliberately shared by both public flows,
    but it is bound to immutable booking selections.  It cannot be replayed
    to create a second request or silently substitute another service/slot.
    """
    if attempt_id is None:
        return None
    booking_request = BookingRequest.query.filter_by(
        garage_id=garage.id, payment_attempt_id=attempt_id
    ).first()
    if booking_request is None:
        return None
    if (
        booking_request.appointment_type_id != (appt_type.id if appt_type else None)
        or booking_request.preferred_date != data["preferred_date"]
        or booking_request.preferred_time != data.get("preferred_time")
        or booking_request.status == "AWAITING_PAYMENT"
    ):
        abort(
            409,
            message="This booking attempt belongs to a different service or appointment time. Please start a new booking attempt.",
            errors={"reason": "booking_attempt_mismatch"},
        )
    return booking_request


def _matching_active_plain_request(garage, appt_type, data, answers):
    """Find the active request an accidental no-token retry would duplicate.

    Older/current browser clients may not yet send an attempt UUID for an
    ordinary (no-deposit) form.  Treat the exact same caller, item, service,
    date and time as one active intent while it reserves capacity; this is a
    conservative server-side fallback, not a replacement for the explicit
    token.  Different vehicle/customer/service/slot selections remain
    independent bookings.
    """
    item = tracked_item_kwargs(bound_values(answers))
    submitted_vehicle_reference = item.get("reference") or data.get("vehicle_registration")
    candidate = (
        BookingRequest.query.filter_by(
            garage_id=garage.id,
            source=BOOKING_REQUEST_SOURCE_WEB,
            status="PENDING",
            customer_email=data["customer_email"],
            customer_phone=data.get("customer_phone"),
            vehicle_registration=submitted_vehicle_reference,
            appointment_type_id=appt_type.id if appt_type else None,
            preferred_date=data["preferred_date"],
            preferred_time=data.get("preferred_time"),
        )
        .order_by(BookingRequest.created_at.desc())
        .first()
    )
    if candidate is None:
        return None

    # The fallback is only for a byte-for-byte-equivalent submission from an
    # older browser that has no explicit idempotency token.  Returning a prior
    # request after the caller corrected notes, their name, or a booking-form
    # answer would silently discard customer data.  In that case let ordinary
    # availability validation return its truthful result instead.
    item_snapshot = {
        "vehicle_make": item.get("make") or data.get("vehicle_make"),
        "vehicle_model": item.get("model") or data.get("vehicle_model"),
        "vehicle_year": item.get("year") or data.get("vehicle_year"),
        "vehicle_mileage": item.get("usage") or data.get("vehicle_mileage"),
    }
    submitted_scalars = {
        "customer_first_name": data["customer_first_name"],
        "customer_last_name": data["customer_last_name"],
        "preferred_employee_note": data.get("preferred_employee_note"),
        "notes": data.get("notes"),
        **item_snapshot,
    }
    if any(getattr(candidate, key) != value for key, value in submitted_scalars.items()):
        return None

    submitted_answers = [
        (str(row["field"].id), row["value"], tuple(row["value_list"])) for row in answers
    ]
    stored_answers = [
        (str(row.booking_flow_field_id), row.value, tuple(row.value_list))
        for row in sorted(candidate.answers, key=lambda row: row.order)
    ]
    return candidate if stored_answers == submitted_answers else None


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

        # Serialise the idempotency lookup, customer/vehicle resolution and
        # eventual insertion even for date-only requests (which do not take
        # the slot-validation lock below).  This is also the database-side
        # counterpart to the unique payment_attempt_id constraint.
        db.session.query(Garage).filter_by(id=garage.id).with_for_update().one()
        existing = _existing_plain_booking_attempt(
            garage, data.get("payment_attempt_id"), appt_type, data
        )
        if existing is not None:
            return existing

        resolved_answers = _validate_answers_or_abort(garage, data, data.get("appointment_type_id"))

        if data.get("payment_attempt_id") is None:
            existing_duplicate = _matching_active_plain_request(
                garage, appt_type, data, resolved_answers
            )
            if existing_duplicate is not None:
                return existing_duplicate

        preferred_time = _lock_and_validate_slot(garage, data, appt_type)

        # Create (or match, by email) the customer's account + vehicle right
        # away, rather than waiting for staff to approve the request - see
        # app/booking_requests/service.py::resolve_customer_and_vehicle. The
        # appointment itself still isn't created until a staff member
        # approves and assigns it a slot/employee (see
        # app/booking_requests/routes.py::BookingRequestApprove).
        booking_request = _build_booking_request(
            garage, data, appt_type, preferred_time, status="PENDING", answers=resolved_answers
        )

        db.session.add(booking_request)
        db.session.flush()
        persist_answers(booking_request, resolved_answers)
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

        resolved_answers = _validate_answers_or_abort(garage, data, data.get("appointment_type_id"))

        # Release any expired holds first so their capacity is genuinely
        # free before this one is validated against it.
        expire_stale_payment_holds(garage_id=garage.id)

        # Serialise lookup/create with the same lock the capacity check uses.
        # A retried browser request resumes its own active hold instead of
        # treating it as a competing booking; every different attempt still
        # receives the ordinary capacity check below.
        db.session.query(Garage).filter_by(id=garage.id).with_for_update().one()
        existing = _existing_deposit_attempt(
            garage, data.get("payment_attempt_id"), appt_type, data
        )
        if existing is not None:
            return existing

        preferred_time = _lock_and_validate_slot(garage, data, appt_type)

        booking_request = _build_booking_request(
            garage,
            data,
            appt_type,
            preferred_time,
            status="AWAITING_PAYMENT",
            answers=resolved_answers,
        )
        booking_request.payment_hold_expires_at = payment_hold_deadline()
        recovery_token = token_urlsafe(32)
        booking_request.payment_recovery_token_hash = sha256(recovery_token.encode()).hexdigest()

        db.session.add(booking_request)
        db.session.flush()
        persist_answers(booking_request, resolved_answers)

        try:
            payment, session = create_deposit_hold(
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

        return _deposit_response(booking_request, payment, session, recovery_token=recovery_token)


@public_booking_blp.route("/<slug>/booking-requests/deposit-attempt/recover")
class DepositAttemptRecovery(MethodView):
    """Recover a deposit checkout strictly from the server-owned request.

    The token is a high-entropy, one-purpose capability, stored only as a
    hash.  It is scoped to the garage in this route and deliberately posted
    in the body rather than included in a bookmarkable URL.  Unknown tokens
    and cross-tenant tokens both yield the same 404 to prevent enumeration.
    """

    @limiter.limit(lambda: current_app.config["PUBLIC_BOOKING_RATELIMIT"])
    @public_booking_blp.arguments(DepositAttemptRecoverySchema)
    @public_booking_blp.response(200, DepositAttemptRecoveryResponseSchema)
    def post(self, data, slug):
        garage = _get_garage_by_slug(slug)
        token_hash = sha256(data["recovery_token"].encode()).hexdigest()

        # Use the existing sweeper: it serialises with payment success and
        # reconciles a provider success before ever releasing a stale hold.
        expire_stale_payment_holds(garage_id=garage.id)
        booking_request = BookingRequest.query.filter_by(
            garage_id=garage.id, payment_recovery_token_hash=token_hash
        ).first()
        if booking_request is None:
            abort(404, message="Booking recovery attempt not found.")

        booking_request = reconcile_public_payment_recovery(booking_request)

        payment = booking_request.active_payment
        session = None
        if (
            booking_request.status == "AWAITING_PAYMENT"
            and payment is not None
            and payment.provider_payment_id is not None
            and payment.status in ("REQUIRES_PAYMENT", "PENDING")
        ):
            try:
                session = get_provider(
                    payment.provider, connected_account_id=payment.provider_account_id
                ).get_payment_status(payment.provider_payment_id)
            except PaymentProviderError:
                abort(502, message="Could not resume the deposit payment. Please try again.")

        return _recovery_response(booking_request, payment, session)


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
        # Keep polling responses aligned with the immutable checkout request,
        # not a price an owner may edit after the customer has paid.
        base_price = booking_request.requested_price
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
