"""Provider errors an operator can act on.

The existing ``app/communications/delivery_status.py`` already turns a Twilio
**messaging** error code into a sentence a garage owner can read; that is the
customer-facing half and is reused verbatim here rather than duplicated. What
Platform Admin needs on top is the operator-facing half: what the code
*means* about the configuration, and what CoMaz should do about it - which is
a different answer from "tell the customer their message didn't arrive".

So one entry has up to three parts:

* ``meaning`` - what the provider is actually telling us,
* ``recommended_action`` - what the operator should do next, and
* the original code and message, always preserved untouched.

Anything not listed still gets a row: the raw code and message, with a
generic meaning. Never invent a specific cause for a code we don't know.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.communications.delivery_status import describe_delivery_failure

CHANNEL_VOICE = "VOICE"
CHANNEL_WHATSAPP = "WHATSAPP"


@dataclass(frozen=True)
class ErrorGuidance:
    meaning: str
    recommended_action: str
    #: True when the fix is something only the *business* can do, so Platform
    #: Admin can say "waiting for customer" rather than queueing an operator
    #: task nobody can complete.
    customer_must_act: bool = False


#: Codes seen during onboarding and day-to-day operation. Sources: Twilio's
#: Messaging, WhatsApp and Voice error dictionaries. Only codes that change
#: what an operator should do are listed.
_GUIDANCE: dict[str, ErrorGuidance] = {
    # --- WhatsApp: messaging window and recipient ------------------------
    "63016": ErrorGuidance(
        meaning=(
            "The business tried to send free-form text more than 24 hours after the "
            "customer last messaged. WhatsApp only allows an approved template "
            "message outside that window. This is a WhatsApp policy limit, not a "
            "broken configuration."
        ),
        recommended_action=(
            "No setup change needed. Move this message onto an approved WhatsApp "
            "template, or have the business wait for the customer to message first."
        ),
    ),
    "63003": ErrorGuidance(
        meaning="The destination number is not reachable on WhatsApp.",
        recommended_action="No setup change needed - check the customer's stored phone number.",
    ),
    "63024": ErrorGuidance(
        meaning="WhatsApp rejected the destination as an invalid recipient.",
        recommended_action="No setup change needed - correct the customer's phone number.",
    ),
    "63013": ErrorGuidance(
        meaning="WhatsApp blocked the message under its business messaging policy.",
        recommended_action=(
            "Review the message content with the business. Repeated policy blocks put "
            "the WABA's quality rating at risk."
        ),
    ),
    "63018": ErrorGuidance(
        meaning="This sender hit WhatsApp's rate limit.",
        recommended_action="Retry shortly. If it recurs, review the business's messaging volume tier.",
    ),
    # --- WhatsApp: sender / account configuration ------------------------
    "63007": ErrorGuidance(
        meaning=(
            "Twilio does not have a correctly configured WhatsApp sender for the "
            "'From' address this business sent as. Usually the sender was never "
            "registered, or it is registered under a different subaccount."
        ),
        recommended_action=(
            "Open this business's Communications setup and check the WhatsApp sender "
            "status; re-register the sender if it is missing."
        ),
    ),
    "63002": ErrorGuidance(
        meaning="Twilio did not recognise the WhatsApp sender number for this account.",
        recommended_action=(
            "Confirm the sender belongs to this business's own Twilio subaccount, then "
            "run Check status."
        ),
    ),
    "63015": ErrorGuidance(
        meaning=(
            "The sender is still the shared Twilio WhatsApp sandbox, not a registered "
            "business sender."
        ),
        recommended_action="Complete WhatsApp onboarding for this business so it gets its own sender.",
    ),
    "63110": ErrorGuidance(
        meaning=(
            "The phone number is already registered to a WhatsApp account, so it "
            "cannot be registered again."
        ),
        recommended_action=(
            "Do not delete anything yourself. Ask the business to remove the existing "
            "WhatsApp / WhatsApp Business App account for this number (or disable 2FA "
            "if it sits on another WhatsApp Business Platform), then retry."
        ),
        customer_must_act=True,
    ),
    "63051": ErrorGuidance(
        meaning="Meta rejected the WhatsApp template used for this message.",
        recommended_action="Review and resubmit the template in the WhatsApp Manager.",
    ),
    "63049": ErrorGuidance(
        meaning="The WhatsApp template is not approved for this recipient's language or category.",
        recommended_action="Check the template's approved languages before sending to this customer.",
    ),
    # --- Destination / account permissions --------------------------------
    "21211": ErrorGuidance(
        meaning="The destination phone number is not a valid number.",
        recommended_action="No setup change needed - correct the customer record.",
    ),
    "21408": ErrorGuidance(
        meaning="This Twilio account is not permitted to message that country.",
        recommended_action=(
            "Enable the destination country in Twilio Geo Permissions for this "
            "business's subaccount."
        ),
    ),
    "21610": ErrorGuidance(
        meaning="The customer replied STOP and is unsubscribed from this sender.",
        recommended_action="No setup change needed, and do not re-subscribe them on their behalf.",
    ),
    "21612": ErrorGuidance(
        meaning="This sender cannot reach that destination number.",
        recommended_action="Check the sender's capabilities and the destination country's rules.",
    ),
    # --- Voice ----------------------------------------------------------
    "11200": ErrorGuidance(
        meaning=(
            "Twilio could not reach this deployment's voice webhook - the call was "
            "answered by Twilio but CoMaz never responded."
        ),
        recommended_action=(
            "Check the API is reachable at its public HTTPS origin, then re-run "
            "Configure voice to rewrite the number's webhook URLs."
        ),
    ),
    "11205": ErrorGuidance(
        meaning="Twilio could not connect to the voice webhook URL (connection refused or timed out).",
        recommended_action="Confirm PUBLIC_API_BASE_URL points at a reachable HTTPS origin, then reconfigure voice.",
    ),
    "12100": ErrorGuidance(
        meaning="Twilio received a response from the voice webhook that was not valid TwiML.",
        recommended_action="Check the API logs for the failing request - the call fell through to the fallback.",
    ),
    "13224": ErrorGuidance(
        meaning="Twilio refused to dial the escalation/fallback number.",
        recommended_action=(
            "Check the escalation number is valid E.164 and that the subaccount's Geo "
            "Permissions allow calls to it."
        ),
    ),
    "32009": ErrorGuidance(
        meaning="The number could not be verified for outbound calling from this account.",
        recommended_action="Verify the destination number in the Twilio console, or use a purchased number.",
    ),
}

_GENERIC = ErrorGuidance(
    meaning="The provider rejected this with a code CoMaz has no specific guidance for.",
    recommended_action=(
        "Look the code up in the provider's error dictionary and record what you find. "
        "The original code and message are preserved above."
    ),
)


def _normalise(code: object) -> str | None:
    if code is None or code == "":
        return None
    return str(code).strip()


def guidance_for(code: object) -> ErrorGuidance:
    """Operator guidance for a provider error code. Always returns something."""
    key = _normalise(code)
    if key is None:
        return _GENERIC
    return _GUIDANCE.get(key, _GENERIC)


def explain(code: object, message: str | None = None, *, channel: str | None = None) -> dict:
    """One admin-readable failure record.

    Carries the *explained* view (meaning, recommended action, and the
    customer-facing sentence ``delivery_status.py`` already produces) without
    ever discarding the provider's own code and message - a support engineer
    reading this must still be able to search Twilio's docs for the raw value.
    """
    key = _normalise(code)
    advice = guidance_for(key)
    return {
        "error_code": key,
        "error_message": message,
        "channel": channel,
        "meaning": advice.meaning,
        "recommended_action": advice.recommended_action,
        "customer_must_act": advice.customer_must_act,
        # The wording the business itself would see for this failure, when
        # there is one - so Platform Admin and the garage app never describe
        # the same failure differently.
        "customer_explanation": describe_delivery_failure(key),
        "known": key in _GUIDANCE,
    }


def known_error_codes() -> list[dict]:
    """The whole catalogue, for the console's reference panel."""
    return [explain(code, None) for code in sorted(_GUIDANCE, key=lambda c: int(c))]
