"""Whether one business is genuinely ready to go live - the question Platform
Admin needs answered before onboarding a real customer, not just whether the
right rows exist.

Three sections, each a list of ``{key, label, ok}`` checks in the same shape
``embedded_signup_prerequisites()`` already uses for WhatsApp:

* **business** - the onboarding steps that block public booking at all
  (:func:`app.platform_admin.onboarding.onboarding_progress`).
* **payments** - read from the connected account's own stored state
  (``GaragePaymentSettings``, kept current by Stripe webhooks/refresh - this
  never calls Stripe itself, so it's cheap enough for a dashboard read).
  "Stripe connected" alone is never enough to call payments ready: charges
  and payouts must both be enabled, matching
  :func:`app.payments.settings.stripe_connect_ready`.
* **communications** - delegates to the same per-business view Communications
  Setup already renders (:func:`app.platform_admin.communications.communications_detail`),
  so this can never disagree with what an operator sees on that page.

Nothing here is fabricated from ID presence: a Stripe account id with
``charges_enabled=False`` reports not-ready, exactly as it should before a
business's first real deposit.
"""

from __future__ import annotations

from sqlalchemy import select

from app.extensions import db
from app.models.employee import Employee, employee_roles
from app.models.garage import GARAGE_STATUS_ACTIVE, GARAGE_STATUS_TRIAL, Garage
from app.models.role import Role
from app.payments.config import is_payments_configured

from .onboarding import onboarding_progress


def _check(key: str, label: str, ok: bool) -> dict:
    return {"key": key, "label": label, "ok": ok}


def _owner_exists(garage_id) -> bool:
    return (
        db.session.scalar(
            select(Employee.id)
            .join(employee_roles, employee_roles.c.employee_id == Employee.id)
            .join(Role, Role.id == employee_roles.c.role_id)
            .where(Role.name == "OWNER", Employee.garage_id == garage_id)
            .limit(1)
        )
        is not None
    )


def _business_checks(garage: Garage) -> list[dict]:
    progress = onboarding_progress(garage)
    completed = {step["key"]: step["complete"] for step in progress["steps"]}
    return [
        _check("business_created", "Business created", True),
        _check("owner_exists", "Owner account exists", _owner_exists(garage.id)),
        _check("services_configured", "Services configured", completed.get("services", False)),
        _check(
            "opening_hours_configured",
            "Opening hours configured",
            completed.get("opening_hours", False),
        ),
        _check(
            "public_booking_enabled",
            "Public booking enabled",
            garage.status in (GARAGE_STATUS_ACTIVE, GARAGE_STATUS_TRIAL),
        ),
    ]


def _payments_checks(garage: Garage) -> list[dict]:
    settings = garage.payment_settings
    connected = bool(settings and settings.stripe_account_id)
    charges = bool(settings and settings.stripe_charges_enabled)
    payouts = bool(settings and settings.stripe_payouts_enabled)
    return [
        _check("stripe_connected", "Stripe connected", connected),
        _check("stripe_charges_enabled", "Charges enabled", charges),
        _check("stripe_payouts_enabled", "Payouts enabled", payouts),
        _check(
            "deposit_configuration_valid",
            "Deposit configuration valid",
            is_payments_configured("stripe", garage),
        ),
    ]


def _communications_checks(garage: Garage) -> list[dict]:
    from .communications import communications_detail

    detail = communications_detail(garage)
    voice = detail["voice"]
    return [
        _check(
            "twilio_subaccount",
            "Twilio subaccount",
            bool(detail["twilio_subaccount_sid"]),
        ),
        _check("phone_number", "Phone number", bool(voice["phone_number"])),
        _check("voice_routing", "Voice routing configured", bool(voice["webhooks_configured"])),
        _check(
            "openai_voice",
            "OpenAI voice",
            voice["status"] in ("ONLINE",) or bool(voice.get("webhooks_configured")),
        ),
        _check(
            "communications_enabled",
            "Communications enabled",
            detail["communications_enabled"],
        ),
    ]


def business_readiness(garage: Garage) -> dict:
    """The three-section go-live checklist for one business."""
    business = _business_checks(garage)
    payments = _payments_checks(garage)
    communications = _communications_checks(garage)

    def _all_ok(checks: list[dict]) -> bool:
        return all(c["ok"] for c in checks)

    return {
        "garage_id": garage.id,
        "garage_name": garage.name,
        "business": business,
        "business_ready": _all_ok(business),
        "payments": payments,
        "payments_ready": _all_ok(payments),
        "communications": communications,
        "communications_ready": _all_ok(communications),
        # Communications is optional for go-live (a business can take public
        # bookings with no voice/WhatsApp at all) - payments and the core
        # business steps are not.
        "ready_for_public_booking": _all_ok(business),
        "ready_to_take_deposits": _all_ok(business) and _all_ok(payments),
    }
