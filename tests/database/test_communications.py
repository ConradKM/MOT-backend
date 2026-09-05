"""Model-level tests for the communications data model:
GarageCommunicationSettings (one optional row per garage) and
CommunicationLog (tenant-scoped, multi-channel communication history).
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.communications.communication_log import (
    CHANNEL_VOICE,
    CHANNEL_WHATSAPP,
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
    CommunicationLog,
)
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)


def test_communication_settings_default_to_disabled(session, garage):
    settings = GarageCommunicationSettings(garage_id=garage.id)
    session.add(settings)
    session.commit()

    assert settings.communications_enabled is False
    assert settings.twilio_subaccount_sid is None
    assert settings.voice_phone_number is None
    assert settings.whatsapp_sender is None
    assert settings.messaging_service_sid is None


def test_garage_communication_settings_relationship_round_trips(session, garage):
    settings = GarageCommunicationSettings(garage_id=garage.id, communications_enabled=True)
    session.add(settings)
    session.commit()
    session.refresh(garage)

    assert garage.communication_settings is settings
    assert settings.garage is garage


def test_garage_communication_settings_unique_per_garage(session, garage):
    session.add(GarageCommunicationSettings(garage_id=garage.id))
    session.commit()

    session.add(GarageCommunicationSettings(garage_id=garage.id))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_communication_log_is_tenant_scoped(session, garage, second_garage):
    session.add(
        CommunicationLog(
            garage_id=garage.id,
            channel=CHANNEL_VOICE,
            direction=DIRECTION_INBOUND,
            status="received",
        )
    )
    session.add(
        CommunicationLog(
            garage_id=second_garage.id,
            channel=CHANNEL_VOICE,
            direction=DIRECTION_INBOUND,
            status="received",
        )
    )
    session.commit()

    garage_logs = CommunicationLog.query.filter_by(garage_id=garage.id).all()
    other_logs = CommunicationLog.query.filter_by(garage_id=second_garage.id).all()

    assert len(garage_logs) == 1
    assert len(other_logs) == 1
    assert garage_logs[0].garage_id != other_logs[0].garage_id


def test_communication_log_allows_fully_unlinked_row(session, garage):
    """An inbound call/message from an unrecognised party still gets a row -
    customer/appointment/booking_request are all independently optional."""
    log = CommunicationLog(
        garage_id=garage.id,
        channel=CHANNEL_WHATSAPP,
        direction=DIRECTION_INBOUND,
        status="received",
        body="Hi, can I book an MOT?",
    )
    session.add(log)
    session.commit()

    assert log.customer_id is None
    assert log.appointment_id is None
    assert log.booking_request_id is None


def test_communication_log_external_id_unique_when_present(session, garage):
    session.add(
        CommunicationLog(
            garage_id=garage.id,
            channel=CHANNEL_WHATSAPP,
            direction=DIRECTION_OUTBOUND,
            status="queued",
            external_id="SM123",
        )
    )
    session.commit()

    session.add(
        CommunicationLog(
            garage_id=garage.id,
            channel=CHANNEL_WHATSAPP,
            direction=DIRECTION_OUTBOUND,
            status="queued",
            external_id="SM123",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_communication_log_external_id_allows_many_nulls(session, garage):
    """Postgres does not treat NULL as equal to NULL in a unique index - two
    SKIPPED_NOT_CONFIGURED rows (no provider SID) must not collide."""
    session.add(
        CommunicationLog(
            garage_id=garage.id, channel=CHANNEL_VOICE, direction=DIRECTION_OUTBOUND,
            status="SKIPPED_NOT_CONFIGURED", external_id=None,
        )
    )
    session.add(
        CommunicationLog(
            garage_id=garage.id, channel=CHANNEL_VOICE, direction=DIRECTION_OUTBOUND,
            status="SKIPPED_NOT_CONFIGURED", external_id=None,
        )
    )
    session.commit()  # must not raise

    assert (
        CommunicationLog.query.filter_by(garage_id=garage.id, external_id=None).count() == 2
    )
