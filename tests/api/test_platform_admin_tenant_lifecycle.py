"""Tenant lifecycle: archive, unarchive, permanent delete.

Mirrors the shape of the suspend/reactivate tests in
test_platform_admin_tenants.py - archive is suspend's long-lived, reversible
sibling; delete is the one irreversible step, gated on the tenant already
being SUSPENDED or ARCHIVED and on typing its exact name.
"""

from datetime import UTC, datetime

from app.models.garage import Garage
from app.models.platform.audit_log import (
    ACTION_TENANT_ARCHIVE,
    ACTION_TENANT_DELETE,
    ACTION_TENANT_UNARCHIVE,
    PlatformAuditLog,
)
from app.models.reminder import Reminder
from tests.conftest import DEFAULT_PASSWORD


def test_archiving_a_tenant_records_the_reason(platform_client, garage):
    response = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/archive",
        json={"reason": "Owner closed the garage; confirmed by phone."},
    )

    assert response.status_code == 200
    assert response.json["garage"]["status"] == "ARCHIVED"
    assert response.json["garage"]["archive_reason"].startswith("Owner closed")

    entry = PlatformAuditLog.query.filter_by(action=ACTION_TENANT_ARCHIVE).one()
    assert "Owner closed" in entry.details["reason"]


def test_archive_requires_a_reason(platform_client, garage):
    response = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/archive", json={"reason": "   "}
    )

    assert response.status_code == 422
    assert garage.status == "ACTIVE"


def test_an_archived_tenants_staff_cannot_log_in(platform_client, client, garage, user):
    platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/archive", json={"reason": "Churned."}
    )

    response = client.post(
        "/api/auth/login", json={"email": user.email, "password": DEFAULT_PASSWORD}
    )

    assert response.status_code == 403


def test_archiving_kills_a_live_staff_token(platform_client, authenticated_client, garage):
    assert authenticated_client.get("/api/garage").status_code == 200

    platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/archive", json={"reason": "Churned."}
    )

    assert authenticated_client.get("/api/garage").status_code == 401


def test_archiving_does_not_affect_another_tenant(
    platform_client, second_authenticated_client, garage, second_garage
):
    platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/archive", json={"reason": "Churned."}
    )

    assert second_authenticated_client.get("/api/garage").status_code == 200


def test_archiving_deletes_nothing(platform_client, garage, customer, vehicle, session):
    platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/archive", json={"reason": "Churned."}
    )

    session.expire_all()
    assert session.get(Garage, garage.id) is not None
    detail = platform_client.get(f"/api/platform-admin/tenants/{garage.id}").json
    assert detail["customer_count"] == 1
    assert detail["vehicle_count"] == 1


def test_archiving_twice_is_rejected(platform_client, garage):
    platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/archive", json={"reason": "Churned."}
    )
    again = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/archive", json={"reason": "Churned."}
    )

    assert again.status_code == 422


def test_support_admin_cannot_archive(support_client, garage):
    resp = support_client.post(
        f"/api/platform-admin/tenants/{garage.id}/archive", json={"reason": "Churned."}
    )
    assert resp.status_code == 403
    assert garage.status == "ACTIVE"


def test_unarchiving_restores_access_and_clears_the_reason(platform_client, client, garage, user):
    platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/archive", json={"reason": "Churned."}
    )

    response = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/unarchive", json={"status": "ACTIVE"}
    )

    assert response.status_code == 200
    assert response.json["garage"]["status"] == "ACTIVE"
    assert response.json["garage"]["archive_reason"] is None
    assert PlatformAuditLog.query.filter_by(action=ACTION_TENANT_UNARCHIVE).count() == 1

    login = client.post("/api/auth/login", json={"email": user.email, "password": DEFAULT_PASSWORD})
    assert login.status_code == 200


def test_unarchiving_a_tenant_that_is_not_archived_is_rejected(platform_client, garage):
    response = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/unarchive", json={"status": "ACTIVE"}
    )

    assert response.status_code == 422


def test_support_admin_cannot_unarchive(platform_client, support_client, garage):
    platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/archive", json={"reason": "Churned."}
    )

    resp = support_client.post(
        f"/api/platform-admin/tenants/{garage.id}/unarchive", json={"status": "ACTIVE"}
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------
# Permanent delete
# --------------------------------------------------------------------------


def test_deleting_an_active_tenant_is_rejected(platform_client, garage, session):
    response = platform_client.delete(
        f"/api/platform-admin/tenants/{garage.id}", json={"confirm": garage.name}
    )

    assert response.status_code == 422
    session.expire_all()
    assert session.get(Garage, garage.id) is not None


def test_deleting_a_suspended_tenant_with_the_wrong_confirmation_is_rejected(
    platform_client, garage, session
):
    platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/suspend", json={"reason": "Non-payment."}
    )

    response = platform_client.delete(
        f"/api/platform-admin/tenants/{garage.id}", json={"confirm": "Not the right name"}
    )

    assert response.status_code == 422
    session.expire_all()
    assert session.get(Garage, garage.id) is not None


def test_deleting_a_suspended_tenant_permanently_removes_it(
    platform_client, garage, customer, vehicle, session
):
    garage_id = garage.id
    garage_name = garage.name

    platform_client.post(
        f"/api/platform-admin/tenants/{garage_id}/suspend", json={"reason": "Non-payment."}
    )

    response = platform_client.delete(
        f"/api/platform-admin/tenants/{garage_id}", json={"confirm": garage_name}
    )

    assert response.status_code == 200
    session.expire_all()
    assert session.get(Garage, garage_id) is None

    # Audit trail survives the delete: garage_id is nulled (the row it
    # pointed to is gone), but the snapshot fields are not.
    entry = PlatformAuditLog.query.filter_by(action=ACTION_TENANT_DELETE).one()
    assert entry.garage_id is None
    assert entry.garage_name == garage_name


def test_deleting_an_archived_tenant_with_an_outstanding_reminder_does_not_fk_error(
    platform_client, garage, customer, vehicle, session
):
    """Regression: reminders.garage_id was the one foreign key into garages
    still ON DELETE NO ACTION (every sibling table is CASCADE) - a tenant
    with any Reminder row would have failed to delete with an
    IntegrityError."""
    garage_id = garage.id
    garage_name = garage.name

    reminder = Reminder(
        garage_id=garage_id,
        customer_id=customer.id,
        vehicle_id=vehicle.id,
        type="MOT_EXPIRY",
        channel="EMAIL",
        scheduled_at=datetime.now(UTC),
        status="PENDING",
    )
    session.add(reminder)
    session.commit()
    # Captured before anything expires: the DB's own ON DELETE CASCADE (not
    # this session's ORM delete tracking) is what removes this row, so
    # accessing reminder.id afterwards - once its row is truly gone - raises
    # ObjectDeletedError rather than returning the id.
    reminder_id = reminder.id

    platform_client.post(
        f"/api/platform-admin/tenants/{garage_id}/archive", json={"reason": "Churned."}
    )

    response = platform_client.delete(
        f"/api/platform-admin/tenants/{garage_id}", json={"confirm": garage_name}
    )

    assert response.status_code == 200
    session.expire_all()
    assert session.get(Garage, garage_id) is None
    assert Reminder.query.filter_by(id=reminder_id).first() is None


def test_deleting_a_tenant_does_not_affect_another_tenant(
    platform_client, second_authenticated_client, garage, second_garage
):
    platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/suspend", json={"reason": "Non-payment."}
    )
    platform_client.delete(
        f"/api/platform-admin/tenants/{garage.id}", json={"confirm": garage.name}
    )

    assert second_authenticated_client.get("/api/garage").status_code == 200


def test_support_admin_cannot_delete(support_client, platform_client, garage):
    platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/suspend", json={"reason": "Non-payment."}
    )

    resp = support_client.delete(
        f"/api/platform-admin/tenants/{garage.id}", json={"confirm": garage.name}
    )
    assert resp.status_code == 403
