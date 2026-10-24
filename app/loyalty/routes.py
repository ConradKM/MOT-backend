"""Settings -> Discounts: the loyalty rule and every discount code.

Garage-scoped through the authenticated employee. Any staff member can read
(the appointment form and customer page need it); only the OWNER can change
anything. The public pieces - the checkout's validate endpoint - live in
app/public_booking/routes.py; the unsubscribe endpoints are here, on their own
public blueprint.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from flask import current_app
from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.appointments.add_ons.service import price_delta, resolve_selection, total_price
from app.appointments.discounts import booked_base_price, evaluate_for_staff
from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.extensions import db, limiter
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.customer import Customer
from app.models.garage import Garage
from app.models.loyalty.discount_code import (
    DISCOUNT_SOURCE_LOYALTY,
    DISCOUNT_SOURCE_MANUAL,
    DISCOUNT_STATUS_ACTIVE,
    DiscountCode,
)
from app.models.loyalty.loyalty_settings import LoyaltySettings

from .codes import display_code, is_valid_code, normalize_code
from .issuing import create_unique_code, loyalty_summary
from .schemas import (
    DiscountCodeCreateSchema,
    DiscountCodePageSchema,
    DiscountCodeQueryArgsSchema,
    DiscountCodeSchema,
    DiscountCodeUpdateSchema,
    DiscountEvaluateSchema,
    DiscountPreviewSchema,
    LoyaltySettingsSchema,
    LoyaltySummarySchema,
    UnsubscribeResultSchema,
    UnsubscribeTokenSchema,
)
from .service import (
    DiscountConfigError,
    code_status,
    code_uses,
    get_loyalty_settings,
    list_codes,
    validate_discount,
    validate_loyalty_settings,
)
from .unsubscribe import customer_for_token

loyalty_blp = Blueprint(
    "loyalty",
    "loyalty",
    url_prefix="/api/loyalty",
    description="Loyalty rewards and discount codes for the authenticated employee's "
    "business. Reads are open to all staff; changes are owner-only.",
)
public_marketing_blp = Blueprint(
    "public_marketing",
    "public_marketing",
    url_prefix="/api/public/marketing",
    description="Unauthenticated marketing-email unsubscribe, by signed link.",
)

_AUTH_DOC: dict[str, list[dict[str, list[str]]]] = {"security": [{"bearerAuth": []}]}


def _settings_payload(settings: LoyaltySettings | None) -> LoyaltySettings | dict:
    """Never configured reads as "off", with the defaults the form starts from."""
    if settings is not None:
        return settings
    return {
        "enabled": False,
        "visits_required": 5,
        "discount_type": "PERCENTAGE",
        "discount_value": Decimal("10.00"),
        "expiry_days": 90,
        "enabled_at": None,
    }


def _decorate(code: DiscountCode, uses: int | None = None, status: str | None = None):
    code._uses = code_uses(code) if uses is None else uses
    code._derived_status = code_status(code) if status is None else status
    return code


def _owned_code(code_id, garage_id, *, lock=False) -> DiscountCode:
    query = DiscountCode.query.filter_by(id=code_id, garage_id=garage_id)
    if lock:
        query = query.with_for_update()
    code = cast(DiscountCode | None, query.first())
    if code is None:
        abort(404, message="Discount code not found.")
    assert code is not None
    return code


@loyalty_blp.route("/settings")
class LoyaltySettingsResource(MethodView):
    @jwt_required()
    @loyalty_blp.doc(**_AUTH_DOC)
    @loyalty_blp.response(200, LoyaltySettingsSchema)
    def get(self):
        return _settings_payload(get_loyalty_settings(get_current_employee().garage_id))

    @jwt_required()
    @owner_required
    @loyalty_blp.doc(**_AUTH_DOC)
    @loyalty_blp.arguments(LoyaltySettingsSchema)
    @loyalty_blp.response(200, LoyaltySettingsSchema)
    def put(self, data):
        garage_id = get_current_employee().garage_id
        try:
            validate_loyalty_settings(
                visits_required=data["visits_required"],
                discount_type=data["discount_type"],
                discount_value=data["discount_value"],
                expiry_days=data["expiry_days"],
            )
        except DiscountConfigError as exc:
            abort(422, message=str(exc), errors={"json": {"discount_value": [str(exc)]}})

        settings = LoyaltySettings.query.filter_by(garage_id=garage_id).with_for_update().first()
        if settings is None:
            settings = LoyaltySettings(garage_id=garage_id)
            db.session.add(settings)
        # Turning it on (re)starts the reconcile window - see
        # LoyaltySettings.enabled_at. Saving while already on keeps it.
        if data["enabled"] and not settings.enabled:
            settings.enabled_at = datetime.now(UTC)
        for field in (
            "enabled",
            "visits_required",
            "discount_type",
            "discount_value",
            "expiry_days",
        ):
            setattr(settings, field, data[field])
        db.session.commit()
        return settings


@loyalty_blp.route("/codes")
class DiscountCodeList(MethodView):
    @jwt_required()
    @loyalty_blp.doc(**_AUTH_DOC)
    @loyalty_blp.arguments(DiscountCodeQueryArgsSchema, location="query")
    @loyalty_blp.response(200, DiscountCodePageSchema)
    def get(self, args):
        rows, total = list_codes(get_current_employee().garage_id, **args)
        return {
            "items": [_decorate(code, uses, status) for code, uses, status in rows],
            "total": total,
        }

    @jwt_required()
    @owner_required
    @loyalty_blp.doc(**_AUTH_DOC)
    @loyalty_blp.arguments(DiscountCodeCreateSchema)
    @loyalty_blp.response(201, DiscountCodeSchema)
    def post(self, data):
        """Create a promo code. Leave ``code`` blank to have one generated."""
        employee = get_current_employee()
        garage_id = employee.garage_id
        try:
            validate_discount(data["discount_type"], data["discount_value"])
        except DiscountConfigError as exc:
            abort(422, message=str(exc), errors={"json": {"discount_value": [str(exc)]}})

        fields = {
            "garage_id": garage_id,
            "source": DISCOUNT_SOURCE_MANUAL,
            "discount_type": data["discount_type"],
            "discount_value": data["discount_value"],
            "status": DISCOUNT_STATUS_ACTIVE,
            "valid_from": data.get("valid_from"),
            "expires_at": data.get("expires_at"),
            "max_uses": data.get("max_uses"),
            "max_uses_per_customer": data.get("max_uses_per_customer"),
            "created_by_employee_id": employee.id,
        }
        raw = (data.get("code") or "").strip()
        if raw:
            normalised = normalize_code(raw)
            if not is_valid_code(normalised):
                message = "Codes are 3-32 letters and numbers (spaces and dashes are ignored)."
                abort(422, message=message, errors={"json": {"code": [message]}})
            if DiscountCode.query.filter_by(
                garage_id=garage_id, code_normalized=normalised
            ).first():
                message = "A code like this already exists."
                abort(409, message=message, errors={"json": {"code": [message]}})
            code = DiscountCode(code=display_code(raw), code_normalized=normalised, **fields)
            db.session.add(code)
        else:
            code = create_unique_code(
                lambda text, normalised: DiscountCode(
                    code=text, code_normalized=normalised, **fields
                ),
                garage_id=garage_id,
            )
            if code is None:
                abort(409, message="Could not create a code - please try again.")
        db.session.commit()
        return _decorate(code)


@loyalty_blp.route("/codes/<uuid:code_id>")
class DiscountCodeResource(MethodView):
    @jwt_required()
    @owner_required
    @loyalty_blp.doc(**_AUTH_DOC)
    @loyalty_blp.arguments(DiscountCodeUpdateSchema)
    @loyalty_blp.response(200, DiscountCodeSchema)
    def patch(self, data, code_id):
        code = _owned_code(code_id, get_current_employee().garage_id, lock=True)
        if code.source == DISCOUNT_SOURCE_LOYALTY and (
            "max_uses" in data or "max_uses_per_customer" in data
        ):
            message = "Loyalty codes are always single-use."
            abort(422, message=message, errors={"json": {"max_uses": [message]}})
        for field, value in data.items():
            setattr(code, field, value)
        if (
            code.valid_from is not None
            and code.expires_at is not None
            and code.expires_at <= code.valid_from
        ):
            message = "expires_at must be after valid_from."
            abort(422, message=message, errors={"json": {"expires_at": [message]}})
        db.session.commit()
        return _decorate(code)


@loyalty_blp.route("/codes/<uuid:code_id>/resend")
class DiscountCodeResend(MethodView):
    @jwt_required()
    @owner_required
    @loyalty_blp.doc(**_AUTH_DOC)
    @loyalty_blp.response(200, DiscountCodeSchema)
    def post(self, code_id):
        """Send a loyalty code's reward email again - deliberately bypassing
        the "already sent" guard. Still never emails an opted-out customer."""
        from app.email.service import send_loyalty_reward_email

        code = _owned_code(code_id, get_current_employee().garage_id)
        if code.source != DISCOUNT_SOURCE_LOYALTY:
            abort(422, message="Only loyalty codes have a reward email.")
        send_loyalty_reward_email(code, force=True)
        return _decorate(code)


@loyalty_blp.route("/evaluate")
class DiscountEvaluate(MethodView):
    @jwt_required()
    @loyalty_blp.doc(**_AUTH_DOC)
    @loyalty_blp.arguments(DiscountEvaluateSchema)
    @loyalty_blp.response(200, DiscountPreviewSchema)
    def post(self, data):
        """Preview a code on the staff appointment form. Advisory only - the
        appointment create/update re-checks it and is authoritative."""
        garage_id = get_current_employee().garage_id
        garage = Garage.query.filter_by(id=garage_id).one()
        customer = Customer.query.filter_by(id=data["customer_id"], garage_id=garage_id).first()
        if customer is None:
            abort(422, message="customer_id does not belong to your business.")
        appointment_type = GarageAppointmentType.query.filter_by(
            id=data["appointment_type_id"], garage_id=garage_id
        ).first()
        if appointment_type is None:
            abort(422, message="appointment_type_id does not belong to your business.")

        appointment = None
        if data.get("appointment_id") is not None:
            appointment = Appointment.query.filter_by(
                id=data["appointment_id"], garage_id=garage_id
            ).first()
            if appointment is None:
                abort(404, message="Appointment not found")

        same_service = appointment is not None and (
            appointment.appointment_type_id == appointment_type.id
        )
        applied = (
            {r.add_on_id: r.quantity for r in appointment.add_ons if r.add_on_id is not None}
            if same_service and appointment is not None
            else None
        )
        add_ons = resolve_selection(appointment_type, data.get("add_ons"), already_applied=applied)
        base = (
            booked_base_price(appointment)
            if same_service and appointment is not None
            else appointment_type.base_price
        )
        total = total_price(base, price_delta(add_ons))
        result = evaluate_for_staff(
            garage,
            data["code"],
            customer=customer,
            appointment_type=appointment_type,
            total=total,
            appointment=appointment,
        )
        db.session.rollback()  # evaluation may have healed stale redemptions; preview writes nothing
        return {
            "code": result.code.code,
            "discount_type": result.code.discount_type,
            "discount_value": result.code.discount_value,
            "original_total": result.original_total,
            "discount_amount": result.discount_amount,
            "new_total": result.new_total,
        }


@loyalty_blp.route("/customers/<uuid:customer_id>")
class CustomerLoyaltySummary(MethodView):
    @jwt_required()
    @loyalty_blp.doc(**_AUTH_DOC)
    @loyalty_blp.response(200, LoyaltySummarySchema)
    def get(self, customer_id):
        garage_id = get_current_employee().garage_id
        customer = Customer.query.filter_by(id=customer_id, garage_id=garage_id).first()
        if customer is None:
            abort(404, message="Customer not found")
        rows, _ = list_codes(garage_id, customer_id=customer.id, limit=100)
        return {
            **loyalty_summary(customer),
            "marketing_email_opt_out": customer.marketing_email_opt_out,
            "codes": [_decorate(code, uses, status) for code, uses, status in rows],
        }


def _customer_or_404(token):
    customer = customer_for_token(token)
    if customer is None:
        abort(404, message="This unsubscribe link isn't valid.")
    return customer


@public_marketing_blp.route("/unsubscribe/preview")
class UnsubscribePreview(MethodView):
    """Which business a link is for, and whether it's already used - so the
    page can ask before changing anything. POST, so the token stays out of
    access logs; mail scanners that prefetch the page change nothing."""

    @limiter.limit(lambda: current_app.config["PUBLIC_AVAILABILITY_RATELIMIT"])
    @public_marketing_blp.arguments(UnsubscribeTokenSchema)
    @public_marketing_blp.response(200, UnsubscribeResultSchema)
    def post(self, data):
        customer = _customer_or_404(data["token"])
        return {
            "business_name": customer.garage.name,
            "unsubscribed": customer.marketing_email_opt_out,
        }


@public_marketing_blp.route("/unsubscribe")
class Unsubscribe(MethodView):
    @limiter.limit(lambda: current_app.config["PUBLIC_AVAILABILITY_RATELIMIT"])
    @public_marketing_blp.arguments(UnsubscribeTokenSchema)
    @public_marketing_blp.response(200, UnsubscribeResultSchema)
    def post(self, data):
        customer = _customer_or_404(data["token"])
        if not customer.marketing_email_opt_out:
            customer.marketing_email_opt_out = True
            customer.marketing_email_opt_out_at = datetime.now(UTC)
            db.session.commit()
        return {"business_name": customer.garage.name, "unsubscribed": True}
