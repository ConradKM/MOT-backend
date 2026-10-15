"""Garage-facing Stripe Connect onboarding API - authenticated, owner-only
(connecting or reconnecting a business's own payment account is account
administration, not a normal staff task - mirrors app/garages/details.py's
own owner-only gate on business configuration).

Never returns a secret: a connected account id is not one (see
app/payments/connect.py's module docstring), and the onboarding link itself
is single-use and short-lived by Stripe's own design.
"""

from __future__ import annotations

from flask import current_app, redirect, request
from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.payments import square_oauth
from app.payments.connect import (
    ConnectError,
    create_account_link,
    create_connected_account,
    get_wallet_domain_status,
    refresh_connect_status,
)
from app.payments.schemas import (
    PaymentProviderSelectionSchema,
    SquareConnectLinkSchema,
    SquareConnectStatusSchema,
    SquareDisconnectResultSchema,
    SquareLocationUpdateSchema,
    StripeConnectLinkSchema,
    StripeConnectStatusSchema,
)
from app.payments.settings import (
    ProviderSelectionError,
    payments_in_flight,
    provider_account_ready,
    resolve_provider_name,
    selectable_providers,
    set_garage_provider,
)

payments_blp = Blueprint(
    "payments",
    "payments",
    url_prefix="/api/payments",
    description="Business-facing payment provider selection, Stripe Connect onboarding, "
    "and Square OAuth connection.",
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


# --- provider selection -----------------------------------------------------


def _provider_payload(garage) -> dict:
    return {
        "provider": resolve_provider_name(garage),
        "options": [
            {"name": name, "ready": provider_account_ready(garage, name)}
            for name in selectable_providers()
        ],
    }


@payments_blp.route("/provider")
class PaymentProviderSelection(MethodView):
    @jwt_required()
    @owner_required
    @payments_blp.doc(**_AUTH_DOC)
    @payments_blp.response(200, PaymentProviderSelectionSchema)
    def get(self):
        """This business's active payment provider and the choices."""
        return _provider_payload(get_current_employee().garage)

    @jwt_required()
    @owner_required
    @payments_blp.doc(**_AUTH_DOC)
    @payments_blp.arguments(PaymentProviderSelectionSchema)
    @payments_blp.response(200, PaymentProviderSelectionSchema)
    def put(self, data):
        """Choose Stripe or Square. Refused (409) while a deposit is mid-
        checkout; earlier payments keep refunding through their own provider."""
        employee = get_current_employee()
        garage = employee.garage
        try:
            set_garage_provider(garage, data["provider"], employee=employee)
        except ProviderSelectionError as exc:
            status = 409 if exc.reason == "payment_in_progress" else 422
            abort(status, message=str(exc), errors={"reason": exc.reason})
        return _provider_payload(garage)


# --- Square OAuth -----------------------------------------------------------


@payments_blp.route("/square/connect", methods=["POST"])
class SquareConnectStart(MethodView):
    @jwt_required()
    @owner_required
    @payments_blp.doc(**_AUTH_DOC)
    @payments_blp.response(200, SquareConnectLinkSchema)
    def post(self):
        """Square's own authorisation page for this business - the frontend
        redirects the browser to ``url``. Carries a signed, expiring state
        bound to this garage and owner."""
        employee = get_current_employee()
        try:
            url = square_oauth.authorize_url(employee.garage, employee)
        except square_oauth.SquareConnectError as exc:
            current_app.logger.warning(
                "SQUARE_CONNECT_START_FAILED garage=%s code=%s", employee.garage_id, exc.code
            )
            abort(
                503,
                message="Square isn't available on CoMaz right now. Please contact CoMaz support.",
                errors={"reason": exc.code},
            )
        return {"url": url}


@payments_blp.route("/square/oauth/callback")
@payments_blp.doc(
    description="Square's OAuth redirect (registered in the Square Developer Console). "
    "Unauthenticated by necessity - the signed state is what binds it to a business."
)
def square_oauth_callback():
    try:
        garage = square_oauth.complete_authorization(
            state=request.args.get("state"),
            code=request.args.get("code"),
            error=request.args.get("error"),
        )
    except square_oauth.SquareConnectError as exc:
        current_app.logger.warning("SQUARE_OAUTH_CALLBACK_FAILED code=%s", exc.code)
        garage = getattr(exc, "garage", None)
        if garage is None:
            # No trustworthy garage to send them back to (tampered/expired
            # state) - a plain error, never a redirect chosen by the request.
            return {"error": "This Square connection link is invalid or has expired."}, 400
        return redirect(square_oauth.settings_return_url(garage.id, error=exc.code))
    return redirect(square_oauth.settings_return_url(garage.id))


@payments_blp.route("/square/status")
class SquareConnectStatus(MethodView):
    @jwt_required()
    @owner_required
    @payments_blp.doc(**_AUTH_DOC)
    @payments_blp.response(200, SquareConnectStatusSchema)
    def get(self):
        """Connection state, merchant, chosen location and readiness - the
        source of truth after the ``?square=return`` redirect."""
        return square_oauth.connection_status(get_current_employee().garage)


@payments_blp.route("/square/location")
class SquareLocation(MethodView):
    @jwt_required()
    @owner_required
    @payments_blp.doc(**_AUTH_DOC)
    @payments_blp.arguments(SquareLocationUpdateSchema)
    @payments_blp.response(200, SquareConnectStatusSchema)
    def put(self, data):
        """Choose which Square location takes this business's deposits."""
        garage = get_current_employee().garage
        try:
            square_oauth.set_location(garage, data["location_id"])
        except square_oauth.SquareNotConnected as exc:
            abort(409, message="Square isn't connected.", errors={"reason": exc.code})
        except square_oauth.SquareConnectError as exc:
            abort(422, message=str(exc), errors={"reason": exc.code})
        return square_oauth.connection_status(garage)


@payments_blp.route("/square/disconnect", methods=["POST"])
class SquareDisconnect(MethodView):
    @jwt_required()
    @owner_required
    @payments_blp.doc(**_AUTH_DOC)
    @payments_blp.response(200, SquareDisconnectResultSchema)
    def post(self):
        """Revoke CoMaz's access with Square and forget the connection."""
        garage = get_current_employee().garage
        if payments_in_flight(garage.id, provider="square"):
            abort(
                409,
                message="A customer is paying a deposit through Square right now. "
                "Please try again in a few minutes.",
                errors={"reason": "payment_in_progress"},
            )
        try:
            confirmed = square_oauth.disconnect(garage)
        except square_oauth.SquareNotConnected:
            abort(409, message="Square isn't connected.", errors={"reason": "not_connected"})
        return {"disconnected": True, "remote_revoke_confirmed": confirmed}
