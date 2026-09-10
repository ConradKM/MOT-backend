"""Communications onboarding: taking one business from "no Twilio at all" to
Voice and WhatsApp live, and saying exactly where it is stuck in between.

Layered so each file has one reason to change:

* ``states.py``      - the two state machines and what each state means. No I/O.
* ``errors.py``      - provider error codes translated into operator guidance.
* ``subaccounts.py`` - Twilio subaccount per business, and the two clients
                       (master-acting vs subaccount-acting) that implies.
* ``voice.py``       - number search/purchase and webhook configuration.
* ``whatsapp.py``    - Meta Embedded Signup inputs and the Twilio Senders v2 calls.
* ``service.py``     - the only module that writes state, and the entry point
                       every Platform Admin action goes through.

Everything Twilio-facing stays server-side. The browser is given Meta's
*public* Embedded Signup identifiers and nothing else: no Twilio Auth Token,
no subaccount token, no Meta access token, no one-time code.
"""

from .states import (
    DISPLAY_STATUSES,
    VOICE_STATUSES,
    WHATSAPP_STATUSES,
    overall_display,
    voice_meaning,
    whatsapp_meaning,
)

__all__ = [
    "DISPLAY_STATUSES",
    "VOICE_STATUSES",
    "WHATSAPP_STATUSES",
    "overall_display",
    "voice_meaning",
    "whatsapp_meaning",
]
