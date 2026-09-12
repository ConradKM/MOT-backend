"""Inbound payment-provider webhooks (Stripe today).

Plain function-based route (like app/communications/voice_webhooks.py), not
the MethodView + marshmallow style used elsewhere: the provider POSTs its
own event envelope, not JSON we control, and needs the *raw* request body
(request.get_data()) for signature verification - Flask's parsed
request.json would have already lost the exact bytes that were signed.
"""

from flask import current_app, request
from flask_smorest import Blueprint

from app.extensions import db
from app.payments.config import is_payments_configured
from app.payments.providers.base import WebhookVerificationError
from app.payments.service import process_webhook

payment_webhooks_blp = Blueprint(
    "payment_webhooks",
    "payment_webhooks",
    url_prefix="/api/webhooks/payments",
)


@payment_webhooks_blp.route("/stripe", methods=["POST"])
def stripe_webhook():
    if not is_payments_configured():
        # Never a 500: a webhook arriving for a deployment that hasn't set
        # up a provider yet (e.g. a stray retry from before it was
        # deconfigured) is a normal, if unexpected, occurrence.
        return {"error": "Payments are not configured for this deployment."}, 503

    payload = request.get_data()  # raw bytes - required for signature checks
    headers = {
        "Stripe-Signature": request.headers.get("Stripe-Signature", ""),
        # The fake provider (tests) reads this instead - see
        # app/payments/providers/fake.py.
        "Fake-Signature": request.headers.get("Fake-Signature", ""),
    }

    try:
        process_webhook(payload, headers)
    except WebhookVerificationError:
        db.session.rollback()
        current_app.logger.warning("Rejected payment webhook: signature verification failed.")
        return {"error": "Invalid signature."}, 400
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Unhandled error processing payment webhook.")
        raise

    return {"received": True}, 200
