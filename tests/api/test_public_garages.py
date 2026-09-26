"""API tests for GET /api/public/garages, GET /api/public/garages/<id>."""

import uuid

import pytest

from app.models.garage import GARAGE_STATUS_ARCHIVED, GARAGE_STATUS_SUSPENDED


def test_unauthenticated_client_can_list_garages(client, garage, second_garage):
    resp = client.get("/api/public/garages/")

    assert resp.status_code == 200
    names = {g["name"] for g in resp.get_json()}
    assert {garage.name, second_garage.name} <= names


def test_list_only_exposes_public_fields(client, garage):
    resp = client.get("/api/public/garages/")

    body = next(g for g in resp.get_json() if g["id"] == str(garage.id))
    assert set(body.keys()) == {
        "id",
        "name",
        "slug",
        "logo_url",
        "booking_display_mode",
        "appointment_type_groups",
        "appointment_types",
    }


def test_unauthenticated_client_can_fetch_one_garage(client, garage):
    resp = client.get(f"/api/public/garages/{garage.id}")

    assert resp.status_code == 200
    assert resp.get_json() == {
        "id": str(garage.id),
        "name": garage.name,
        "slug": garage.slug,
        "logo_url": None,
        "booking_display_mode": "LIST",
        "appointment_type_groups": [],
        # Same shape as GET /api/public/<slug> - this is the /book/:garageId
        # entry point, so it is the *same page*, not a second view of it.
        # Both build it through app/public_booking/payload.py.
        "appointment_types": [],
    }


def test_fetch_one_garage_includes_the_persisted_logo_url(client, garage, session):
    """Regression: this lookup (the /book/:garageId entry point's initial
    load) had been silently missing logo_url - only GET /api/public/<slug>
    ever got it (PublicGarageDetailSchema), because PublicGarageSchema was
    never updated to match when the logo work landed. The booking wizard
    fetches by id first, so the public booking page showed the business's
    name but no logo until this field existed here too."""
    from datetime import UTC, datetime

    garage.logo_storage_key = "garages/g1/branding/logo.png"
    garage.logo_content_type = "image/png"
    garage.logo_uploaded_at = datetime.now(UTC)
    session.commit()

    resp = client.get(f"/api/public/garages/{garage.id}")

    assert resp.get_json()["logo_url"] is not None
    assert resp.get_json()["logo_url"].startswith("https://")


def test_fetch_one_garage_includes_only_active_appointment_types(client, garage, session):
    from app.models.appointments.appointment_type import GarageAppointmentType

    active = GarageAppointmentType(garage_id=garage.id, name="Haircut", status="ACTIVE")
    hidden = GarageAppointmentType(garage_id=garage.id, name="Retired Type", status="HIDDEN")
    session.add_all([active, hidden])
    session.commit()

    resp = client.get(f"/api/public/garages/{garage.id}")

    names = {t["name"] for t in resp.get_json()["appointment_types"]}
    assert names == {"Haircut"}


def test_fetching_unknown_garage_id_returns_404(client):
    resp = client.get(f"/api/public/garages/{uuid.uuid4()}")

    assert resp.status_code == 404


@pytest.mark.parametrize("status", [GARAGE_STATUS_SUSPENDED, GARAGE_STATUS_ARCHIVED])
def test_offline_garage_is_not_exposed_by_public_id_routes(client, session, garage, status):
    """Lifecycle state must guard every public booking entry point, not just
    the slug route used by the normal booking wizard."""
    garage.status = status
    session.commit()

    listed = client.get("/api/public/garages/")
    by_id = client.get(f"/api/public/garages/{garage.id}")

    assert listed.status_code == 200
    assert str(garage.id) not in {item["id"] for item in listed.get_json()}
    assert by_id.status_code == 404


def test_the_two_public_lookups_return_the_same_payload(client, session, garage):
    """The by-id and by-slug lookups feed the same booking page.

    They are separate endpoints with separate history, and the by-id one
    silently fell behind when groups were added - the page crashed on a
    missing key, and no test caught it because both are mocked in the
    frontend suite. They share a builder now; this is what keeps them shared.
    """
    from app.models.appointments.appointment_type import GarageAppointmentType
    from app.models.appointments.appointment_type_group import AppointmentTypeGroup

    group = AppointmentTypeGroup(garage_id=garage.id, name="Colour", display_mode="GRID")
    session.add(group)
    session.commit()
    session.add(
        GarageAppointmentType(
            garage_id=garage.id, name="Balayage", status="ACTIVE", group_id=group.id
        )
    )
    session.commit()

    by_id = client.get(f"/api/public/garages/{garage.id}").get_json()
    by_slug = client.get(f"/api/public/{garage.slug}").get_json()

    assert by_id == by_slug
