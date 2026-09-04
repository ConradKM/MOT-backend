"""API tests for GET /api/garage (read-only business details) and the
platform-side update path (app/garages/details.py)."""

from app.garages.details import GarageNotFoundError, resolve_garage, update_garage_details


def test_authenticated_user_can_retrieve_their_garage(authenticated_user):
    resp = authenticated_user.client.get("/api/garage")

    assert resp.status_code == 200
    assert resp.get_json()["id"] == str(authenticated_user.garage.id)
    assert resp.get_json()["name"] == authenticated_user.garage.name


def test_garage_payload_includes_business_detail_fields(authenticated_user):
    body = authenticated_user.client.get("/api/garage").get_json()

    for field in ("email", "phone", "address", "postcode", "website", "slug"):
        assert field in body


def test_unauthenticated_user_receives_auth_error(client):
    resp = client.get("/api/garage")
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# Garage details are read-only for garage users
# --------------------------------------------------------------------------


def test_owner_cannot_patch_garage_details(authenticated_user):
    resp = authenticated_user.client.patch(
        "/api/garage", json={"name": "Renamed Garage", "phone": "+44 20 0000 0000"}
    )

    assert resp.status_code == 403


def test_patch_garage_does_not_persist_anything(authenticated_user, session):
    authenticated_user.client.patch("/api/garage", json={"name": "Should Not Stick"})

    session.refresh(authenticated_user.garage)
    assert authenticated_user.garage.name != "Should Not Stick"


def test_staff_role_also_gets_403_on_patch(authenticated_user, session):
    authenticated_user.user.roles = []
    session.commit()

    resp = authenticated_user.client.patch("/api/garage", json={"name": "Staff Update"})

    assert resp.status_code == 403


# --------------------------------------------------------------------------
# Platform-side update (developer CLI path)
# --------------------------------------------------------------------------


def test_update_garage_details_changes_one_tenant(garage, second_garage, session):
    update_garage_details(
        garage,
        name="Kingsway Renamed",
        phone="+44 20 1111 2222",
        postcode="SW1A 1AA",
        website="https://kingsway.example",
    )

    session.refresh(garage)
    session.refresh(second_garage)
    assert garage.name == "Kingsway Renamed"
    assert garage.postcode == "SW1A 1AA"
    assert garage.website == "https://kingsway.example"
    # Other tenant untouched.
    assert second_garage.name == "Garage B"
    assert second_garage.postcode is None


def test_update_garage_details_never_touches_the_slug(garage, session):
    original_slug = garage.slug
    update_garage_details(garage, name="A Totally Different Trading Name")

    session.refresh(garage)
    assert garage.slug == original_slug


def test_update_garage_details_rejects_unknown_fields(garage):
    import pytest

    with pytest.raises(ValueError):
        update_garage_details(garage, slug="hand-picked")


def test_resolve_garage_by_slug_and_by_id(garage):
    assert resolve_garage(garage.slug).id == garage.id
    assert resolve_garage(str(garage.id)).id == garage.id


def test_resolve_garage_unknown_raises(app):
    import pytest

    with pytest.raises(GarageNotFoundError):
        resolve_garage("no-such-garage")


def test_cli_update_garage_details_updates_only_the_named_garage(
    app, garage, second_garage, session
):
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=[
            "update-garage-details",
            "--garage", garage.slug,
            "--phone", "+44 20 9999 0000",
        ]
    )

    assert result.exit_code == 0, result.output
    session.refresh(garage)
    session.refresh(second_garage)
    assert garage.phone == "+44 20 9999 0000"
    assert second_garage.phone == "+44 20 7946 0002"


def test_user_from_garage_a_only_ever_sees_their_own_garage(
    authenticated_user, second_authenticated_client, second_garage
):
    resp_a = authenticated_user.client.get("/api/garage")
    resp_b = second_authenticated_client.get("/api/garage")

    assert resp_a.get_json()["id"] == str(authenticated_user.garage.id)
    assert resp_b.get_json()["id"] == str(second_garage.id)
    assert resp_a.get_json()["id"] != resp_b.get_json()["id"]
