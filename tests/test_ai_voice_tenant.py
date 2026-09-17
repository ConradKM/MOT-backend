"""app/ai_voice/tenant.py - mapping an inbound OpenAI Realtime SIP call's
dialled number to the correct CoMaz business, tenant-safely.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.ai_voice.tenant import (
    caller_number_for_sip_call,
    phone_from_sip_uri,
    resolve_business_for_sip_call,
    sip_header,
)
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)


def _headers_dict(to=None, from_=None):
    headers = []
    if to is not None:
        headers.append({"name": "To", "value": to})
    if from_ is not None:
        headers.append({"name": "From", "value": from_})
    return headers


def _headers_objects(to=None, from_=None):
    """Mirrors the openai SDK's parsed DataSipHeader (attribute access, not
    dict) - confirms sip_header() works against either shape."""
    headers = []
    if to is not None:
        headers.append(SimpleNamespace(name="To", value=to))
    if from_ is not None:
        headers.append(SimpleNamespace(name="From", value=from_))
    return headers


def test_sip_header_reads_dict_and_object_headers():
    dict_headers = _headers_dict(to="sip:+442012345678@sip.example.com")
    obj_headers = _headers_objects(to="sip:+442012345678@sip.example.com")
    assert sip_header(dict_headers, "To") == "sip:+442012345678@sip.example.com"
    assert sip_header(obj_headers, "To") == "sip:+442012345678@sip.example.com"
    assert sip_header(dict_headers, "to") == "sip:+442012345678@sip.example.com"  # case-insensitive
    assert sip_header(dict_headers, "From") is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("sip:+442012345678@sip.example.com", "+442012345678"),
        ("tel:+442012345678", "+442012345678"),
        ("sips:+442012345678@sip.example.com;user=phone", "+442012345678"),
        (None, None),
        ("", None),
    ],
)
def test_phone_from_sip_uri(raw, expected):
    assert phone_from_sip_uri(raw) == expected


def test_resolve_business_for_sip_call_known_number(session, garage):
    session.add(
        GarageCommunicationSettings(
            garage_id=garage.id, communications_enabled=True, voice_phone_number="+442012345678"
        )
    )
    session.commit()

    resolved = resolve_business_for_sip_call(_headers_dict(to="sip:+442012345678@sip.example.com"))
    assert resolved is not None
    assert resolved.id == garage.id


def test_resolve_business_for_sip_call_unknown_number(garage):
    resolved = resolve_business_for_sip_call(_headers_dict(to="sip:+441614969999@sip.example.com"))
    assert resolved is None


def test_resolve_business_for_sip_call_missing_to_header(garage):
    assert resolve_business_for_sip_call([]) is None
    assert resolve_business_for_sip_call(_headers_dict(from_="sip:+447123456789@x")) is None


def test_resolve_business_for_sip_call_malformed_number(garage):
    assert resolve_business_for_sip_call(_headers_dict(to="sip:not-a-number@x")) is None


def test_resolve_business_for_sip_call_e164_normalisation(session, garage):
    """A UK-formatted To header (0-prefixed national form embedded in the
    SIP URI) still resolves against a stored E.164 voice_phone_number."""
    session.add(
        GarageCommunicationSettings(
            garage_id=garage.id, communications_enabled=True, voice_phone_number="+441614960001"
        )
    )
    session.commit()

    resolved = resolve_business_for_sip_call(_headers_dict(to="sip:01614960001@sip.example.com"))
    assert resolved is not None
    assert resolved.id == garage.id


def test_duplicate_voice_number_mapping_is_rejected_at_the_database_level(
    session, garage, second_garage
):
    """Tenant-safety guarantee this whole module leans on: two businesses
    can never share a voice_phone_number, so `.first()` inside
    resolve_garage_by_voice_number can never silently pick a tenant at
    random - see GarageCommunicationSettings' own unique constraint."""
    session.add(
        GarageCommunicationSettings(
            garage_id=garage.id, communications_enabled=True, voice_phone_number="+442012345678"
        )
    )
    session.commit()

    session.add(
        GarageCommunicationSettings(
            garage_id=second_garage.id,
            communications_enabled=True,
            voice_phone_number="+442012345678",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_caller_number_for_sip_call_normalises_when_possible(garage):
    assert caller_number_for_sip_call(_headers_dict(from_="sip:07123456789@x")) == "+447123456789"


def test_caller_number_for_sip_call_falls_back_to_raw_value(garage):
    # Not a parseable UK number - still returned as-is rather than dropped,
    # since it's only ever used for logging/contact defaults, never auth.
    assert caller_number_for_sip_call(_headers_dict(from_="sip:anonymous@x")) == "anonymous"


def test_caller_number_for_sip_call_empty_when_absent(garage):
    assert caller_number_for_sip_call([]) == ""


def test_cross_tenant_number_cannot_resolve_another_business(session, garage, second_garage):
    session.add(
        GarageCommunicationSettings(
            garage_id=garage.id, communications_enabled=True, voice_phone_number="+442012345678"
        )
    )
    session.add(
        GarageCommunicationSettings(
            garage_id=second_garage.id,
            communications_enabled=True,
            voice_phone_number="+441614960002",
        )
    )
    session.commit()

    resolved = resolve_business_for_sip_call(_headers_dict(to="sip:+441614960002@x"))
    assert resolved is not None
    assert resolved.id == second_garage.id
    assert resolved.id != garage.id
