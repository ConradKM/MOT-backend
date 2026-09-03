"""API tests for checklist-item photo/video evidence.

The app only issues presigned URLs; the test suite runs with STORAGE_BACKEND
"none" (app/storage/memory.py), so "uploading" is `storage.mark_uploaded(key)`.
"""

from app.storage import get_storage


def _checklist_item(authenticated_user, customer, media_type="PHOTO"):
    """Snapshot a one-item checklist and return that item's dict."""
    appt_type = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT"}
    ).get_json()
    authenticated_user.client.post(
        f"/api/appointment-types/{appt_type['id']}/checklist-template"
    )
    authenticated_user.client.post(
        f"/api/appointment-types/{appt_type['id']}/checklist-template/items",
        json={"label": "Brakes", "media_type": media_type},
    )
    appointment = authenticated_user.client.post(
        "/api/appointments/",
        json={
            "employee_id": str(authenticated_user.user.id),
            "customer_id": str(customer.id),
            "start_time": "2026-09-15T09:00:00+00:00",
            "end_time": "2026-09-15T10:00:00+00:00",
            "appointment_type_id": appt_type["id"],
        },
    ).get_json()
    checklist = authenticated_user.client.post(
        f"/api/appointments/{appointment['id']}/checklist"
    ).get_json()
    return appointment, checklist["items"][0]


def _request_url(client, item_id, **overrides):
    payload = {"media_type": "PHOTO", "content_type": "image/jpeg"}
    payload.update(overrides)
    return client.post(
        f"/api/appointment-checklist-items/{item_id}/media", json=payload
    )


# --------------------------------------------------------------------------
# Requesting an upload URL
# --------------------------------------------------------------------------


def test_request_upload_url_creates_pending_media(authenticated_user, customer):
    _, item = _checklist_item(authenticated_user, customer)

    resp = _request_url(authenticated_user.client, item["id"])

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["upload_url"].startswith("https://")
    assert body["storage_key"].startswith(f"garages/{authenticated_user.garage.id}/")
    assert body["expires_in"] > 0

    # It's on the item but not yet in the item's uploaded-media list.
    media_list = authenticated_user.client.get(
        f"/api/appointment-checklist-items/{item['id']}/media"
    ).get_json()
    assert media_list == []


def test_request_rejects_media_type_the_item_does_not_accept(authenticated_user, customer):
    _, item = _checklist_item(authenticated_user, customer, media_type="PHOTO")

    resp = _request_url(
        authenticated_user.client, item["id"], media_type="VIDEO", content_type="video/mp4"
    )
    assert resp.status_code == 422


def test_either_item_accepts_photo_and_video(authenticated_user, customer):
    _, item = _checklist_item(authenticated_user, customer, media_type="EITHER")

    photo = _request_url(authenticated_user.client, item["id"])
    video = _request_url(
        authenticated_user.client, item["id"], media_type="VIDEO", content_type="video/mp4"
    )
    assert photo.status_code == 201
    assert video.status_code == 201


def test_request_rejects_unsupported_content_type(authenticated_user, customer):
    _, item = _checklist_item(authenticated_user, customer)

    resp = _request_url(authenticated_user.client, item["id"], content_type="image/gif")
    assert resp.status_code == 422


def test_request_rejects_oversized_file(authenticated_user, customer, app):
    _, item = _checklist_item(authenticated_user, customer)

    resp = _request_url(
        authenticated_user.client,
        item["id"],
        size_bytes=app.config["MEDIA_MAX_BYTES"] + 1,
    )
    assert resp.status_code == 422


def test_request_for_another_garages_item_is_404(
    authenticated_user, second_authenticated_client, customer
):
    _, item = _checklist_item(authenticated_user, customer)

    resp = second_authenticated_client.post(
        f"/api/appointment-checklist-items/{item['id']}/media",
        json={"media_type": "PHOTO", "content_type": "image/jpeg"},
    )
    assert resp.status_code == 404


def test_request_requires_authentication(client, authenticated_user, customer):
    _, item = _checklist_item(authenticated_user, customer)

    resp = client.post(
        f"/api/appointment-checklist-items/{item['id']}/media",
        json={"media_type": "PHOTO", "content_type": "image/jpeg"},
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# Finalize / fetch / delete
# --------------------------------------------------------------------------


def test_finalize_after_upload_marks_media_uploaded(authenticated_user, customer):
    _, item = _checklist_item(authenticated_user, customer)
    ticket = _request_url(authenticated_user.client, item["id"]).get_json()
    get_storage().mark_uploaded(ticket["storage_key"])

    resp = authenticated_user.client.post(
        f"/api/checklist-item-media/{ticket['id']}/finalize", json={"size_bytes": 2048}
    )

    assert resp.status_code == 200
    assert resp.get_json()["uploaded_at"] is not None

    media_list = authenticated_user.client.get(
        f"/api/appointment-checklist-items/{item['id']}/media"
    ).get_json()
    assert [m["id"] for m in media_list] == [ticket["id"]]


def test_finalize_without_an_uploaded_object_is_409(authenticated_user, customer):
    _, item = _checklist_item(authenticated_user, customer)
    ticket = _request_url(authenticated_user.client, item["id"]).get_json()

    resp = authenticated_user.client.post(
        f"/api/checklist-item-media/{ticket['id']}/finalize", json={}
    )
    assert resp.status_code == 409


def test_finalize_twice_is_409(authenticated_user, customer):
    _, item = _checklist_item(authenticated_user, customer)
    ticket = _request_url(authenticated_user.client, item["id"]).get_json()
    get_storage().mark_uploaded(ticket["storage_key"])
    authenticated_user.client.post(f"/api/checklist-item-media/{ticket['id']}/finalize", json={})

    resp = authenticated_user.client.post(
        f"/api/checklist-item-media/{ticket['id']}/finalize", json={}
    )
    assert resp.status_code == 409


def test_get_media_returns_a_download_url_after_finalize(authenticated_user, customer):
    _, item = _checklist_item(authenticated_user, customer)
    ticket = _request_url(authenticated_user.client, item["id"]).get_json()
    get_storage().mark_uploaded(ticket["storage_key"])
    authenticated_user.client.post(f"/api/checklist-item-media/{ticket['id']}/finalize", json={})

    resp = authenticated_user.client.get(f"/api/checklist-item-media/{ticket['id']}")

    assert resp.status_code == 200
    assert "method=GET" in resp.get_json()["download_url"]


def test_get_media_before_finalize_is_409(authenticated_user, customer):
    _, item = _checklist_item(authenticated_user, customer)
    ticket = _request_url(authenticated_user.client, item["id"]).get_json()

    resp = authenticated_user.client.get(f"/api/checklist-item-media/{ticket['id']}")
    assert resp.status_code == 409


def test_delete_media(authenticated_user, customer):
    _, item = _checklist_item(authenticated_user, customer)
    ticket = _request_url(authenticated_user.client, item["id"]).get_json()

    resp = authenticated_user.client.delete(f"/api/checklist-item-media/{ticket['id']}")
    assert resp.status_code == 204

    assert (
        authenticated_user.client.get(f"/api/checklist-item-media/{ticket['id']}").status_code
        == 404
    )


def test_media_is_garage_scoped(
    authenticated_user, second_authenticated_client, customer
):
    _, item = _checklist_item(authenticated_user, customer)
    ticket = _request_url(authenticated_user.client, item["id"]).get_json()

    assert (
        second_authenticated_client.get(
            f"/api/checklist-item-media/{ticket['id']}"
        ).status_code
        == 404
    )
    assert (
        second_authenticated_client.delete(
            f"/api/checklist-item-media/{ticket['id']}"
        ).status_code
        == 404
    )


def test_checklist_response_includes_uploaded_media(authenticated_user, customer):
    appointment, item = _checklist_item(authenticated_user, customer)
    ticket = _request_url(authenticated_user.client, item["id"]).get_json()
    get_storage().mark_uploaded(ticket["storage_key"])
    authenticated_user.client.post(f"/api/checklist-item-media/{ticket['id']}/finalize", json={})

    checklist = authenticated_user.client.get(
        f"/api/appointments/{appointment['id']}/checklist"
    ).get_json()

    media = checklist["items"][0]["media"]
    assert [m["id"] for m in media] == [ticket["id"]]
    assert media[0]["media_type"] == "PHOTO"
