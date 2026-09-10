"""The communications onboarding state machines, and what each state *means*.

Two channels, two state machines, one shared vocabulary:

* :data:`VOICE_STATUSES` and :data:`WHATSAPP_STATUSES` are the detailed,
  per-channel states persisted on
  :class:`~app.models.communications.comms_onboarding.GarageCommunicationsOnboarding`.
  They are deliberately **not** booleans: "we asked Twilio and are waiting"
  and "Twilio said no" are different situations needing different admin
  actions, and a boolean cannot tell them apart.
* :data:`DISPLAY_STATUSES` are the eight coarse labels Platform Admin shows in
  the Communications Setup list. Every detailed state maps to exactly one of
  them (:data:`VOICE_DISPLAY` / :data:`WHATSAPP_DISPLAY`), so the list view and
  the detail view can never disagree about whether a business is live.

Alongside the mapping, each state carries the three things an operator
actually needs: what is *blocking* it, what **CoMaz** should do next, and what
the **customer** must do next. Those live here rather than in the UI so the
console and any future notification/report reads one definition.

Nothing in this module talks to Twilio, Meta or the database - it is pure
vocabulary, which is what makes it cheap to test exhaustively.
"""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------
# The eight coarse statuses Platform Admin renders
# --------------------------------------------------------------------------

DISPLAY_ONLINE = "ONLINE"
DISPLAY_SETUP_REQUIRED = "SETUP_REQUIRED"
DISPLAY_WAITING_FOR_CUSTOMER = "WAITING_FOR_CUSTOMER"
DISPLAY_WAITING_FOR_META = "WAITING_FOR_META"
DISPLAY_WAITING_FOR_TWILIO = "WAITING_FOR_TWILIO"
DISPLAY_ACTION_REQUIRED = "ACTION_REQUIRED"
DISPLAY_FAILED = "FAILED"
DISPLAY_DISABLED = "DISABLED"

DISPLAY_STATUSES = (
    DISPLAY_ONLINE,
    DISPLAY_SETUP_REQUIRED,
    DISPLAY_WAITING_FOR_CUSTOMER,
    DISPLAY_WAITING_FOR_META,
    DISPLAY_WAITING_FOR_TWILIO,
    DISPLAY_ACTION_REQUIRED,
    DISPLAY_FAILED,
    DISPLAY_DISABLED,
)

#: Human wording for each display status, so the console renders the server's
#: label rather than inventing a second set of words.
DISPLAY_LABELS: dict[str, str] = {
    DISPLAY_ONLINE: "Online",
    DISPLAY_SETUP_REQUIRED: "Setup required",
    DISPLAY_WAITING_FOR_CUSTOMER: "Waiting for customer",
    DISPLAY_WAITING_FOR_META: "Waiting for Meta",
    DISPLAY_WAITING_FOR_TWILIO: "Waiting for Twilio",
    DISPLAY_ACTION_REQUIRED: "Action required",
    DISPLAY_FAILED: "Failed",
    DISPLAY_DISABLED: "Disabled",
}


# --------------------------------------------------------------------------
# Voice
# --------------------------------------------------------------------------

VOICE_NOT_STARTED = "NOT_STARTED"
VOICE_SUBACCOUNT_PENDING = "SUBACCOUNT_PENDING"
VOICE_SUBACCOUNT_READY = "SUBACCOUNT_READY"
VOICE_NUMBER_PURCHASING = "NUMBER_PURCHASING"
VOICE_NUMBER_ASSIGNED = "NUMBER_ASSIGNED"
VOICE_WEBHOOKS_CONFIGURED = "WEBHOOKS_CONFIGURED"
VOICE_TESTING = "TESTING"
VOICE_ONLINE = "ONLINE"
VOICE_ACTION_REQUIRED = "ACTION_REQUIRED"
VOICE_FAILED = "FAILED"
VOICE_DISABLED = "DISABLED"

VOICE_STATUSES = (
    VOICE_NOT_STARTED,
    VOICE_SUBACCOUNT_PENDING,
    VOICE_SUBACCOUNT_READY,
    VOICE_NUMBER_PURCHASING,
    VOICE_NUMBER_ASSIGNED,
    VOICE_WEBHOOKS_CONFIGURED,
    VOICE_TESTING,
    VOICE_ONLINE,
    VOICE_ACTION_REQUIRED,
    VOICE_FAILED,
    VOICE_DISABLED,
)


# --------------------------------------------------------------------------
# WhatsApp
# --------------------------------------------------------------------------

WA_NOT_STARTED = "NOT_STARTED"
WA_NUMBER_ENTERED = "NUMBER_ENTERED"
#: The number is already in use by consumer WhatsApp or the WhatsApp Business
#: App. CoMaz never migrates or deletes that registration itself - see the
#: module docstring of app/communications/provisioning/whatsapp.py.
WA_EXISTING_MIGRATION_REQUIRED = "EXISTING_WHATSAPP_MIGRATION_REQUIRED"
WA_META_SIGNUP_REQUIRED = "META_SIGNUP_REQUIRED"
WA_META_SIGNUP_IN_PROGRESS = "META_SIGNUP_IN_PROGRESS"
WA_META_SIGNUP_COMPLETED = "META_SIGNUP_COMPLETED"
WA_WABA_RECEIVED = "WABA_RECEIVED"
WA_TWILIO_SUBACCOUNT_READY = "TWILIO_SUBACCOUNT_READY"
WA_SENDER_REGISTRATION_PENDING = "SENDER_REGISTRATION_PENDING"
WA_OTP_REQUIRED = "OTP_REQUIRED"
WA_SENDER_REGISTERING = "SENDER_REGISTERING"
WA_TESTING = "TESTING"
WA_ONLINE = "ONLINE"
WA_ACTION_REQUIRED = "ACTION_REQUIRED"
WA_FAILED = "FAILED"
WA_DISABLED = "DISABLED"

WHATSAPP_STATUSES = (
    WA_NOT_STARTED,
    WA_NUMBER_ENTERED,
    WA_EXISTING_MIGRATION_REQUIRED,
    WA_META_SIGNUP_REQUIRED,
    WA_META_SIGNUP_IN_PROGRESS,
    WA_META_SIGNUP_COMPLETED,
    WA_WABA_RECEIVED,
    WA_TWILIO_SUBACCOUNT_READY,
    WA_SENDER_REGISTRATION_PENDING,
    WA_OTP_REQUIRED,
    WA_SENDER_REGISTERING,
    WA_TESTING,
    WA_ONLINE,
    WA_ACTION_REQUIRED,
    WA_FAILED,
    WA_DISABLED,
)


# --------------------------------------------------------------------------
# What each state means
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StateMeaning:
    """Everything Platform Admin needs to render one channel's state.

    ``blocker`` answers "why isn't this live?"; ``admin_action`` and
    ``customer_action`` answer "so who does what next?". A state where CoMaz
    is not waiting on anybody (ONLINE, DISABLED) has none of the three.
    """

    label: str
    display: str
    blocker: str | None = None
    admin_action: str | None = None
    customer_action: str | None = None


VOICE_MEANINGS: dict[str, StateMeaning] = {
    VOICE_NOT_STARTED: StateMeaning(
        label="Not started",
        display=DISPLAY_SETUP_REQUIRED,
        blocker="No Twilio subaccount has been created for this business.",
        admin_action="Create Twilio subaccount",
    ),
    VOICE_SUBACCOUNT_PENDING: StateMeaning(
        label="Creating subaccount",
        display=DISPLAY_WAITING_FOR_TWILIO,
        blocker="Twilio has not yet confirmed the subaccount.",
        admin_action="Check status",
    ),
    VOICE_SUBACCOUNT_READY: StateMeaning(
        label="Subaccount ready",
        display=DISPLAY_SETUP_REQUIRED,
        blocker="This business has no voice number yet.",
        admin_action="Buy voice number",
    ),
    VOICE_NUMBER_PURCHASING: StateMeaning(
        label="Buying number",
        display=DISPLAY_WAITING_FOR_TWILIO,
        blocker="Twilio has not yet confirmed the number purchase.",
        admin_action="Check status",
    ),
    VOICE_NUMBER_ASSIGNED: StateMeaning(
        label="Number assigned",
        display=DISPLAY_SETUP_REQUIRED,
        blocker="The number's webhooks do not point at CoMaz yet.",
        admin_action="Configure voice",
    ),
    VOICE_WEBHOOKS_CONFIGURED: StateMeaning(
        label="Webhooks configured",
        display=DISPLAY_SETUP_REQUIRED,
        blocker="No successful test call has been recorded.",
        admin_action="Test voice",
    ),
    VOICE_TESTING: StateMeaning(
        label="Test call in progress",
        display=DISPLAY_WAITING_FOR_TWILIO,
        blocker="Waiting for the test call to complete.",
        admin_action="Check status",
    ),
    VOICE_ONLINE: StateMeaning(label="Online", display=DISPLAY_ONLINE),
    VOICE_ACTION_REQUIRED: StateMeaning(
        label="Action required",
        display=DISPLAY_ACTION_REQUIRED,
        blocker="Voice setup stopped and needs an operator decision.",
        admin_action="Review the last error and retry setup",
    ),
    VOICE_FAILED: StateMeaning(
        label="Failed",
        display=DISPLAY_FAILED,
        blocker="The last voice provisioning step failed at Twilio.",
        admin_action="Retry setup",
    ),
    VOICE_DISABLED: StateMeaning(label="Disabled", display=DISPLAY_DISABLED),
}


WHATSAPP_MEANINGS: dict[str, StateMeaning] = {
    WA_NOT_STARTED: StateMeaning(
        label="Not started",
        display=DISPLAY_SETUP_REQUIRED,
        blocker="No WhatsApp number has been recorded for this business.",
        admin_action="Connect WhatsApp",
        customer_action="Confirm which number the business wants to use on WhatsApp.",
    ),
    WA_NUMBER_ENTERED: StateMeaning(
        label="Number recorded",
        display=DISPLAY_SETUP_REQUIRED,
        blocker="Meta Embedded Signup has not been started for this number.",
        admin_action="Continue Meta setup",
        customer_action="Be ready to sign in to Facebook/Meta with the business's own account.",
    ),
    WA_EXISTING_MIGRATION_REQUIRED: StateMeaning(
        label="Existing WhatsApp registration",
        display=DISPLAY_ACTION_REQUIRED,
        blocker=(
            "This number is already registered with WhatsApp or the WhatsApp "
            "Business App. CoMaz will not delete or migrate that account."
        ),
        admin_action="Confirm with the business, then mark the migration done",
        customer_action=(
            "Delete the existing WhatsApp / WhatsApp Business App account for this "
            "number (or turn off two-factor authentication if it is already on the "
            "WhatsApp Business Platform), then tell CoMaz it is clear."
        ),
    ),
    WA_META_SIGNUP_REQUIRED: StateMeaning(
        label="Meta signup required",
        display=DISPLAY_WAITING_FOR_CUSTOMER,
        blocker="The business has not completed Meta Embedded Signup.",
        admin_action="Send or launch the Embedded Signup link",
        customer_action="Complete Meta Embedded Signup and consent to CoMaz messaging on their behalf.",
    ),
    WA_META_SIGNUP_IN_PROGRESS: StateMeaning(
        label="Meta signup in progress",
        display=DISPLAY_WAITING_FOR_CUSTOMER,
        blocker="Embedded Signup was launched but has not reported completion.",
        admin_action="Check status",
        customer_action="Finish the Meta window that was opened - do not close it early.",
    ),
    WA_META_SIGNUP_COMPLETED: StateMeaning(
        label="Meta signup completed",
        display=DISPLAY_WAITING_FOR_META,
        blocker="Meta has not yet returned a WhatsApp Business Account id.",
        admin_action="Check status",
    ),
    WA_WABA_RECEIVED: StateMeaning(
        label="WABA received",
        display=DISPLAY_SETUP_REQUIRED,
        blocker="The WABA is not yet attached to a dedicated Twilio subaccount.",
        admin_action="Create Twilio subaccount",
    ),
    WA_TWILIO_SUBACCOUNT_READY: StateMeaning(
        label="Subaccount ready",
        display=DISPLAY_SETUP_REQUIRED,
        blocker="The WhatsApp sender has not been registered with Twilio.",
        admin_action="Register sender",
    ),
    WA_SENDER_REGISTRATION_PENDING: StateMeaning(
        label="Sender registration pending",
        display=DISPLAY_WAITING_FOR_TWILIO,
        blocker="Twilio has accepted the sender but has not brought it online.",
        admin_action="Check status",
    ),
    WA_OTP_REQUIRED: StateMeaning(
        label="Verification code required",
        display=DISPLAY_WAITING_FOR_CUSTOMER,
        blocker="Meta sent a one-time code to the number and Twilio is waiting for it.",
        admin_action="Enter the code the business received",
        customer_action="Read out the WhatsApp verification code sent to the business number.",
    ),
    WA_SENDER_REGISTERING: StateMeaning(
        label="Sender registering",
        display=DISPLAY_WAITING_FOR_META,
        blocker="Meta is still reviewing or activating this sender.",
        admin_action="Check status",
    ),
    WA_TESTING: StateMeaning(
        label="Testing",
        display=DISPLAY_WAITING_FOR_TWILIO,
        blocker="A test message has been sent and its result is not in yet.",
        admin_action="Check status",
    ),
    WA_ONLINE: StateMeaning(label="Online", display=DISPLAY_ONLINE),
    WA_ACTION_REQUIRED: StateMeaning(
        label="Action required",
        display=DISPLAY_ACTION_REQUIRED,
        blocker="WhatsApp setup stopped and needs an operator decision.",
        admin_action="Review the last error and retry setup",
    ),
    WA_FAILED: StateMeaning(
        label="Failed",
        display=DISPLAY_FAILED,
        blocker="The last WhatsApp provisioning step failed.",
        admin_action="Retry setup",
    ),
    WA_DISABLED: StateMeaning(label="Disabled", display=DISPLAY_DISABLED),
}


def voice_meaning(status: str | None) -> StateMeaning:
    return VOICE_MEANINGS.get(status or VOICE_NOT_STARTED, VOICE_MEANINGS[VOICE_NOT_STARTED])


def whatsapp_meaning(status: str | None) -> StateMeaning:
    return WHATSAPP_MEANINGS.get(status or WA_NOT_STARTED, WHATSAPP_MEANINGS[WA_NOT_STARTED])


#: Twilio Senders API status -> our WhatsApp state. Twilio's own vocabulary
#: (verified against the Senders v2 resource) is narrower than ours: it says
#: nothing about Meta Embedded Signup, which happens before a sender exists.
#: Anything Twilio reports that isn't listed here leaves our state alone
#: rather than guessing - see whatsapp.py::apply_sender_status.
SENDER_STATUS_TO_STATE: dict[str, str] = {
    "CREATING": WA_SENDER_REGISTRATION_PENDING,
    "STUBBED": WA_SENDER_REGISTRATION_PENDING,
    "DRAFT": WA_SENDER_REGISTRATION_PENDING,
    "PENDING_VERIFICATION": WA_OTP_REQUIRED,
    "VERIFYING": WA_SENDER_REGISTERING,
    "TWILIO_REVIEW": WA_SENDER_REGISTERING,
    "ONLINE": WA_ONLINE,
    "ONLINE:UPDATING": WA_ONLINE,
    "OFFLINE": WA_ACTION_REQUIRED,
}


#: The single worst-news-first ordering used to derive a business's *overall*
#: communications status from its two channels. A business with voice online
#: and WhatsApp failed is "Failed", not "Online".
_DISPLAY_SEVERITY = {
    DISPLAY_FAILED: 0,
    DISPLAY_ACTION_REQUIRED: 1,
    DISPLAY_SETUP_REQUIRED: 2,
    DISPLAY_WAITING_FOR_CUSTOMER: 3,
    DISPLAY_WAITING_FOR_META: 4,
    DISPLAY_WAITING_FOR_TWILIO: 5,
    DISPLAY_ONLINE: 6,
    DISPLAY_DISABLED: 7,
}


def overall_display(*displays: str) -> str:
    """The one status for a business, given each channel's.

    Worst news wins, with two deliberate exceptions at the bottom of the
    ordering: ONLINE only if every channel is online or disabled, and
    DISABLED only if *everything* is disabled - a half-disabled business is
    reported by whatever its live channel is doing.
    """
    candidates = [d for d in displays if d]
    if not candidates:
        return DISPLAY_SETUP_REQUIRED
    if all(d == DISPLAY_DISABLED for d in candidates):
        return DISPLAY_DISABLED
    live = [d for d in candidates if d != DISPLAY_DISABLED]
    return min(live, key=lambda d: _DISPLAY_SEVERITY.get(d, 2))
