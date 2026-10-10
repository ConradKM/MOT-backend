"""Business Terms & Conditions: versioning, enforcement and acceptance.

A business writes its own terms (plain text) in Settings. While they exist,
every customer-facing way of booking requires the customer to accept them:

* the public booking form and the walk-in queue join page send
  ``terms_accepted`` + ``terms_version`` with the submission, checked by
  :func:`require_web_acceptance` before anything is created;
* the conversational channel (WhatsApp/SMS/voice) has no checkbox, so its
  bookings are created ``AWAITING_TERMS`` - a capacity-holding hold like
  ``AWAITING_PAYMENT`` - and the customer is texted a link to a web page
  where they accept (:func:`accept_by_token`).

Staff-created appointments are deliberately exempt: staff are not the
customer accepting anything.

Every acceptance records *which* wording was accepted (a GarageTermsVersion
number) and when, stamped here on the server. A business with no terms is
completely unaffected: no check, no hold, no stamp, no link.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe

from flask import current_app
from flask_smorest import abort

from app.extensions import db
from app.models.booking_request import BookingRequest
from app.models.garage import Garage
from app.models.garage_terms_version import GarageTermsVersion

#: The settings API's cap; long enough for any real set of terms, short
#: enough that the public payload every booking page loads stays small.
MAX_TERMS_LENGTH = 50_000

#: ``errors.reason`` codes, stable for the frontend to switch on.
REASON_VERSION_MISMATCH = "terms_version_mismatch"
REASON_ALREADY_ACCEPTED = "terms_already_accepted"
REASON_ACCEPTANCE_EXPIRED = "terms_acceptance_expired"
REASON_ACCEPTANCE_CLOSED = "terms_acceptance_closed"

#: What an acceptance link resolves to - see :func:`acceptance_state`.
STATE_AWAITING = "AWAITING_ACCEPTANCE"
STATE_ACCEPTED = "ACCEPTED"
STATE_EXPIRED = "EXPIRED"
STATE_CLOSED = "CLOSED"


# --- the business's own terms ----------------------------------------------


def normalise_terms(text: str | None) -> str | None:
    """Trimmed text, or None for nothing/whitespace - "no terms" has exactly
    one representation, so nothing else ever has to ask whether an empty
    string counts."""
    if text is None:
        return None
    text = text.strip()
    return text or None


def has_terms(garage: Garage) -> bool:
    return bool(garage.terms_and_conditions) and garage.terms_version is not None


def current_terms(garage: Garage) -> GarageTermsVersion | None:
    if not has_terms(garage):
        return None
    return get_version(garage.id, garage.terms_version)


def get_version(garage_id, version) -> GarageTermsVersion | None:
    if version is None:
        return None
    row: GarageTermsVersion | None = GarageTermsVersion.query.filter_by(
        garage_id=garage_id, version=version
    ).first()
    return row


def save_terms(garage: Garage, text: str | None, *, employee=None) -> None:
    """Make ``text`` the business's current terms.

    A new version is only created when the trimmed wording actually differs
    from the latest one - saving unchanged text (or switching terms off and
    back on with the same wording) keeps the existing version, so customers
    are not re-prompted for words they already accepted. Clearing the text
    turns terms off and leaves every past version in place.
    """
    body = normalise_terms(text)
    # Serialise version allocation per business - two owners saving at once
    # must not both pick the same next number.
    db.session.query(Garage).filter_by(id=garage.id).with_for_update().one()

    if body is None:
        garage.terms_and_conditions = None
        db.session.commit()
        return

    latest: GarageTermsVersion | None = (
        GarageTermsVersion.query.filter_by(garage_id=garage.id)
        .order_by(GarageTermsVersion.version.desc())
        .first()
    )
    if latest is not None and latest.body == body:
        garage.terms_version = latest.version
    else:
        version = (latest.version if latest is not None else 0) + 1
        db.session.add(
            GarageTermsVersion(
                garage_id=garage.id,
                version=version,
                body=body,
                created_by_employee_id=employee.id if employee is not None else None,
            )
        )
        garage.terms_version = version
    garage.terms_and_conditions = body
    db.session.commit()


# --- links -------------------------------------------------------------------


def _base_url() -> str:
    return str(current_app.config["BOOKING_BASE_URL"]).rstrip("/")


def terms_url(garage_id, version: int | None = None) -> str:
    """The public Terms page (MOT-frontend ``/terms/:garageId``), or one exact
    version of it (``/terms/:garageId/v/:version``) - what confirmations link
    to, so a later edit never changes what the link shows."""
    url = f"{_base_url()}/terms/{garage_id}"
    return f"{url}/v/{version}" if version is not None else url


def accepted_terms_url(record) -> str | None:
    """Link to the wording a booking request / queue entry accepted, or None
    when it recorded no acceptance (the business had no terms)."""
    version = getattr(record, "terms_version", None)
    if getattr(record, "terms_accepted_at", None) is None or version is None:
        return None
    return terms_url(record.garage_id, version)


def acceptance_url(garage: Garage, token: str) -> str:
    """The page a conversational customer accepts the terms on. The token
    rides in the URL fragment, which browsers never send to a server, so it
    stays out of access logs - the page posts it in a request body instead
    (same shape as the walk-in queue's status link)."""
    return f"{_base_url()}/accept-terms/{garage.id}#{token}"


# --- web acceptance (booking form, queue join) --------------------------------


def require_web_acceptance(garage: Garage, data) -> int | None:
    """Enforce acceptance on a web submission; returns the version to stamp,
    or None when the business has no terms (nothing to check or record).

    Missing/false acceptance is a 422 on ``terms_accepted`` - an ordinary
    required-field error. A version that no longer matches the current one is
    a 409 with ``errors.reason = terms_version_mismatch``: the customer ticked
    a box next to words that have since changed, so the page must show them
    the new wording rather than the server silently accepting it for them.
    """
    if not has_terms(garage):
        return None
    if not data.get("terms_accepted"):
        abort(
            422,
            message="Please read and accept the terms and conditions to continue.",
            errors={"json": {"terms_accepted": ["You must accept the terms and conditions."]}},
        )
    if data.get("terms_version") != garage.terms_version:
        abort(
            409,
            message="The terms and conditions were updated. "
            "Please read and accept the latest version.",
            errors={"reason": REASON_VERSION_MISMATCH},
        )
    return garage.terms_version


def stamp_acceptance(record, version: int | None, now: datetime | None = None) -> None:
    """Record acceptance on a BookingRequest/QueueEntry. A no-op for None (a
    business without terms), so callers never branch on it."""
    if version is None:
        return
    record.terms_accepted_at = now or datetime.now(UTC)
    record.terms_version = version


# --- conversational acceptance (texted link) -----------------------------------


def terms_hold_deadline(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    minutes = current_app.config.get("TERMS_ACCEPTANCE_HOLD_MINUTES", 60)
    return now + timedelta(minutes=minutes)


def hash_token(token: str) -> str:
    return sha256(token.encode()).hexdigest()


def hold_for_acceptance(booking_request: BookingRequest, now: datetime | None = None) -> str:
    """Put a not-yet-committed conversational request on an AWAITING_TERMS
    hold and return the plaintext acceptance token (only its hash is kept)."""
    token = token_urlsafe(32)
    booking_request.status = "AWAITING_TERMS"
    booking_request.payment_hold_expires_at = terms_hold_deadline(now)
    booking_request.terms_acceptance_token_hash = hash_token(token)
    return token


def find_by_token(garage: Garage, token: str) -> BookingRequest | None:
    """Scoped to the business in the URL: a token for another business looks
    exactly like an unknown one."""
    booking_request: BookingRequest | None = BookingRequest.query.filter_by(
        garage_id=garage.id, terms_acceptance_token_hash=hash_token(token)
    ).first()
    return booking_request


def acceptance_state(booking_request: BookingRequest, now: datetime | None = None) -> str:
    if booking_request.terms_accepted_at is not None:
        return STATE_ACCEPTED
    if booking_request.status == "EXPIRED" or (
        booking_request.status == "AWAITING_TERMS" and _hold_lapsed(booking_request, now)
    ):
        return STATE_EXPIRED
    if booking_request.status == "AWAITING_TERMS":
        return STATE_AWAITING
    return STATE_CLOSED


def _hold_lapsed(booking_request: BookingRequest, now: datetime | None = None) -> bool:
    from app.booking_requests.service import is_request_stale

    now = now or datetime.now(UTC)
    expires = booking_request.payment_hold_expires_at
    return (expires is not None and expires < now) or is_request_stale(booking_request, now)


def accept_by_token(garage: Garage, booking_request: BookingRequest, data) -> dict:
    """Accept the terms for one AWAITING_TERMS request and move it on.

    A normal booking becomes PENDING (and is then exactly what the web form
    would have created). A deposit booking becomes AWAITING_PAYMENT with a
    fresh payment hold and a recovery token, so the page can continue
    straight into the existing checkout - the deposit is never taken before
    the terms are accepted. Returns ``{"status", "recovery_token"}``.
    """
    # Imported here: these modules import the public booking/payment stack,
    # which itself imports this module.
    from app.booking_requests.service import auto_accept_booking_request
    from app.communications.events import BOOKING_REQUEST_CREATED, emit_event
    from app.payments.money import DepositConfigError
    from app.payments.providers.base import PaymentProviderError
    from app.payments.service import (
        PaymentUnavailableError,
        create_deposit_hold,
        payment_hold_deadline,
    )

    now = datetime.now(UTC)
    # Same per-garage lock as every other path that moves capacity, then the
    # request itself, so a double-tap or a racing expiry sweep sees one state.
    db.session.query(Garage).filter_by(id=garage.id).with_for_update().one()
    locked: BookingRequest = (
        BookingRequest.query.filter_by(id=booking_request.id, garage_id=garage.id)
        .with_for_update()
        .one()
    )

    state = acceptance_state(locked, now)
    if state == STATE_ACCEPTED:
        abort(
            409,
            message="These terms have already been accepted for this booking.",
            errors={"reason": REASON_ALREADY_ACCEPTED},
        )
    if state == STATE_EXPIRED:
        if locked.status == "AWAITING_TERMS":
            locked.status = "EXPIRED"
            locked.payment_hold_expires_at = None
            db.session.commit()
        abort(
            409,
            message="This booking was held for a limited time and has now expired. "
            "Please get in touch with the business to book again.",
            errors={"reason": REASON_ACCEPTANCE_EXPIRED},
        )
    if state != STATE_AWAITING:
        abort(
            409,
            message="This booking can no longer be confirmed here. "
            "Please get in touch with the business.",
            errors={"reason": REASON_ACCEPTANCE_CLOSED},
        )

    # The customer hasn't accepted anything yet, so a wording change since
    # the booking was made needs no special handling - the page shows the
    # current terms and that is what they accept. The version check only
    # catches an edit made while the page was open.
    version = require_web_acceptance(garage, data)
    stamp_acceptance(locked, version, now)

    appointment_type = locked.appointment_type
    recovery_token = None
    if appointment_type is not None and appointment_type.deposit_required:
        locked.status = "AWAITING_PAYMENT"
        locked.payment_hold_expires_at = payment_hold_deadline(now)
        recovery_token = token_urlsafe(32)
        locked.payment_recovery_token_hash = hash_token(recovery_token)
        try:
            create_deposit_hold(
                garage=garage, appointment_type=appointment_type, booking_request=locked
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
        # BOOKING_REQUEST_CREATED fires once the deposit succeeds, exactly as
        # for a web deposit booking (app/payments/service.py).
    else:
        locked.status = "PENDING"
        locked.payment_hold_expires_at = None
        db.session.commit()
        # Only now is this a booking request in the ordinary sense - the
        # "we've received your request" messages and auto-accept run here,
        # never while it was waiting on the customer.
        emit_event(BOOKING_REQUEST_CREATED, garage=garage, booking_request=locked)
        auto_accept_booking_request(garage_id=garage.id, request_id=locked.id)

    return {"status": locked.status, "recovery_token": recovery_token}
