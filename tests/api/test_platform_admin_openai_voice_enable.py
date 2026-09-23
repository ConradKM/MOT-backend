"""Explicitly enabling OpenAI Voice for a business - a separate, deliberate
action from acquiring or assigning the voice number itself.

Twilio is never called for real - every test patches the provisioning
module's own seam (``service.openai_voice_sip.enable_openai_voice``),
matching the convention in tests/api/test_platform_admin_voice_return_to_parent.py.
"""

from __future__ import annotations

import pytest

from app.communications.provisioning import states
from app.communications.provisioning.openai_voice_sip import OpenAIVoiceProvisioningError
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
def with_number(session, settings):
    """A business with a working, webhook-configured voice number - the
    state OpenAI Voice enablement requires before it can route anything."""
    settings.twilio_subaccount_sid = "ACtest0000000000000000000000000001"
    settings.voice_phone_number = "+441234567890"
    settings.voice_number_sid = "PNtest0000000000000000000000000001"
    session.commit()
    return settings


def _enable(platform_client, garage_id):
    return platform_client.post(f"{COMMS.format(garage_id=garage_id)}/voice/openai/enable")


def test_enabling_provisions_the_trunk_and_marks_ready(
    monkeypatch, platform_client, garage, with_number, session
):
    from app.communications.provisioning import service

    calls = []
    monkeypatch.setattr(
        service.openai_voice_sip,
        "enable_openai_voice",
        lambda g, number_sid: calls.append(number_sid) or "TKnew00000000000000000000000000001",
    )

    response = _enable(platform_client, garage.id)

    assert response.status_code == 200
    assert calls == ["PNtest0000000000000000000000000001"]
    body = response.get_json()
    assert body["openai_voice"]["status"] == states.OPENAI_VOICE_READY
    assert body["openai_voice"]["trunk_sid"] == "TKnew00000000000000000000000000001"


def test_enabling_without_a_number_is_refused(platform_client, garage, settings):
    settings.twilio_subaccount_sid = "ACtest0000000000000000000000000001"

    response = _enable(platform_client, garage.id)

    assert response.status_code == 422
    assert "voice number" in response.get_json()["message"]


def test_retrying_is_idempotent_and_never_calls_twilio_twice_once_ready(
    monkeypatch, platform_client, garage, with_number
):
    from app.communications.provisioning import service

    calls = []
    monkeypatch.setattr(
        service.openai_voice_sip,
        "enable_openai_voice",
        lambda g, number_sid: calls.append(number_sid) or "TKnew00000000000000000000000000001",
    )

    first = _enable(platform_client, garage.id)
    second = _enable(platform_client, garage.id)

    assert first.status_code == 200
    assert second.status_code == 200
    # Already READY on the second call - never re-provisioned.
    assert calls == ["PNtest0000000000000000000000000001"]


def test_provider_failure_leaves_a_truthful_action_required_state(
    monkeypatch, platform_client, garage, with_number, session
):
    from app.communications.provisioning import service

    def _fail(g, number_sid):
        raise OpenAIVoiceProvisioningError(
            "Twilio refused to create a SIP trunk for this business.", code="20003"
        )

    monkeypatch.setattr(service.openai_voice_sip, "enable_openai_voice", _fail)

    response = _enable(platform_client, garage.id)

    assert response.status_code == 422
    body = response.get_json()
    assert "Twilio refused" in body["message"]

    follow_up = platform_client.get(COMMS.format(garage_id=garage.id))
    detail = follow_up.get_json()["openai_voice"]
    assert detail["status"] == states.OPENAI_VOICE_ACTION_REQUIRED
    assert detail["last_error"]["error_code"] == "20003"
    # Never marked ready off a failed provider call, and no trunk recorded.
    assert detail["trunk_sid"] is None
