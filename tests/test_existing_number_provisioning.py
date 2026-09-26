"""Provider-agnostic existing-number onboarding
(app/communications/provisioning/existing_number.py): capability ->
recommended mode, provider-info validation, readiness ("still required"),
and idempotent Twilio BYOC provisioning.

Uses fake Twilio resource doubles rather than the real SDK - the same
convention as tests/test_openai_voice_sip_provisioning.py - so these exercise
existing_number.py's own reuse-or-create logic directly, one level below the
route tests further down this file.
"""

from __future__ import annotations

import pytest

from app.communications.provisioning import existing_number
from app.communications.provisioning.existing_number import (
    CAPABILITY_BIDIRECTIONAL_SIP,
    CAPABILITY_FORWARDING_ONLY,
    CAPABILITY_INBOUND_SIP_ONLY,
    CAPABILITY_UNKNOWN,
    STATUS_ACTIVE,
    STATUS_AWAITING_CARRIER_CONFIGURATION,
    STATUS_ERROR,
    STATUS_NOT_CONFIGURED,
    ExistingNumberProvisioningError,
    ExistingNumberValidationError,
)
from app.communications.telephony import MODE_PSTN_FORWARD, MODE_SIP_BYOC
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)

PBX_URI = "sip:reception@bt-pbx.example.co.uk"
CARRIER_IPS = "203.0.113.10, 203.0.113.11"


@pytest.fixture()
def voice_config(app, monkeypatch):
    monkeypatch.setitem(app.config, "TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
    monkeypatch.setitem(app.config, "TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setitem(app.config, "PUBLIC_API_BASE_URL", "https://api.example.test")


# --------------------------------------------------------------------------
# Fake Twilio resources - reuse-or-create doubles
# --------------------------------------------------------------------------


class _Resource:
    def __init__(self, sid, **fields):
        self.sid = sid
        for key, value in fields.items():
            setattr(self, key, value)


class _FakeCollection:
    """A minimal stand-in for a Twilio list resource: .list()/.create()."""

    def __init__(self, prefix, key_field, existing=None):
        self._prefix = prefix
        self._key_field = key_field
        self._items = list(existing or [])
        self.create_calls = []

    def list(self, limit=None):
        return list(self._items)

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        sid = f"{self._prefix}{len(self._items):030d}"
        item = _Resource(sid, **kwargs)
        self._items.append(item)
        return item


class _FakeIpAddresses(_FakeCollection):
    def __init__(self, existing=None):
        super().__init__("IP", "ip_address", existing)


class _FakeAclMappings(_FakeCollection):
    def __init__(self, existing=None):
        super().__init__("MP", "ip_access_control_list_sid", existing)


class _FakeAclContext:
    def __init__(self, ip_addresses=None):
        self.ip_addresses = ip_addresses or _FakeIpAddresses()


class _FakeIpAcls(_FakeCollection):
    def __init__(self, existing=None, contexts=None):
        super().__init__("AL", "friendly_name", existing)
        self._contexts = contexts or {}

    def __call__(self, sid):
        return self._contexts.setdefault(sid, _FakeAclContext())


class _FakeDomainContext:
    def __init__(self, ip_access_control_list_mappings=None):
        self.ip_access_control_list_mappings = ip_access_control_list_mappings or _FakeAclMappings()
        self.update_calls = []

    def update(self, **kwargs):
        self.update_calls.append(kwargs)


class _FakeDomains(_FakeCollection):
    def __init__(self, existing=None, contexts=None):
        super().__init__("SD", "domain_name", existing)
        self._contexts = contexts or {}

    def __call__(self, sid):
        return self._contexts.setdefault(sid, _FakeDomainContext())


class _FakeTargets(_FakeCollection):
    def __init__(self, existing=None):
        super().__init__("KT", "target", existing)


class _FakePolicyContext:
    def __init__(self, targets=None):
        self.connection_policy_targets = targets or _FakeTargets()


class _FakeConnectionPolicies(_FakeCollection):
    def __init__(self, existing=None, contexts=None):
        super().__init__("NY", "friendly_name", existing)
        self._contexts = contexts or {}

    def __call__(self, sid):
        return self._contexts.setdefault(sid, _FakePolicyContext())


class _FakeByocContext:
    def __init__(self):
        self.update_calls = []

    def update(self, **kwargs):
        self.update_calls.append(kwargs)


class _FakeByocTrunks(_FakeCollection):
    def __init__(self, existing=None, contexts=None):
        super().__init__("BY", "friendly_name", existing)
        self._contexts = contexts or {}

    def __call__(self, sid):
        return self._contexts.setdefault(sid, _FakeByocContext())


class _FakeSip:
    def __init__(self, domains=None, ip_access_control_lists=None):
        self.domains = domains or _FakeDomains()
        self.ip_access_control_lists = ip_access_control_lists or _FakeIpAcls()


class _FakeVoiceV1:
    def __init__(self, connection_policies=None, byoc_trunks=None):
        self.connection_policies = connection_policies or _FakeConnectionPolicies()
        self.byoc_trunks = byoc_trunks or _FakeByocTrunks()


class _FakeVoice:
    def __init__(self, v1=None):
        self.v1 = v1 or _FakeVoiceV1()


class _FakeClient:
    def __init__(self, sip=None, voice=None):
        self.sip = sip or _FakeSip()
        self.voice = voice or _FakeVoice()


class _RaisingCollection:
    """A fake that fails on .create() - for the partial-failure tests."""

    def __init__(self, existing=None):
        self._items = list(existing or [])

    def list(self, limit=None):
        return list(self._items)

    def create(self, **kwargs):
        from twilio.base.exceptions import TwilioRestException

        raise TwilioRestException(400, "https://voice.twilio.com/v1/ByocTrunks", msg="nope")


# --------------------------------------------------------------------------
# Capability -> recommended mode
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("capability", "expected"),
    [
        (CAPABILITY_BIDIRECTIONAL_SIP, MODE_SIP_BYOC),
        (CAPABILITY_INBOUND_SIP_ONLY, MODE_SIP_BYOC),
        (CAPABILITY_FORWARDING_ONLY, MODE_PSTN_FORWARD),
        (CAPABILITY_UNKNOWN, None),
        (None, None),
        ("", None),
    ],
)
def test_recommend_mode(capability, expected):
    assert existing_number.recommend_mode(capability) == expected


# --------------------------------------------------------------------------
# Provider-info validation
# --------------------------------------------------------------------------


def test_validate_provider_info_normalises_and_dedupes_ips():
    values = existing_number.validate_provider_info(
        {
            "integration_provider_name": "  BT  ",
            "integration_provider_product": "Cloud Voice",
            "integration_capability": "bidirectional_sip",
            "integration_carrier_sip_uri": PBX_URI,
            "integration_carrier_ip_addresses": "203.0.113.10, 203.0.113.10, 203.0.113.11",
        }
    )
    assert values["integration_provider_name"] == "BT"
    assert values["integration_capability"] == CAPABILITY_BIDIRECTIONAL_SIP
    assert values["integration_carrier_sip_uri"] == PBX_URI
    assert values["integration_carrier_ip_addresses"] == "203.0.113.10, 203.0.113.11"


def test_validate_provider_info_rejects_unknown_capability():
    with pytest.raises(ExistingNumberValidationError) as exc:
        existing_number.validate_provider_info({"integration_capability": "CARRIER_PIGEON"})
    assert exc.value.field == "integration_capability"


def test_validate_provider_info_rejects_a_bad_ip():
    with pytest.raises(ExistingNumberValidationError) as exc:
        existing_number.validate_provider_info(
            {
                "integration_capability": CAPABILITY_INBOUND_SIP_ONLY,
                "integration_carrier_ip_addresses": "not-an-ip",
            }
        )
    assert exc.value.field == "integration_carrier_ip_addresses"


def test_validate_provider_info_rejects_a_carrier_sip_uri_that_routes_into_comaz():
    """Loop prevention applies to the carrier's own egress URI too - a typo
    that points back at Twilio/OpenAI must never be recorded as an outbound
    target."""
    with pytest.raises(ExistingNumberValidationError) as exc:
        existing_number.validate_provider_info(
            {
                "integration_capability": CAPABILITY_BIDIRECTIONAL_SIP,
                "integration_carrier_sip_uri": "sip:x@comaz.sip.twilio.com",
            }
        )
    assert exc.value.field == "integration_carrier_sip_uri"


def test_validate_provider_info_defaults_to_unknown():
    values = existing_number.validate_provider_info({})
    assert values["integration_capability"] == CAPABILITY_UNKNOWN


# --------------------------------------------------------------------------
# Readiness - "still required"
# --------------------------------------------------------------------------


def test_unknown_capability_blocks_on_capability_alone(session, garage):
    session.add(GarageCommunicationSettings(garage_id=garage.id))
    session.commit()
    session.refresh(garage)
    missing = existing_number.still_required(garage)
    assert len(missing) == 1
    assert "connectivity" in missing[0]


def test_bidirectional_sip_still_required_lists_every_gap(session, garage):
    session.add(
        GarageCommunicationSettings(
            garage_id=garage.id, integration_capability=CAPABILITY_BIDIRECTIONAL_SIP
        )
    )
    session.commit()
    session.refresh(garage)
    missing = existing_number.still_required(garage)
    assert any("public number" in m for m in missing)
    assert any("signalling IP" in m for m in missing)
    assert any("SIP endpoint" in m for m in missing)
    assert any("primary human handoff" in m for m in missing)


def test_inbound_sip_only_does_not_require_a_carrier_egress_uri(session, garage):
    """Inbound-only capability has no outbound leg to the carrier - a safe
    PSTN human destination is the expected fallback, not a blocker."""
    session.add(
        GarageCommunicationSettings(
            garage_id=garage.id,
            integration_capability=CAPABILITY_INBOUND_SIP_ONLY,
            public_business_number="+441619990001",
            integration_carrier_ip_addresses=CARRIER_IPS,
            human_primary_type="PSTN_NUMBER",
            human_primary_destination="+447911123111",
        )
    )
    session.commit()
    session.refresh(garage)
    assert existing_number.still_required(garage) == []


def test_forwarding_only_needs_a_comaz_ingress_number(session, garage):
    session.add(
        GarageCommunicationSettings(
            garage_id=garage.id,
            integration_capability=CAPABILITY_FORWARDING_ONLY,
            public_business_number="+441619990002",
            human_primary_type="PSTN_NUMBER",
            human_primary_destination="+447911123111",
        )
    )
    session.commit()
    session.refresh(garage)
    missing = existing_number.still_required(garage)
    assert any("CoMaz number" in m for m in missing)


def test_a_pre_telephony_business_reports_not_configured_without_crashing(garage):
    """A garage with no communication_settings row at all - backward
    compatibility with every tenant onboarded before this existed."""
    view = existing_number.describe_integration(garage)
    assert view["integration_status"] == STATUS_NOT_CONFIGURED
    assert view["integration_capability"] == CAPABILITY_UNKNOWN
    assert view["recommended_mode"] is None
    assert view["configure_at_carrier"] is None
    assert view["can_activate"] is False


# --------------------------------------------------------------------------
# Provisioning - idempotent, tenant-isolated, no secrets
# --------------------------------------------------------------------------


def _ready_settings(garage_id, **overrides):
    fields = {
        "garage_id": garage_id,
        "integration_capability": CAPABILITY_BIDIRECTIONAL_SIP,
        "public_business_number": "+441619990001",
        "integration_carrier_ip_addresses": CARRIER_IPS,
        "integration_carrier_sip_uri": PBX_URI,
        "human_primary_type": "SIP_URI",
        "human_primary_destination": PBX_URI,
    }
    fields.update(overrides)
    return GarageCommunicationSettings(**fields)


def test_provision_creates_every_resource_once(monkeypatch, session, garage, voice_config):
    client = _FakeClient()
    monkeypatch.setattr(existing_number, "get_subaccount_client", lambda g: client)
    session.add(_ready_settings(garage.id))
    session.commit()
    session.refresh(garage)

    result = existing_number.provision(garage)

    assert result["integration_status"] == STATUS_AWAITING_CARRIER_CONFIGURATION
    assert len(client.sip.domains.create_calls) == 1
    assert len(client.sip.ip_access_control_lists.create_calls) == 1
    assert len(client.voice.v1.connection_policies.create_calls) == 1
    assert len(client.voice.v1.byoc_trunks.create_calls) == 1
    settings = garage.communication_settings
    assert settings.byoc_sip_domain_sid.startswith("SD")
    assert settings.byoc_trunk_sid.startswith("BY")
    assert settings.integration_connection_policy_sid.startswith("NY")
    assert settings.integration_ip_acl_sid.startswith("AL")


def test_provisioning_twice_reuses_every_resource(monkeypatch, session, garage, voice_config):
    """Pressing Provision twice - or retrying after a timeout - must not
    create duplicate trunks/domains/policies."""
    client = _FakeClient()
    monkeypatch.setattr(existing_number, "get_subaccount_client", lambda g: client)
    session.add(_ready_settings(garage.id))
    session.commit()
    session.refresh(garage)

    existing_number.provision(garage)
    existing_number.provision(garage)

    assert len(client.sip.domains.create_calls) == 1
    assert len(client.sip.ip_access_control_lists.create_calls) == 1
    assert len(client.voice.v1.connection_policies.create_calls) == 1
    assert len(client.voice.v1.byoc_trunks.create_calls) == 1
    # The IP address itself is only added once too.
    acl_sid = garage.communication_settings.integration_ip_acl_sid
    assert len(client.sip.ip_access_control_lists(acl_sid).ip_addresses.list()) == 2


def test_two_businesses_never_collide_on_the_same_fake_twilio_account(
    monkeypatch, session, garage, second_garage, voice_config
):
    """Tenant isolation: each business's resources are matched by a name
    derived from *that* garage, so provisioning one never finds - or
    touches - the other's."""
    client = _FakeClient()
    monkeypatch.setattr(existing_number, "get_subaccount_client", lambda g: client)
    session.add(_ready_settings(garage.id, public_business_number="+441619990001"))
    session.add(_ready_settings(second_garage.id, public_business_number="+441619990099"))
    session.commit()
    session.refresh(garage)
    session.refresh(second_garage)

    existing_number.provision(garage)
    existing_number.provision(second_garage)

    assert len(client.sip.domains.create_calls) == 2
    assert len(client.voice.v1.byoc_trunks.create_calls) == 2
    first = garage.communication_settings
    second = second_garage.communication_settings
    assert first.byoc_sip_domain_sid != second.byoc_sip_domain_sid
    assert first.byoc_trunk_sid != second.byoc_trunk_sid


def test_provisioning_without_a_carrier_egress_uri_skips_the_connection_policy(
    monkeypatch, session, garage, voice_config
):
    """Inbound-only: no outbound leg to the carrier, so no Connection
    Policy is created - CoMaz never invents a target it wasn't given."""
    client = _FakeClient()
    monkeypatch.setattr(existing_number, "get_subaccount_client", lambda g: client)
    session.add(
        _ready_settings(
            garage.id,
            integration_capability=CAPABILITY_INBOUND_SIP_ONLY,
            integration_carrier_sip_uri=None,
            human_primary_type="PSTN_NUMBER",
            human_primary_destination="+447911123111",
        )
    )
    session.commit()
    session.refresh(garage)

    existing_number.provision(garage)

    assert client.voice.v1.connection_policies.create_calls == []
    assert garage.communication_settings.integration_connection_policy_sid is None


def test_provisioning_records_error_and_can_be_retried(monkeypatch, session, garage, voice_config):
    """A failure mid-provisioning is recorded (ERROR + message), and a retry
    after the underlying problem is fixed succeeds."""
    failing_client = _FakeClient(voice=_FakeVoice(_FakeVoiceV1(byoc_trunks=_RaisingCollection())))
    monkeypatch.setattr(existing_number, "get_subaccount_client", lambda g: failing_client)
    session.add(_ready_settings(garage.id))
    session.commit()
    session.refresh(garage)

    with pytest.raises(ExistingNumberProvisioningError):
        existing_number.provision(garage)

    settings = garage.communication_settings
    assert settings.integration_status == STATUS_ERROR
    assert settings.integration_error
    # The SIP Domain step ran fine before the trunk step failed.
    assert settings.byoc_sip_domain_sid is not None

    working_client = _FakeClient()
    monkeypatch.setattr(existing_number, "get_subaccount_client", lambda g: working_client)
    result = existing_number.provision(garage)
    assert result["integration_status"] == STATUS_AWAITING_CARRIER_CONFIGURATION
    assert result["integration_error"] is None


def test_provisioning_refuses_when_still_required_is_non_empty(session, garage):
    session.add(
        GarageCommunicationSettings(
            garage_id=garage.id, integration_capability=CAPABILITY_BIDIRECTIONAL_SIP
        )
    )
    session.commit()
    session.refresh(garage)
    with pytest.raises(ExistingNumberProvisioningError):
        existing_number.provision(garage)


def test_provisioning_refuses_an_unknown_capability(session, garage):
    session.add(GarageCommunicationSettings(garage_id=garage.id))
    session.commit()
    session.refresh(garage)
    with pytest.raises(ExistingNumberProvisioningError):
        existing_number.provision(garage)


def test_forwarding_only_provisions_without_touching_twilio(
    monkeypatch, session, garage, voice_config
):
    """PSTN_FORWARD needs no new Twilio resource - the ingress number
    already exists."""

    def _boom(_garage):
        raise AssertionError("forwarding-only must not call Twilio at all")

    monkeypatch.setattr(existing_number, "get_subaccount_client", _boom)
    session.add(
        GarageCommunicationSettings(
            garage_id=garage.id,
            voice_phone_number="+441611234567",
            integration_capability=CAPABILITY_FORWARDING_ONLY,
            public_business_number="+441619990002",
            human_primary_type="PSTN_NUMBER",
            human_primary_destination="+447911123111",
        )
    )
    session.commit()
    session.refresh(garage)

    result = existing_number.provision(garage)
    assert result["integration_status"] == STATUS_AWAITING_CARRIER_CONFIGURATION
    assert result["configure_at_carrier"]["forward_to"] == "+441611234567"


def test_no_test_checklist_before_provisioning(session, garage):
    """The operator has nothing to dial and listen for yet - showing a
    checklist against an unprovisioned integration would be misleading."""
    session.add(_ready_settings(garage.id))
    session.commit()
    session.refresh(garage)
    assert existing_number.describe_integration(garage)["test_checklist"] == []


def test_checklist_names_the_effective_human_chain(monkeypatch, session, garage, voice_config):
    client = _FakeClient()
    monkeypatch.setattr(existing_number, "get_subaccount_client", lambda g: client)
    session.add(
        _ready_settings(
            garage.id,
            human_primary_type="SIP_URI",
            human_primary_destination=PBX_URI,
            human_secondary_type="PSTN_NUMBER",
            human_secondary_destination="+447911123111",
        )
    )
    session.commit()
    session.refresh(garage)

    result = existing_number.provision(garage)
    checklist = result["test_checklist"]
    assert any(PBX_URI in item for item in checklist)
    assert any("+447911123111" in item for item in checklist)
    assert any("loop" in item.lower() or "menu" in item.lower() for item in checklist)


def test_checklist_flags_no_reachable_destination(monkeypatch, session, garage, voice_config):
    """A business that provisions successfully but has no safe human
    destination must not get a checklist implying one exists."""
    client = _FakeClient()
    monkeypatch.setattr(existing_number, "get_subaccount_client", lambda g: client)
    session.add(
        _ready_settings(
            garage.id,
            human_primary_type="PSTN_NUMBER",
            human_primary_destination="+441619990001",  # == its own public number - loop
        )
    )
    session.commit()
    session.refresh(garage)

    result = existing_number.provision(garage)
    assert any(
        "no human destination is currently reachable" in item.lower()
        for item in result["test_checklist"]
    )


def test_describe_integration_never_carries_a_secret_looking_key(
    monkeypatch, session, garage, voice_config
):
    client = _FakeClient()
    monkeypatch.setattr(existing_number, "get_subaccount_client", lambda g: client)
    session.add(_ready_settings(garage.id))
    session.commit()
    session.refresh(garage)
    existing_number.provision(garage)

    view = existing_number.describe_integration(garage)
    blob = str(view).lower()
    for needle in ("token", "secret", "password", "auth_token"):
        assert needle not in blob


# --------------------------------------------------------------------------
# Activation - separate from provisioning, requires real readiness
# --------------------------------------------------------------------------


def test_activation_refused_before_provisioning(session, garage):
    session.add(_ready_settings(garage.id))
    session.commit()
    session.refresh(garage)
    with pytest.raises(ExistingNumberProvisioningError):
        existing_number.activate(garage)


def test_activation_refused_without_a_reachable_human_destination(
    monkeypatch, session, garage, voice_config
):
    client = _FakeClient()
    monkeypatch.setattr(existing_number, "get_subaccount_client", lambda g: client)
    session.add(_ready_settings(garage.id))
    session.commit()
    session.refresh(garage)
    existing_number.provision(garage)

    # The primary destination is the business's own ingress number - it
    # would never dial anywhere and gets skipped by loop prevention.
    settings = garage.communication_settings
    settings.human_primary_destination = settings.public_business_number
    settings.human_primary_type = "PSTN_NUMBER"
    session.commit()

    with pytest.raises(ExistingNumberProvisioningError):
        existing_number.activate(garage)


def test_activation_succeeds_once_ready(monkeypatch, session, garage, voice_config):
    client = _FakeClient()
    monkeypatch.setattr(existing_number, "get_subaccount_client", lambda g: client)
    session.add(_ready_settings(garage.id))
    session.commit()
    session.refresh(garage)
    existing_number.provision(garage)

    result = existing_number.activate(garage)
    assert result["integration_status"] == STATUS_ACTIVE


# --------------------------------------------------------------------------
# Platform Admin routes
# --------------------------------------------------------------------------

COMMS = "/api/platform-admin/tenants/{garage_id}/communications"


def test_admin_records_provider_info(platform_client, session, garage):
    session.add(GarageCommunicationSettings(garage_id=garage.id, communications_enabled=True))
    session.commit()
    resp = platform_client.put(
        f"{COMMS.format(garage_id=garage.id)}/voice/existing-number-integration",
        json={
            "integration_provider_name": "BT",
            "integration_provider_product": "Cloud Voice",
            "integration_capability": "BIDIRECTIONAL_SIP",
            "integration_carrier_sip_uri": PBX_URI,
            "integration_carrier_ip_addresses": CARRIER_IPS,
        },
    )
    assert resp.status_code == 200, resp.get_json()
    view = resp.get_json()["voice"]["existing_number_integration"]
    assert view["integration_provider_name"] == "BT"
    assert view["recommended_mode"] == "SIP_BYOC"
    assert view["integration_status"] == "NOT_CONFIGURED"


def test_admin_rejects_an_unknown_capability_value(platform_client, session, garage):
    session.add(GarageCommunicationSettings(garage_id=garage.id, communications_enabled=True))
    session.commit()
    resp = platform_client.put(
        f"{COMMS.format(garage_id=garage.id)}/voice/existing-number-integration",
        json={"integration_capability": "CARRIER_PIGEON"},
    )
    assert resp.status_code == 422, resp.get_json()


def test_support_admin_cannot_record_provider_info(support_client, session, garage):
    session.add(GarageCommunicationSettings(garage_id=garage.id, communications_enabled=True))
    session.commit()
    resp = support_client.put(
        f"{COMMS.format(garage_id=garage.id)}/voice/existing-number-integration",
        json={"integration_capability": "BIDIRECTIONAL_SIP"},
    )
    assert resp.status_code == 403


def test_admin_provisions_and_activates_over_the_api(
    monkeypatch, platform_client, session, garage, voice_config
):
    client = _FakeClient()
    monkeypatch.setattr(existing_number, "get_subaccount_client", lambda g: client)
    session.add(_ready_settings(garage.id))
    session.commit()

    provision_resp = platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/voice/existing-number-integration/provision"
    )
    assert provision_resp.status_code == 200, provision_resp.get_json()
    assert (
        provision_resp.get_json()["voice"]["existing_number_integration"]["integration_status"]
        == "AWAITING_CARRIER_CONFIGURATION"
    )

    activate_resp = platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/voice/existing-number-integration/activate"
    )
    assert activate_resp.status_code == 200, activate_resp.get_json()
    assert (
        activate_resp.get_json()["voice"]["existing_number_integration"]["integration_status"]
        == "ACTIVE"
    )


def test_provisioning_over_the_api_reports_a_twilio_failure_as_422(
    monkeypatch, platform_client, session, garage, voice_config
):
    failing_client = _FakeClient(voice=_FakeVoice(_FakeVoiceV1(byoc_trunks=_RaisingCollection())))
    monkeypatch.setattr(existing_number, "get_subaccount_client", lambda g: failing_client)
    session.add(_ready_settings(garage.id))
    session.commit()

    resp = platform_client.post(
        f"{COMMS.format(garage_id=garage.id)}/voice/existing-number-integration/provision"
    )
    assert resp.status_code == 422, resp.get_json()
