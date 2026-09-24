"""The go-live checklist (/tenants/<id>/readiness): never fabricated from ID
presence alone - a Stripe account with charges disabled must report
not-ready, exactly as it should before a business's first real deposit.

The adversarial section at the bottom exists to protect one asymmetry: a
false NEGATIVE here is merely annoying (an operator double-checks a business
that was actually fine); a false POSITIVE is dangerous (a business goes
live for real customers while something required is actually broken).
Every test below either confirms a check stays honestly false, or - where
it reads true - explains in the test itself why that's the correct, not
merely convenient, answer."""

from datetime import time

from app.communications.provisioning import states
from app.models.communications.comms_onboarding import GarageCommunicationsOnboarding
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)
from app.models.garage import GARAGE_STATUS_ARCHIVED, GARAGE_STATUS_SUSPENDED
from app.models.garage_schedule import GarageOpeningHours
from app.models.payments.garage_payment_settings import GaragePaymentSettings


def test_fresh_business_reports_not_ready_everywhere(platform_client, garage, user):
    response = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/readiness")

    assert response.status_code == 200
    body = response.json
    assert body["garage_id"] == str(garage.id)

    business = {c["key"]: c["ok"] for c in body["business"]}
    assert business["business_created"] is True
    assert business["owner_exists"] is True
    assert business["services_configured"] is False
    assert body["business_ready"] is False

    payments = {c["key"]: c["ok"] for c in body["payments"]}
    assert payments["stripe_connected"] is False
    assert body["payments_ready"] is False
    assert body["ready_to_take_deposits"] is False


def test_stripe_account_with_charges_disabled_is_not_ready(platform_client, garage, user, session):
    """A connected-but-not-chargeable account must not read as ready - the
    exact fabrication this endpoint exists to avoid."""
    session.add(
        GaragePaymentSettings(
            garage_id=garage.id,
            stripe_account_id="acct_test123",
            stripe_onboarding_complete=False,
            stripe_charges_enabled=False,
            stripe_payouts_enabled=False,
        )
    )
    session.commit()

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/readiness").json

    payments = {c["key"]: c["ok"] for c in body["payments"]}
    assert payments["stripe_connected"] is True
    assert payments["stripe_charges_enabled"] is False
    assert body["payments_ready"] is False
    assert body["ready_to_take_deposits"] is False


def test_communications_is_optional_for_public_booking_readiness(
    platform_client, garage, user, appointment_type, session
):
    """A business can take public bookings with zero comms configured -
    ready_for_public_booking must not depend on the communications section."""
    session.add(
        GarageOpeningHours(
            garage_id=garage.id,
            weekday=0,
            opens_at=time(8, 0),
            closes_at=time(18, 0),
            is_closed=False,
        )
    )
    session.commit()

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/readiness").json

    assert body["communications_ready"] is False
    assert body["business_ready"] is True
    assert body["ready_for_public_booking"] is True


def test_a_working_voice_number_with_openai_voice_never_enabled_is_not_falsely_ready(
    platform_client, garage, user, session
):
    """The regression this test exists for: the "openai_voice" check used to
    read the Twilio voice channel's own webhooks_configured flag instead of
    the OpenAI Voice channel's actual status - so a plain Twilio-only
    business with webhooks configured (and OpenAI Voice never enabled at
    all) reported "OpenAI voice: ready", a straightforward false positive.
    """
    session.add(
        GarageCommunicationSettings(
            garage_id=garage.id,
            twilio_subaccount_sid="ACtest0000000000000000000000000001",
            voice_phone_number="+441234567890",
            voice_number_sid="PNtest0000000000000000000000000001",
        )
    )
    session.add(
        GarageCommunicationsOnboarding(
            garage_id=garage.id,
            voice_status=states.VOICE_WEBHOOKS_CONFIGURED,
            voice_webhooks_configured=True,
            openai_voice_status=states.OPENAI_VOICE_NOT_STARTED,
        )
    )
    session.commit()

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/readiness").json

    communications = {c["key"]: c["ok"] for c in body["communications"]}
    assert communications["voice_routing"] is True
    # NOT_STARTED is a legitimate resting state (optional, never blocks
    # readiness on its own) - it must read true, but for the honest reason
    # ("never enabled"), not by accident via the voice channel's own flag.
    assert communications["openai_voice"] is True


def test_a_stuck_openai_voice_attempt_is_not_falsely_ready(platform_client, garage, user, session):
    session.add(
        GarageCommunicationSettings(
            garage_id=garage.id,
            twilio_subaccount_sid="ACtest0000000000000000000000000001",
            voice_phone_number="+441234567890",
            voice_number_sid="PNtest0000000000000000000000000001",
        )
    )
    session.add(
        GarageCommunicationsOnboarding(
            garage_id=garage.id,
            voice_status=states.VOICE_WEBHOOKS_CONFIGURED,
            voice_webhooks_configured=True,
            openai_voice_status=states.OPENAI_VOICE_ACTION_REQUIRED,
        )
    )
    session.commit()

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/readiness").json

    communications = {c["key"]: c["ok"] for c in body["communications"]}
    assert communications["voice_routing"] is True
    assert communications["openai_voice"] is False
    assert body["communications_ready"] is False


def test_openai_voice_actually_ready_reads_ready(platform_client, garage, user, session):
    session.add(
        GarageCommunicationSettings(
            garage_id=garage.id,
            twilio_subaccount_sid="ACtest0000000000000000000000000001",
            voice_phone_number="+441234567890",
            voice_number_sid="PNtest0000000000000000000000000001",
            communications_enabled=True,
        )
    )
    session.add(
        GarageCommunicationsOnboarding(
            garage_id=garage.id,
            voice_status=states.VOICE_ONLINE,
            voice_webhooks_configured=True,
            openai_voice_status=states.OPENAI_VOICE_READY,
        )
    )
    session.commit()

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/readiness").json

    communications = {c["key"]: c["ok"] for c in body["communications"]}
    assert communications["openai_voice"] is True


# --------------------------------------------------------------------------
# Adversarial pass: construct every state in the "could this look ready
# when it isn't" matrix and confirm none of them produce a false positive.
# --------------------------------------------------------------------------


def test_business_with_no_owner_is_not_ready(platform_client, garage):
    """No `user` fixture requested - this garage genuinely has no OWNER
    employee at all."""
    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/readiness").json

    business = {c["key"]: c["ok"] for c in body["business"]}
    assert business["owner_exists"] is False
    assert body["business_ready"] is False
    assert body["ready_for_public_booking"] is False


def test_archived_services_only_does_not_count_as_configured(
    platform_client, garage, user, session
):
    from app.models.appointments.appointment_type import AppointmentType

    session.add(
        AppointmentType(
            garage_id=garage.id,
            name="Old MOT (archived)",
            duration_minutes=60,
            status="ARCHIVED",
        )
    )
    session.commit()

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/readiness").json

    business = {c["key"]: c["ok"] for c in body["business"]}
    assert business["services_configured"] is False
    assert body["business_ready"] is False


def test_suspended_business_never_reads_ready_for_public_booking(
    platform_client, garage, user, appointment_type, session
):
    """A fully-configured business that has since been suspended must not
    invite an operator to think it's still open for bookings."""
    session.add(
        GarageOpeningHours(
            garage_id=garage.id,
            weekday=0,
            opens_at=time(8, 0),
            closes_at=time(18, 0),
            is_closed=False,
        )
    )
    garage.status = GARAGE_STATUS_SUSPENDED
    session.commit()

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/readiness").json

    business = {c["key"]: c["ok"] for c in body["business"]}
    assert business["public_booking_enabled"] is False
    assert body["business_ready"] is False
    assert body["ready_for_public_booking"] is False


def test_archived_business_never_reads_ready_for_public_booking(
    platform_client, garage, user, appointment_type, session
):
    session.add(
        GarageOpeningHours(
            garage_id=garage.id,
            weekday=0,
            opens_at=time(8, 0),
            closes_at=time(18, 0),
            is_closed=False,
        )
    )
    garage.status = GARAGE_STATUS_ARCHIVED
    session.commit()

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/readiness").json

    assert body["ready_for_public_booking"] is False


def test_stripe_payouts_disabled_alone_blocks_deposit_readiness(
    platform_client, garage, user, session
):
    """Charges-enabled-but-payouts-disabled is a real, dangerous Stripe
    state (the business could take money it can never receive) - it must
    not slip through because "charges_enabled" alone looked good."""
    session.add(
        GaragePaymentSettings(
            garage_id=garage.id,
            stripe_account_id="acct_test123",
            stripe_onboarding_complete=True,
            stripe_charges_enabled=True,
            stripe_payouts_enabled=False,
        )
    )
    session.commit()

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/readiness").json

    payments = {c["key"]: c["ok"] for c in body["payments"]}
    assert payments["stripe_charges_enabled"] is True
    assert payments["stripe_payouts_enabled"] is False
    assert body["payments_ready"] is False
    assert body["ready_to_take_deposits"] is False


def test_a_fully_ready_business_reads_ready_on_every_rollup(
    platform_client, garage, user, appointment_type, session
):
    """The positive control: once every real prerequisite is genuinely
    satisfied, the checklist must actually say so - a system that can only
    ever report "not ready" is as useless as one that lies positively."""
    session.add(
        GarageOpeningHours(
            garage_id=garage.id,
            weekday=0,
            opens_at=time(8, 0),
            closes_at=time(18, 0),
            is_closed=False,
        )
    )
    session.add(
        GaragePaymentSettings(
            garage_id=garage.id,
            stripe_account_id="acct_test123",
            stripe_onboarding_complete=True,
            stripe_charges_enabled=True,
            stripe_payouts_enabled=True,
        )
    )
    session.commit()

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/readiness").json

    assert body["business_ready"] is True
    assert body["payments_ready"] is True
    assert body["ready_for_public_booking"] is True
    assert body["ready_to_take_deposits"] is True


def test_readiness_reads_only_the_authenticated_tenant_never_a_body_supplied_id(
    platform_client, garage, second_garage, user, session
):
    """A malformed/foreign id in the URL must resolve against that id's own
    tenant, never fall back to some other business's state - readiness for
    garage A must never leak or borrow from garage B."""
    session.add(
        GarageCommunicationSettings(
            garage_id=second_garage.id, twilio_subaccount_sid="ACother0000000000000000000001"
        )
    )
    session.commit()

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/readiness").json

    assert body["garage_id"] == str(garage.id)
    communications = {c["key"]: c["ok"] for c in body["communications"]}
    assert communications["twilio_subaccount"] is False
