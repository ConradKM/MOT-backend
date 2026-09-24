"""voice.buy_number's reconciliation: recovering an already-purchased number
after a lost response or a crashed commit, without ever buying a second one.

Uses a fake Twilio client double rather than the real SDK - these exercise
voice.py's own logic directly, one level below the route/service tests in
tests/api/test_platform_admin_communications.py which mock voice.buy_number
itself."""

from __future__ import annotations

import pytest

from app.communications.provisioning import voice


class _FakeNumberResource:
    def __init__(self, phone_number, sid):
        self.phone_number = phone_number
        self.sid = sid
        self.capabilities = {"voice": True}
        self.updated_with = None

    def update(self, **kwargs):
        self.updated_with = kwargs
        return self


class _FakeIncomingPhoneNumbers:
    def __init__(self, existing=None):
        self._existing = existing
        self.create_calls = []
        self.list_calls = []

    def list(self, phone_number=None, limit=None):
        self.list_calls.append(phone_number)
        if self._existing and self._existing.phone_number == phone_number:
            return [self._existing]
        return []

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return _FakeNumberResource(kwargs["phone_number"], "PNnew0000000000000000000000000001")


class _FakeAvailableNumber:
    def __init__(self, phone_number, address_requirements):
        self.phone_number = phone_number
        self.address_requirements = address_requirements


class _FakeAvailableNumbersLocal:
    def __init__(self, numbers):
        self._numbers = numbers
        self.list_calls = []

    def list(self, contains=None, **kwargs):
        self.list_calls.append(contains)
        return [n for n in self._numbers if n.phone_number == contains]


class _FakeAvailablePhoneNumbers:
    def __init__(self, numbers):
        self.local = _FakeAvailableNumbersLocal(numbers)


class _FakeAddressResource:
    def __init__(self, sid, *, street, city, region, postal_code, iso_country, customer_name=None):
        self.sid = sid
        self.street = street
        self.city = city
        self.region = region
        self.postal_code = postal_code
        self.iso_country = iso_country
        self.customer_name = customer_name


class _FakeAddresses:
    def __init__(self, existing=None, error=None):
        self._existing = existing or []
        self._error = error
        self.create_calls = []
        self.list_calls = 0

    def list(self, limit=None):
        self.list_calls += 1
        return self._existing

    def create(self, **kwargs):
        if self._error is not None:
            raise self._error
        self.create_calls.append(kwargs)
        resource = _FakeAddressResource("ADnew0000000000000000000000000001", **kwargs)
        self._existing.append(resource)
        return resource


class _FakeClient:
    def __init__(
        self,
        existing=None,
        *,
        address_requirements="none",
        existing_addresses=None,
        address_error=None,
    ):
        self.incoming_phone_numbers = _FakeIncomingPhoneNumbers(existing)
        number = existing.phone_number if existing else "+441234567890"
        self._available = _FakeAvailablePhoneNumbers(
            [_FakeAvailableNumber(number, address_requirements)]
        )
        self.addresses = _FakeAddresses(existing_addresses, address_error)

    def available_phone_numbers(self, iso_country):
        return self._available


@pytest.fixture(autouse=True)
def _twilio_configured(app):
    app.config["TWILIO_ACCOUNT_SID"] = "ACmaster0000000000000000000000001"
    app.config["TWILIO_AUTH_TOKEN"] = "token"
    app.config["PUBLIC_API_BASE_URL"] = "https://example.onrender.com"
    yield


def test_buy_number_purchases_when_nothing_already_owned(monkeypatch, garage):
    client = _FakeClient()
    monkeypatch.setattr(voice, "get_client_for_subaccount_resources", lambda g: client)

    result = voice.buy_number(garage, "+441234567890")

    assert len(client.incoming_phone_numbers.create_calls) == 1
    assert result["sid"] == "PNnew0000000000000000000000000001"


def test_buy_number_reconciles_an_already_owned_number_instead_of_buying_again(monkeypatch, garage):
    """The exact residual risk this closes: Twilio's purchase succeeded on a
    prior attempt, but CoMaz never recorded it (lost response, crashed
    commit). A retry must recover that number, not buy a second one."""
    already_owned = _FakeNumberResource("+441234567890", "PNexisting00000000000000000000001")
    client = _FakeClient(existing=already_owned)
    monkeypatch.setattr(voice, "get_client_for_subaccount_resources", lambda g: client)

    result = voice.buy_number(garage, "+441234567890")

    assert client.incoming_phone_numbers.create_calls == []  # never bought a second number
    assert result["sid"] == "PNexisting00000000000000000000001"
    assert already_owned.updated_with["voice_method"] == "POST"


def test_buy_number_never_confuses_a_different_number_for_an_existing_purchase(monkeypatch, garage):
    other_number = _FakeNumberResource("+449999999999", "PNother00000000000000000000000001")
    client = _FakeClient(existing=other_number)
    monkeypatch.setattr(voice, "get_client_for_subaccount_resources", lambda g: client)

    result = voice.buy_number(garage, "+441234567890")

    assert len(client.incoming_phone_numbers.create_calls) == 1
    assert result["sid"] == "PNnew0000000000000000000000000001"


# --------------------------------------------------------------------------
# Regression coverage for the "Phone Number Requires an Address" bug: a UK
# number search that comes back with address_requirements != "none" must
# never reach Twilio's purchase call without an AddressSid, must never
# invent one from an incomplete tenant address, and must never create a
# duplicate Address resource on a retry.
# --------------------------------------------------------------------------


def _complete_address(garage):
    garage.address = "1 Test Street"
    garage.address_city = "London"
    garage.address_region = "Greater London"
    garage.postcode = "SW1A 1AA"


def test_buy_number_with_no_address_requirement_still_works(monkeypatch, garage):
    client = _FakeClient(address_requirements="none")
    monkeypatch.setattr(voice, "get_client_for_subaccount_resources", lambda g: client)

    result = voice.buy_number(garage, "+441234567890")

    assert "address_sid" not in client.incoming_phone_numbers.create_calls[0]
    assert client.addresses.list_calls == 0
    assert result["sid"] == "PNnew0000000000000000000000000001"


def test_buy_number_creates_and_supplies_an_address_when_required(monkeypatch, garage):
    _complete_address(garage)
    client = _FakeClient(address_requirements="local")
    monkeypatch.setattr(voice, "get_client_for_subaccount_resources", lambda g: client)

    result = voice.buy_number(garage, "+441234567890")

    assert len(client.addresses.create_calls) == 1
    created = client.addresses.create_calls[0]
    assert created["street"] == "1 Test Street"
    assert created["city"] == "London"
    assert created["region"] == "Greater London"
    assert created["postal_code"] == "SW1A 1AA"
    assert created["iso_country"] == "GB"
    assert client.incoming_phone_numbers.create_calls[0]["address_sid"] == "ADnew0000000000000000000000000001"
    assert result["sid"] == "PNnew0000000000000000000000000001"


def test_buy_number_reuses_an_existing_matching_address_instead_of_duplicating(monkeypatch, garage):
    """A retried Buy click (or a second business at the same address, e.g. a
    franchise) must never create a second identical Address resource."""
    _complete_address(garage)
    existing_address = _FakeAddressResource(
        "ADexisting000000000000000000001",
        street="1 Test Street",
        city="London",
        region="Greater London",
        postal_code="SW1A 1AA",
        iso_country="GB",
    )
    client = _FakeClient(address_requirements="local", existing_addresses=[existing_address])
    monkeypatch.setattr(voice, "get_client_for_subaccount_resources", lambda g: client)

    result = voice.buy_number(garage, "+441234567890")

    assert client.addresses.create_calls == []  # reused, never duplicated
    assert client.incoming_phone_numbers.create_calls[0]["address_sid"] == "ADexisting000000000000000000001"
    assert result["sid"] == "PNnew0000000000000000000000000001"


def test_buy_number_fails_before_purchase_when_business_address_is_incomplete(monkeypatch, garage):
    """The address is missing city/region - the actionable CoMaz error must
    appear *before* any provider call, never the raw Twilio AddressSid
    error, and no number may be bought while unclear whose address it is."""
    client = _FakeClient(address_requirements="local")
    monkeypatch.setattr(voice, "get_client_for_subaccount_resources", lambda g: client)

    with pytest.raises(voice.MissingBusinessAddressError) as exc_info:
        voice.buy_number(garage, "+441234567890")

    assert "address" in str(exc_info.value).lower()
    assert client.incoming_phone_numbers.create_calls == []
    assert client.addresses.create_calls == []


def test_buy_number_surfaces_a_clean_error_on_address_compliance_failure(monkeypatch, garage):
    """A provider-side rejection of the address itself (e.g. non-deliverable)
    must surface as CoMaz's own error type, not propagate a raw Twilio
    exception up through the action layer."""
    from twilio.base.exceptions import TwilioRestException

    _complete_address(garage)
    client = _FakeClient(
        address_requirements="local",
        address_error=TwilioRestException(400, "uri", msg="Address is not valid.", code=21620),
    )
    monkeypatch.setattr(voice, "get_client_for_subaccount_resources", lambda g: client)

    with pytest.raises(voice.VoiceProvisioningError, match="Address is not valid"):
        voice.buy_number(garage, "+441234567890")

    assert client.incoming_phone_numbers.create_calls == []


class _FakeOwnedNumber:
    def __init__(self, phone_number, sid, account_sid):
        self.phone_number = phone_number
        self.sid = sid
        self.account_sid = account_sid
        self.update_calls = []

    def fetch(self):
        return self

    def update(self, **kwargs):
        self.update_calls.append(kwargs)
        self.account_sid = kwargs.get("account_sid", self.account_sid)
        return self


class _FakeSubaccountResourceClient:
    def __init__(self, number: _FakeOwnedNumber):
        self._number = number

    def incoming_phone_numbers(self, sid):
        assert sid == self._number.sid
        return self._number


def test_return_to_parent_moves_a_subaccount_owned_number_back(monkeypatch, garage):
    number = _FakeOwnedNumber(
        "+441234567890", "PNowned0000000000000000000000001", "ACsubaccount000000000000000000001"
    )
    client = _FakeSubaccountResourceClient(number)
    monkeypatch.setattr(voice, "get_client_for_subaccount_resources", lambda g: client)

    result = voice.return_to_parent(garage, number.sid)

    assert number.update_calls == [{"account_sid": "ACmaster0000000000000000000000001"}]
    assert result["sid"] == number.sid


def test_return_to_parent_is_idempotent_if_already_moved(monkeypatch, garage):
    """A prior attempt's Twilio call succeeded but its result was never
    recorded - the retry must recognise that and not attempt to move an
    already-parent-owned number again."""
    number = _FakeOwnedNumber(
        "+441234567890", "PNowned0000000000000000000000001", "ACmaster0000000000000000000000001"
    )
    client = _FakeSubaccountResourceClient(number)
    monkeypatch.setattr(voice, "get_client_for_subaccount_resources", lambda g: client)

    result = voice.return_to_parent(garage, number.sid)

    assert number.update_calls == []  # already parent-owned - no provider mutation attempted
    assert result["sid"] == number.sid
