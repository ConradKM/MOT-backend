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
    assert set(body.keys()) == {"id", "name", "slug"}


def test_unauthenticated_client_can_fetch_one_garage(client, garage):
    resp = client.get(f"/api/public/garages/{garage.id}")

    assert resp.status_code == 200
    assert resp.get_json() == {
        "id": str(garage.id),
        "name": garage.name,
        "slug": garage.slug,
    }


def test_fetching_unknown_garage_id_returns_404(client):
    resp = client.get(f"/api/public/garages/{uuid.uuid4()}")

    assert resp.status_code == 404
