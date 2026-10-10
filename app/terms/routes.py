from datetime import UTC, datetime

from flask import current_app
from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.extensions import limiter
from app.garages.logo import logo_public_url
from app.models.garage import GARAGE_STATUS_ACTIVE, GARAGE_STATUS_TRIAL, Garage
from app.payments.service import expire_stale_payment_holds
from app.public_booking.routes import _get_garage_by_slug

from . import service
from .schemas import (
    GarageTermsSchema,
    GarageTermsUpdateSchema,
    PublicTermsSchema,
    TermsAcceptanceLookupSchema,
    TermsAcceptanceSubmitSchema,
    TermsAcceptanceTokenSchema,
    TermsAcceptedSchema,
)

terms_settings_blp = Blueprint(
    "garage_terms",
    "garage_terms",
    url_prefix="/api/garage/terms",
    description="The business's own Terms & Conditions, which customers accept before booking",
)

public_terms_blp = Blueprint(
    "public_terms",
    "public_terms",
    url_prefix="/api/public",
    description="Unauthenticated Terms & Conditions pages, and accepting them for a booking "
    "made by WhatsApp/SMS/phone",
)

# The current wording can change at any time, so it is only briefly
# cacheable; a numbered version never changes (GarageTermsVersion is
# immutable), so its page can be cached for as long as anyone likes.
_CURRENT_CACHE = {"Cache-Control": "public, max-age=300"}
_VERSION_CACHE = {"Cache-Control": "public, max-age=86400, immutable"}


def _settings_payload(garage: Garage) -> dict:
    current = service.current_terms(garage)
    return {
        "terms_and_conditions": garage.terms_and_conditions,
        "terms_version": garage.terms_version if current is not None else None,
        "terms_updated_at": current.created_at if current is not None else None,
    }


@terms_settings_blp.route("")
class GarageTerms(MethodView):
    @jwt_required()
    @terms_settings_blp.response(200, GarageTermsSchema)
    def get(self):
        """The caller's business's current terms. Any staff member may read
        them - they are shown to every customer anyway."""
        return _settings_payload(get_current_employee().garage)

    @jwt_required()
    @owner_required
    @terms_settings_blp.arguments(GarageTermsUpdateSchema)
    @terms_settings_blp.response(200, GarageTermsSchema)
    def put(self, data):
        """Replace the terms. OWNER only, like the business's other details
        (``PATCH /api/garage``). Empty text turns terms off; unchanged text
        keeps the current version."""
        employee = get_current_employee()
        garage = employee.garage
        service.save_terms(garage, data["terms_and_conditions"], employee=employee)
        return _settings_payload(garage)


def _public_garage(garage_id) -> Garage:
    # Same lifecycle boundary as the public booking page: a suspended or
    # archived business is indistinguishable from an unknown one.
    garage: Garage | None = Garage.query.filter(
        Garage.id == garage_id,
        Garage.status.in_((GARAGE_STATUS_ACTIVE, GARAGE_STATUS_TRIAL)),
    ).first()
    if garage is None:
        abort(404, message="Business not found.")
    assert garage is not None
    return garage


def _public_payload(garage: Garage, row) -> dict:
    return {
        "garage_id": garage.id,
        "garage_name": garage.name,
        "logo_url": logo_public_url(garage),
        "version": row.version,
        "body": row.body,
        "effective_at": row.created_at,
        "is_current": service.has_terms(garage) and garage.terms_version == row.version,
    }


@public_terms_blp.route("/garages/<uuid:garage_id>/terms")
class PublicCurrentTerms(MethodView):
    @limiter.limit(lambda: current_app.config["PUBLIC_AVAILABILITY_RATELIMIT"])
    @public_terms_blp.response(200, PublicTermsSchema)
    def get(self, garage_id):
        """The business's current terms - the standalone page a business can
        link to from its own website. 404 when it has none."""
        garage = _public_garage(garage_id)
        row = service.current_terms(garage)
        if row is None:
            abort(404, message="This business has no terms and conditions.")
        return _public_payload(garage, row), 200, _CURRENT_CACHE


@public_terms_blp.route("/garages/<uuid:garage_id>/terms/<int:version>")
class PublicTermsVersion(MethodView):
    @limiter.limit(lambda: current_app.config["PUBLIC_AVAILABILITY_RATELIMIT"])
    @public_terms_blp.response(200, PublicTermsSchema)
    def get(self, garage_id, version):
        """One exact wording - what a booking confirmation links to. Still
        served after the business edits or removes its terms: it is what a
        past customer agreed to."""
        garage = _public_garage(garage_id)
        row = service.get_version(garage.id, version)
        if row is None:
            abort(404, message="That version of the terms doesn't exist.")
        return _public_payload(garage, row), 200, _VERSION_CACHE


def _request_for_token(garage, token):
    booking_request = service.find_by_token(garage, token)
    if booking_request is None:
        # Unknown and cross-tenant tokens look identical - no enumeration.
        abort(404, message="We couldn't find that booking.")
    return booking_request


def _lookup_payload(garage, booking_request) -> dict:
    state = service.acceptance_state(booking_request, datetime.now(UTC))
    appointment_type = booking_request.appointment_type
    awaiting = state == service.STATE_AWAITING
    return {
        "state": state,
        "status": booking_request.status,
        "garage_id": garage.id,
        "garage_name": garage.name,
        "booking": {
            "booking_reference": booking_request.booking_reference,
            "customer_first_name": booking_request.customer_first_name,
            "service_name": booking_request.requested_appointment_type_name
            or (appointment_type.name if appointment_type else None),
            "add_ons": booking_request.add_ons,
            "preferred_date": booking_request.preferred_date,
            "preferred_time": booking_request.preferred_time,
            "duration_minutes": booking_request.requested_duration_minutes,
            "price": booking_request.requested_price,
            "vehicle_registration": booking_request.vehicle_registration,
            "deposit_required": bool(appointment_type and appointment_type.deposit_required),
        },
        # The current wording, and only while there is something to accept -
        # if it changed since the booking was made the customer simply sees
        # (and accepts) the new one; they hadn't accepted anything yet.
        "terms_and_conditions": garage.terms_and_conditions if awaiting else None,
        "terms_version": garage.terms_version if awaiting and service.has_terms(garage) else None,
        "hold_expires_at": booking_request.payment_hold_expires_at if awaiting else None,
    }


@public_terms_blp.route("/<slug>/terms-acceptance/lookup")
class TermsAcceptanceLookup(MethodView):
    @limiter.limit(lambda: current_app.config["PUBLIC_AVAILABILITY_RATELIMIT"])
    @public_terms_blp.arguments(TermsAcceptanceTokenSchema)
    @public_terms_blp.response(200, TermsAcceptanceLookupSchema)
    def post(self, data, slug):
        """Resolve a texted acceptance link to its booking summary, the
        terms to accept, and whether it can still be accepted."""
        garage = _get_garage_by_slug(slug)
        expire_stale_payment_holds(garage_id=garage.id)
        booking_request = _request_for_token(garage, data["token"])
        return _lookup_payload(garage, booking_request)


@public_terms_blp.route("/<slug>/terms-acceptance/accept")
class TermsAcceptanceAccept(MethodView):
    @limiter.limit(lambda: current_app.config["PUBLIC_BOOKING_RATELIMIT"])
    @public_terms_blp.arguments(TermsAcceptanceSubmitSchema)
    @public_terms_blp.response(200, TermsAcceptedSchema)
    def post(self, data, slug):
        """Accept the terms for a WhatsApp/SMS/phone booking. Single use: a
        second attempt is a 409 (``terms_already_accepted``)."""
        garage = _get_garage_by_slug(slug)
        booking_request = _request_for_token(garage, data["token"])
        result = service.accept_by_token(garage, booking_request, data)
        return {**result, "booking_reference": booking_request.booking_reference}
