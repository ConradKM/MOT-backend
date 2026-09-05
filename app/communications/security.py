"""Twilio webhook signature verification.

Twilio signs every webhook request - HMAC-SHA1 over the exact URL it called
plus the POSTed form fields, keyed by the account's Auth Token - and sends
the result as ``X-Twilio-Signature``
(see https://www.twilio.com/docs/usage/webhooks/webhooks-security). This
recomputes that signature with the official SDK and compares it before
anything in this codebase trusts a webhook payload.

The URL used for the comparison is rebuilt from ``PUBLIC_API_BASE_URL``
rather than trusted from Flask's own ``request.url`` - behind a reverse proxy
or tunnel, Flask can report the wrong scheme/host unless proxy headers are
wired up correctly, which would make every signature silently fail (or, set
up wrong, silently pass). ``PUBLIC_API_BASE_URL`` already has to be correct
for another reason (it's what you hand Twilio when configuring a number), so
reusing it here means both agree by construction instead of by coincidence.
"""

from __future__ import annotations

from flask import Request, current_app
from twilio.request_validator import RequestValidator

from .config import is_twilio_configured


def _external_url(request: Request) -> str:
    base = (current_app.config.get("PUBLIC_API_BASE_URL") or "").rstrip("/")
    url = f"{base}{request.path}"
    if request.query_string:
        url = f"{url}?{request.query_string.decode()}"
    return url


def validate_twilio_request(request: Request) -> bool:
    """Whether ``request`` carries a valid Twilio signature for its body.

    Returns ``True`` unchecked only when ``TWILIO_WEBHOOK_VALIDATE`` is
    explicitly disabled (local/manual testing - see .env.example); returns
    ``False`` outright when Twilio isn't configured, since without an Auth
    Token there is no key to verify against and nothing should be trusted.
    """
    if not current_app.config.get("TWILIO_WEBHOOK_VALIDATE", True):
        current_app.logger.warning(
            "[twilio] webhook signature validation is DISABLED "
            "(TWILIO_WEBHOOK_VALIDATE=false) - never run production traffic like this."
        )
        return True

    if not is_twilio_configured():
        return False

    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        return False

    validator = RequestValidator(current_app.config["TWILIO_AUTH_TOKEN"])
    return validator.validate(_external_url(request), request.form.to_dict(), signature)
