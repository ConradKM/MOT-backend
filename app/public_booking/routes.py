from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from secrets import token_urlsafe

from flask import current_app, g, request
from flask.views import MethodView
from flask_smorest import Blueprint, abort

from app.appointments.add_ons.service import (
    ResolvedAddOn,
    duration_delta,
    parse_query_selection,
    price_delta,
    resolve_selection,
    selection_signature,
    snapshot_kwargs,
    total_duration,
    total_price,
)
from app.booking_flow.answers import (
    AnswerError,
    bound_values,
    persist_answers,
    tracked_item_kwargs,
    validate_answers,
)
from app.booking_flow.resolve import resolve_sections
from app.booking_requests.reference import unique_booking_reference
from app.booking_requests.service import auto_accept_booking_request, resolve_customer_and_vehicle
from app.communications.events import BOOKING_REQUEST_CREATED, emit_event
from app.extensions import db, limiter
from app.garages.timezones import local_slot_as_utc
from app.loyalty.codes import normalize_code
from app.loyalty.service import (
    DiscountFailure,
    DiscountResult,
    apply_snapshot,
    evaluate_code,
    find_customer_by_email,
    log_rejection,
    redeem,
)
from app.models.appointments.applied_add_on import BookingRequestAddOn
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BOOKING_REQUEST_SOURCE_WEB, BookingRequest
from app.models.garage import GARAGE_STATUS_ACTIVE, GARAGE_STATUS_TRIAL, Garage
from app.models.loyalty.discount_redemption import REDEMPTION_CONSUMED, REDEMPTION_RESERVED
from app.payments.money import (
    PENCE_PER_POUND,
    DepositConfigError,
    calculate_deposit_minor,
    minor_to_decimal,
)
from app.payments.providers.base import PaymentProviderError, ProviderNotConfigured
from app.payments.service import (
    HoldNotAbandonable,
    PaymentSubmissionError,
    PaymentUnavailableError,
    abandon_public_hold,
    create_deposit_hold,
    expire_stale_payment_holds,
    payment_hold_deadline,
    reconcile_public_payment_recovery,
    reconcile_submitted_payment,
    resume_payment_session,
    submit_client_payment_token,
)
from app.terms.service import require_web_acceptance, stamp_acceptance

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
    DiscountValidateSchema,
    DiscountValidationResultSchema,
    PaymentTokenSubmissionSchema,
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


def _resolve_add_ons(appt_type, selection) -> list[ResolvedAddOn]:
    if not selection:
        return []
    if appt_type is None:
        abort(422, message="add_ons require an appointment_type_id.")
    return resolve_selection(appt_type, selection)


def _selected_duration(garage, appt_type, add_ons) -> int | None:
    """The job's real length once add-ons are applied - what availability
    and slot validation must check against, so an add-on that lengthens the
    job can never be booked into a slot that only fits the base service.
    None (use the type's own default) when there are no add-ons."""
    if not add_ons:
        return None
    return total_duration(
        _slot_duration_for_type(appt_type, resolve_settings(garage)), duration_delta(add_ons)
    )


def _pre_discount_total(appt_type, add_ons) -> Decimal | None:
    if appt_type is None:
        return None
    return total_price(appt_type.base_price, price_delta(add_ons))


def _abort_discount(garage, failure: DiscountFailure):
    """One generic message for every reason a code could be probed by
    (unknown, expired, used, disabled, someone else's) - see
    app/loyalty/service.py::GENERIC_FAILURES."""
    log_rejection(garage.id, failure, channel="public")
    abort(
        422,
        message=failure.public_message,
        errors={
            "json": {"discount_code": [failure.public_message]},
            "reason": "discount_code_invalid",
        },
    )


def _evaluate_public_discount(garage, data, appt_type, add_ons, *, lock) -> DiscountResult | None:
    """The authoritative check behind a submission's ``discount_code``, or
    None when none was sent. The customer is the one this booking will
    resolve to by email, without creating them."""
    code = (data.get("discount_code") or "").strip()
    if not code:
        return None
    result = evaluate_code(
        garage,
        code,
        customer=find_customer_by_email(garage.id, data.get("customer_email")),
        appointment_type=appt_type,
        total=_pre_discount_total(appt_type, add_ons),
        lock=lock,
    )
    if isinstance(result, DiscountFailure):
        _abort_discount(garage, result)
    assert isinstance(result, DiscountResult)
    return result


def _redeem_public(garage, discount, booking_request, status):
    redemption = redeem(
        garage,
        discount,
        customer_id=booking_request.customer_id,
        status=status,
        booking_request=booking_request,
    )
    if isinstance(redemption, DiscountFailure):
        db.session.rollback()
        _abort_discount(garage, redemption)


def _same_discount_code(booking_request, data) -> bool:
    return normalize_code(booking_request.discount_code) == normalize_code(
        data.get("discount_code")
    )


def _deposit_preview(appt_type, total: Decimal) -> tuple[bool, Decimal | None]:
    """(deposit still required?, deposit amount) for a discounted total -
    the same calculation and cap as app/payments/service.py::create_deposit_hold."""
    if appt_type is None or not appt_type.deposit_required or total <= 0:
        return False, None
    assert appt_type.deposit_type is not None and appt_type.deposit_value is not None
    minor = calculate_deposit_minor(
        deposit_type=appt_type.deposit_type,
        deposit_value=appt_type.deposit_value,
        base_price=total,
    )
    minor = min(minor, int(total * PENCE_PER_POUND))
    return True, minor_to_decimal(minor)


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
        add_ons = _resolve_add_ons(appt_type, parse_query_selection(args.get("add_ons")))
        return availability_range(
            garage,
            args.get("from_"),
            args.get("to"),
            datetime.now(UTC),
            appointment_type=appt_type,
            duration_min=_selected_duration(garage, appt_type, add_ons),
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
        add_ons = _resolve_add_ons(appt_type, parse_query_selection(args.get("add_ons")))
        return single_day(
            garage,
            parsed,
            datetime.now(UTC),
            appointment_type=appt_type,
            duration_min=_selected_duration(garage, appt_type, add_ons),
        )


_SLOT_REJECTIONS = {
    "past": "That appointment time is in the past.",
    "closed": "The garage is not open at that time.",
    "outside_hours": "The garage is not open at that time.",
    "too_soon": "That time is inside the garage's minimum booking notice. Please pick a later slot.",
    "out_of_window": "That date is too far ahead to book.",
    "full": "This time is no longer available. Please select another time.",
}


def _lock_and_validate_slot(garage, data, appt_type, add_ons):
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
        selected_duration = _selected_duration(garage, appt_type, add_ons)
        reason = validate_slot(
            garage,
            data["preferred_date"],
            preferred_time,
            datetime.now(UTC),
            appointment_type=appt_type,
            duration_min=selected_duration,
        )
        if reason is not None:
            # Full diagnostic context on every rejection - a byte-count in
            # the access log can't tell two different reasons apart, and an
            # advertised-then-rejected report is otherwise nearly
            # impossible to root-cause after the fact (the exact capacity
            # snapshot at rejection time is gone by the time anyone looks).
            extra = ""
            if reason == "full":
                duration = selected_duration or _slot_duration_for_type(
                    appt_type, resolve_settings(garage)
                )
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


def _build_booking_request(
    garage, data, appt_type, add_ons, preferred_time, *, status, answers, discount=None
):
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

    booking_request = BookingRequest(
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
        # Add-ons are folded into both, so staff review, approval and the
        # capacity this request reserves all see the real job.
        requested_duration_minutes=(
            _selected_duration(garage, appt_type, add_ons)
            or (appt_type.default_duration_minutes if appt_type is not None else None)
        ),
        requested_price=(
            total_price(appt_type.base_price, price_delta(add_ons))
            if appt_type is not None
            else None
        ),
        requested_appointment_type_name=appt_type.name if appt_type is not None else None,
        add_ons=[BookingRequestAddOn(**snapshot_kwargs(row, garage.id)) for row in add_ons],
        preferred_date=data["preferred_date"],
        preferred_time=preferred_time,
        preferred_employee_note=data.get("preferred_employee_note"),
        notes=data.get("notes"),
    )
    if discount is not None:
        # requested_price becomes the discounted total - what the deposit is
        # taken from and what staff approve - with the original kept beside it.
        apply_snapshot(booking_request, discount)
        booking_request.requested_price = discount.new_total
    return booking_request


def _discount_fields(booking_request) -> dict:
    return {
        "discount_code": booking_request.discount_code,
        "discount_amount": booking_request.discount_amount,
        "price_before_discount": booking_request.price_before_discount,
    }


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
        **_discount_fields(booking_request),
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
        **_discount_fields(booking_request),
        "appointment_type_id": booking_request.appointment_type_id,
        "appointment_type_name": (
            booking_request.requested_appointment_type_name
            or (appointment_type.name if appointment_type else None)
        ),
        "add_ons": booking_request.add_ons,
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


def _existing_deposit_attempt(garage, attempt_id, appt_type, add_ons, data):
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
        or selection_signature(booking_request.add_ons) != selection_signature(add_ons)
        or booking_request.preferred_date != data["preferred_date"]
        or booking_request.preferred_time != data.get("preferred_time")
        or not _same_discount_code(booking_request, data)
    ):
        abort(
            409,
            message="This payment attempt belongs to a different service or appointment time. Please start a new payment attempt.",
            errors={"reason": "payment_attempt_mismatch"},
        )
    payment = booking_request.active_payment
    if payment is None:
        return None
    try:
        session = resume_payment_session(payment)
    except PaymentProviderError:
        abort(502, message="Could not resume the deposit payment. Please try again.")
    if session is None:
        return None
    return _deposit_response(booking_request, payment, session)


def _existing_plain_booking_attempt(garage, attempt_id, appt_type, add_ons, data):
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
        or selection_signature(booking_request.add_ons) != selection_signature(add_ons)
        or booking_request.preferred_date != data["preferred_date"]
        or booking_request.preferred_time != data.get("preferred_time")
        or booking_request.status == "AWAITING_PAYMENT"
        or not _same_discount_code(booking_request, data)
    ):
        abort(
            409,
            message="This booking attempt belongs to a different service or appointment time. Please start a new booking attempt.",
            errors={"reason": "booking_attempt_mismatch"},
        )
    return booking_request


def _matching_active_plain_request(garage, appt_type, add_ons, data, answers):
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
    if selection_signature(candidate.add_ons) != selection_signature(add_ons):
        return None
    if not _same_discount_code(candidate, data):
        return None

    submitted_answers = [
        (str(row["field"].id), row["value"], tuple(row["value_list"])) for row in answers
    ]
    stored_answers = [
        (str(row.booking_flow_field_id), row.value, tuple(row.value_list))
        for row in sorted(candidate.answers, key=lambda row: row.order)
    ]
    return candidate if stored_answers == submitted_answers else None


@public_booking_blp.route("/<slug>/discount-codes/validate")
class DiscountCodeValidate(MethodView):
    """The checkout's "Apply": what a code would take off this booking and
    the deposit that would then be due. Advisory - the booking submit and
    deposit-intent endpoints re-check the code themselves.

    Unauthenticated, so it is rate-limited strictly per IP *and* per
    business, and every way a code can be wrong gets the same message, so
    codes can't be probed or enumerated."""

    @limiter.limit(lambda: current_app.config["PUBLIC_DISCOUNT_VALIDATE_RATELIMIT"])
    @limiter.limit(
        lambda: current_app.config["PUBLIC_DISCOUNT_VALIDATE_GARAGE_RATELIMIT"],
        key_func=lambda: (
            f"discount-validate:{request.view_args.get('slug') if request.view_args else ''}"
        ),
    )
    @public_booking_blp.arguments(DiscountValidateSchema)
    @public_booking_blp.response(200, DiscountValidationResultSchema)
    def post(self, data, slug):
        garage = _get_garage_by_slug(slug)
        # Expired holds must give their codes back before limits are checked.
        expire_stale_payment_holds(garage_id=garage.id)
        appt_type = _get_active_appointment_type(garage, data["appointment_type_id"])
        add_ons = _resolve_add_ons(appt_type, data.get("add_ons"))
        result = _evaluate_public_discount(
            garage,
            {"discount_code": data["code"], "customer_email": data["customer_email"]},
            appt_type,
            add_ons,
            lock=False,
        )
        assert result is not None
        deposit_required, deposit_amount = _deposit_preview(appt_type, result.new_total)
        response = {
            "code": result.code.code,
            "discount_type": result.code.discount_type,
            "discount_value": result.code.discount_value,
            "original_total": result.original_total,
            "discount_amount": result.discount_amount,
            "new_total": result.new_total,
            "deposit_required": deposit_required,
            "deposit_amount": deposit_amount,
            "remaining_balance": (
                result.new_total - deposit_amount if deposit_amount is not None else None
            ),
        }
        # Evaluating may have released stale redemptions - keep that, it's true.
        db.session.commit()
        return response


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

        # Before every lookup below, including the idempotent-retry ones: no
        # path through this endpoint proceeds without acceptance of the
        # current terms, and a retry that returns an existing request never
        # re-stamps it.
        terms_version = require_web_acceptance(garage, data)

        appt_type = _get_active_appointment_type(garage, data.get("appointment_type_id"))
        add_ons = _resolve_add_ons(appt_type, data.get("add_ons"))

        deposit_message = (
            "This service requires a deposit - start the deposit payment "
            "flow instead of submitting directly."
        )
        # A deposit service only comes this way when a discount code makes
        # it free - re-checked below, once the code has been evaluated.
        if (
            appt_type is not None
            and appt_type.deposit_required
            and not (data.get("discount_code") or "").strip()
        ):
            abort(422, message=deposit_message)

        # Serialise the idempotency lookup, customer/vehicle resolution and
        # eventual insertion even for date-only requests (which do not take
        # the slot-validation lock below).  This is also the database-side
        # counterpart to the unique payment_attempt_id constraint.
        db.session.query(Garage).filter_by(id=garage.id).with_for_update().one()
        existing = _existing_plain_booking_attempt(
            garage, data.get("payment_attempt_id"), appt_type, add_ons, data
        )
        if existing is not None:
            return existing

        resolved_answers = _validate_answers_or_abort(garage, data, data.get("appointment_type_id"))

        if data.get("payment_attempt_id") is None:
            existing_duplicate = _matching_active_plain_request(
                garage, appt_type, add_ons, data, resolved_answers
            )
            if existing_duplicate is not None:
                return existing_duplicate

        discount = _evaluate_public_discount(garage, data, appt_type, add_ons, lock=True)
        if (
            appt_type is not None
            and appt_type.deposit_required
            and (discount is None or discount.new_total > 0)
        ):
            abort(422, message=deposit_message)

        preferred_time = _lock_and_validate_slot(garage, data, appt_type, add_ons)

        # Create (or match, by email) the customer's account + vehicle right
        # away, rather than waiting for staff to approve the request - see
        # app/booking_requests/service.py::resolve_customer_and_vehicle. The
        # appointment itself still isn't created until a staff member
        # approves and assigns it a slot/employee (see
        # app/booking_requests/routes.py::BookingRequestApprove).
        booking_request = _build_booking_request(
            garage,
            data,
            appt_type,
            add_ons,
            preferred_time,
            status="PENDING",
            answers=resolved_answers,
            discount=discount,
        )
        stamp_acceptance(booking_request, terms_version)

        db.session.add(booking_request)
        db.session.flush()
        persist_answers(booking_request, resolved_answers)
        if discount is not None:
            # A plain booking is confirmed at submission, so the code is used now.
            _redeem_public(garage, discount, booking_request, REDEMPTION_CONSUMED)
        db.session.commit()

        emit_event(BOOKING_REQUEST_CREATED, garage=garage, booking_request=booking_request)
        # Deposit-required requests remain AWAITING_PAYMENT until their
        # provider webhook has succeeded; this plain path is the only point
        # where an unpaid booking can be auto-approved.
        auto_accept_booking_request(garage_id=garage.id, request_id=booking_request.id)

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

        # Checked before the hold (and the payment session) exists, and before
        # a retry can resume one: the customer must have accepted the current
        # terms before being sent to pay. See BookingRequestSubmit.post.
        terms_version = require_web_acceptance(garage, data)

        appt_type = _get_active_appointment_type(garage, data.get("appointment_type_id"))
        if appt_type is None or not appt_type.deposit_required:
            abort(422, message="This service does not require a deposit.")
        add_ons = _resolve_add_ons(appt_type, data.get("add_ons"))

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
            garage, data.get("payment_attempt_id"), appt_type, add_ons, data
        )
        if existing is not None:
            return existing

        discount = _evaluate_public_discount(garage, data, appt_type, add_ons, lock=True)
        if discount is not None and discount.new_total <= 0:
            # Fully discounted: nothing to take a deposit on. The client
            # submits it as an ordinary booking instead.
            abort(
                422,
                message="Your discount code covers the whole booking, so no deposit is needed.",
                errors={"reason": "deposit_not_required"},
            )

        preferred_time = _lock_and_validate_slot(garage, data, appt_type, add_ons)

        booking_request = _build_booking_request(
            garage,
            data,
            appt_type,
            add_ons,
            preferred_time,
            status="AWAITING_PAYMENT",
            answers=resolved_answers,
            discount=discount,
        )
        stamp_acceptance(booking_request, terms_version)
        booking_request.payment_hold_expires_at = payment_hold_deadline()
        recovery_token = token_urlsafe(32)
        booking_request.payment_recovery_token_hash = sha256(recovery_token.encode()).hexdigest()

        db.session.add(booking_request)
        db.session.flush()
        persist_answers(booking_request, resolved_answers)
        if discount is not None:
            # Held until the deposit succeeds (app/payments/service.py), and
            # given back if the hold expires unpaid.
            _redeem_public(garage, discount, booking_request, REDEMPTION_RESERVED)

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
            and payment.status in ("REQUIRES_PAYMENT", "PENDING")
        ):
            try:
                session = resume_payment_session(payment)
            except PaymentProviderError:
                abort(502, message="Could not resume the deposit payment. Please try again.")

        return _recovery_response(booking_request, payment, session)


@public_booking_blp.route("/<slug>/booking-requests/deposit-attempt/abandon")
class DepositAttemptAbandon(MethodView):
    """Give up this browser's own unpaid deposit hold so a fresh attempt can
    start - used when the customer applies or removes a discount code after
    the payment step has opened (the hold's price is fixed at creation).

    Authorised by the same one-purpose recovery capability as recovery, in
    the body; unknown and cross-tenant tokens get the same 404. Never
    abandons a hold that has been paid or whose payment is being confirmed
    (409, with ``errors.reason``)."""

    @limiter.limit(lambda: current_app.config["PUBLIC_BOOKING_RATELIMIT"])
    @public_booking_blp.arguments(DepositAttemptRecoverySchema)
    @public_booking_blp.response(204)
    def post(self, data, slug):
        garage = _get_garage_by_slug(slug)
        token_hash = sha256(data["recovery_token"].encode()).hexdigest()
        booking_request = BookingRequest.query.filter_by(
            garage_id=garage.id, payment_recovery_token_hash=token_hash
        ).first()
        if booking_request is None:
            abort(404, message="Booking recovery attempt not found.")
        try:
            abandon_public_hold(booking_request)
        except HoldNotAbandonable as exc:
            db.session.rollback()
            abort(409, message=str(exc), errors={"reason": exc.reason})
        return ""


@public_booking_blp.route("/<slug>/booking-requests/<reference>/payments/token", methods=["POST"])
class DepositTokenSubmission(MethodView):
    """Phase two of a two-phase provider's deposit (Square): the browser has
    tokenised the card (SCA runs inside the SDK's tokenize()) and posts the
    single-use ``source_id`` (plus an optional legacy ``verification_token``)
    here; the backend
    charges it on the business's own merchant account.

    Returns PENDING, never "paid": the final state arrives by verified
    webhook, and the browser keeps polling ``payment-status`` exactly as
    for Stripe. Idempotent and safe against double-submit (see
    app/payments/service.py::submit_client_payment_token). Provider-neutral
    by construction: it branches on the adapter's
    ``requires_client_token_submission`` capability, never a provider name.
    The garage and payment come only from the slug + reference, never from
    the body.
    """

    @limiter.limit(lambda: current_app.config["PUBLIC_BOOKING_RATELIMIT"])
    @public_booking_blp.arguments(PaymentTokenSubmissionSchema)
    @public_booking_blp.response(202, DepositStatusSchema)
    def post(self, data, slug, reference):
        garage = _get_garage_by_slug(slug)
        booking_request = BookingRequest.query.filter_by(
            garage_id=garage.id, booking_reference=reference
        ).first()
        if booking_request is None:
            abort(404, message="Booking not found.")

        expire_stale_payment_holds(garage_id=garage.id)
        db.session.refresh(booking_request)

        try:
            payment = submit_client_payment_token(
                booking_request,
                source_id=data["source_id"],
                verification_token=data.get("verification_token"),
            )
        except PaymentSubmissionError as exc:
            db.session.rollback()
            status = 422 if exc.reason == "not_token_based" else 409
            abort(status, message=str(exc), errors={"reason": exc.reason})
        except ProviderNotConfigured:
            abort(
                503,
                message="Deposit payments aren't available for this business right now. "
                "Please contact them directly to book.",
            )
        except PaymentProviderError as exc:
            # A customer-safe message from the adapter (e.g. "Your card was
            # declined.") - never a raw provider response.
            abort(400, message=str(exc), errors={"reason": "payment_failed"})

        db.session.refresh(booking_request)
        base_price = booking_request.requested_price
        deposit_amount = minor_to_decimal(payment.amount_minor)
        return {
            "booking_request_id": booking_request.id,
            "booking_reference": booking_request.booking_reference,
            "status": booking_request.status,
            "payment_status": payment.status,
            "currency": payment.currency,
            "service_total": base_price,
            "deposit_amount": deposit_amount,
            "remaining_balance": (base_price - deposit_amount) if base_price is not None else None,
            "hold_expires_at": booking_request.payment_hold_expires_at,
            **_discount_fields(booking_request),
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
        reconcile_submitted_payment(booking_request)
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
            **_discount_fields(booking_request),
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
