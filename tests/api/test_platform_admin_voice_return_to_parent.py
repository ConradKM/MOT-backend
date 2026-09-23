"""Returning a business's voice number to CoMaz's parent inventory - the
"take it out" half of the existing-number lifecycle (#187/#189). Never
released or deleted, always moved to where the existing discovery flow
finds it again.

Twilio is never called for real - every test patches the provisioning
module's own seam (``service.voice``), matching the convention in
tests/api/test_platform_admin_communications.py and
tests/api/test_platform_admin_voice_existing_numbers.py."""

from __future__ import annotations

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
def assigned(session, settings):
    """A business with a subaccount and a live voice number - the state
    every test in this file starts from."""
    settings.twilio_subaccount_sid = "ACtest0000000000000000000000000001"
    settings.voice_phone_number = "+441234567890"
    settings.voice_number_sid = "PNtest0000000000000000000000000001"
    session.commit()
    return settings


def _return(platform_client, garage_id, **body):
    return platform_client.post(
        f"{COMMS.format(garage_id=garage_id)}/voice/number/return-to-parent",
        json=body,
    )


def test_returning_a_number_moves_it_and_clears_local_state(
    monkeypatch, platform_client, garage, assigned, session
):
    from app.communications.provisioning import service

    calls = []
    monkeypatch.setattr(
        service.voice,
        "return_to_parent",
        lambda g, number_sid: (
            calls.append(number_sid) or {"phone_number": "+441234567890", "sid": number_sid}
        ),
    )

    response = _return(platform_client, garage.id)

    assert response.status_code == 200
    assert calls == ["PNtest0000000000000000000000000001"]
    body = response.get_json()
    assert body["voice"]["phone_number"] is None
    assert body["voice"]["status"] == states.VOICE_SUBACCOUNT_READY
    session.refresh(assigned)
    assert assigned.voice_phone_number is None
    assert assigned.voice_number_sid is None


def test_returning_with_no_number_is_a_no_op(platform_client, garage, settings, session):
    """A business with no number has nothing to return - never an error,
    never a Twilio call."""
    settings.twilio_subaccount_sid = "ACtest0000000000000000000000000001"
    session.commit()

    response = _return(platform_client, garage.id)

    assert response.status_code == 200
    assert response.get_json()["voice"]["phone_number"] is None


def test_retrying_return_is_idempotent_and_never_calls_twilio_twice(
    monkeypatch, platform_client, garage, assigned
):
    from app.communications.provisioning import service

    calls = []
    monkeypatch.setattr(
        service.voice,
        "return_to_parent",
        lambda g, number_sid: (
            calls.append(number_sid) or {"phone_number": "+441234567890", "sid": number_sid}
        ),
    )

    first = _return(platform_client, garage.id)
    second = _return(platform_client, garage.id)

    assert first.status_code == 200
    assert second.status_code == 200
    # The second request found no local number left to return - the
    # provider is never asked to move something already moved.
    assert calls == ["PNtest0000000000000000000000000001"]


def test_provider_failure_leaves_the_number_assigned(
    monkeypatch, platform_client, garage, assigned, session
):
    from app.communications.provisioning import service

    def _fail(g, number_sid):
        raise VoiceProvisioningError("Twilio refused to move that number.", code="70001")

    monkeypatch.setattr(service.voice, "return_to_parent", _fail)

    response = _return(platform_client, garage.id)

    assert response.status_code == 422
    session.refresh(assigned)
    # Never falsely cleared - the number is still this business's as far as
    # CoMaz's own records show, matching what Twilio actually did (nothing).
    assert assigned.voice_phone_number == "+441234567890"
    assert assigned.voice_number_sid == "PNtest0000000000000000000000000001"


def test_a_whatsapp_configured_number_requires_acknowledgement_before_returning(
    monkeypatch, platform_client, garage, assigned, second_garage, session
):
    session.add(
        GarageCommunicationSettings(garage_id=second_garage.id, whatsapp_sender="+441234567890")
    )
    session.commit()

    from app.communications.provisioning import service

    calls = []
    monkeypatch.setattr(
        service.voice,
        "return_to_parent",
        lambda g, number_sid: (
            calls.append(number_sid) or {"phone_number": "+441234567890", "sid": number_sid}
        ),
    )

    refused = _return(platform_client, garage.id)
    assert refused.status_code == 422
    assert "WhatsApp" in refused.get_json()["message"]
    assert calls == []

    acknowledged = _return(platform_client, garage.id, acknowledge_whatsapp=True)
    assert acknowledged.status_code == 200
    assert calls == ["PNtest0000000000000000000000000001"]
