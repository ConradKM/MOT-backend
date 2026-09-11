"""API tests for GET /api/public/garages, GET /api/public/garages/<id>."""

import uuid


def test_unauthenticated_client_can_list_garages(client, garage, second_garage):
    resp = client.get("/api/public/garages/")

    assert resp.status_code == 200
    names = {g["name"] for g in resp.get_json()}
    assert {garage.name, second_garage.name} <= names


def test_list_only_exposes_public_fields(client, garage):
    resp = client.get("/api/public/garages/")

    body = next(g for g in resp.get_json() if g["id"] == str(garage.id))
    assert set(body.keys()) == {"id", "name", "slug", "logo_url", "appointment_types"}


def test_unauthenticated_client_can_fetch_one_garage(client, garage):
    resp = client.get(f"/api/public/garages/{garage.id}")

    assert resp.status_code == 200
    assert resp.get_json() == {
        "id": str(garage.id),
        "name": garage.name,
        "slug": garage.slug,
        "logo_url": None,
        # Same shape as GET /api/public/<slug> (see PublicGarageDetailSchema)
        # - the id-based booking entry point needs the type list too, to
        # drive the date/type/time step (item 3).
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
