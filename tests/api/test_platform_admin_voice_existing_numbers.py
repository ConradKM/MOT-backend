"""Existing-number onboarding: discovering, adopting, and transferring
Twilio numbers CoMaz already has some claim on, versus a genuinely external
one - and the tenant-isolation and WhatsApp-safety guarantees around all of
it.

Twilio is never called for real - every test patches the provisioning
module's own seam (``service.voice``), matching
tests/api/test_platform_admin_communications.py's convention. No test in
this file references, purchases, or mutates a real number; the two
protected production numbers (+447402220792, +443330382135) never appear
here at all - discovery/adoption/transfer only ever act on whatever a test
puts into its own mocked Twilio double.
"""

from types import SimpleNamespace

import pytest

from app.communications.provisioning import states
from app.communications.provisioning.voice import VoiceProvisioningError
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)

COMMS = "/api/platform-admin/tenants/{garage_id}/communications"


@pytest.fixture()
def settings(session, garage):
    row = GarageCommunicationSettings(garage_id=garage.id)
    session.add(row)
    session.commit()
    return row


@pytest.fixture()
def subaccounted(session, settings):
    """A business with a subaccount but no number yet."""
    settings.twilio_subaccount_sid = "ACtest0000000000000000000000000001"
    session.commit()
    return settings


def _number(phone_number, sid, *, capabilities=None):
    return SimpleNamespace(
        phone_number=phone_number,
        sid=sid,
        friendly_name=None,
        capabilities=capabilities or {"voice": True},
    )


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def test_discover_lists_subaccount_and_parent_numbers_separately(
    monkeypatch, platform_client, subaccounted, garage
):
    from app.communications.provisioning import service

    monkeypatch.setattr(
        service.voice,
        "list_owned_numbers",
        lambda g: [
            {
                "phone_number": "+441111111111",
                "sid": "PNown1",
                "capabilities": [],
                "friendly_name": None,
                "whatsapp_configured": False,
            }
        ],
    )
    monkeypatch.setattr(
        service.voice,
        "list_parent_numbers",
        lambda: [
            {
                "phone_number": "+442222222222",
                "sid": "PNparent00000000000000000000001",
                "capabilities": [],
                "friendly_name": None,
                "whatsapp_configured": False,
            }
        ],
    )

    response = platform_client.get(f"{COMMS.format(garage_id=garage.id)}/voice/existing-numbers")

    assert response.status_code == 200
    body = response.get_json()
    assert [n["phone_number"] for n in body["subaccount"]] == ["+441111111111"]
    assert [n["phone_number"] for n in body["parent"]] == ["+442222222222"]


def test_discover_without_a_subaccount_skips_subaccount_lookup_entirely(
    monkeypatch, platform_client, settings, garage
):
    """No subaccount yet - there is nothing to list, and no Twilio call
    should even be attempted for it."""
    from app.communications.provisioning import service

    calls = []
    monkeypatch.setattr(service.voice, "list_owned_numbers", lambda g: calls.append("called") or [])
    monkeypatch.setattr(service.voice, "list_parent_numbers", list)

    response = platform_client.get(f"{COMMS.format(garage_id=garage.id)}/voice/existing-numbers")

    assert response.status_code == 200
    assert response.get_json()["subaccount"] == []
    assert calls == []


# --------------------------------------------------------------------------
# Case 1: adopt a number already in this business's own subaccount
# --------------------------------------------------------------------------


def test_adopting_an_owned_number_is_idempotent_on_retry(
    monkeypatch, platform_client, subaccounted, garage, session
):
    from app.communications.provisioning import service

    calls = []
    monkeypatch.setattr(
        service.voice,
        "assign_existing_number",
        lambda g, phone_number: (
            calls.append(phone_number)
            or {"phone_number": phone_number, "sid": "PNown1", "capabilities": []}
        ),
    )

    body = {"phone_number": "+441111111111", "already_owned": True}
    first = platform_client.post(f"{COMMS.format(garage_id=garage.id)}/voice/number", json=body)
    second = platform_client.post(f"{COMMS.format(garage_id=garage.id)}/voice/number", json=body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls == ["+441111111111"]  # the second call never reached Twilio at all
    session.refresh(subaccounted)
    assert subaccounted.voice_phone_number == "+441111111111"


def test_adopting_a_whatsapp_configured_number_requires_acknowledgement(
    monkeypatch, platform_client, subaccounted, garage, second_garage, session
):
    """WhatsApp sender configuration never moves with a number - adopting
    one that already carries a WhatsApp sender must not proceed silently."""
    from app.communications.provisioning import service

    other_settings = GarageCommunicationSettings(
        garage_id=second_garage.id, whatsapp_sender="+441111111111"
    )
    session.add(other_settings)
    session.commit()

    monkeypatch.setattr(
        service.voice,
        "assign_existing_number",
        lambda g, phone_number: {"phone_number": phone_number, "sid": "PNown1", "capabilities": []},
    )

    refused = platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/voice/number",
        json={"phone_number": "+441111111111", "already_owned": True},
    )
    assert refused.status_code == 422
    assert "WhatsApp" in refused.get_json()["message"]

    acknowledged = platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/voice/number",
        json={
            "phone_number": "+441111111111",
            "already_owned": True,
            "acknowledge_whatsapp": True,
        },
    )
    assert acknowledged.status_code == 200


# --------------------------------------------------------------------------
# Case 2: transfer a parent-account number
# --------------------------------------------------------------------------


def test_transferring_a_parent_number_configures_and_assigns_it(
    monkeypatch, platform_client, subaccounted, garage, session
):
    from app.communications.provisioning import service

    monkeypatch.setattr(
        service.voice,
        "transfer_from_parent",
        lambda g, phone_number_sid: {
            "phone_number": "+443333333333",
            "sid": phone_number_sid,
            "voice_url": "https://example.onrender.com/api/webhooks/twilio/voice/incoming",
            "status_callback": "https://example.onrender.com/api/webhooks/twilio/voice/status",
        },
    )

    response = platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/voice/number/transfer",
        json={"phone_number_sid": "PNparent00000000000000000000001"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["voice"]["phone_number"] == "+443333333333"
    assert body["voice"]["status"] == states.VOICE_WEBHOOKS_CONFIGURED
    session.refresh(subaccounted)
    assert subaccounted.voice_number_sid == "PNparent00000000000000000000001"


def test_transfer_is_idempotent_and_never_retransfers(
    monkeypatch, platform_client, subaccounted, garage
):
    from app.communications.provisioning import service

    calls = []
    monkeypatch.setattr(
        service.voice,
        "transfer_from_parent",
        lambda g, phone_number_sid: (
            calls.append(phone_number_sid)
            or {"phone_number": "+443333333333", "sid": phone_number_sid}
        ),
    )

    body = {"phone_number_sid": "PNparent00000000000000000000001"}
    platform_client.post(f"{COMMS.format(garage_id=garage.id)}/voice/number/transfer", json=body)
    second = platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/voice/number/transfer", json=body
    )

    assert second.status_code == 200
    assert calls == [
        "PNparent00000000000000000000001"
    ]  # only the first request ever reached Twilio


def test_transfer_refuses_a_number_no_longer_owned_by_the_parent_account(
    monkeypatch, platform_client, subaccounted, garage
):
    """The provider layer re-checks ownership before moving anything - a
    stale/incorrect SID from the caller is never trusted blindly."""
    from app.communications.provisioning import service

    def _refuse(g, phone_number_sid):
        raise VoiceProvisioningError(
            "+443333333333 is no longer owned by CoMaz's parent account - it may already have "
            "been moved. Refresh and try again."
        )

    monkeypatch.setattr(service.voice, "transfer_from_parent", _refuse)

    response = platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/voice/number/transfer",
        json={"phone_number_sid": "PNparent00000000000000000000001"},
    )

    assert response.status_code == 422
    assert "no longer owned" in response.get_json()["message"]


def test_provider_failure_during_transfer_leaves_a_truthful_action_required_state(
    monkeypatch, platform_client, subaccounted, garage, session
):
    from app.communications.provisioning import service

    def _fail(g, phone_number_sid):
        raise VoiceProvisioningError("Twilio refused to move that number.", code="70001")

    monkeypatch.setattr(service.voice, "transfer_from_parent", _fail)

    response = platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/voice/number/transfer",
        json={"phone_number_sid": "PNparent00000000000000000000001"},
    )

    assert response.status_code == 422
    session.refresh(subaccounted)
    assert subaccounted.voice_phone_number is None  # never falsely marked assigned


# --------------------------------------------------------------------------
# Case 3: cross-tenant protection
# --------------------------------------------------------------------------


def test_transfer_refuses_a_number_already_assigned_to_another_business(
    monkeypatch, app, garage, second_garage, subaccounted, session
):
    """Exercises voice.transfer_from_parent directly against a fake Twilio
    client - the real cross-tenant DB check that must fire before any
    Twilio mutation is attempted."""
    session.add(
        GarageCommunicationSettings(garage_id=second_garage.id, voice_phone_number="+443333333333")
    )
    session.commit()

    from app.communications.provisioning import voice as voice_module

    class _FakeNumber:
        phone_number = "+443333333333"
        sid = "PNparent00000000000000000000001"
        account_sid = "ACmaster0000000000000000000000001"

        def fetch(self):
            return self

        def update(self, **kwargs):  # pragma: no cover - must never be reached
            raise AssertionError("must never update a cross-tenant-claimed number")

    class _FakeNumberResource:
        def __call__(self, sid):
            return _FakeNumber()

    class _FakeParentClient:
        incoming_phone_numbers = _FakeNumberResource()

    app.config["TWILIO_ACCOUNT_SID"] = "ACmaster0000000000000000000000001"
    app.config["TWILIO_AUTH_TOKEN"] = "token"
    monkeypatch.setattr(
        voice_module, "get_twilio_account_management_client", lambda: _FakeParentClient()
    )

    with pytest.raises(VoiceProvisioningError, match="already assigned"):
        voice_module.transfer_from_parent(garage, "PNparent00000000000000000000001")


# --------------------------------------------------------------------------
# Case 4: external / typed number lookup
# --------------------------------------------------------------------------


def test_lookup_of_a_genuinely_external_number_never_marks_it_ready(
    monkeypatch, platform_client, subaccounted, garage, session
):
    from app.communications.provisioning import service

    monkeypatch.setattr(service.voice, "list_owned_numbers", lambda g: [])
    monkeypatch.setattr(service.voice, "list_parent_numbers", list)

    response = platform_client.get(
        f"{COMMS.format(garage_id=garage.id)}/voice/number/lookup",
        query_string={"phone_number": "+19995550123"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"status": "external", "detail": None}
    session.refresh(subaccounted)
    assert subaccounted.voice_phone_number is None  # nothing was assigned or changed


def test_lookup_finds_a_number_in_this_business_own_subaccount(
    monkeypatch, platform_client, subaccounted, garage
):
    from app.communications.provisioning import service

    monkeypatch.setattr(
        service.voice,
        "list_owned_numbers",
        lambda g: [
            {
                "phone_number": "+441111111111",
                "sid": "PNown1",
                "capabilities": [],
                "friendly_name": None,
                "whatsapp_configured": False,
            }
        ],
    )
    monkeypatch.setattr(service.voice, "list_parent_numbers", list)

    response = platform_client.get(
        f"{COMMS.format(garage_id=garage.id)}/voice/number/lookup",
        query_string={"phone_number": "+441111111111"},
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "own_subaccount"


def test_lookup_reports_a_number_assigned_to_another_business_without_exposing_a_list(
    monkeypatch, platform_client, subaccounted, garage, second_garage, session
):
    session.add(
        GarageCommunicationSettings(garage_id=second_garage.id, voice_phone_number="+441111111111")
    )
    session.commit()

    from app.communications.provisioning import service

    monkeypatch.setattr(
        service.voice, "list_owned_numbers", lambda g: (_ for _ in ()).throw(AssertionError())
    )

    response = platform_client.get(
        f"{COMMS.format(garage_id=garage.id)}/voice/number/lookup",
        query_string={"phone_number": "+441111111111"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "assigned_elsewhere"
    assert body["detail"] == second_garage.name
