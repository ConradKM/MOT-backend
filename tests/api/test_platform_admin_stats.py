"""Per-tenant and platform-wide statistics.

The rule these tests exist to protect: a rate over an empty denominator is
``null``, never ``0``. "No booking requests yet" and "every request was
rejected" must never render as the same number on an operator's screen.
"""

import datetime

from app.models.appointments.appointment_checklist import AppointmentChecklist
from app.models.appointments.appointment_checklist_item import AppointmentChecklistItem
from app.models.booking_request import BookingRequest
from app.models.communications.communication_log import CommunicationLog


def _booking_request(session, garage, status, days_ahead=3):
    row = BookingRequest(
        garage_id=garage.id,
        status=status,
        customer_first_name="Pat",
        customer_last_name="Rivera",
        vehicle_registration="BK11REQ",
        preferred_date=datetime.datetime.now(datetime.UTC).date()
        + datetime.timedelta(days=days_ahead),
    )
    session.add(row)
    session.commit()
    return row


def test_stats_for_a_brand_new_tenant_are_zero_not_null(platform_client, garage):
    response = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/stats")

    assert response.status_code == 200
    assert response.json["booking_requests"]["total"] == 0
    assert response.json["appointments"]["total"] == 0


def test_rates_are_null_when_there_is_nothing_to_divide_by(platform_client, garage):
    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/stats").json

    assert body["booking_requests"]["approval_rate"] is None
    assert body["appointments"]["no_show_rate"] is None
    assert body["customers"]["repeat_rate"] is None


def test_booking_request_approval_and_rejection_rates(platform_client, garage, session):
    for status in ("APPROVED", "APPROVED", "APPROVED", "REJECTED"):
        _booking_request(session, garage, status)
    # Expired requests are excluded from the decision rates - nobody decided them.
    _booking_request(session, garage, "EXPIRED")

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/stats").json

    assert body["booking_requests"]["total"] == 5
    assert body["booking_requests"]["approval_rate"] == 75.0
    assert body["booking_requests"]["rejection_rate"] == 25.0
    assert body["booking_requests"]["expiry_rate"] == 20.0


def test_appointment_throughput_and_no_show_rate(platform_client, garage, make_appointment):
    now = datetime.datetime.now(datetime.UTC)
    make_appointment(now - datetime.timedelta(days=1), status="COMPLETED")
    make_appointment(now - datetime.timedelta(days=2), status="COMPLETED")
    make_appointment(now - datetime.timedelta(days=3), status="COMPLETED")
    make_appointment(now - datetime.timedelta(days=4), status="NO_SHOW")
    # An upcoming booking is not a missed one - it must not drag the rate down.
    make_appointment(now + datetime.timedelta(days=1), status="BOOKED")

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/stats").json

    assert body["appointments"]["completed"] == 3
    assert body["appointments"]["no_show"] == 1
    assert body["appointments"]["upcoming"] == 1
    assert body["appointments"]["no_show_rate"] == 25.0


def test_repeat_customer_counts(platform_client, garage, make_appointment, customer):
    now = datetime.datetime.now(datetime.UTC)
    make_appointment(now - datetime.timedelta(days=40))
    make_appointment(now - datetime.timedelta(days=2))

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/stats").json

    assert body["customers"]["total"] == 1
    assert body["customers"]["with_appointments"] == 1
    assert body["customers"]["repeat_customers"] == 1
    assert body["customers"]["repeat_rate"] == 100.0


def test_checklist_completion(platform_client, garage, make_appointment, session):
    appointment = make_appointment(datetime.datetime.now(datetime.UTC))
    checklist = AppointmentChecklist(garage_id=garage.id, appointment_id=appointment.id)
    session.add(checklist)
    session.flush()
    session.add_all(
        [
            AppointmentChecklistItem(
                garage_id=garage.id,
                appointment_checklist_id=checklist.id,
                order=index,
                label=f"Item {index}",
                status=status,
            )
            for index, status in enumerate(("PASS", "PASS", "NOT_CHECKED", "ADVISORY"))
        ]
    )
    session.commit()

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/stats").json

    assert body["checklists"]["started"] == 1
    assert body["checklists"]["items"] == 4
    assert body["checklists"]["items_completed"] == 3
    assert body["checklists"]["item_completion_rate"] == 75.0
    # Not every item was checked, so the checklist isn't fully complete.
    assert body["checklists"]["fully_completed"] == 0


def test_communication_volume_and_delivery_rates(platform_client, garage, session):
    session.add_all(
        [
            CommunicationLog(
                garage_id=garage.id,
                channel="EMAIL",
                direction="OUTBOUND",
                status=status,
                to_address="jane@example.com",
            )
            for status in ("SENT", "SENT", "SENT", "FAILED")
        ]
        + [
            CommunicationLog(
                garage_id=garage.id,
                channel="WHATSAPP",
                direction="INBOUND",
                status="received",
                from_address="+447700900001",
            ),
            # A channel the tenant never turned on: not a delivery failure.
            CommunicationLog(
                garage_id=garage.id,
                channel="SMS",
                direction="OUTBOUND",
                status="SKIPPED_NOT_CONFIGURED",
                to_address="+447700900001",
            ),
        ]
    )
    session.commit()

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/stats").json
    email = body["communications"]["by_channel"]["EMAIL"]

    assert email["total"] == 4
    assert email["delivered"] == 3
    assert email["failed"] == 1
    assert email["delivery_rate"] == 75.0
    assert body["communications"]["by_channel"]["WHATSAPP"]["inbound"] == 1
    # A skipped send never reached a provider, so it can't have a delivery rate.
    sms = body["communications"]["by_channel"]["SMS"]
    assert sms["skipped_not_configured"] == 1
    assert sms["delivery_rate"] is None


def test_stats_never_count_another_tenants_rows(platform_client, garage, second_garage, session):
    _booking_request(session, garage, "APPROVED")
    _booking_request(session, second_garage, "APPROVED")
    _booking_request(session, second_garage, "REJECTED")

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/stats").json

    assert body["booking_requests"]["total"] == 1


def test_the_period_window_is_respected(platform_client, garage, make_appointment):
    now = datetime.datetime.now(datetime.UTC)
    make_appointment(now - datetime.timedelta(days=5), status="COMPLETED")
    make_appointment(now - datetime.timedelta(days=60), status="COMPLETED")

    last_week = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/stats?days=7").json
    last_quarter = platform_client.get(
        f"/api/platform-admin/tenants/{garage.id}/stats?days=90"
    ).json

    assert last_week["appointments"]["total"] == 1
    assert last_quarter["appointments"]["total"] == 2


# --------------------------------------------------------------------------
# Platform-wide
# --------------------------------------------------------------------------


def test_platform_overview_counts_tenants_by_status(
    platform_client, garage, second_garage, session
):
    second_garage.status = "SUSPENDED"
    session.commit()

    body = platform_client.get("/api/platform-admin/stats/overview").json

    assert body["tenants"]["total"] == 2
    assert body["tenants"]["active"] == 1
    assert body["tenants"]["suspended"] == 1
    assert body["tenants"]["by_plan"]["STANDARD"] == 2


def test_platform_overview_reports_signups_and_usage(platform_client, garage, customer, vehicle):
    body = platform_client.get("/api/platform-admin/stats/overview").json

    assert body["signups"]["in_period"] == 1
    assert body["signups"]["all_time"] == 1
    assert body["usage"]["customers_total"] == 1
    assert body["usage"]["vehicles_total"] == 1


def test_revenue_is_absent_rather_than_invented(platform_client, garage):
    body = platform_client.get("/api/platform-admin/stats/overview").json

    assert body["revenue"] is None
    # The hook a future subscription metric hangs off is present.
    assert "by_plan" in body["tenants"]


def test_a_dormant_tenant_is_derived_from_activity(platform_client, garage, session):
    garage.created_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=200)
    session.commit()

    body = platform_client.get("/api/platform-admin/stats/overview").json

    assert body["tenants"]["dormant"] == 1


def test_a_new_tenant_is_never_dormant_on_day_one(platform_client, garage):
    body = platform_client.get("/api/platform-admin/stats/overview").json

    assert body["tenants"]["dormant"] == 0


def test_growth_fills_in_quiet_days(platform_client, garage):
    response = platform_client.get("/api/platform-admin/stats/growth?days=30")

    assert response.status_code == 200
    points = response.json["points"]
    assert len(points) == 30
    assert sum(point["signups"] for point in points) == 1
    # The running total never goes backwards.
    assert points[-1]["cumulative"] >= points[0]["cumulative"]
