"""API tests for PUT /api/garage/booking-appearance and where the colours are
read back: GET /api/garage (the settings page) and the public garage payload
(every customer-facing page of that business)."""

import pytest

URL = "/api/garage/booking-appearance"
DARK = {"booking_background_color": "#0B1F3A", "booking_text_color": "#F5F7FA"}


def test_owner_can_set_the_colours_and_they_are_stored_lowercase(authenticated_user, session):
    resp = authenticated_user.client.put(URL, json=DARK)

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["booking_background_color"] == "#0b1f3a"
    assert body["booking_text_color"] == "#f5f7fa"
    session.refresh(authenticated_user.garage)
    assert authenticated_user.garage.booking_background_color == "#0b1f3a"
    assert authenticated_user.garage.booking_text_color == "#f5f7fa"


def test_the_colours_are_read_back_on_the_garage_record(authenticated_user):
    assert authenticated_user.client.get("/api/garage").get_json()["booking_text_color"] is None

    authenticated_user.client.put(URL, json=DARK)

    body = authenticated_user.client.get("/api/garage").get_json()
    assert body["booking_background_color"] == "#0b1f3a"
    assert body["booking_text_color"] == "#f5f7fa"


def test_both_null_resets_to_the_default(authenticated_user, session):
    authenticated_user.client.put(URL, json=DARK)

    resp = authenticated_user.client.put(
        URL, json={"booking_background_color": None, "booking_text_color": None}
    )

    assert resp.status_code == 200
    assert resp.get_json()["booking_background_color"] is None
    session.refresh(authenticated_user.garage)
    assert authenticated_user.garage.booking_background_color is None
    assert authenticated_user.garage.booking_text_color is None


def test_staff_without_the_owner_role_get_403(authenticated_user, session):
    authenticated_user.user.roles = []
    session.commit()

    resp = authenticated_user.client.put(URL, json=DARK)

    assert resp.status_code == 403
    session.refresh(authenticated_user.garage)
    assert authenticated_user.garage.booking_background_color is None


def test_unauthenticated_request_is_rejected(client):
    assert client.put(URL, json=DARK).status_code == 401


@pytest.mark.parametrize("bad", ["#fff", "red", "rgb(0,0,0)", "#0b1f3aff", "#0b1f3a;}", ""])
def test_a_value_that_is_not_six_digit_hex_is_a_422(authenticated_user, bad):
    resp = authenticated_user.client.put(URL, json={**DARK, "booking_background_color": bad})

    assert resp.status_code == 422


@pytest.mark.parametrize("missing", ["booking_background_color", "booking_text_color"])
def test_one_colour_set_without_the_other_is_a_422(authenticated_user, missing):
    resp = authenticated_user.client.put(URL, json={**DARK, missing: None})

    assert resp.status_code == 422
    assert "both" in resp.get_json()["message"]


def test_a_request_must_name_both_keys(authenticated_user):
    resp = authenticated_user.client.put(URL, json={"booking_background_color": "#000000"})

    assert resp.status_code == 422


def test_low_contrast_is_a_422_that_states_the_ratio_and_minimum(authenticated_user, session):
    resp = authenticated_user.client.put(
        URL, json={"booking_background_color": "#ffffff", "booking_text_color": "#777777"}
    )

    assert resp.status_code == 422
    message = resp.get_json()["message"]
    assert "4.48:1" in message
    assert "4.5:1" in message
    session.refresh(authenticated_user.garage)
    assert authenticated_user.garage.booking_background_color is None


def test_an_owner_only_ever_changes_their_own_business(
    authenticated_user, second_authenticated_client, second_garage, session
):
    resp = second_authenticated_client.put(URL, json=DARK)

    assert resp.status_code == 200
    session.refresh(authenticated_user.garage)
    session.refresh(second_garage)
    assert authenticated_user.garage.booking_background_color is None
    assert second_garage.booking_background_color == "#0b1f3a"


# --------------------------------------------------------------------------
# The public payload
# --------------------------------------------------------------------------


def test_public_payload_by_id_and_slug_carries_the_colours(authenticated_user, client):
    authenticated_user.client.put(URL, json=DARK)
    garage = authenticated_user.garage

    for path in (f"/api/public/garages/{garage.id}", f"/api/public/{garage.slug}"):
        body = client.get(path).get_json()
        assert body["booking_background_color"] == "#0b1f3a", path
        assert body["booking_text_color"] == "#f5f7fa", path


def test_public_payload_of_another_business_stays_null(authenticated_user, client, second_garage):
    authenticated_user.client.put(URL, json=DARK)

    body = client.get(f"/api/public/garages/{second_garage.id}").get_json()

    assert body["booking_background_color"] is None
    assert body["booking_text_color"] is None


def test_public_payload_emits_null_for_a_stored_pair_that_fails_validation(client, garage, session):
    # e.g. a manual DB edit - never pass it on to a customer's browser.
    garage.booking_background_color = "#ffffff"
    garage.booking_text_color = "#eeeeee"
    session.commit()

    body = client.get(f"/api/public/garages/{garage.id}").get_json()

    assert body["booking_background_color"] is None
    assert body["booking_text_color"] is None
