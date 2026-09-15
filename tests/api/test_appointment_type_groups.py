"""API tests for service groups.

/api/appointment-type-groups/ and the group-related additions to
/api/appointment-types/ (group assignment, explicit ordering, images).
"""

import uuid

from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.appointments.appointment_type_group import AppointmentTypeGroup


def _make_group(session, garage, name, order=0, display_mode=None):
    g = AppointmentTypeGroup(garage_id=garage.id, name=name, order=order, display_mode=display_mode)
    session.add(g)
    session.commit()
    return g


def _make_type(session, garage, name, group=None, order=0, status="ACTIVE"):
    t = GarageAppointmentType(
        garage_id=garage.id,
        name=name,
        status=status,
        default_duration_minutes=60,
        group_id=None if group is None else group.id,
        order=order,
    )
    session.add(t)
    session.commit()
    return t


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------


def test_create_and_list_groups(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/appointment-type-groups/",
        json={"name": "Colour", "description": "Tints and balayage", "display_mode": "GRID"},
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["name"] == "Colour"
    assert body["display_mode"] == "GRID"
    assert body["image_url"] is None

    listed = authenticated_user.client.get("/api/appointment-type-groups/").get_json()
    assert [g["name"] for g in listed] == ["Colour"]


def test_display_mode_defaults_to_null_meaning_inherit(authenticated_user):
    """A group with no opinion must store NULL, not a copy of the business
    default - otherwise changing the business default later would leave it
    behind."""
    body = authenticated_user.client.post(
        "/api/appointment-type-groups/", json={"name": "Repairs"}
    ).get_json()

    assert body["display_mode"] is None


def test_groups_are_listed_in_display_order_not_alphabetically(authenticated_user, session):
    garage = authenticated_user.garage
    _make_group(session, garage, "Zebra", order=0)
    _make_group(session, garage, "Alpha", order=1)

    listed = authenticated_user.client.get("/api/appointment-type-groups/").get_json()

    assert [g["name"] for g in listed] == ["Zebra", "Alpha"]


def test_rejects_an_unknown_display_mode(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/appointment-type-groups/", json={"name": "Colour", "display_mode": "CAROUSEL"}
    )

    assert resp.status_code == 422
    assert "display_mode" in resp.get_json()["errors"]["json"]


def test_staff_cannot_create_a_group(authenticated_user, session):
    authenticated_user.user.roles = []
    session.commit()

    resp = authenticated_user.client.post("/api/appointment-type-groups/", json={"name": "Colour"})

    assert resp.status_code == 403


# --------------------------------------------------------------------------
# Tenant isolation
# --------------------------------------------------------------------------


def test_garage_a_only_sees_its_own_groups(authenticated_user, session, second_garage):
    _make_group(session, authenticated_user.garage, "Mine")
    _make_group(session, second_garage, "Theirs")

    listed = authenticated_user.client.get("/api/appointment-type-groups/").get_json()

    assert [g["name"] for g in listed] == ["Mine"]


def test_cannot_assign_a_service_to_another_businesss_group(
    authenticated_user, session, second_garage
):
    """The FK alone would accept this and leak the other tenant's group name
    onto this business's booking page."""
    foreign = _make_group(session, second_garage, "Theirs")

    resp = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT", "group_id": str(foreign.id)}
    )

    assert resp.status_code == 422
    assert "not a group for this business" in resp.get_json()["message"]


def test_cannot_patch_a_service_onto_another_businesss_group(
    authenticated_user, session, second_garage, appointment_type
):
    foreign = _make_group(session, second_garage, "Theirs")

    resp = authenticated_user.client.patch(
        f"/api/appointment-types/{appointment_type.id}",
        json={"group_id": str(foreign.id)},
    )

    assert resp.status_code == 422


def test_unknown_group_id_is_422_not_500(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT", "group_id": str(uuid.uuid4())}
    )

    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Deleting a group
# --------------------------------------------------------------------------


def test_deleting_a_group_ungroups_its_services_rather_than_deleting_them(
    authenticated_user, session
):
    """The whole point of SET NULL: a business tidying up its navigation must
    never lose services (and their booking history) as a side effect."""
    garage = authenticated_user.garage
    group = _make_group(session, garage, "Colour")
    service = _make_type(session, garage, "Balayage", group=group)

    resp = authenticated_user.client.delete(f"/api/appointment-type-groups/{group.id}")
    assert resp.status_code == 204

    session.expire_all()
    survivor = session.get(GarageAppointmentType, service.id)
    assert survivor is not None
    assert survivor.group_id is None


def test_deleting_a_group_from_another_business_is_404(authenticated_user, session, second_garage):
    foreign = _make_group(session, second_garage, "Theirs")

    resp = authenticated_user.client.delete(f"/api/appointment-type-groups/{foreign.id}")

    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Reordering
# --------------------------------------------------------------------------


def test_reorder_groups_applies_the_whole_list(authenticated_user, session):
    garage = authenticated_user.garage
    a = _make_group(session, garage, "A", order=0)
    b = _make_group(session, garage, "B", order=1)
    c = _make_group(session, garage, "C", order=2)

    resp = authenticated_user.client.put(
        "/api/appointment-type-groups/order",
        json={"ids": [str(c.id), str(a.id), str(b.id)]},
    )

    assert resp.status_code == 200
    assert [g["name"] for g in resp.get_json()] == ["C", "A", "B"]


def test_reorder_rejects_a_partial_list(authenticated_user, session):
    """A partial list would leave the omitted groups at stale positions,
    colliding with the new ones."""
    garage = authenticated_user.garage
    a = _make_group(session, garage, "A", order=0)
    _make_group(session, garage, "B", order=1)

    resp = authenticated_user.client.put(
        "/api/appointment-type-groups/order", json={"ids": [str(a.id)]}
    )

    assert resp.status_code == 422


def test_reorder_rejects_another_businesss_group(authenticated_user, session, second_garage):
    garage = authenticated_user.garage
    a = _make_group(session, garage, "A")
    foreign = _make_group(session, second_garage, "Theirs")

    resp = authenticated_user.client.put(
        "/api/appointment-type-groups/order",
        json={"ids": [str(a.id), str(foreign.id)]},
    )

    assert resp.status_code == 422


def test_reorder_services_within_a_group(authenticated_user, session):
    garage = authenticated_user.garage
    group = _make_group(session, garage, "Colour")
    first = _make_type(session, garage, "Balayage", group=group, order=0)
    second = _make_type(session, garage, "Full head", group=group, order=1)

    resp = authenticated_user.client.put(
        f"/api/appointment-type-groups/{group.id}/services/order",
        json={"ids": [str(second.id), str(first.id)]},
    )

    assert resp.status_code == 204
    session.expire_all()
    assert session.get(GarageAppointmentType, second.id).order == 0
    assert session.get(GarageAppointmentType, first.id).order == 1


# --------------------------------------------------------------------------
# Service listing order
# --------------------------------------------------------------------------


def test_services_list_in_display_order_with_name_as_tie_break(authenticated_user, session):
    """A business that has never reordered anything keeps the alphabetical
    listing this endpoint has always returned."""
    garage = authenticated_user.garage
    _make_type(session, garage, "Zebra", order=0)
    _make_type(session, garage, "Alpha", order=0)
    _make_type(session, garage, "Middle", order=-0)

    listed = authenticated_user.client.get("/api/appointment-types/").get_json()

    assert [t["name"] for t in listed] == ["Alpha", "Middle", "Zebra"]


def test_explicit_order_beats_the_alphabetical_tie_break(authenticated_user, session):
    garage = authenticated_user.garage
    _make_type(session, garage, "Zebra", order=0)
    _make_type(session, garage, "Alpha", order=1)

    listed = authenticated_user.client.get("/api/appointment-types/").get_json()

    assert [t["name"] for t in listed] == ["Zebra", "Alpha"]


# --------------------------------------------------------------------------
# Public payload
# --------------------------------------------------------------------------


def test_public_payload_exposes_groups_and_membership(client, session, garage):
    group = _make_group(session, garage, "Colour", display_mode="GRID")
    _make_type(session, garage, "Balayage", group=group)
    _make_type(session, garage, "Consultation")

    body = client.get(f"/api/public/{garage.slug}").get_json()

    assert body["booking_display_mode"] == "LIST"
    assert [g["name"] for g in body["appointment_type_groups"]] == ["Colour"]
    by_name = {t["name"]: t for t in body["appointment_types"]}
    assert by_name["Balayage"]["group_id"] == str(group.id)
    assert by_name["Consultation"]["group_id"] is None


def test_public_payload_keeps_every_active_service_in_the_flat_list(client, session, garage):
    """Grouped services must stay in `appointment_types` too - a client that
    knows nothing about groups still has to see the whole menu."""
    group = _make_group(session, garage, "Colour")
    _make_type(session, garage, "Balayage", group=group)

    body = client.get(f"/api/public/{garage.slug}").get_json()

    assert [t["name"] for t in body["appointment_types"]] == ["Balayage"]


def test_public_payload_resolves_a_groups_inherited_display_mode(client, session, garage):
    """NULL means inherit, and the resolution happens server-side so no client
    reimplements the rule."""
    garage.booking_display_mode = "GRID"
    group = _make_group(session, garage, "Colour", display_mode=None)
    _make_type(session, garage, "Balayage", group=group)
    session.commit()

    body = client.get(f"/api/public/{garage.slug}").get_json()

    assert body["appointment_type_groups"][0]["display_mode"] == "GRID"


def test_public_payload_hides_a_group_with_nothing_active_in_it(client, session, garage):
    """An empty group is a configuration leftover; rendering it would give the
    customer a heading that leads nowhere."""
    group = _make_group(session, garage, "Retired")
    _make_type(session, garage, "Old service", group=group, status="HIDDEN")

    body = client.get(f"/api/public/{garage.slug}").get_json()

    assert body["appointment_type_groups"] == []


def test_public_services_come_back_in_display_order(client, session, garage):
    _make_type(session, garage, "Zebra", order=0)
    _make_type(session, garage, "Alpha", order=1)

    body = client.get(f"/api/public/{garage.slug}").get_json()

    assert [t["name"] for t in body["appointment_types"]] == ["Zebra", "Alpha"]


# --------------------------------------------------------------------------
# Images
#
# The suite runs with STORAGE_BACKEND "none" (app/storage/memory.py), so
# "uploading" is get_storage().mark_uploaded(key, data) with real magic bytes
# where finalize's content sniff has to genuinely succeed or fail.
# --------------------------------------------------------------------------

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
NOT_AN_IMAGE_BYTES = b"MZ" + b"\x00" * 16  # a Windows executable's header


def _upload(client, url, data=PNG_BYTES, content_type="image/png"):
    from app.storage import get_storage

    ticket = client.post(
        url, json={"content_type": content_type, "size_bytes": len(data)}
    ).get_json()
    get_storage().mark_uploaded(ticket["storage_key"], data)
    return ticket, client.put(url, json={"storage_key": ticket["storage_key"]})


def test_group_image_upload_and_finalize(authenticated_user, session):
    group = _make_group(session, authenticated_user.garage, "Colour")
    url = f"/api/appointment-type-groups/{group.id}/image"

    ticket, resp = _upload(authenticated_user.client, url)

    assert ticket["storage_key"].startswith(
        f"garages/{authenticated_user.garage.id}/service-images/"
    )
    assert resp.status_code == 200
    assert resp.get_json()["image_url"] is not None
    assert resp.get_json()["image_content_type"] == "image/png"


def test_a_ticket_alone_does_not_give_the_group_an_image(authenticated_user, session):
    """A client that takes a ticket and never uploads must not leave the group
    claiming an image it doesn't have."""
    group = _make_group(session, authenticated_user.garage, "Colour")

    authenticated_user.client.post(
        f"/api/appointment-type-groups/{group.id}/image", json={"content_type": "image/png"}
    )

    body = authenticated_user.client.get(f"/api/appointment-type-groups/{group.id}").get_json()
    assert body["image_url"] is None


def test_finalize_rejects_content_that_is_not_really_an_image(authenticated_user, session):
    """The declared content type is only a claim - a presigned PUT goes
    straight to the bucket, so only the bytes decide."""
    group = _make_group(session, authenticated_user.garage, "Colour")
    url = f"/api/appointment-type-groups/{group.id}/image"

    _, resp = _upload(authenticated_user.client, url, data=NOT_AN_IMAGE_BYTES)

    assert resp.status_code == 422


def test_finalize_before_upload_is_409(authenticated_user, session):
    group = _make_group(session, authenticated_user.garage, "Colour")
    url = f"/api/appointment-type-groups/{group.id}/image"

    ticket = authenticated_user.client.post(url, json={"content_type": "image/png"}).get_json()
    resp = authenticated_user.client.put(url, json={"storage_key": ticket["storage_key"]})

    assert resp.status_code == 409


def test_finalize_rejects_a_key_from_another_business(
    authenticated_user, session, second_garage, second_authenticated_client
):
    from app.storage import get_storage

    foreign = _make_group(session, second_garage, "Theirs")
    mine = _make_group(session, authenticated_user.garage, "Mine")

    foreign_ticket = second_authenticated_client.post(
        f"/api/appointment-type-groups/{foreign.id}/image", json={"content_type": "image/png"}
    ).get_json()
    get_storage().mark_uploaded(foreign_ticket["storage_key"], PNG_BYTES)

    resp = authenticated_user.client.put(
        f"/api/appointment-type-groups/{mine.id}/image",
        json={"storage_key": foreign_ticket["storage_key"]},
    )

    assert resp.status_code == 422


def test_rejects_an_unsupported_declared_type(authenticated_user, session):
    group = _make_group(session, authenticated_user.garage, "Colour")

    resp = authenticated_user.client.post(
        f"/api/appointment-type-groups/{group.id}/image",
        json={"content_type": "image/gif"},
    )

    assert resp.status_code == 422


def test_deleting_a_group_image_clears_it(authenticated_user, session):
    group = _make_group(session, authenticated_user.garage, "Colour")
    url = f"/api/appointment-type-groups/{group.id}/image"
    _upload(authenticated_user.client, url)

    resp = authenticated_user.client.delete(url)

    assert resp.status_code == 200
    assert resp.get_json()["image_url"] is None


def test_service_image_upload_and_finalize(authenticated_user, appointment_type):
    url = f"/api/appointment-types/{appointment_type.id}/image"

    _, resp = _upload(authenticated_user.client, url)

    assert resp.status_code == 200
    assert resp.get_json()["image_url"] is not None


def test_service_image_appears_on_the_public_payload(authenticated_user, appointment_type, client):
    _upload(authenticated_user.client, f"/api/appointment-types/{appointment_type.id}/image")

    body = client.get(f"/api/public/{authenticated_user.garage.slug}").get_json()

    assert body["appointment_types"][0]["image_url"] is not None
