"""Communications setup: provisioning Twilio Voice and WhatsApp per business.

Every view here is ``@platform_admin_required``, and every view that *changes*
anything - which is all of them except the four reads - is additionally
``@superadmin_required`` and audited. That is stricter than the rest of
Platform Admin on purpose: these endpoints spend money (buying numbers),
create resources in a third-party account, and place real calls and messages
to real people.

**Nothing secret crosses this boundary outbound.** No response schema in
``app/platform_admin/schemas.py`` has a field for ``TWILIO_AUTH_TOKEN``, a
subaccount Auth Token, a Meta access token or a one-time code, and the two
secrets that come *in* (an attached subaccount's token, Meta's OTP) are
``load_only``: the first is encrypted on arrival, the second is forwarded to
Twilio and never stored. The audit trail records SIDs and numbers, never
credentials - ``app/platform_admin/audit.py::_scrub`` drops anything
secret-looking as a backstop.
"""

import uuid

from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.communications.provisioning import voice as voice_provisioning
from app.communications.provisioning.errors import known_error_codes
from app.communications.provisioning.service import (
    ProvisioningActionError,
    action_attach_subaccount,
    action_buy_voice_number,
    action_complete_embedded_signup,
    action_configure_voice,
    action_create_subaccount,
    action_mark_migration_complete,
    action_mark_voice_online,
    action_reconfigure_whatsapp_webhooks,
    action_refresh_whatsapp_status,
    action_register_sender,
    action_set_automation_enabled,
    action_set_communications_enabled,
    action_set_voice_routing,
    action_set_whatsapp_number,
    action_start_embedded_signup,
    action_submit_verification_code,
    action_test_voice,
    action_test_whatsapp,
)
from app.communications.provisioning.voice import VoiceProvisioningError
from app.models.platform.audit_log import (
    ACTION_COMMS_AUTOMATION_TOGGLE,
    ACTION_COMMS_ENABLED_TOGGLE,
    ACTION_COMMS_META_SIGNUP,
    ACTION_COMMS_SENDER_REGISTER,
    ACTION_COMMS_SUBACCOUNT_CREATE,
    ACTION_COMMS_TEST,
    ACTION_COMMS_VOICE_CONFIGURE,
    ACTION_COMMS_VOICE_NUMBER,
    ACTION_COMMS_WHATSAPP_NUMBER,
)
from app.platform_admin.audit import record_audit
from app.platform_admin.communications import (
    communications_detail,
    communications_overview,
    recent_channel_errors,
)
from app.platform_admin.schemas import (
    AvailableNumberListSchema,
    AvailableNumberQuerySchema,
    ChannelErrorSchema,
    CommunicationsDetailSchema,
    CommunicationsOverviewQuerySchema,
    CommunicationsOverviewSchema,
    EmbeddedSignupConfigSchema,
    EmbeddedSignupResultSchema,
    ErrorCatalogueSchema,
    SenderRegistrationSchema,
    SubaccountAttachSchema,
    TestCallResultSchema,
    TestCallSchema,
    TestMessageResultSchema,
    TestMessageSchema,
    ToggleSchema,
    VerificationCodeSchema,
    VoiceNumberPurchaseSchema,
    VoiceRoutingSchema,
    WhatsAppNumberSchema,
)
from app.platform_admin.security import (
    get_current_platform_admin,
    platform_admin_required,
    superadmin_required,
)
from app.platform_admin.tenants import get_tenant

platform_communications_blp = Blueprint(
    "platform_admin_communications",
    "platform_admin_communications",
    url_prefix="/api/platform-admin",
    description="Platform Admin: Twilio Voice and WhatsApp setup per business",
)


def _require_tenant(garage_id: uuid.UUID):
    garage = get_tenant(garage_id)
    if garage is None:
        abort(404, message="Business not found.")
    return garage


def _audit(garage, action: str, summary: str, **details):
    record_audit(
        admin=get_current_platform_admin(),
        action=action,
        garage=garage,
        target_type="communications",
        target_id=garage.id,
        summary=summary,
        details=details or None,
    )


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


@platform_communications_blp.route("/operations/communications-setup")
class CommunicationsSetupOverview(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_communications_blp.arguments(CommunicationsOverviewQuerySchema, location="query")
    @platform_communications_blp.response(200, CommunicationsOverviewSchema)
    def get(self, args):
        """Every business and where its communications onboarding has got to.

        The operator worklist behind Operations > Communications Setup: one
        row per business with its subaccount, numbers, per-channel state, the
        current blocker and who has to act next.
        """
        return communications_overview(search=args.get("search"), status=args.get("status"))


@platform_communications_blp.route("/operations/communications-setup/error-codes")
class CommunicationsErrorCatalogue(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_communications_blp.response(200, ErrorCatalogueSchema)
    def get(self):
        """Provider error codes CoMaz has specific guidance for - what each
        one means and what an operator should do about it."""
        return {"items": known_error_codes()}


@platform_communications_blp.route("/tenants/<uuid:garage_id>/communications")
class TenantCommunications(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_communications_blp.response(200, CommunicationsDetailSchema)
    def get(self, garage_id):
        """One business's full Voice and WhatsApp setup, its recent provider
        failures, and exactly which actions are available in its current
        state."""
        return communications_detail(_require_tenant(garage_id))


@platform_communications_blp.route("/tenants/<uuid:garage_id>/communications/errors")
class TenantCommunicationErrors(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_communications_blp.response(200, ChannelErrorSchema(many=True))
    def get(self, garage_id):
        """This business's recent failed Voice/WhatsApp communications.

        Read from ``communication_logs`` - the same rows Operations >
        Failures counts, not a second failure store.
        """
        return recent_channel_errors(_require_tenant(garage_id))


# --------------------------------------------------------------------------
# Subaccount
# --------------------------------------------------------------------------


@platform_communications_blp.route("/tenants/<uuid:garage_id>/communications/subaccount")
class TenantSubaccount(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_communications_blp.response(200, CommunicationsDetailSchema)
    def post(self, garage_id):
        """Create this business's own Twilio subaccount.

        Idempotent: a business that already has one keeps it. The subaccount's
        Auth Token is encrypted on arrival and never returned.
        """
        garage = _require_tenant(garage_id)
        try:
            action_create_subaccount(garage)
        except ProvisioningActionError as exc:
            abort(422, message=str(exc))
        _audit(
            garage,
            ACTION_COMMS_SUBACCOUNT_CREATE,
            f"Created Twilio subaccount for {garage.name}",
            subaccount_sid=garage.communication_settings.twilio_subaccount_sid,
        )
        return communications_detail(garage)

    @jwt_required()
    @superadmin_required
    @platform_communications_blp.arguments(SubaccountAttachSchema)
    @platform_communications_blp.response(200, CommunicationsDetailSchema)
    def put(self, data, garage_id):
        """Attach a subaccount that already exists in the Twilio console.

        Refused if another business already claims that SID - two tenants on
        one subaccount would cross-route their numbers and senders.
        """
        garage = _require_tenant(garage_id)
        try:
            action_attach_subaccount(
                garage,
                subaccount_sid=data["subaccount_sid"],
                auth_token=data["auth_token"],
            )
        except ProvisioningActionError as exc:
            abort(422, message=str(exc))
        # The SID identifies the resource; the token that came with it is
        # already encrypted and is deliberately absent from the audit details.
        _audit(
            garage,
            ACTION_COMMS_SUBACCOUNT_CREATE,
            f"Attached existing Twilio subaccount to {garage.name}",
            subaccount_sid=data["subaccount_sid"],
        )
        return communications_detail(garage)


# --------------------------------------------------------------------------
# Voice
# --------------------------------------------------------------------------


@platform_communications_blp.route(
    "/tenants/<uuid:garage_id>/communications/voice/available-numbers"
)
class VoiceAvailableNumbers(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_communications_blp.arguments(AvailableNumberQuerySchema, location="query")
    @platform_communications_blp.response(200, AvailableNumberListSchema)
    def get(self, args, garage_id):
        """Numbers this business could buy. Read-only - nothing is reserved."""
        garage = _require_tenant(garage_id)
        try:
            return {
                "items": voice_provisioning.search_available_numbers(
                    garage,
                    country=args.get("country"),
                    area_code=args.get("area_code"),
                    contains=args.get("contains"),
                    limit=args.get("limit") or 10,
                )
            }
        except VoiceProvisioningError as exc:
            abort(422, message=str(exc))


@platform_communications_blp.route("/tenants/<uuid:garage_id>/communications/voice/number")
class VoiceNumber(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_communications_blp.arguments(VoiceNumberPurchaseSchema)
    @platform_communications_blp.response(200, CommunicationsDetailSchema)
    def post(self, data, garage_id):
        """Buy a voice number into this business's subaccount, or adopt one it
        already owns (``already_owned``).

        A bought number leaves Twilio already pointed at CoMaz's existing
        voice webhooks, so there is no window in which it answers with
        Twilio's default message.
        """
        garage = _require_tenant(garage_id)
        try:
            action_buy_voice_number(
                garage,
                phone_number=data["phone_number"],
                already_owned=data.get("already_owned", False),
            )
        except ProvisioningActionError as exc:
            abort(422, message=str(exc))
        _audit(
            garage,
            ACTION_COMMS_VOICE_NUMBER,
            f"{'Assigned' if data.get('already_owned') else 'Bought'} voice number "
            f"{data['phone_number']} for {garage.name}",
            phone_number=data["phone_number"],
            already_owned=bool(data.get("already_owned")),
        )
        return communications_detail(garage)


@platform_communications_blp.route("/tenants/<uuid:garage_id>/communications/voice/configure")
class VoiceConfigure(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_communications_blp.response(200, CommunicationsDetailSchema)
    def post(self, garage_id):
        """(Re)point this business's number at CoMaz's voice webhooks.

        Safe to repeat - the repair action after this deployment changes
        origin, as well as a setup step.
        """
        garage = _require_tenant(garage_id)
        try:
            action_configure_voice(garage)
        except ProvisioningActionError as exc:
            abort(422, message=str(exc))
        _audit(
            garage,
            ACTION_COMMS_VOICE_CONFIGURE,
            f"Configured voice webhooks for {garage.name}",
            **voice_provisioning.webhook_urls(),
        )
        return communications_detail(garage)


@platform_communications_blp.route("/tenants/<uuid:garage_id>/communications/voice/routing")
class VoiceRouting(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_communications_blp.arguments(VoiceRoutingSchema)
    @platform_communications_blp.response(200, CommunicationsDetailSchema)
    def put(self, data, garage_id):
        """Set the human escalation number and the outage fallback number."""
        garage = _require_tenant(garage_id)
        action_set_voice_routing(
            garage,
            escalation_number=data.get("escalation_number"),
            fallback_number=data.get("fallback_number"),
        )
        _audit(
            garage,
            ACTION_COMMS_VOICE_CONFIGURE,
            f"Set voice escalation/fallback for {garage.name}",
            escalation_number=data.get("escalation_number"),
            fallback_number=data.get("fallback_number"),
        )
        return communications_detail(garage)


@platform_communications_blp.route("/tenants/<uuid:garage_id>/communications/voice/test")
class VoiceTest(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_communications_blp.arguments(TestCallSchema)
    @platform_communications_blp.response(201, TestCallResultSchema)
    def post(self, data, garage_id):
        """Place a real test call from this business's number.

        Rings a real phone, so it is superadmin-only and audited like any
        other outbound action.
        """
        garage = _require_tenant(garage_id)
        try:
            result = action_test_voice(garage, to_number=data["to_number"])
        except ProvisioningActionError as exc:
            abort(422, message=str(exc))
        _audit(
            garage,
            ACTION_COMMS_TEST,
            f"Placed a test call for {garage.name}",
            channel="VOICE",
            to_number=data["to_number"],
            call_sid=result.get("call_sid"),
        )
        return {**result, "from_": result.get("from")}


@platform_communications_blp.route("/tenants/<uuid:garage_id>/communications/voice/online")
class VoiceMarkOnline(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_communications_blp.response(200, CommunicationsDetailSchema)
    def post(self, garage_id):
        """Confirm voice is live for this business.

        An explicit operator decision - "the test call connected" and "this
        business is ready for its customers" are different claims.
        """
        garage = _require_tenant(garage_id)
        try:
            action_mark_voice_online(garage)
        except ProvisioningActionError as exc:
            abort(422, message=str(exc))
        _audit(garage, ACTION_COMMS_VOICE_CONFIGURE, f"Marked voice online for {garage.name}")
        return communications_detail(garage)


# --------------------------------------------------------------------------
# WhatsApp
# --------------------------------------------------------------------------


@platform_communications_blp.route("/tenants/<uuid:garage_id>/communications/whatsapp/number")
class WhatsAppNumber(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_communications_blp.arguments(WhatsAppNumberSchema)
    @platform_communications_blp.response(200, CommunicationsDetailSchema)
    def put(self, data, garage_id):
        """Record the business's own WhatsApp number - always the first step,
        because Meta needs the full number before Embedded Signup starts.

        ``already_on_whatsapp`` routes onboarding to the migration state
        instead of to signup. CoMaz never clears an existing registration.
        """
        garage = _require_tenant(garage_id)
        try:
            action_set_whatsapp_number(
                garage,
                number_e164=data["number_e164"],
                already_on_whatsapp=data.get("already_on_whatsapp", False),
            )
        except ProvisioningActionError as exc:
            abort(422, message=str(exc))
        _audit(
            garage,
            ACTION_COMMS_WHATSAPP_NUMBER,
            f"Set WhatsApp number {data['number_e164']} for {garage.name}",
            number=data["number_e164"],
            already_on_whatsapp=bool(data.get("already_on_whatsapp")),
        )
        return communications_detail(garage)


@platform_communications_blp.route("/tenants/<uuid:garage_id>/communications/whatsapp/migration")
class WhatsAppMigration(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_communications_blp.response(200, CommunicationsDetailSchema)
    def post(self, garage_id):
        """Record that the business has removed the number's existing WhatsApp
        registration themselves, and setup may continue."""
        garage = _require_tenant(garage_id)
        try:
            action_mark_migration_complete(garage)
        except ProvisioningActionError as exc:
            abort(422, message=str(exc))
        _audit(
            garage,
            ACTION_COMMS_WHATSAPP_NUMBER,
            f"Recorded existing WhatsApp registration cleared for {garage.name}",
        )
        return communications_detail(garage)


@platform_communications_blp.route(
    "/tenants/<uuid:garage_id>/communications/whatsapp/embedded-signup"
)
class WhatsAppEmbeddedSignup(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_communications_blp.response(201, EmbeddedSignupConfigSchema)
    def post(self, garage_id):
        """Start Meta Embedded Signup and return what the browser needs to
        open it.

        Public Meta identifiers plus a one-time ``state`` nonce - no app
        secret and no access token. Meta's window is where the business owner
        authenticates, picks a Business Portfolio, creates a WABA and verifies
        the number; none of that can or should be automated away.
        """
        garage = _require_tenant(garage_id)
        try:
            config = action_start_embedded_signup(garage)
        except ProvisioningActionError as exc:
            abort(422, message=str(exc))
        _audit(
            garage,
            ACTION_COMMS_META_SIGNUP,
            f"Started Meta Embedded Signup for {garage.name}",
        )
        return config


@platform_communications_blp.route(
    "/tenants/<uuid:garage_id>/communications/whatsapp/embedded-signup/complete"
)
class WhatsAppEmbeddedSignupComplete(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_communications_blp.arguments(EmbeddedSignupResultSchema)
    @platform_communications_blp.response(200, CommunicationsDetailSchema)
    def post(self, data, garage_id):
        """Record the WABA Meta handed back when signup finished.

        The ``state`` nonce must match this business's own launch, and the
        WABA must not already belong to another business.
        """
        garage = _require_tenant(garage_id)
        try:
            action_complete_embedded_signup(
                garage,
                state=data["state"],
                waba_id=data["waba_id"],
                phone_number_id=data.get("phone_number_id"),
                business_id=data.get("business_id"),
            )
        except ProvisioningActionError as exc:
            abort(422, message=str(exc))
        _audit(
            garage,
            ACTION_COMMS_META_SIGNUP,
            f"Meta Embedded Signup completed for {garage.name}",
            waba_id=data["waba_id"],
        )
        return communications_detail(garage)


@platform_communications_blp.route("/tenants/<uuid:garage_id>/communications/whatsapp/sender")
class WhatsAppSender(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_communications_blp.arguments(SenderRegistrationSchema)
    @platform_communications_blp.response(201, CommunicationsDetailSchema)
    def post(self, data, garage_id):
        """Register the WhatsApp sender with Twilio.

        Uses this business's own subaccount credentials and passes its WABA
        id, which is what binds that WABA to that subaccount.
        """
        garage = _require_tenant(garage_id)
        try:
            action_register_sender(
                garage,
                display_name=data["display_name"],
                verification_method=data.get("verification_method"),
            )
        except ProvisioningActionError as exc:
            abort(422, message=str(exc))
        _audit(
            garage,
            ACTION_COMMS_SENDER_REGISTER,
            f"Registered WhatsApp sender for {garage.name}",
            display_name=data["display_name"],
            sender_sid=garage.communications_onboarding.whatsapp_sender_sid,
        )
        return communications_detail(garage)

    @jwt_required()
    @superadmin_required
    @platform_communications_blp.response(200, CommunicationsDetailSchema)
    def patch(self, garage_id):
        """Re-point an existing sender at CoMaz's WhatsApp webhooks."""
        garage = _require_tenant(garage_id)
        try:
            action_reconfigure_whatsapp_webhooks(garage)
        except ProvisioningActionError as exc:
            abort(422, message=str(exc))
        _audit(
            garage,
            ACTION_COMMS_SENDER_REGISTER,
            f"Reconfigured WhatsApp sender webhooks for {garage.name}",
        )
        return communications_detail(garage)


@platform_communications_blp.route("/tenants/<uuid:garage_id>/communications/whatsapp/verify")
class WhatsAppVerify(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_communications_blp.arguments(VerificationCodeSchema)
    @platform_communications_blp.response(200, CommunicationsDetailSchema)
    def post(self, data, garage_id):
        """Submit the one-time code Meta sent to the business's number.

        The code is forwarded to Twilio and never stored, never logged and
        never returned.
        """
        garage = _require_tenant(garage_id)
        try:
            action_submit_verification_code(garage, code=data["code"])
        except ProvisioningActionError as exc:
            abort(422, message=str(exc))
        # No `code` in the details, deliberately.
        _audit(
            garage,
            ACTION_COMMS_SENDER_REGISTER,
            f"Submitted WhatsApp verification code for {garage.name}",
        )
        return communications_detail(garage)


@platform_communications_blp.route("/tenants/<uuid:garage_id>/communications/whatsapp/status")
class WhatsAppStatus(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_communications_blp.response(200, CommunicationsDetailSchema)
    def post(self, garage_id):
        """Ask Twilio where this business's sender has got to, and move our
        own state to match."""
        garage = _require_tenant(garage_id)
        try:
            action_refresh_whatsapp_status(garage)
        except ProvisioningActionError as exc:
            abort(422, message=str(exc))
        return communications_detail(garage)


@platform_communications_blp.route("/tenants/<uuid:garage_id>/communications/whatsapp/test")
class WhatsAppTest(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_communications_blp.arguments(TestMessageSchema)
    @platform_communications_blp.response(201, TestMessageResultSchema)
    def post(self, data, garage_id):
        """Send a real WhatsApp message through the ordinary send path, so a
        pass proves the path customers will actually use."""
        garage = _require_tenant(garage_id)
        try:
            result = action_test_whatsapp(garage, to_number=data["to_number"])
        except ProvisioningActionError as exc:
            abort(422, message=str(exc))
        _audit(
            garage,
            ACTION_COMMS_TEST,
            f"Sent a test WhatsApp message for {garage.name}",
            channel="WHATSAPP",
            to_number=data["to_number"],
            status=result.get("status"),
        )
        return result


# --------------------------------------------------------------------------
# Switches
# --------------------------------------------------------------------------


@platform_communications_blp.route("/tenants/<uuid:garage_id>/communications/enabled")
class CommunicationsEnabled(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_communications_blp.arguments(ToggleSchema)
    @platform_communications_blp.response(200, CommunicationsDetailSchema)
    def put(self, data, garage_id):
        """The master switch. Disabling stops every send for this business and
        releases nothing at Twilio, so re-enabling needs no re-provisioning."""
        garage = _require_tenant(garage_id)
        action_set_communications_enabled(garage, enabled=data["enabled"])
        _audit(
            garage,
            ACTION_COMMS_ENABLED_TOGGLE,
            f"{'Enabled' if data['enabled'] else 'Disabled'} communications for {garage.name}",
            enabled=data["enabled"],
        )
        return communications_detail(garage)


@platform_communications_blp.route("/tenants/<uuid:garage_id>/communications/automation")
class CommunicationsAutomation(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_communications_blp.arguments(ToggleSchema)
    @platform_communications_blp.response(200, CommunicationsDetailSchema)
    def put(self, data, garage_id):
        """Turn the conversation engine on or off for this business - the same
        flag ``flask set-conversation-automation`` sets."""
        garage = _require_tenant(garage_id)
        action_set_automation_enabled(garage, enabled=data["enabled"])
        _audit(
            garage,
            ACTION_COMMS_AUTOMATION_TOGGLE,
            f"{'Enabled' if data['enabled'] else 'Disabled'} conversation automation for "
            f"{garage.name}",
            enabled=data["enabled"],
        )
        return communications_detail(garage)
