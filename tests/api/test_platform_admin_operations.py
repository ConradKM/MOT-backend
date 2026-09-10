"""Operations: email delivery log, resends, communication failures, job health,
and the audit trail that records all of it.
"""

import datetime

import pytest

from app.models.communications.communication_log import CommunicationLog
from app.models.platform.audit_log import ACTION_EMAIL_RESEND, PlatformAuditLog


@pytest.fixture()
def failed_email(session, garage, customer):
    row = CommunicationLog(
        garage_id=garage.id,
        customer_id=customer.id,
        channel="EMAIL",
        direction="OUTBOUND",
        external_provider="resend",
        from_address="owner-a@garage-a.example",
        to_address="jane.doe@example.com",
        subject="Your appointment is confirmed",
        trigger_event="APPOINTMENT_CREATED",
        body="Your appointment is confirmed for Tuesday.",
        status="FAILED",
        error_message="Resend API returned 429",
    )
    session.add(row)
    session.commit()
    return row


@pytest.fixture()
def sent_email(session, garage):
    row = CommunicationLog(
        garage_id=garage.id,
        channel="EMAIL",
        direction="OUTBOUND",
        to_address="pat@example.com",
        subject="We've received your booking request",
        trigger_event="BOOKING_REQUEST_CREATED",
        body="Thanks - we'll be in touch.",
        status="SENT",
    )
    session.add(row)
    session.commit()
    return row


def test_email_log_lists_every_tenants_email(platform_client, failed_email, sent_email):
    response = platform_client.get("/api/platform-admin/operations/emails")

    assert response.status_code == 200
    assert response.json["total"] == 2
    subjects = {item["subject"] for item in response.json["items"]}
    assert "Your appointment is confirmed" in subjects


def test_email_log_shows_the_tenant_and_the_failure_reason(platform_client, failed_email, garage):
    item = platform_client.get("/api/platform-admin/operations/emails?status=failed").json["items"][
        0
    ]

    assert item["garage_name"] == garage.name
    assert item["error_message"] == "Resend API returned 429"
    assert item["to_address"] == "jane.doe@example.com"


def test_email_log_filters_by_status(platform_client, failed_email, sent_email):
    failed = platform_client.get("/api/platform-admin/operations/emails?status=failed")
    sent = platform_client.get("/api/platform-admin/operations/emails?status=sent")

    assert [i["status"] for i in failed.json["items"]] == ["FAILED"]
    assert [i["status"] for i in sent.json["items"]] == ["SENT"]


def test_email_log_filters_by_tenant(platform_client, failed_email, second_garage, session):
    session.add(
        CommunicationLog(
            garage_id=second_garage.id,
            channel="EMAIL",
            direction="OUTBOUND",
            to_address="other@example.com",
            status="SENT",
        )
    )
    session.commit()

    response = platform_client.get(
        f"/api/platform-admin/operations/emails?garage_id={failed_email.garage_id}"
    )

    assert response.json["total"] == 1


def test_email_summary_reports_the_failure_rate(platform_client, failed_email, sent_email):
    body = platform_client.get("/api/platform-admin/operations/emails/summary").json

    assert body["total"] == 2
    assert body["failed"] == 1
    assert body["failure_rate"] == 50.0


def test_summary_failure_rate_is_null_with_no_emails(platform_client, garage):
    body = platform_client.get("/api/platform-admin/operations/emails/summary").json

    assert body["total"] == 0
    assert body["failure_rate"] is None


def test_resending_a_failure_creates_a_linked_retry(platform_client, failed_email):
    response = platform_client.post(
        f"/api/platform-admin/operations/emails/{failed_email.id}/resend"
    )

    assert response.status_code == 201
    assert response.json["status"] == "SENT"
    assert response.json["retry_of_id"] == str(failed_email.id)
    assert response.json["to_address"] == failed_email.to_address


def test_the_original_failure_is_never_mutated(platform_client, failed_email, session):
    platform_client.post(f"/api/platform-admin/operations/emails/{failed_email.id}/resend")

    session.expire_all()
    original = session.get(CommunicationLog, failed_email.id)
    assert original.status == "FAILED"
    assert original.error_message == "Resend API returned 429"


def test_resending_is_audited(platform_client, failed_email, garage, platform_admin):
    platform_client.post(f"/api/platform-admin/operations/emails/{failed_email.id}/resend")

    entry = PlatformAuditLog.query.filter_by(action=ACTION_EMAIL_RESEND).one()
    assert entry.admin_email == platform_admin.email
    assert entry.garage_id == garage.id
    assert entry.details["original_id"] == str(failed_email.id)


def test_a_successful_email_cannot_be_resent(platform_client, sent_email):
    response = platform_client.post(f"/api/platform-admin/operations/emails/{sent_email.id}/resend")

    assert response.status_code == 422
    assert "failed" in response.json["message"].lower()


def test_a_non_email_message_cannot_be_resent(platform_client, garage, session):
    row = CommunicationLog(
        garage_id=garage.id,
        channel="WHATSAPP",
        direction="OUTBOUND",
        to_address="+447700900001",
        status="FAILED",
        body="hi",
    )
    session.add(row)
    session.commit()

    response = platform_client.post(f"/api/platform-admin/operations/emails/{row.id}/resend")

    assert response.status_code == 422


def test_support_admins_cannot_resend(support_client, failed_email):
    """Resending sends real mail to a real customer - superadmin only."""
    response = support_client.post(
        f"/api/platform-admin/operations/emails/{failed_email.id}/resend"
    )

    assert response.status_code == 403


def test_the_recipient_cannot_be_overridden_by_the_request(platform_client, failed_email):
    """The address is read from the stored row, never from the caller - this
    endpoint must not be usable as an open relay."""
    response = platform_client.post(
        f"/api/platform-admin/operations/emails/{failed_email.id}/resend",
        json={"to_address": "attacker@evil.example"},
    )

    assert response.status_code == 201
    assert response.json["to_address"] == "jane.doe@example.com"


# --------------------------------------------------------------------------
# Communication failures + job health
# --------------------------------------------------------------------------


def test_communication_failures_group_by_channel_and_tenant(
    platform_client, garage, second_garage, session
):
    session.add_all(
        [
            CommunicationLog(
                garage_id=garage.id,
                channel="EMAIL",
                direction="OUTBOUND",
                status="FAILED",
                to_address="a@example.com",
                error_code="429",
            ),
            CommunicationLog(
                garage_id=garage.id,
                channel="WHATSAPP",
                direction="OUTBOUND",
                status="failed",
                to_address="+447700900001",
            ),
            CommunicationLog(
                garage_id=second_garage.id,
                channel="EMAIL",
                direction="OUTBOUND",
                status="FAILED",
                to_address="b@example.com",
            ),
            # Not a failure - the tenant simply hasn't enabled the channel.
            CommunicationLog(
                garage_id=garage.id,
                channel="SMS",
                direction="OUTBOUND",
                status="SKIPPED_NOT_CONFIGURED",
                to_address="+447700900002",
            ),
        ]
    )
    session.commit()

    body = platform_client.get("/api/platform-admin/operations/communication-failures").json

    assert body["total"] == 3
    assert body["by_channel"] == {"EMAIL": 2, "WHATSAPP": 1}
    assert {row["garage_name"] for row in body["by_tenant"]} == {garage.name, second_garage.name}
    assert len(body["recent"]) == 3


def test_job_health_reports_checks_with_a_worst_case_status(platform_client, garage):
    response = platform_client.get("/api/platform-admin/operations/jobs")

    assert response.status_code == 200
    assert response.json["status"] in {"ok", "warning", "critical", "unknown"}
    keys = {check["key"] for check in response.json["checks"]}
    assert {"celery_broker", "mot_reminder_job", "stuck_reminders"} <= keys


def test_a_reminder_job_with_no_history_is_unknown_not_ok(platform_client, garage):
    """No evidence either way is more useful than a green light nothing
    verified."""
    checks = {
        check["key"]: check
        for check in platform_client.get("/api/platform-admin/operations/jobs").json["checks"]
    }

    assert checks["mot_reminder_job"]["status"] == "unknown"


def test_recent_communication_failures_escalate_job_health(platform_client, garage, session):
    session.add_all(
        [
            CommunicationLog(
                garage_id=garage.id,
                channel="EMAIL",
                direction="OUTBOUND",
                status="FAILED",
                to_address=f"{index}@example.com",
            )
            for index in range(12)
        ]
    )
    session.commit()

    body = platform_client.get("/api/platform-admin/operations/jobs").json
    checks = {check["key"]: check for check in body["checks"]}

    assert checks["communication_failures_24h"]["status"] == "critical"
    assert body["status"] == "critical"


def test_stale_pending_booking_requests_are_flagged(platform_client, garage, session):
    from app.models.booking_request import BookingRequest

    session.add(
        BookingRequest(
            garage_id=garage.id,
            status="PENDING",
            customer_first_name="Pat",
            customer_last_name="Rivera",
            vehicle_registration="OLD11REQ",
            preferred_date=datetime.datetime.now(datetime.UTC).date() - datetime.timedelta(days=5),
        )
    )
    session.commit()

    checks = {
        check["key"]: check
        for check in platform_client.get("/api/platform-admin/operations/jobs").json["checks"]
    }

    assert checks["stale_booking_requests"]["status"] == "warning"
    assert checks["stale_booking_requests"]["count"] == 1


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------


def test_the_audit_trail_shows_who_changed_what_and_when(platform_client, garage, platform_admin):
    platform_client.patch(f"/api/platform-admin/tenants/{garage.id}", json={"name": "Renamed"})

    response = platform_client.get("/api/platform-admin/audit-logs")

    assert response.status_code == 200
    entry = next(e for e in response.json["items"] if e["action"] == "tenant.update")
    assert entry["admin_email"] == platform_admin.email
    assert entry["garage_name"] == garage.name
    assert entry["created_at"]
    assert entry["summary"]


def test_audit_entries_never_carry_secrets(platform_client, garage):
    platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/impersonate",
        json={"reason": "Investigating a missing calendar - ticket 4412."},
    )

    body = platform_client.get("/api/platform-admin/audit-logs").get_data(as_text=True)

    assert "password_hash" not in body
    assert "handoff_code" not in body
    assert "access_token" not in body


def test_the_audit_trail_filters_by_tenant_and_action(platform_client, garage, second_garage):
    platform_client.patch(f"/api/platform-admin/tenants/{garage.id}", json={"name": "A2"})
    platform_client.post(
        f"/api/platform-admin/tenants/{second_garage.id}/suspend", json={"reason": "Non-payment."}
    )

    by_tenant = platform_client.get(f"/api/platform-admin/audit-logs?garage_id={second_garage.id}")
    by_action = platform_client.get("/api/platform-admin/audit-logs?action=tenant.update")

    assert {e["action"] for e in by_tenant.json["items"]} == {"tenant.suspend"}
    assert {e["garage_name"] for e in by_action.json["items"]} == {garage.name}


def test_the_audit_trail_is_read_only(platform_client, garage):
    """No verb here creates, edits or deletes an entry."""
    assert platform_client.post("/api/platform-admin/audit-logs", json={}).status_code == 405
    assert platform_client.delete("/api/platform-admin/audit-logs").status_code == 405


def test_a_rolled_back_action_leaves_no_audit_entry(platform_client, garage):
    """The audit row commits with its action, never separately - a rejected
    change must not leave a trail claiming it happened."""
    platform_client.patch(
        f"/api/platform-admin/tenants/{garage.id}", json={"plan": "NOT_A_REAL_PLAN"}
    )

    assert PlatformAuditLog.query.filter_by(action="tenant.update").count() == 0
    assert PlatformAuditLog.query.filter_by(action="tenant.plan_change").count() == 0
