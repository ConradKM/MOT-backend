"""Communications setup in Platform Admin: the Voice and WhatsApp onboarding
state machines, the provisioning endpoints, and the tenancy guarantees that
keep one business's Twilio and Meta resources out of another's.

Twilio is never called for real. Every test that would reach the network
patches the provisioning module's own seam (``subaccounts``/``voice``/
``whatsapp``), which is also the seam production code goes through - so a
passing test proves the orchestration, state transitions and persistence, not
a mock of them.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.communications.provisioning import states
from app.communications.secrets import decrypt_secret, encrypt_secret
from app.models.communications.comms_onboarding import (
    GarageCommunicationsOnboarding,
    TwilioSubaccountCredential,
)
from app.models.communications.communication_log import CommunicationLog
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)
from app.models.platform.audit_log import PlatformAuditLog

COMMS = "/api/platform-admin/tenants/{garage_id}/communications"
OVERVIEW = "/api/platform-admin/operations/communications-setup"


@pytest.fixture()
def settings(session, garage):
    row = GarageCommunicationSettings(garage_id=garage.id)
    session.add(row)
    session.commit()
    return row


@pytest.fixture()
def provisioned(session, garage, settings):
    """A business with a subaccount and a configured voice number - the state
    most WhatsApp tests want to start from."""
    settings.twilio_subaccount_sid = "ACtest0000000000000000000000000001"
    settings.voice_phone_number = "+441234567890"
    settings.voice_number_sid = "PNtest0000000000000000000000000001"
    session.add(
        TwilioSubaccountCredential(
            subaccount_sid=settings.twilio_subaccount_sid,
            auth_token_encrypted=encrypt_secret("subaccount-token"),
        )
    )
    row = GarageCommunicationsOnboarding(
        garage_id=garage.id,
        voice_status=states.VOICE_WEBHOOKS_CONFIGURED,
        voice_webhooks_configured=True,
    )
    session.add(row)
    session.commit()
    return row


# --------------------------------------------------------------------------
# State machine vocabulary
# --------------------------------------------------------------------------


def test_every_voice_state_has_a_meaning():
    """A state with no meaning would render as a blank blocker in the console."""
    for status in states.VOICE_STATUSES:
        meaning = states.voice_meaning(status)
        assert meaning.label
        assert meaning.display in states.DISPLAY_STATUSES


def test_every_whatsapp_state_has_a_meaning():
    for status in states.WHATSAPP_STATUSES:
        meaning = states.whatsapp_meaning(status)
        assert meaning.label
        assert meaning.display in states.DISPLAY_STATUSES


def test_live_states_have_no_blocker_and_stuck_states_do():
    assert states.voice_meaning(states.VOICE_ONLINE).blocker is None
    assert states.whatsapp_meaning(states.WA_ONLINE).blocker is None
    assert states.voice_meaning(states.VOICE_NOT_STARTED).blocker
    assert states.whatsapp_meaning(states.WA_OTP_REQUIRED).blocker


def test_existing_whatsapp_registration_asks_the_customer_to_act():
    """CoMaz must never present deleting a customer's WhatsApp account as
    something it will do."""
    meaning = states.whatsapp_meaning(states.WA_EXISTING_MIGRATION_REQUIRED)
    assert meaning.display == states.DISPLAY_ACTION_REQUIRED
    assert meaning.customer_action


def test_overall_status_reports_the_worst_channel():
    assert (
        states.overall_display(states.DISPLAY_ONLINE, states.DISPLAY_FAILED)
        == states.DISPLAY_FAILED
    )
    assert (
        states.overall_display(states.DISPLAY_ONLINE, states.DISPLAY_ONLINE)
        == states.DISPLAY_ONLINE
    )


def test_a_half_disabled_business_reports_its_live_channel():
    """DISABLED must not mask a channel that is still working, or still stuck."""
    assert (
        states.overall_display(states.DISPLAY_DISABLED, states.DISPLAY_SETUP_REQUIRED)
        == states.DISPLAY_SETUP_REQUIRED
    )
    assert (
        states.overall_display(states.DISPLAY_DISABLED, states.DISPLAY_DISABLED)
        == states.DISPLAY_DISABLED
    )


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------


def test_garage_credentials_cannot_read_communications_setup(authenticated_client, garage):
    """A garage owner's token is authenticated, but never as a platform admin."""
    assert authenticated_client.get(OVERVIEW).status_code == 403
    assert authenticated_client.get(COMMS.format(garage_id=garage.id)).status_code == 403


def test_support_admin_may_read_but_not_provision(support_client, garage, settings):
    assert support_client.get(OVERVIEW).status_code == 200
    assert support_client.get(COMMS.format(garage_id=garage.id)).status_code == 200
    # Provisioning spends money and creates third-party resources: superadmin only.
    assert support_client.post(f"{COMMS.format(garage_id=garage.id)}/subaccount").status_code == 403
    assert (
        support_client.put(
            f"{COMMS.format(garage_id=garage.id)}/whatsapp/number",
            json={"number_e164": "+447700900123"},
        ).status_code
        == 403
    )


def test_unknown_business_is_a_404(platform_client):
    response = platform_client.get(COMMS.format(garage_id=uuid.uuid4()))
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------


def test_overview_lists_every_business_with_a_blocker(platform_client, garage, second_garage):
    body = platform_client.get(OVERVIEW).get_json()
    names = {item["garage_name"] for item in body["items"]}
    assert {garage.name, second_garage.name} <= names

    row = next(item for item in body["items"] if item["garage_name"] == garage.name)
    assert row["display_status"] == states.DISPLAY_SETUP_REQUIRED
    assert row["blocker"]
    assert row["next_admin_action"]
    assert row["voice_status"] == states.VOICE_NOT_STARTED
    assert row["whatsapp_status"] == states.WA_NOT_STARTED


def test_overview_reports_platform_prerequisites_once(platform_client):
    """Deployment-level blockers belong on the page, not on every row."""
    platform = platform_client.get(OVERVIEW).get_json()["platform"]
    assert platform["twilio_configured"] is False
    assert platform["secrets_configured"] is True
    keys = {item["key"] for item in platform["embedded_signup"]}
    assert {"meta_app_id", "meta_config_id", "twilio_solution_id"} <= keys
    # Every unmet prerequisite says how to fix it.
    for item in platform["embedded_signup"]:
        if not item["satisfied"]:
            assert item["how_to_fix"]


def test_overview_filters_by_status(platform_client, garage, provisioned):
    body = platform_client.get(f"{OVERVIEW}?status=ONLINE").get_json()
    assert body["items"] == []
    body = platform_client.get(f"{OVERVIEW}?status=SETUP_REQUIRED").get_json()
    assert any(item["garage_id"] == str(garage.id) for item in body["items"])


def test_overview_never_exposes_a_credential(platform_client, garage, provisioned):
    """The subaccount SID identifies the resource; its token must not appear
    anywhere in a response."""
    raw = platform_client.get(OVERVIEW).get_data(as_text=True)
    assert "ACtest0000000000000000000000000001" in raw
    assert "subaccount-token" not in raw
    assert "auth_token" not in raw


# --------------------------------------------------------------------------
# Subaccount
# --------------------------------------------------------------------------


def test_create_subaccount_stores_the_token_encrypted(
    monkeypatch, platform_client, session, garage, settings
):
    from app.communications.provisioning import subaccounts

    monkeypatch.setattr(subaccounts, "is_twilio_configured", lambda: True)
    monkeypatch.setattr(
        subaccounts,
        "get_twilio_client",
        lambda: SimpleNamespace(
            api=SimpleNamespace(
                v2010=SimpleNamespace(
                    accounts=SimpleNamespace(
                        create=lambda friendly_name: SimpleNamespace(
                            sid="ACnew000000000000000000000000000001",
                            auth_token="brand-new-token",
                        )
                    )
                )
            )
        ),
    )

    response = platform_client.post(f"{COMMS.format(garage_id=garage.id)}/subaccount")
    assert response.status_code == 200
    body = response.get_json()
    assert body["twilio_subaccount_sid"] == "ACnew000000000000000000000000000001"
    assert body["subaccount_state"] == "READY"
    assert "brand-new-token" not in response.get_data(as_text=True)

    stored = (
        session.query(TwilioSubaccountCredential)
        .filter_by(subaccount_sid="ACnew000000000000000000000000000001")
        .one()
    )
    # Encrypted at rest, and decryptable only with the configured key.
    assert stored.auth_token_encrypted != "brand-new-token"
    assert decrypt_secret(stored.auth_token_encrypted) == "brand-new-token"


def test_create_subaccount_is_idempotent(platform_client, garage, provisioned, settings):
    """A double-click must not leave a stray Twilio subaccount behind."""
    response = platform_client.post(f"{COMMS.format(garage_id=garage.id)}/subaccount")
    assert response.status_code == 200
    assert response.get_json()["twilio_subaccount_sid"] == "ACtest0000000000000000000000000001"


def test_a_subaccount_cannot_be_shared_between_businesses(
    platform_client, garage, second_garage, provisioned
):
    """The tenancy guarantee: one subaccount, one business."""
    response = platform_client.put(
        f"{COMMS.format(garage_id=second_garage.id)}/subaccount",
        json={
            "subaccount_sid": "ACtest0000000000000000000000000001",
            "auth_token": "someone-elses-token",
        },
    )
    assert response.status_code == 422
    assert garage.name in response.get_json()["message"]


def test_attaching_a_subaccount_never_echoes_its_token(platform_client, session, second_garage):
    response = platform_client.put(
        f"{COMMS.format(garage_id=second_garage.id)}/subaccount",
        json={
            "subaccount_sid": "ACother00000000000000000000000001",
            "auth_token": "attached-token-value",
        },
    )
    assert response.status_code == 200
    assert "attached-token-value" not in response.get_data(as_text=True)

    entry = (
        session.query(PlatformAuditLog)
        .filter_by(action="tenant.communications.subaccount")
        .order_by(PlatformAuditLog.created_at.desc())
        .first()
    )
    assert entry is not None
    assert entry.details == {"subaccount_sid": "ACother00000000000000000000000001"}


# --------------------------------------------------------------------------
# Voice
# --------------------------------------------------------------------------


def test_buying_a_number_needs_a_subaccount_first(platform_client, garage, settings):
    response = platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/voice/number",
        json={"phone_number": "+441234567890"},
    )
    assert response.status_code == 422
    assert "subaccount" in response.get_json()["message"]


def test_buying_a_number_configures_webhooks_in_the_same_step(
    monkeypatch, platform_client, session, garage, settings
):
    """A bought number must never be live-but-unconfigured."""
    settings.twilio_subaccount_sid = "ACtest0000000000000000000000000009"
    session.commit()

    from app.communications.provisioning import service

    monkeypatch.setattr(
        service.voice,
        "buy_number",
        lambda garage, phone_number: {
            "phone_number": phone_number,
            "sid": "PNbought0000000000000000000000001",
            "capabilities": ["voice", "SMS"],
        },
    )

    response = platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/voice/number",
        json={"phone_number": "+441234567890"},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["voice"]["status"] == states.VOICE_WEBHOOKS_CONFIGURED
    assert body["voice"]["phone_number"] == "+441234567890"
    assert body["voice"]["capabilities"] == ["voice", "SMS"]
    assert body["voice"]["webhooks_configured"] is True

    # Mirrored onto the row inbound webhooks resolve a tenant against.
    session.refresh(settings)
    assert settings.voice_phone_number == "+441234567890"
    assert settings.voice_number_sid == "PNbought0000000000000000000000001"


def test_a_failed_purchase_records_the_provider_error(
    monkeypatch, platform_client, session, garage, settings
):
    """The error is persisted before the request fails, so the console can
    explain what happened rather than showing a bare 422."""
    settings.twilio_subaccount_sid = "ACtest0000000000000000000000000009"
    session.commit()

    from app.communications.provisioning import service
    from app.communications.provisioning.voice import VoiceProvisioningError

    def boom(garage, phone_number):
        raise VoiceProvisioningError("Twilio has no such number.", code="21422")

    monkeypatch.setattr(service.voice, "buy_number", boom)

    response = platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/voice/number",
        json={"phone_number": "+441234567890"},
    )
    assert response.status_code == 422

    detail = platform_client.get(COMMS.format(garage_id=garage.id)).get_json()
    assert detail["voice"]["status"] == states.VOICE_FAILED
    assert detail["voice"]["last_error"]["error_code"] == "21422"
    # The raw provider message survives alongside the explanation.
    assert "no such number" in detail["voice"]["last_error"]["error_message"]
    assert detail["voice"]["last_error"]["recommended_action"]


def test_voice_routing_is_stored_and_used_for_escalation(
    platform_client, session, garage, provisioned, settings
):
    response = platform_client.put(
        f"{COMMS.format(garage_id=garage.id)}/voice/routing",
        json={"escalation_number": "+447700900999", "fallback_number": "+447700900888"},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["voice"]["escalation_number"] == "+447700900999"
    assert body["voice"]["fallback_number"] == "+447700900888"

    session.refresh(settings)
    assert settings.voice_escalation_number == "+447700900999"


def test_marking_voice_online_requires_configured_webhooks(
    platform_client, session, garage, settings
):
    settings.twilio_subaccount_sid = "ACtest0000000000000000000000000009"
    settings.voice_phone_number = "+441234567890"
    session.commit()

    response = platform_client.post(f"{COMMS.format(garage_id=garage.id)}/voice/online")
    assert response.status_code == 422
    assert "webhook" in response.get_json()["message"].lower()


def test_marking_voice_online_enables_communications(
    platform_client, session, garage, provisioned, settings
):
    response = platform_client.post(f"{COMMS.format(garage_id=garage.id)}/voice/online")
    assert response.status_code == 200
    body = response.get_json()
    assert body["voice"]["status"] == states.VOICE_ONLINE
    assert body["voice"]["online_at"]
    assert body["communications_enabled"] is True


def test_reconfiguring_voice_never_demotes_a_live_number(
    monkeypatch, platform_client, session, garage, provisioned, settings
):
    """Repairing an ONLINE business must not make the setup list claim it is
    no longer live."""
    provisioned.voice_status = states.VOICE_ONLINE
    session.commit()

    from app.communications.provisioning import service

    monkeypatch.setattr(
        service.voice,
        "configure_number",
        lambda garage, number_sid: {
            "phone_number": "+441234567890",
            "sid": number_sid,
            "voice_url": "https://api.example/api/webhooks/twilio/voice/incoming",
            "status_callback": "https://api.example/api/webhooks/twilio/voice/status",
            "configured_at": None,
        },
    )

    response = platform_client.post(f"{COMMS.format(garage_id=garage.id)}/voice/configure")
    assert response.status_code == 200
    assert response.get_json()["voice"]["status"] == states.VOICE_ONLINE


# --------------------------------------------------------------------------
# WhatsApp
# --------------------------------------------------------------------------


def test_recording_a_number_already_on_whatsapp_stops_at_migration(
    platform_client, garage, settings
):
    """CoMaz must not walk into a registration it would have to destroy."""
    response = platform_client.put(
        f"{COMMS.format(garage_id=garage.id)}/whatsapp/number",
        json={"number_e164": "+447700900123", "already_on_whatsapp": True},
    )
    assert response.status_code == 200
    whatsapp = response.get_json()["whatsapp"]
    assert whatsapp["status"] == states.WA_EXISTING_MIGRATION_REQUIRED
    assert whatsapp["display_status"] == states.DISPLAY_ACTION_REQUIRED
    # The console is given instructions for the business, not a destructive action.
    assert whatsapp["existing_registration"]["steps"]
    assert "never deletes" in whatsapp["existing_registration"]["comaz_will_not"]


def test_embedded_signup_is_refused_while_migration_is_outstanding(
    platform_client, garage, settings
):
    platform_client.put(
        f"{COMMS.format(garage_id=garage.id)}/whatsapp/number",
        json={"number_e164": "+447700900123", "already_on_whatsapp": True},
    )
    response = platform_client.post(f"{COMMS.format(garage_id=garage.id)}/whatsapp/embedded-signup")
    assert response.status_code == 422
    assert "existing WhatsApp account" in response.get_json()["message"]


def test_migration_can_be_marked_complete_and_setup_continues(platform_client, garage, settings):
    platform_client.put(
        f"{COMMS.format(garage_id=garage.id)}/whatsapp/number",
        json={"number_e164": "+447700900123", "already_on_whatsapp": True},
    )
    response = platform_client.post(f"{COMMS.format(garage_id=garage.id)}/whatsapp/migration")
    assert response.status_code == 200
    assert response.get_json()["whatsapp"]["status"] == states.WA_NUMBER_ENTERED


def test_embedded_signup_is_refused_until_meta_is_configured(platform_client, garage, settings):
    """No sending a business into a Meta window that cannot finish."""
    platform_client.put(
        f"{COMMS.format(garage_id=garage.id)}/whatsapp/number",
        json={"number_e164": "+447700900123"},
    )
    response = platform_client.post(f"{COMMS.format(garage_id=garage.id)}/whatsapp/embedded-signup")
    assert response.status_code == 422
    assert "Embedded Signup is not configured" in response.get_json()["message"]


@pytest.fixture()
def meta_configured(app):
    """The three external identifiers Embedded Signup needs, as a live
    deployment would have them after Meta and Twilio approval."""
    app.config["META_APP_ID"] = "1234567890"
    app.config["META_EMBEDDED_SIGNUP_CONFIG_ID"] = "9876543210"
    app.config["TWILIO_PARTNER_SOLUTION_ID"] = "solution-abc"
    app.config["PUBLIC_API_BASE_URL"] = "https://api.comaz.example"
    yield
    app.config["META_APP_ID"] = ""
    app.config["META_EMBEDDED_SIGNUP_CONFIG_ID"] = ""
    app.config["TWILIO_PARTNER_SOLUTION_ID"] = ""
    app.config["PUBLIC_API_BASE_URL"] = "http://localhost:5001"


def test_embedded_signup_returns_public_identifiers_only(
    platform_client, garage, settings, meta_configured
):
    platform_client.put(
        f"{COMMS.format(garage_id=garage.id)}/whatsapp/number",
        json={"number_e164": "+447700900123"},
    )
    response = platform_client.post(f"{COMMS.format(garage_id=garage.id)}/whatsapp/embedded-signup")
    assert response.status_code == 201
    body = response.get_json()
    assert body["ready"] is True
    assert body["app_id"] == "1234567890"
    assert body["config_id"] == "9876543210"
    assert body["solution_id"] == "solution-abc"
    assert body["state"]
    # Nothing secret is handed to the browser.
    raw = response.get_data(as_text=True)
    assert "auth_token" not in raw
    assert "app_secret" not in raw
    assert "access_token" not in raw


def test_signup_completion_requires_this_business_own_nonce(
    platform_client, garage, settings, meta_configured
):
    """A completion payload from one launch must not apply to another business."""
    platform_client.put(
        f"{COMMS.format(garage_id=garage.id)}/whatsapp/number",
        json={"number_e164": "+447700900123"},
    )
    platform_client.post(f"{COMMS.format(garage_id=garage.id)}/whatsapp/embedded-signup")

    response = platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/whatsapp/embedded-signup/complete",
        json={"state": "a-state-from-somewhere-else", "waba_id": "WABA123"},
    )
    assert response.status_code == 422
    assert "does not belong to this business" in response.get_json()["message"]


def _complete_signup(client, garage, waba_id="WABA1234567", number="+447700900123"):
    client.put(
        COMMS.format(garage_id=garage.id) + "/whatsapp/number",
        json={"number_e164": number},
    )
    state = client.post(COMMS.format(garage_id=garage.id) + "/whatsapp/embedded-signup").get_json()[
        "state"
    ]
    return client.post(
        COMMS.format(garage_id=garage.id) + "/whatsapp/embedded-signup/complete",
        json={"state": state, "waba_id": waba_id, "phone_number_id": "PN999"},
    )


def test_signup_completion_records_the_waba(
    platform_client, garage, provisioned, settings, meta_configured
):
    response = _complete_signup(platform_client, garage)
    assert response.status_code == 200
    whatsapp = response.get_json()["whatsapp"]
    assert whatsapp["waba_id"] == "WABA1234567"
    assert whatsapp["meta_phone_number_id"] == "PN999"
    # The subaccount already exists, so the next step is registering the sender.
    assert whatsapp["status"] == states.WA_TWILIO_SUBACCOUNT_READY


def test_a_waba_cannot_be_connected_to_two_businesses(
    platform_client, garage, second_garage, provisioned, settings, meta_configured
):
    """The other half of the tenancy guarantee: one WABA, one business."""
    assert _complete_signup(platform_client, garage).status_code == 200
    # A different number, so this exercises the WABA guard rather than the
    # separate number guard - two businesses can end up pointed at one WABA
    # through a mis-clicked Meta window even with distinct numbers.
    response = _complete_signup(
        platform_client, second_garage, waba_id="WABA1234567", number="+447700900456"
    )
    assert response.status_code == 422
    assert "already connected to another CoMaz business" in response.get_json()["message"]


def test_signup_nonce_cannot_be_replayed(
    platform_client, garage, provisioned, settings, meta_configured
):
    platform_client.put(
        f"{COMMS.format(garage_id=garage.id)}/whatsapp/number",
        json={"number_e164": "+447700900123"},
    )
    state = platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/whatsapp/embedded-signup"
    ).get_json()["state"]
    payload = {"state": state, "waba_id": "WABA555"}
    assert (
        platform_client.post(
            f"{COMMS.format(garage_id=garage.id)}/whatsapp/embedded-signup/complete",
            json=payload,
        ).status_code
        == 200
    )
    replay = platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/whatsapp/embedded-signup/complete",
        json=payload,
    )
    assert replay.status_code == 422


def test_registering_a_sender_needs_a_waba(platform_client, garage, provisioned, settings):
    platform_client.put(
        f"{COMMS.format(garage_id=garage.id)}/whatsapp/number",
        json={"number_e164": "+447700900123"},
    )
    response = platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/whatsapp/sender",
        json={"display_name": "Garage A"},
    )
    assert response.status_code == 422
    assert "WhatsApp Business Account" in response.get_json()["message"]


def _sender(status="CREATING", sender_id="whatsapp:+447700900123"):
    return {
        "sid": "XEsender000000000000000000000001",
        "status": status,
        "sender_id": sender_id,
        "waba_id": "WABA1234567",
        "display_name": "Garage A",
        "callback_url": "https://api.comaz.example/api/webhooks/twilio/whatsapp/incoming",
        "status_callback_url": "https://api.comaz.example/api/webhooks/twilio/whatsapp/status",
        "offline_reasons": [],
    }


def test_registering_a_sender_maps_twilio_status_onto_our_state(
    monkeypatch, platform_client, garage, provisioned, settings, meta_configured
):
    _complete_signup(platform_client, garage)

    from app.communications.provisioning import service

    monkeypatch.setattr(
        service.whatsapp,
        "register_sender",
        lambda garage, **kwargs: _sender("PENDING_VERIFICATION"),
    )

    response = platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/whatsapp/sender",
        json={"display_name": "Garage A", "verification_method": "sms"},
    )
    assert response.status_code == 201
    whatsapp = response.get_json()["whatsapp"]
    assert whatsapp["sender_sid"] == "XEsender000000000000000000000001"
    assert whatsapp["sender_status"] == "PENDING_VERIFICATION"
    # Twilio's "waiting for a code" is our OTP_REQUIRED, which is a
    # waiting-for-customer state, not a failure.
    assert whatsapp["status"] == states.WA_OTP_REQUIRED
    assert whatsapp["display_status"] == states.DISPLAY_WAITING_FOR_CUSTOMER
    assert any(a["key"] == "submit_otp" for a in response.get_json()["whatsapp_actions"])


def test_a_sender_going_online_mirrors_the_address_for_tenant_resolution(
    monkeypatch, platform_client, session, garage, provisioned, settings, meta_configured
):
    """The mirror is what lets an inbound WhatsApp message find this tenant."""
    _complete_signup(platform_client, garage)

    from app.communications.provisioning import service

    monkeypatch.setattr(
        service.whatsapp, "register_sender", lambda garage, **kwargs: _sender("ONLINE")
    )
    response = platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/whatsapp/sender",
        json={"display_name": "Garage A"},
    )
    assert response.status_code == 201
    assert response.get_json()["whatsapp"]["status"] == states.WA_ONLINE

    session.refresh(settings)
    assert settings.whatsapp_sender == "whatsapp:+447700900123"
    assert settings.communications_enabled is True

    from app.communications.tenant_resolution import resolve_garage_by_whatsapp_sender

    assert resolve_garage_by_whatsapp_sender("whatsapp:+447700900123").id == garage.id


def test_an_unmapped_twilio_status_leaves_our_state_alone(
    monkeypatch, platform_client, session, garage, provisioned, settings, meta_configured
):
    """A new Twilio status is far more likely than a failure - guessing FAILED
    for a live business would be the expensive mistake."""
    _complete_signup(platform_client, garage)

    from app.communications.provisioning import service

    monkeypatch.setattr(
        service.whatsapp, "register_sender", lambda garage, **kwargs: _sender("ONLINE")
    )
    platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/whatsapp/sender",
        json={"display_name": "Garage A"},
    )

    monkeypatch.setattr(
        service.whatsapp,
        "fetch_sender",
        lambda garage, sid: _sender("SOMETHING_TWILIO_ADDED_LATER"),
    )
    response = platform_client.post(f"{COMMS.format(garage_id=garage.id)}/whatsapp/status")
    assert response.status_code == 200
    whatsapp = response.get_json()["whatsapp"]
    assert whatsapp["sender_status"] == "SOMETHING_TWILIO_ADDED_LATER"
    assert whatsapp["status"] == states.WA_ONLINE


def test_the_verification_code_is_never_stored_or_echoed(
    monkeypatch, platform_client, session, garage, provisioned, settings, meta_configured
):
    _complete_signup(platform_client, garage)

    from app.communications.provisioning import service

    monkeypatch.setattr(
        service.whatsapp,
        "register_sender",
        lambda garage, **kwargs: _sender("PENDING_VERIFICATION"),
    )
    platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/whatsapp/sender",
        json={"display_name": "Garage A"},
    )

    monkeypatch.setattr(
        service.whatsapp,
        "submit_verification_code",
        lambda garage, sid, code: _sender("ONLINE"),
    )
    response = platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/whatsapp/verify", json={"code": "907531"}
    )
    assert response.status_code == 200
    assert "907531" not in response.get_data(as_text=True)

    row = session.query(GarageCommunicationsOnboarding).filter_by(garage_id=garage.id).one()
    assert "907531" not in (row.whatsapp_last_error_message or "")

    entry = (
        session.query(PlatformAuditLog)
        .filter_by(action="tenant.communications.whatsapp.sender")
        .order_by(PlatformAuditLog.created_at.desc())
        .first()
    )
    assert entry is not None
    assert "907531" not in str(entry.details or {})


def test_the_test_message_goes_through_the_ordinary_send_path(
    platform_client, session, garage, provisioned, settings
):
    """With Twilio unconfigured the send is skipped and logged, never faked."""
    response = platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/whatsapp/test",
        json={"to_number": "+447700900321"},
    )
    assert response.status_code == 201
    assert response.get_json()["status"] == "SKIPPED_NOT_CONFIGURED"

    log = (
        session.query(CommunicationLog)
        .filter_by(garage_id=garage.id, trigger_event="PLATFORM_ADMIN_TEST")
        .one()
    )
    assert log.channel == "WHATSAPP"


# --------------------------------------------------------------------------
# Failures, and the link back to Operations
# --------------------------------------------------------------------------


def test_recent_errors_explain_the_provider_code_without_losing_it(
    platform_client, session, garage, settings
):
    """The click-through from Operations > Failures lands here, on the same
    rows, with an operator-readable explanation attached."""
    session.add(
        CommunicationLog(
            garage_id=garage.id,
            channel="WHATSAPP",
            direction="OUTBOUND",
            to_address="whatsapp:+447700900321",
            status="failed",
            error_code="63016",
            error_message="Twilio said: 63016",
        )
    )
    session.commit()

    body = platform_client.get(COMMS.format(garage_id=garage.id)).get_json()
    error = body["recent_errors"][0]
    assert error["error_code"] == "63016"
    assert error["error_message"] == "Twilio said: 63016"
    assert "24 hours" in error["meaning"]
    assert error["recommended_action"]
    assert error["known"] is True


def test_an_unknown_error_code_is_never_given_an_invented_cause(
    platform_client, session, garage, settings
):
    session.add(
        CommunicationLog(
            garage_id=garage.id,
            channel="VOICE",
            direction="INBOUND",
            status="failed",
            error_code="99999",
            error_message="Something new",
        )
    )
    session.commit()

    error = platform_client.get(COMMS.format(garage_id=garage.id)).get_json()["recent_errors"][0]
    assert error["known"] is False
    assert error["error_code"] == "99999"
    assert "no specific guidance" in error["meaning"]


def test_error_catalogue_is_readable_by_support(support_client):
    body = support_client.get(f"{OVERVIEW}/error-codes").get_json()
    codes = {item["error_code"] for item in body["items"]}
    assert "63016" in codes
    assert "63110" in codes
    for item in body["items"]:
        assert item["meaning"] and item["recommended_action"]


def test_failure_counts_appear_on_the_overview_row(platform_client, session, garage, settings):
    for _ in range(3):
        session.add(
            CommunicationLog(
                garage_id=garage.id,
                channel="WHATSAPP",
                direction="OUTBOUND",
                status="failed",
                error_code="63016",
            )
        )
    session.commit()

    body = platform_client.get(OVERVIEW).get_json()
    row = next(item for item in body["items"] if item["garage_id"] == str(garage.id))
    assert row["recent_failures"] == 3


# --------------------------------------------------------------------------
# Switches, and contextual actions
# --------------------------------------------------------------------------


def test_actions_are_contextual_not_a_full_toolbar(platform_client, garage, settings):
    """A business with nothing set up is offered subaccount creation, and is
    not offered "Register sender"."""
    body = platform_client.get(COMMS.format(garage_id=garage.id)).get_json()
    voice_keys = {a["key"] for a in body["voice_actions"]}
    whatsapp_keys = {a["key"] for a in body["whatsapp_actions"]}
    assert "create_subaccount" in voice_keys
    assert "buy_voice_number" not in voice_keys
    assert "test_voice" not in voice_keys
    assert whatsapp_keys == {"connect_whatsapp"}


def test_disabling_communications_reports_disabled_without_losing_resources(
    platform_client, session, garage, provisioned, settings
):
    platform_client.post(f"{COMMS.format(garage_id=garage.id)}/voice/online")

    response = platform_client.put(
        f"{COMMS.format(garage_id=garage.id)}/enabled",
        json={"enabled": False},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["display_status"] == states.DISPLAY_DISABLED
    assert body["voice"]["status"] == states.VOICE_DISABLED
    # Nothing at Twilio was released, so the number is still ours.
    assert body["voice"]["phone_number"] == "+441234567890"

    back = platform_client.put(
        f"{COMMS.format(garage_id=garage.id)}/enabled", json={"enabled": True}
    )
    assert back.get_json()["voice"]["status"] == states.VOICE_ONLINE


def test_re_enabling_does_not_throw_away_completed_meta_signup(
    platform_client, session, garage, provisioned, settings, meta_configured
):
    """Disabling releases nothing, so re-enabling must not make a business
    redo Embedded Signup - the one step that needs its owner back on a call."""
    _complete_signup(platform_client, garage)

    platform_client.put(f"{COMMS.format(garage_id=garage.id)}/enabled", json={"enabled": False})
    back = platform_client.put(
        f"{COMMS.format(garage_id=garage.id)}/enabled", json={"enabled": True}
    ).get_json()

    # Straight back to "register the sender", not to NOT_STARTED.
    assert back["whatsapp"]["status"] == states.WA_TWILIO_SUBACCOUNT_READY
    assert back["whatsapp"]["waba_id"] == "WABA1234567"


def test_re_enabling_remembers_an_outstanding_migration(
    platform_client, garage, provisioned, settings
):
    platform_client.put(
        f"{COMMS.format(garage_id=garage.id)}/whatsapp/number",
        json={"number_e164": "+447700900123", "already_on_whatsapp": True},
    )
    platform_client.put(f"{COMMS.format(garage_id=garage.id)}/enabled", json={"enabled": False})
    back = platform_client.put(
        f"{COMMS.format(garage_id=garage.id)}/enabled", json={"enabled": True}
    ).get_json()

    assert back["whatsapp"]["status"] == states.WA_EXISTING_MIGRATION_REQUIRED


def test_two_businesses_cannot_claim_the_same_whatsapp_number(
    platform_client, garage, second_garage, settings
):
    """Caught when the number is recorded, not when the sender goes live - by
    which point Meta signup would already have been done for the wrong
    business."""
    assert (
        platform_client.put(
            f"{COMMS.format(garage_id=garage.id)}/whatsapp/number",
            json={"number_e164": "+447700900123"},
        ).status_code
        == 200
    )
    response = platform_client.put(
        f"{COMMS.format(garage_id=second_garage.id)}/whatsapp/number",
        json={"number_e164": "+447700900123"},
    )
    assert response.status_code == 422
    assert garage.name in response.get_json()["message"]


def test_automation_toggle_writes_the_same_flag_as_the_cli(
    platform_client, garage, provisioned, settings
):
    from app.conversation.automation import is_conversation_automation_enabled

    response = platform_client.put(
        f"{COMMS.format(garage_id=garage.id)}/automation", json={"enabled": True}
    )
    assert response.status_code == 200
    assert response.get_json()["automation_enabled"] is True
    assert is_conversation_automation_enabled(garage) is True


def test_provisioning_actions_are_audited(platform_client, session, garage, settings):
    platform_client.put(
        f"{COMMS.format(garage_id=garage.id)}/whatsapp/number",
        json={"number_e164": "+447700900123"},
    )
    actions = {
        entry.action
        for entry in session.query(PlatformAuditLog).filter_by(garage_id=garage.id).all()
    }
    assert "tenant.communications.whatsapp.number" in actions


# --------------------------------------------------------------------------
# The Twilio Senders v2 request bodies
#
# Every other WhatsApp test patches `whatsapp.register_sender`, which is the
# right seam for orchestration but means the request body itself is never
# built. These two exercise the real serialisation: the SDK calls `.to_dict()`
# on the request object *and* on each nested member, so a plain dict raises
# AttributeError before a byte reaches Twilio.
# --------------------------------------------------------------------------


def test_the_sender_registration_body_matches_the_senders_v2_contract():
    from app.communications.provisioning.whatsapp import _RequestBody

    body = _RequestBody(
        {
            "sender_id": "whatsapp:+447700900123",
            "configuration": {"waba_id": "WABA1", "verification_method": "sms"},
            "webhook": {
                "callback_url": "https://api.example/api/webhooks/twilio/whatsapp/incoming",
                "callback_method": "POST",
                "status_callback_url": "https://api.example/api/webhooks/twilio/whatsapp/status",
                "status_callback_method": "POST",
            },
            "profile": {"name": "ABC Motors"},
        }
    ).to_dict()

    assert body["sender_id"] == "whatsapp:+447700900123"
    # The WABA id in `configuration` is what binds that WABA to this
    # subaccount - the association step of the Tech Provider flow.
    assert body["configuration"]["waba_id"] == "WABA1"
    assert body["configuration"]["verification_method"] == "sms"
    # CoMaz's existing webhooks, not new ones.
    assert body["webhook"]["callback_url"].endswith("/api/webhooks/twilio/whatsapp/incoming")
    assert body["webhook"]["status_callback_url"].endswith("/api/webhooks/twilio/whatsapp/status")
    assert body["profile"]["name"] == "ABC Motors"


def test_the_verification_and_webhook_update_bodies_serialise():
    from app.communications.provisioning.whatsapp import _RequestBody

    assert (
        _RequestBody({"configuration": {"verification_code": "907531"}}).to_dict()["configuration"][
            "verification_code"
        ]
        == "907531"
    )

    webhook = _RequestBody(
        {"webhook": {"callback_url": "https://api.example/hook", "callback_method": "POST"}}
    ).to_dict()["webhook"]
    assert webhook["callback_url"] == "https://api.example/hook"
    assert webhook["callback_method"] == "POST"


def test_the_request_body_survives_the_sdks_own_transport_layer():
    """The bug CI caught that local tests could not.

    Twilio's generated request classes changed shape in a *patch* release:
    9.11.0 stored whatever nested members you passed, 9.11.1 constructs them
    from plain dicts and raises AttributeError on a constructed one. Since
    requirements.txt pins only twilio>=9,<10, either can turn up. Both
    versions' transports call exactly one method on the request object, so
    that - not either constructor - is the contract to hold.
    """
    import inspect
    import re

    from twilio.rest.messaging.v2.channels_sender import (
        ChannelsSenderContext,
        ChannelsSenderList,
    )

    for fn in (ChannelsSenderList._create, ChannelsSenderContext._update):
        used = set(
            re.findall(r"messaging_v2_channels_sender_requests_\w+\.(\w+)", inspect.getsource(fn))
        )
        assert used == {"to_dict"}, (
            f"{fn.__qualname__} now uses {sorted(used)} on the request object, not just "
            "to_dict() - _RequestBody in app/communications/provisioning/whatsapp.py "
            "needs to grow to match."
        )


def test_an_omitted_section_is_absent_rather_than_null():
    """Twilio treats a missing key and an explicit null differently on update,
    where null can clear a value the business already has."""
    from app.communications.provisioning.whatsapp import _RequestBody

    body = _RequestBody({"sender_id": "whatsapp:+447700900123", "profile": None}).to_dict()
    assert "profile" not in body
    assert body["sender_id"] == "whatsapp:+447700900123"


def test_the_sender_address_carries_the_whatsapp_prefix():
    """Stored with the prefix because that is exactly what the Messages API
    needs - the convention GarageCommunicationSettings has always used."""
    from app.communications.provisioning.whatsapp import sender_address

    assert sender_address("+447700900123") == "whatsapp:+447700900123"
    assert sender_address("whatsapp:+447700900123") == "whatsapp:+447700900123"


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------


def test_a_stored_credential_round_trips_but_is_not_plaintext(app):
    ciphertext = encrypt_secret("hunter2")
    assert ciphertext != "hunter2"
    assert "hunter2" not in ciphertext
    assert decrypt_secret(ciphertext) == "hunter2"


def test_subaccount_creation_refuses_without_an_encryption_key(
    monkeypatch, app, platform_client, garage, settings
):
    """Better a clear blocker than a live Twilio credential in plaintext."""
    from app.communications.provisioning import subaccounts

    monkeypatch.setattr(subaccounts, "is_twilio_configured", lambda: True)
    monkeypatch.setitem(app.config, "COMMS_SECRET_KEY", "")

    response = platform_client.post(f"{COMMS.format(garage_id=garage.id)}/subaccount")
    assert response.status_code == 422
    assert "COMMS_SECRET_KEY" in response.get_json()["message"]
