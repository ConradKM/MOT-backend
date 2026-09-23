"""openai_voice_sip's per-tenant SIP trunk provisioning: reuse-or-create at
every step, so a retry after a partial failure resumes instead of duplicating
a trunk, an origination URL, or a number association.

Uses a fake Twilio Trunking client double rather than the real SDK - these
exercise openai_voice_sip.py's own logic directly, one level below the
route/service tests in tests/api/test_platform_admin_openai_voice_enable.py
which mock openai_voice_sip.enable_openai_voice itself."""

from __future__ import annotations

import pytest

from app.communications.provisioning import openai_voice_sip
from app.communications.provisioning.openai_voice_sip import OpenAIVoiceProvisioningError


class _FakeTrunk:
    def __init__(self, sid, friendly_name):
        self.sid = sid
        self.friendly_name = friendly_name


class _FakeOriginationUrl:
    def __init__(self, sip_url):
        self.sip_url = sip_url


class _FakeNumberAssociation:
    def __init__(self, sid):
        self.sid = sid


class _FakeOriginationUrls:
    def __init__(self, existing=None):
        self._existing = list(existing or [])
        self.create_calls = []

    def list(self, limit=None):
        return list(self._existing)

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        self._existing.append(_FakeOriginationUrl(kwargs["sip_url"]))


class _FakePhoneNumbers:
    def __init__(self, existing=None):
        self._existing = list(existing or [])
        self.create_calls = []

    def list(self, limit=None):
        return list(self._existing)

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        self._existing.append(_FakeNumberAssociation(kwargs["phone_number_sid"]))


class _FakeTrunkContext:
    def __init__(self, origination_urls=None, phone_numbers=None):
        self.origination_urls = origination_urls or _FakeOriginationUrls()
        self.phone_numbers = phone_numbers or _FakePhoneNumbers()


class _FakeTrunks:
    def __init__(self, existing_trunks=None, contexts=None):
        self._existing = list(existing_trunks or [])
        self._contexts = contexts or {}
        self.create_calls = []

    def list(self, limit=None):
        return list(self._existing)

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        sid = f"TKnew{len(self._existing):031d}"
        trunk = _FakeTrunk(sid, kwargs["friendly_name"])
        self._existing.append(trunk)
        return trunk

    def __call__(self, trunk_sid):
        return self._contexts.setdefault(trunk_sid, _FakeTrunkContext())


class _FakeTrunking:
    def __init__(self, trunks):
        self.v1 = self
        self.trunks = trunks


class _FakeClient:
    def __init__(self, trunks):
        self.trunking = _FakeTrunking(trunks)


@pytest.fixture(autouse=True)
def _openai_configured(app):
    app.config["OPENAI_PROJECT_ID"] = "proj_test123"
    app.config["TWILIO_ACCOUNT_SID"] = "ACmaster0000000000000000000000001"
    app.config["TWILIO_AUTH_TOKEN"] = "token"
    yield


def test_get_or_create_tenant_trunk_creates_one_when_none_exists(monkeypatch, garage):
    trunks = _FakeTrunks()
    client = _FakeClient(trunks)
    monkeypatch.setattr(openai_voice_sip, "get_subaccount_client", lambda g: client)

    sid = openai_voice_sip.get_or_create_tenant_trunk(garage)

    assert sid.startswith("TKnew")
    assert len(trunks.create_calls) == 1


def test_get_or_create_tenant_trunk_reuses_an_existing_trunk_by_friendly_name(monkeypatch, garage):
    """The exact residual risk this closes: trunk creation succeeded on a
    prior attempt, but the caller crashed before recording the SID. A retry
    must reuse that trunk, not create a second one."""
    name = openai_voice_sip.trunk_friendly_name(garage)
    existing = _FakeTrunk("TKexisting0000000000000000000000001", name)
    trunks = _FakeTrunks(existing_trunks=[existing])
    client = _FakeClient(trunks)
    monkeypatch.setattr(openai_voice_sip, "get_subaccount_client", lambda g: client)

    sid = openai_voice_sip.get_or_create_tenant_trunk(garage)

    assert sid == "TKexisting0000000000000000000000001"
    assert trunks.create_calls == []


def test_ensure_origination_url_creates_when_missing(monkeypatch, garage):
    trunk_sid = "TKtest0000000000000000000000000001"
    context = _FakeTrunkContext()
    trunks = _FakeTrunks(contexts={trunk_sid: context})
    client = _FakeClient(trunks)
    monkeypatch.setattr(openai_voice_sip, "get_subaccount_client", lambda g: client)

    openai_voice_sip.ensure_origination_url(garage, trunk_sid)

    assert len(context.origination_urls.create_calls) == 1
    assert context.origination_urls.create_calls[0]["sip_url"] == (
        "sip:proj_test123@sip.api.openai.com;transport=tls"
    )


def test_ensure_origination_url_is_a_no_op_when_already_pointed_at_openai(monkeypatch, garage):
    trunk_sid = "TKtest0000000000000000000000000001"
    sip_url = "sip:proj_test123@sip.api.openai.com;transport=tls"
    context = _FakeTrunkContext(
        origination_urls=_FakeOriginationUrls(existing=[_FakeOriginationUrl(sip_url)])
    )
    trunks = _FakeTrunks(contexts={trunk_sid: context})
    client = _FakeClient(trunks)
    monkeypatch.setattr(openai_voice_sip, "get_subaccount_client", lambda g: client)

    openai_voice_sip.ensure_origination_url(garage, trunk_sid)

    assert context.origination_urls.create_calls == []


def test_ensure_number_associated_creates_when_missing(monkeypatch, garage):
    trunk_sid = "TKtest0000000000000000000000000001"
    number_sid = "PNtest0000000000000000000000000001"
    context = _FakeTrunkContext()
    trunks = _FakeTrunks(contexts={trunk_sid: context})
    client = _FakeClient(trunks)
    monkeypatch.setattr(openai_voice_sip, "get_subaccount_client", lambda g: client)

    openai_voice_sip.ensure_number_associated(garage, trunk_sid, number_sid)

    assert len(context.phone_numbers.create_calls) == 1
    assert context.phone_numbers.create_calls[0]["phone_number_sid"] == number_sid


def test_ensure_number_associated_is_a_no_op_when_already_associated(monkeypatch, garage):
    trunk_sid = "TKtest0000000000000000000000000001"
    number_sid = "PNtest0000000000000000000000000001"
    context = _FakeTrunkContext(
        phone_numbers=_FakePhoneNumbers(existing=[_FakeNumberAssociation(number_sid)])
    )
    trunks = _FakeTrunks(contexts={trunk_sid: context})
    client = _FakeClient(trunks)
    monkeypatch.setattr(openai_voice_sip, "get_subaccount_client", lambda g: client)

    openai_voice_sip.ensure_number_associated(garage, trunk_sid, number_sid)

    assert context.phone_numbers.create_calls == []


def test_enable_openai_voice_never_creates_a_second_trunk_on_retry(monkeypatch, garage):
    """Full orchestration, run twice: the second call must reuse everything
    the first call created rather than duplicating it."""
    trunks = _FakeTrunks()
    client = _FakeClient(trunks)
    monkeypatch.setattr(openai_voice_sip, "get_subaccount_client", lambda g: client)

    first_sid = openai_voice_sip.enable_openai_voice(garage, "PNtest0000000000000000000000000001")
    second_sid = openai_voice_sip.enable_openai_voice(garage, "PNtest0000000000000000000000000001")

    assert first_sid == second_sid
    assert len(trunks.create_calls) == 1
    context = trunks(first_sid)
    assert len(context.origination_urls.create_calls) == 1
    assert len(context.phone_numbers.create_calls) == 1


def test_openai_sip_origination_uri_requires_project_id_configured(app, garage):
    app.config["OPENAI_PROJECT_ID"] = ""

    with pytest.raises(OpenAIVoiceProvisioningError):
        openai_voice_sip.openai_sip_origination_uri()
