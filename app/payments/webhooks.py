"""Inbound payment-provider webhooks.

One route, parametrised by provider name, rather than one function per
provider: ``/api/webhooks/payments/<provider>`` - so
``/api/webhooks/payments/stripe`` (the URL already given to Stripe, and
documented in docs/PAYMENTS_SETUP.md) keeps working unchanged, and a future
``/api/webhooks/payments/paypal`` or ``/square`` needs no new route, just a
real adapter behind app/payments/providers/get_provider().

Plain function-based route (like app/communications/voice_webhooks.py), not
the MethodView + marshmallow style used elsewhere: a provider POSTs its own
event envelope, not JSON we control, and needs the *raw* request body
(request.get_data()) for signature verification - Flask's parsed
request.json would have already lost the exact bytes that were signed.
"""

from flask import current_app, request
from flask_smorest import Blueprint, abort

from app.extensions import db
from app.payments.config import is_payments_configured
from app.payments.providers.base import WebhookVerificationError
from app.payments.service import process_webhook

payment_webhooks_blp = Blueprint(
    "payment_webhooks",
    "payment_webhooks",
    url_prefix="/api/webhooks/payments",
)


@payment_webhooks_blp.route("/<provider>", methods=["POST"])
def provider_webhook(provider):
    if not is_payments_configured(provider):
        # Never a 500: a webhook arriving for a provider that isn't (or
        # isn't yet) configured - a stray retry from before it was
        # deconfigured, or a delivery for a provider CoMaz doesn't support -
        # is a normal, if unexpected, occurrence.
        return {"error": f"{provider} is not configured for this deployment."}, 503

    payload = request.get_data()  # raw bytes - required for signature checks
    headers = {
        "Stripe-Signature": request.headers.get("Stripe-Signature", ""),
        # The fake provider (tests) reads this instead - see
        # app/payments/providers/fake.py.
        "Fake-Signature": request.headers.get("Fake-Signature", ""),
    }

    try:
        process_webhook(provider, payload, headers)
    except ValueError:
        abort(404, message=f"Unknown payment provider: {provider!r}.")
    except WebhookVerificationError:
        db.session.rollback()
        current_app.logger.warning(
            "Rejected %s payment webhook: signature verification failed.", provider
        )
        return {"error": "Invalid signature."}, 400
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Unhandled error processing %s payment webhook.", provider)
        raise

    return {"received": True}, 200


@payment_webhooks_blp.route("/stripe/connect", methods=["POST"])
def stripe_connect_webhook():
    """Stripe Connect's own webhook endpoint - a *different* endpoint/secret
    from ``/stripe`` above, configured in the Dashboard as a Connect
    endpoint (see docs/PAYMENTS_SETUP.md). Carries both ``account.updated``
    (a connected account's own status changed - see
    app/payments/connect.py::sync_account_from_webhook) and the same
    payment_intent.*/charge.refunded/refund.updated events as the platform
    endpoint, for a payment that was a Direct Charge against a connected
    account. Both go through the exact same idempotent process_webhook -
    only the verification secret differs.
    """
    if not current_app.config.get("STRIPE_CONNECT_WEBHOOK_SECRET"):
        return {"error": "Stripe Connect is not configured for this deployment."}, 503

    payload = request.get_data()
    headers = {"Stripe-Signature": request.headers.get("Stripe-Signature", "")}

    try:
        process_webhook(
            "stripe",
            payload,
            headers,
            webhook_secret=current_app.config["STRIPE_CONNECT_WEBHOOK_SECRET"],
        )
    except WebhookVerificationError:
        db.session.rollback()
        current_app.logger.warning(
            "Rejected Stripe Connect webhook: signature verification failed."
        )
        return {"error": "Invalid signature."}, 400
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Unhandled error processing Stripe Connect webhook.")
        raise

    return {"received": True}, 200
