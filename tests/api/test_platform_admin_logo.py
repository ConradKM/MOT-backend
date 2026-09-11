"""API tests for business-logo persistence.

GET    /api/platform-admin/tenants/<id>/logo
POST   /api/platform-admin/tenants/<id>/logo
POST   /api/platform-admin/tenants/<id>/logo/finalize
DELETE /api/platform-admin/tenants/<id>/logo

The app only issues presigned URLs; the test suite runs with STORAGE_BACKEND
"none" (app/storage/memory.py), so "uploading" is
``get_storage().mark_uploaded(key, data)`` with real magic bytes when a test
needs finalize's content sniff to actually succeed or fail.
"""

import uuid

from app.models.platform.audit_log import PlatformAuditLog
from app.storage import get_storage

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP_BYTES = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8
NOT_AN_IMAGE_BYTES = b"MZ" + b"\x00" * 16  # a Windows executable's header


def _upload_and_finalize(platform_client, garage, *, content_type="image/png", data=PNG_BYTES):
    ticket = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/logo",
        json={"content_type": content_type, "size_bytes": len(data)},
    ).json
    get_storage().mark_uploaded(ticket["storage_key"], data)
    resp = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/logo/finalize",
        json={"storage_key": ticket["storage_key"], "original_filename": "logo.png"},
    )
    return ticket, resp


# --------------------------------------------------------------------------
# Requesting an upload ticket
# --------------------------------------------------------------------------


def test_request_upload_ticket(platform_client, garage):
    resp = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/logo",
        json={"content_type": "image/png", "size_bytes": 1024},
    )

    assert resp.status_code == 201
    body = resp.json
    assert body["upload_url"].startswith("https://")
    assert body["storage_key"].startswith(f"garages/{garage.id}/branding/")
    assert body["expires_in"] > 0


def test_ticket_does_not_change_the_business_yet(platform_client, garage):
    platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/logo",
        json={"content_type": "image/png"},
    )

    logo = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/logo").json["logo"]
    assert logo is None


def test_rejects_an_unsupported_declared_type(platform_client, garage):
    resp = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/logo",
        json={"content_type": "image/gif"},
    )
    assert resp.status_code == 422


def test_rejects_an_oversized_declared_file(platform_client, garage, app):
    max_bytes = app.config["LOGO_MAX_BYTES"]
    resp = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/logo",
        json={"content_type": "image/png", "size_bytes": max_bytes + 1},
    )
    assert resp.status_code == 422


def test_unknown_tenant_is_404(platform_client):
    resp = platform_client.post(
        f"/api/platform-admin/tenants/{uuid.uuid4()}/logo",
        json={"content_type": "image/png"},
    )
    assert resp.status_code == 404


def test_support_admin_cannot_request_an_upload(support_client, garage):
    resp = support_client.post(
        f"/api/platform-admin/tenants/{garage.id}/logo",
        json={"content_type": "image/png"},
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------
# Finalize
# --------------------------------------------------------------------------


def test_finalize_persists_png(platform_client, garage, session):
    _, resp = _upload_and_finalize(platform_client, garage, content_type="image/png", data=PNG_BYTES)

    assert resp.status_code == 200
    assert resp.json["logo"]["content_type"] == "image/png"
    assert resp.json["logo"]["original_filename"] == "logo.png"
    assert resp.json["logo"]["url"].startswith("https://")
    session.refresh(garage)
    assert garage.logo_storage_key is not None
    assert garage.logo_uploaded_at is not None


def test_finalize_persists_jpeg(platform_client, garage):
    _, resp = _upload_and_finalize(
        platform_client, garage, content_type="image/jpeg", data=JPEG_BYTES
    )
    assert resp.status_code == 200
    assert resp.json["logo"]["content_type"] == "image/jpeg"


def test_finalize_persists_webp(platform_client, garage):
    _, resp = _upload_and_finalize(
        platform_client, garage, content_type="image/webp", data=WEBP_BYTES
    )
    assert resp.status_code == 200
    assert resp.json["logo"]["content_type"] == "image/webp"


def test_finalize_sniffs_real_content_regardless_of_declared_type(platform_client, garage):
    """A file declared as image/png whose actual bytes are a JPEG still
    finalizes - the *real* content decides, never the declared one."""
    _, resp = _upload_and_finalize(
        platform_client, garage, content_type="image/png", data=JPEG_BYTES
    )
    assert resp.status_code == 200
    assert resp.json["logo"]["content_type"] == "image/jpeg"


def test_finalize_rejects_non_image_content(platform_client, garage, session):
    _, resp = _upload_and_finalize(
        platform_client, garage, content_type="image/png", data=NOT_AN_IMAGE_BYTES
    )
    assert resp.status_code == 422
    session.refresh(garage)
    assert garage.logo_storage_key is None


def test_finalize_before_upload_is_409(platform_client, garage):
    ticket = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/logo",
        json={"content_type": "image/png"},
    ).json

    resp = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/logo/finalize",
        json={"storage_key": ticket["storage_key"]},
    )
    assert resp.status_code == 409


def test_finalize_rejects_a_key_from_another_business(
    platform_client, garage, second_garage
):
    ticket = platform_client.post(
        f"/api/platform-admin/tenants/{second_garage.id}/logo",
        json={"content_type": "image/png"},
    ).json
    get_storage().mark_uploaded(ticket["storage_key"], PNG_BYTES)

    resp = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/logo/finalize",
        json={"storage_key": ticket["storage_key"]},
    )
    assert resp.status_code == 422


def test_finalize_is_audited(platform_client, garage, platform_admin):
    _upload_and_finalize(platform_client, garage)

    entry = (
        PlatformAuditLog.query.filter_by(action="tenant.logo.upload", garage_id=garage.id)
        .order_by(PlatformAuditLog.created_at.desc())
        .first()
    )
    assert entry is not None
    assert entry.admin_id == platform_admin.id


# --------------------------------------------------------------------------
# Replace
# --------------------------------------------------------------------------


def test_replace_deletes_the_previous_object(platform_client, garage):
    first_ticket, _ = _upload_and_finalize(
        platform_client, garage, content_type="image/png", data=PNG_BYTES
    )

    _upload_and_finalize(platform_client, garage, content_type="image/jpeg", data=JPEG_BYTES)

    assert not get_storage().object_exists(first_ticket["storage_key"])
    logo = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/logo").json["logo"]
    assert logo["content_type"] == "image/jpeg"


# --------------------------------------------------------------------------
# Delete
# --------------------------------------------------------------------------


def test_delete_removes_the_logo(platform_client, garage, session):
    ticket, _ = _upload_and_finalize(platform_client, garage)

    resp = platform_client.delete(f"/api/platform-admin/tenants/{garage.id}/logo")

    assert resp.status_code == 200
    session.refresh(garage)
    assert garage.logo_storage_key is None
    assert not get_storage().object_exists(ticket["storage_key"])


def test_delete_with_no_logo_is_still_200(platform_client, garage):
    resp = platform_client.delete(f"/api/platform-admin/tenants/{garage.id}/logo")
    assert resp.status_code == 200


def test_support_admin_cannot_delete(support_client, platform_client, garage):
    _upload_and_finalize(platform_client, garage)
    resp = support_client.delete(f"/api/platform-admin/tenants/{garage.id}/logo")
    assert resp.status_code == 403


# --------------------------------------------------------------------------
# Tenant list / detail surface
# --------------------------------------------------------------------------


def test_tenant_detail_reports_has_logo(platform_client, garage):
    before = platform_client.get(f"/api/platform-admin/tenants/{garage.id}").json
    assert before["garage"]["has_logo"] is False

    _upload_and_finalize(platform_client, garage)

    after = platform_client.get(f"/api/platform-admin/tenants/{garage.id}").json
    assert after["garage"]["has_logo"] is True
