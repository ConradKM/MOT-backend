"""Insights (app/insights): a business's own analytics.

Most tests run on one seeded March 2026 fixture with hand-worked answers, in
a garage whose clock is Europe/London - March 2026 contains the spring DST
change (29 March), so every day boundary is exercised in both GMT and BST.
"""

import csv
import datetime
import io
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from app.insights.periods import Period
from app.models.appointments.applied_add_on import AppointmentAddOn
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.payments.payment import BookingPayment
from app.models.vehicle import Vehicle
from app.platform_admin.features import PLANS
from tests.conftest import AuthenticatedClient

UTC = datetime.UTC
MARCH = "start=2026-03-01&end=2026-03-31"

#: Keys that carry money anywhere in an Insights payload.
MONEY_KEYS = {
    "booked_value",
    "discounts_given",
    "lost_value",
    "deposits_collected",
    "deposits_count",
    "unpriced_appointments",
}


def _utc(*args):
    return datetime.datetime(*args, tzinfo=UTC)


def _keys(value):
    """Every dict key anywhere in a JSON value."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _keys(inner)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture()
def london(session, garage):
    garage.timezone = "Europe/London"
    session.commit()
    return garage


@pytest.fixture()
def pro(session, garage):
    garage.plan = "PRO"
    session.commit()
    return garage


@pytest.fixture()
def staff_member(session, garage, staff_role):
    employee = Employee(
        garage_id=garage.id,
        email="stylist@garage-a.example",
        first_name="Sam",
        last_name="Stylist",
        password_hash="x",
        roles=[staff_role],
    )
    session.add(employee)
    session.commit()
    return employee


@pytest.fixture()
def staff_client(client, staff_member):
    return AuthenticatedClient(client, create_access_token(identity=str(staff_member.id)))


@pytest.fixture()
def make_customer(session, garage):
    def _make(first, last="Customer", **extra):
        row = Customer(garage_id=garage.id, first_name=first, last_name=last, **extra)
        session.add(row)
        session.commit()
        return row

    return _make


@pytest.fixture()
def book(session, garage, user, appointment_type):
    """Factory: an appointment with full control over the fields Insights reads."""

    def _book(
        start, customer, *, status="COMPLETED", price=None, employee=None, type_=None, **extra
    ):
        row = Appointment(
            garage_id=garage.id,
            employee_id=(employee or user).id,
            customer_id=customer.id,
            appointment_type_id=(type_ or appointment_type).id,
            start_time=start,
            end_time=start + datetime.timedelta(hours=1),
            status=status,
            price_at_booking=None if price is None else Decimal(price),
            **extra,
        )
        session.add(row)
        session.commit()
        return row

    return _book


@pytest.fixture()
def pay(session, garage):
    def _pay(amount_minor, status, paid_at, *, refunded=None, appointment=None):
        request = BookingRequest(
            garage_id=garage.id,
            status="APPROVED",
            customer_first_name="Pat",
            customer_last_name="Payer",
            preferred_date=paid_at.date(),
        )
        session.add(request)
        session.flush()
        row = BookingPayment(
            garage_id=garage.id,
            booking_request_id=request.id,
            appointment_id=appointment.id if appointment else None,
            provider="fake",
            amount_minor=amount_minor,
            status=status,
            paid_at=paid_at,
            refunded_amount_minor=refunded,
        )
        session.add(row)
        session.commit()
        return row

    return _pay


@pytest.fixture()
def march(session, london, book, pay, make_customer, staff_member):
    """The known-answer fixture. Worked answers for 1-31 March 2026:

    completed: 50.00 (C1, returning) + 30.00 (C2, discounted, add-on) +
               unpriced (C2's second visit) -> booked 80.00, 1 unpriced
    lost:      no-show 20.00 (staff) + cancelled 15.00 -> 35.00
    upcoming:  one BOOKED 25.00 - not money in any figure
    deposits:  10.00 + (20.00 - 5.00 refunded) = 25.00; fully refunded and
               failed attempts excluded
    """
    c1, c2, c3, c4 = (make_customer(name) for name in ("Ada", "Ben", "Cal", "Dee"))
    # C1's first visit is long before both periods -> returning in March.
    book(_utc(2026, 1, 10, 10), c1, price="40.00")
    # The previous period (29 Jan - 28 Feb): one completed visit.
    book(_utc(2026, 2, 10, 10), c4, price="10.00")

    book(_utc(2026, 3, 2, 10), c1, price="50.00")
    discounted = book(
        _utc(2026, 3, 3, 10),
        c2,
        price="30.00",
        discount_amount=Decimal("5.00"),
        price_before_discount=Decimal("35.00"),
        discount_code="SPRING5",
    )
    book(_utc(2026, 3, 20, 10), c2)  # second visit, same period, unpriced
    no_show = book(_utc(2026, 3, 4, 10), c3, status="NO_SHOW", price="20.00", employee=staff_member)
    book(_utc(2026, 3, 5, 10), c3, status="CANCELLED", price="15.00")
    book(_utc(2026, 3, 30, 9), c3, status="BOOKED", price="25.00")

    session.add(
        AppointmentAddOn(
            garage_id=discounted.garage_id,
            appointment_id=discounted.id,
            name="Beard trim",
            quantity=1,
            price_delta=Decimal("10.00"),
            duration_delta_minutes=15,
        )
    )
    session.commit()

    pay(1000, "SUCCEEDED", _utc(2026, 3, 5, 12), appointment=discounted)
    pay(2000, "PARTIALLY_REFUNDED", _utc(2026, 3, 6, 12), refunded=500)
    pay(1500, "REFUNDED", _utc(2026, 3, 7, 12), refunded=1500)
    pay(999, "FAILED", _utc(2026, 3, 8, 12))
    return {"no_show": no_show, "customers": (c1, c2, c3, c4)}


# --------------------------------------------------------------------------
# Summary, money and rates
# --------------------------------------------------------------------------


def test_owner_summary_matches_hand_worked_answers(authenticated_client, march):
    body = authenticated_client.get(f"/api/insights?{MARCH}").json
    summary = body["summary"]

    assert summary["booked_value"] == "80.00"
    assert summary["lost_value"] == "35.00"
    assert summary["lost_count"] == 2
    assert summary["deposits_collected"] == "25.00"
    assert summary["deposits_count"] == 2
    assert summary["discounts_given"] == "5.00"
    assert summary["unpriced_appointments"] == 1

    appointments = summary["appointments"]
    assert appointments["total"] == 6
    assert appointments["completed"] == 3
    assert appointments["no_show"] == 1
    assert appointments["cancelled"] == 1
    assert appointments["completion_rate"] == 60.0  # 3 of 5 concluded
    assert appointments["no_show_rate"] == 25.0  # 1 of 3 completed + 1 no-show
    assert appointments["cancellation_rate"] == 20.0

    # C1 returning (visited in January); C2 new despite two visits this period.
    assert summary["new_customers"] == 1
    assert summary["returning_customers"] == 1
    assert summary["returning_rate"] == 50.0

    assert body["period"] == {
        "start": "2026-03-01",
        "end": "2026-03-31",
        "days": 31,
        "granularity": "day",
        "timezone": "Europe/London",
    }
    assert body["scope"]["money_visible"] is True


def test_previous_period_is_the_same_length_immediately_before(authenticated_client, march):
    body = authenticated_client.get(f"/api/insights?{MARCH}").json

    assert body["previous_period"] == {"start": "2026-01-29", "end": "2026-02-28", "days": 31}
    previous = body["previous_summary"]
    assert previous["appointments"]["total"] == 1
    assert previous["booked_value"] == "10.00"
    assert previous["new_customers"] == 1


def test_rates_are_null_not_zero_on_an_empty_period(authenticated_client, london):
    summary = authenticated_client.get(f"/api/insights?{MARCH}").json["summary"]

    assert summary["appointments"]["total"] == 0
    assert summary["appointments"]["no_show_rate"] is None
    assert summary["appointments"]["completion_rate"] is None
    assert summary["returning_rate"] is None
    assert summary["booking_requests"]["approval_rate"] is None
    assert summary["booked_value"] == "0.00"


def test_top_services_and_add_on_uptake(authenticated_client, march):
    top = authenticated_client.get(f"/api/insights?{MARCH}").json["top_services"]

    assert len(top["services"]) == 1
    assert top["services"][0]["appointments"] == 6
    assert top["services"][0]["completed"] == 3
    assert top["services"][0]["booked_value"] == "80.00"
    assert top["add_ons"] == [{"name": "Beard trim", "appointments": 1, "quantity": 1}]
    # One add-on booking among the five that weren't cancelled.
    assert top["add_on_uptake_rate"] == 20.0


def test_booking_requests_received_in_the_period(authenticated_client, london, session):
    for status in ("APPROVED", "APPROVED", "REJECTED", "EXPIRED"):
        row = BookingRequest(
            garage_id=london.id,
            status=status,
            customer_first_name="Req",
            customer_last_name="Uest",
            preferred_date=datetime.date(2026, 3, 20),
        )
        session.add(row)
        session.flush()
        row.created_at = _utc(2026, 3, 10, 9)
    session.commit()

    requests = authenticated_client.get(f"/api/insights?{MARCH}").json["summary"][
        "booking_requests"
    ]
    assert requests["total"] == 4
    assert requests["approval_rate"] == 66.7
    assert requests["expired"] == 1


# --------------------------------------------------------------------------
# Timezone and DST
# --------------------------------------------------------------------------


def test_period_bounds_are_local_midnights_across_dst(london):
    period = Period.for_dates(london, datetime.date(2026, 3, 1), datetime.date(2026, 3, 31))

    assert period.start == _utc(2026, 3, 1, 0)  # GMT: local midnight is 00:00 UTC
    assert period.end == _utc(2026, 3, 31, 23)  # BST: 1 April 00:00 local is 23:00 UTC


def test_appointments_are_bucketed_by_local_day(authenticated_client, london, book, make_customer):
    pat = make_customer("Pat")
    book(_utc(2026, 2, 28, 23, 30), pat)  # 28 Feb local (GMT) - out
    book(_utc(2026, 3, 1, 0, 30), pat)  # 1 Mar local - in
    book(_utc(2026, 3, 31, 22, 30), pat)  # 31 Mar 23:30 BST - in
    book(_utc(2026, 3, 31, 23, 30), pat)  # 1 Apr 00:30 BST - out

    assert (
        authenticated_client.get(f"/api/insights?{MARCH}").json["summary"]["appointments"]["total"]
        == 2
    )


def test_heatmap_uses_local_weekday_and_hour(
    authenticated_client, london, pro, book, make_customer
):
    # Monday 30 March 09:00 UTC is 10:00 BST.
    book(_utc(2026, 3, 30, 9), make_customer("Pat"))
    # Cancelled bookings never filled the slot.
    book(_utc(2026, 3, 30, 9), make_customer("Lee"), status="CANCELLED")

    heatmap = authenticated_client.get(f"/api/insights?{MARCH}").json["heatmap"]
    assert heatmap["cells"] == [{"weekday": 1, "hour": 10, "appointments": 1}]
    assert heatmap["max"] == 1


def test_trend_fills_every_day_and_picks_granularity(authenticated_client, march, pro):
    trend = authenticated_client.get(f"/api/insights?{MARCH}").json["trend"]

    assert trend["granularity"] == "day"
    assert len(trend["points"]) == 31
    by_day = {point["bucket_start"]: point for point in trend["points"]}
    assert by_day["2026-03-03"]["appointments"] == 1
    assert by_day["2026-03-03"]["booked_value"] == "30.00"
    assert by_day["2026-03-03"]["new_customers"] == 1  # C2's first visit
    assert by_day["2026-03-02"]["new_customers"] == 0  # C1 is returning
    assert by_day["2026-03-10"]["appointments"] == 0

    weekly = authenticated_client.get("/api/insights?start=2026-01-01&end=2026-03-31").json["trend"]
    assert weekly["granularity"] == "week"
    # Buckets start on Mondays; 29 Dec 2025 is the Monday of 1 Jan's week.
    assert weekly["points"][0]["bucket_start"] == "2025-12-29"

    monthly = authenticated_client.get("/api/insights?start=2025-04-01&end=2026-03-31").json[
        "trend"
    ]
    assert monthly["granularity"] == "month"
    assert len(monthly["points"]) == 12


# --------------------------------------------------------------------------
# Range validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "start=2024-01-01&end=2026-01-01",  # over 730 days
        "start=2026-03-10&end=2026-03-01",  # backwards
        "start=2026-03-10",  # half a range
    ],
)
def test_bad_ranges_are_422(authenticated_client, garage, query):
    assert authenticated_client.get(f"/api/insights?{query}").status_code == 422


def test_default_range_is_the_last_30_days(authenticated_client, garage):
    period = authenticated_client.get("/api/insights").json["period"]
    assert period["days"] == 30


# --------------------------------------------------------------------------
# Staff scoping and money hiding
# --------------------------------------------------------------------------


def test_staff_see_only_their_own_appointments_and_no_money(staff_client, march, pro):
    response = staff_client.get(f"/api/insights?{MARCH}")
    body = response.json

    assert response.status_code == 200
    assert body["summary"]["appointments"]["total"] == 1
    assert body["summary"]["appointments"]["no_show"] == 1
    assert body["scope"]["money_visible"] is False
    assert body["staff"] is None
    assert body["summary"]["booking_requests"] is None
    # Not zeroed - absent, everywhere in the payload.
    assert not MONEY_KEYS & set(_keys(body))
    assert "£" not in response.get_data(as_text=True)


def test_staff_cannot_view_another_employee(staff_client, user, garage):
    response = staff_client.get(f"/api/insights?employee_id={user.id}")
    assert response.status_code == 403


def test_staff_may_name_themselves(staff_client, staff_member, garage):
    response = staff_client.get(f"/api/insights?employee_id={staff_member.id}")
    assert response.status_code == 200


def test_owner_can_filter_to_one_employee(authenticated_client, march, staff_member, pro):
    body = authenticated_client.get(f"/api/insights?{MARCH}&employee_id={staff_member.id}").json

    assert body["summary"]["appointments"]["total"] == 1
    assert body["summary"]["lost_value"] == "20.00"
    assert body["staff"]["rows"][0]["name"] == "Sam Stylist"


def test_owner_staff_table(authenticated_client, march, pro, staff_member):
    rows = authenticated_client.get(f"/api/insights?{MARCH}").json["staff"]["rows"]

    by_name = {row["name"]: row for row in rows}
    assert by_name["Sam Stylist"]["no_show_rate"] == 100.0
    assert by_name["owner-a@garage-a.example"]["booked_value"] == "80.00"
    assert by_name["owner-a@garage-a.example"]["appointments"] == 5


# --------------------------------------------------------------------------
# Plan tiering
# --------------------------------------------------------------------------


def test_insights_advanced_is_a_pro_only_feature():
    assert "insights_advanced" in PLANS["PRO"]
    assert "insights_advanced" not in PLANS["STANDARD"]
    assert "insights_advanced" not in PLANS["TRIAL"]


def test_advanced_sections_are_locked_without_the_feature(authenticated_client, march):
    body = authenticated_client.get(f"/api/insights?{MARCH}").json

    locked = {"locked": True, "feature": "insights_advanced"}
    for section in ("trend", "heatmap", "staff", "customers", "export"):
        assert body[section] == locked, section
    # Basic sections are on every plan.
    assert body["summary"]["appointments"]["total"] == 6
    assert body["top_services"]["services"]
    assert body["scope"]["advanced"] is False


def test_advanced_endpoints_are_403_without_the_feature(authenticated_client, garage):
    assert authenticated_client.get("/api/insights/due-back").status_code == 403
    assert authenticated_client.get(f"/api/insights/export.csv?{MARCH}").status_code == 403


def test_a_platform_override_unlocks_advanced_on_a_lower_plan(
    authenticated_client, garage, session
):
    from app.platform_admin.features import set_feature_override

    set_feature_override(garage, "insights_advanced", True)
    session.commit()

    body = authenticated_client.get(f"/api/insights?{MARCH}").json
    assert body["trend"].get("locked") is None
    assert body["scope"]["advanced"] is True


def test_staff_can_read_their_business_features(staff_client, garage, session):
    body = staff_client.get("/api/garage/features").json
    assert body["plan"] == "STANDARD"
    assert body["features"]["insights_advanced"] is False

    garage.plan = "PRO"
    session.commit()
    assert staff_client.get("/api/garage/features").json["features"]["insights_advanced"] is True


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


def test_bookings_by_channel(authenticated_client, london, book, make_customer, session):
    pat = make_customer("Pat")
    web = book(_utc(2026, 3, 2, 10), pat)
    book(_utc(2026, 3, 3, 10), pat, imported_at=_utc(2026, 2, 1))
    book(_utc(2026, 3, 4, 10), pat)
    session.add(
        BookingRequest(
            garage_id=london.id,
            status="APPROVED",
            source="WEB",
            appointment_id=web.id,
            customer_first_name="Pat",
            customer_last_name="Customer",
            preferred_date=datetime.date(2026, 3, 2),
        )
    )
    session.commit()

    sources = authenticated_client.get(f"/api/insights?{MARCH}").json["sources"]
    assert sources == {"web": 1, "import": 1, "staff": 1}


# --------------------------------------------------------------------------
# Due back
# --------------------------------------------------------------------------


@pytest.fixture()
def due_back_setup(session, pro, book, make_customer, staff_member, user):
    now = datetime.datetime.now(UTC)
    days = datetime.timedelta(days=1)

    haircut = GarageAppointmentType(garage_id=pro.id, name="Haircut", return_interval_days=30)
    one_off = GarageAppointmentType(garage_id=pro.id, name="Consultation")
    # Named nothing like "MOT" - the opt-in flag is what counts...
    annual = GarageAppointmentType(
        garage_id=pro.id, name="Annual check", due_back_uses_mot_expiry=True
    )
    # ...and a service called "MOT" without it never produces entries.
    named_mot = GarageAppointmentType(garage_id=pro.id, name="MOT")
    session.add_all([haircut, one_off, annual, named_mot])
    session.commit()

    overdue, soon, recent, booked, lapsed, driver = (
        make_customer(name) for name in ("Olive", "Sunny", "Rae", "Bo", "Lapsed", "Drew")
    )
    book(now - 100 * days, overdue, type_=haircut)
    book(now - 40 * days, overdue, type_=haircut)  # latest visit wins: due 10 days ago
    book(now - 20 * days, soon, type_=haircut, employee=staff_member)  # due in 10 days
    book(now - 5 * days, recent, type_=haircut)  # due in 25 days: not yet
    book(now - 40 * days, booked, type_=haircut)
    book(now + 7 * days, booked, type_=haircut, status="BOOKED")  # already coming back
    book(now - 400 * days, lapsed, type_=one_off)  # no interval: never due
    book(now - 400 * days, lapsed, type_=haircut, status="CANCELLED")  # not a visit

    car = Vehicle(
        garage_id=pro.id,
        customer_id=driver.id,
        registration_number="DR11VE",
        mot_expiry_date=(now + 7 * days).date(),
    )
    session.add(car)
    session.commit()
    book(now - 358 * days, driver, type_=annual, vehicle_id=car.id)
    book(now - 358 * days, driver, type_=named_mot, vehicle_id=car.id)
    return {"overdue": overdue, "soon": soon, "driver": driver, "car": car}


def test_due_back_list(authenticated_client, due_back_setup):
    page = authenticated_client.get("/api/insights/due-back").json

    assert page["due"] == 1
    assert page["due_soon"] == 2
    names = [
        (item["customer_name"], item["service_name"], item["status"]) for item in page["items"]
    ]
    assert sorted(names) == [
        ("Drew Customer", "Annual check", "due_soon"),
        ("Olive Customer", "Haircut", "due"),
        ("Sunny Customer", "Haircut", "due_soon"),
    ]
    mot = next(item for item in page["items"] if item["basis"] == "mot_expiry")
    assert mot["vehicle_registration"] == "DR11VE"
    assert mot["due_date"] == due_back_setup["car"].mot_expiry_date.isoformat()
    olive = next(item for item in page["items"] if item["customer_name"] == "Olive Customer")
    assert olive["customer_id"] == str(due_back_setup["overdue"].id)


def test_due_back_is_in_the_main_payload_and_paginates(authenticated_client, due_back_setup):
    due_back = authenticated_client.get("/api/insights").json["customers"]["due_back"]
    assert due_back["total"] == 3

    first = authenticated_client.get("/api/insights/due-back?per_page=2&page=1").json
    second = authenticated_client.get("/api/insights/due-back?per_page=2&page=2").json
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert first["total"] == 3


def test_staff_due_back_is_customers_last_seen_by_them(staff_client, due_back_setup):
    page = staff_client.get("/api/insights/due-back").json

    assert [item["customer_name"] for item in page["items"]] == ["Sunny Customer"]
    assert page["total"] == 1


# --------------------------------------------------------------------------
# CSV export
# --------------------------------------------------------------------------


def _csv(response):
    return list(csv.reader(io.StringIO(response.get_data(as_text=True))))


def test_owner_export_has_money_and_contact_details(authenticated_client, march, pro):
    response = authenticated_client.get(f"/api/insights/export.csv?{MARCH}")

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert (
        'filename="insights-garage-a-2026-03-01-to-2026-03-31.csv"'
        in response.headers["Content-Disposition"]
    )
    rows = _csv(response)
    assert rows[0] == [
        "Date",
        "Time",
        "Service",
        "Staff",
        "Status",
        "Customer",
        "Email",
        "Phone",
        "Price (GBP)",
        "Discount code",
        "Discount (GBP)",
        "Deposit (GBP)",
    ]
    assert len(rows) == 1 + 6
    discounted = next(row for row in rows if row[9] == "SPRING5")
    assert discounted[8] == "30.00"
    assert discounted[11] == "10.00"
    # Local time: 2 March is GMT, 30 March is BST.
    assert rows[1][:2] == ["2026-03-02", "10:00"]
    assert rows[-1][:2] == ["2026-03-30", "10:00"]


def test_staff_export_is_their_rows_without_money(staff_client, march, pro):
    rows = _csv(staff_client.get(f"/api/insights/export.csv?{MARCH}"))

    assert rows[0] == ["Date", "Time", "Service", "Staff", "Status", "Customer"]
    assert len(rows) == 2
    assert rows[1][4] == "NO_SHOW"


def test_export_neutralises_spreadsheet_formulas(authenticated_client, pro, book, make_customer):
    book(_utc(2026, 3, 2, 10), make_customer('=HYPERLINK("x")'))

    rows = _csv(authenticated_client.get(f"/api/insights/export.csv?{MARCH}"))
    assert rows[1][5].startswith("'=")


# --------------------------------------------------------------------------
# Tenant isolation
# --------------------------------------------------------------------------


def test_another_business_sees_none_of_it(second_authenticated_client, march):
    body = second_authenticated_client.get(f"/api/insights?{MARCH}").json
    assert body["summary"]["appointments"]["total"] == 0
    assert body["summary"]["deposits_collected"] == "0.00"


def test_owner_cannot_filter_to_another_business_employee(second_authenticated_client, march, user):
    response = second_authenticated_client.get(f"/api/insights?employee_id={user.id}")
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Shared stats: Platform Admin's numbers keep their own basis
# --------------------------------------------------------------------------


def test_platform_admin_stats_keep_their_open_ended_window(
    platform_client, authenticated_client, garage, book, make_customer
):
    now = datetime.datetime.now(UTC)
    pat = make_customer("Pat")
    book(now - datetime.timedelta(days=2), pat)
    book(now + datetime.timedelta(days=10), pat, status="BOOKED")

    tenant = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/stats").json
    # Platform Admin: everything starting in the last 30 days *or later*.
    assert tenant["appointments"]["total"] == 2
    assert tenant["appointments"]["upcoming"] == 1

    # Insights: only the closed period asked for.
    today = now.date()
    body = authenticated_client.get(
        f"/api/insights?start={today - datetime.timedelta(days=6)}&end={today}"
    ).json
    assert body["summary"]["appointments"]["total"] == 1
