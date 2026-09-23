"""Garage-facing Stripe Connect onboarding API - authenticated, owner-only
(connecting or reconnecting a business's own payment account is account
administration, not a normal staff task - mirrors app/garages/details.py's
own owner-only gate on business configuration).

Never returns a secret: a connected account id is not one (see
app/payments/connect.py's module docstring), and the onboarding link itself
is single-use and short-lived by Stripe's own design.
"""

from __future__ import annotations

from flask import current_app
from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.payments.connect import (
    ConnectError,
    create_account_link,
    create_connected_account,
    get_wallet_domain_status,
    refresh_connect_status,
)
from app.payments.schemas import StripeConnectLinkSchema, StripeConnectStatusSchema

payments_blp = Blueprint(
    "payments",
    "payments",
    url_prefix="/api/payments",
    description="Business-facing Stripe Connect onboarding and account status.",
)

_AUTH_DOC: dict[str, list[dict[str, list[str]]]] = {"security": [{"bearerAuth": []}]}


def _status_payload(garage) -> dict:
    settings = garage.payment_settings
    wallet_status = get_wallet_domain_status(garage) or {}
    return {
        "provider": settings.provider if settings else None,
        "stripe_account_id": settings.stripe_account_id if settings else None,
        "stripe_onboarding_complete": bool(settings and settings.stripe_onboarding_complete),
        "stripe_charges_enabled": bool(settings and settings.stripe_charges_enabled),
        "stripe_payouts_enabled": bool(settings and settings.stripe_payouts_enabled),
        "apple_pay_status": wallet_status.get("apple_pay_status"),
        "apple_pay_status_details": wallet_status.get("apple_pay_status_details"),
    }


@payments_blp.route("/stripe/status")
class StripeConnectStatus(MethodView):
    @jwt_required()
    @owner_required
    @payments_blp.doc(**_AUTH_DOC)
    @payments_blp.response(200, StripeConnectStatusSchema)
    def get(self):
        """Current connection status, refreshed live from Stripe if a
        connected account already exists - this is what the settings page
        polls right after the onboarding-return redirect, since Stripe's
        return_url is never itself proof that onboarding actually finished.
        """
        garage = get_current_employee().garage
        settings = garage.payment_settings
        if settings is not None and settings.stripe_account_id:
            try:
                refresh_connect_status(garage)
            except ConnectError:
                # Live refresh failed (network blip, key issue) - fall back
                # to whatever was last synced (e.g. by a webhook); never a
                # 500 for a page that's just trying to show a status badge.
                pass
        return _status_payload(garage)


@payments_blp.route("/stripe/connect", methods=["POST"])
class StripeConnectStart(MethodView):
    @jwt_required()
    @owner_required
    @payments_blp.doc(**_AUTH_DOC)
    @payments_blp.response(200, StripeConnectLinkSchema)
    def post(self):
        """Create the connected account if needed, then a fresh Stripe-hosted
        onboarding link - the frontend redirects the browser straight to the
        returned ``url``. Safe to call again later to resume/redo onboarding
        (Stripe's own account-onboarding link handles "already partially
        done" on its own)."""
        garage = get_current_employee().garage
        # Both point at the same garage-facing settings page: Stripe treats
        # return_url (finished, however incompletely) and refresh_url (the
        # link itself expired mid-flow) differently, but this app only ever
        # needs to land the customer back on Payments settings either way -
        # the GET /stripe/status call that page makes on mount is what
        # actually determines what to show, never the query string alone.
        base = current_app.config["APP_BASE_URL"].rstrip("/")
        return_url = f"{base}/{garage.id}/settings/payments?onboarding=return"
        refresh_url = f"{base}/{garage.id}/settings/payments?onboarding=refresh"
        try:
            create_connected_account(garage)
            url = create_account_link(garage, return_url=return_url, refresh_url=refresh_url)
        except ConnectError as exc:
            # The real Stripe error (which can include a raw API exception
            # message, a request id, and links to Stripe's own API docs -
            # see the account-creation failure log in
            # app/payments/connect.py) is never something a business owner
            # should see verbatim; it belongs in the platform's own logs,
            # not their browser.
            current_app.logger.warning(
                "STRIPE_CONNECT_START_FAILED garage=%s code=%s detail=%s",
                garage.id,
                exc.code,
                str(exc),
            )
            abort(
                503,
                message="We couldn't start Stripe setup. Please try again or contact CoMaz support.",
            )
        return {"url": url}
