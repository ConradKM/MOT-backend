"""Calendar export (app/calendar_export): the one-off .ics download and the
private subscription feeds.

Every ICS assertion parses the output back with ``icalendar`` rather than
string-matching, except the privacy regression, which deliberately greps the
raw bytes so no encoding or folding can hide a leak.
"""

import datetime
import re
import secrets
import uuid

import pytest
from flask_jwt_extended import create_access_token
from icalendar import Calendar
from werkzeug.security import generate_password_hash

from app.extensions import db
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.calendar_feed import CalendarFeed
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.garage import GARAGE_STATUS_SUSPENDED
from app.models.vehicle import Vehicle
from tests.conftest import DEFAULT_PASSWORD, AuthenticatedClient

UTC = datetime.UTC
DOWNLOAD = "/api/calendar-export/appointments.ics"
FEEDS = "/api/calendar-export/feeds"


def _dt(*args):
    return datetime.datetime(*args, tzinfo=UTC)


def _events(response):
    assert response.status_code == 200, response.get_data(as_text=True)
    calendar = Calendar.from_ical(response.data)
    return calendar, {str(e["UID"]).split("@")[0]: e for e in calendar.walk("VEVENT")}


def _download(client, start="2026-03-01", end="2026-03-31", **params):
    query = {"start_date": start, "end_date": end, **params}
    return client.get(DOWNLOAD, query_string=query)


def _feed_path(url: str) -> str:
    return url[url.index("/api/calendar-feeds/") :]


@pytest.fixture()
def staff(session, garage, staff_role):
    employee = Employee(
        garage_id=garage.id,
        email="staff-a@garage-a.example",
        first_name="Sam",
        last_name="Spanner",
        password_hash=generate_password_hash(DEFAULT_PASSWORD),
        roles=[staff_role],
    )
    session.add(employee)
    session.commit()
    return employee


@pytest.fixture()
def staff_client(client, staff):
    return AuthenticatedClient(client, create_access_token(identity=str(staff.id)))


@pytest.fixture()
def named_owner(session, user):
    user.first_name, user.last_name = "Olive", "Owner"
    session.commit()
    return user


# --------------------------------------------------------------------------
# ICS correctness
# --------------------------------------------------------------------------


def test_download_headers_and_calendar_properties(authenticated_client, make_appointment):
    make_appointment(_dt(2026, 3, 10, 9))

    response = _download(authenticated_client)

    assert response.headers["Content-Type"] == "text/calendar; charset=utf-8"
    assert response.headers["Content-Disposition"] == 'attachment; filename="appointments.ics"'
    assert b"\r\n" in response.data
    calendar, events = _events(response)
    assert str(calendar["PRODID"]) == "-//CoMaz OS//Appointments//EN"
    assert str(calendar["VERSION"]) == "2.0"
    assert str(calendar["CALSCALE"]) == "GREGORIAN"
    assert str(calendar["X-WR-CALNAME"]) == "Garage A - Appointments"
    # A download is a snapshot - refresh hints belong to feeds only.
    assert "REFRESH-INTERVAL" not in calendar
    assert len(events) == 1


def test_event_fields(authenticated_client, make_appointment, vehicle, garage, session):
    garage.address_city, garage.postcode = "London", "E1 1AA"
    session.commit()
    appointment = make_appointment(_dt(2026, 3, 10, 9, 30), minutes=45)
    appointment.vehicle_id = vehicle.id
    session.commit()

    _, events = _events(_download(authenticated_client))
    event = events[str(appointment.id)]

    assert str(event["UID"]) == f"{appointment.id}@comaz.co.uk"
    assert str(event["SUMMARY"]) == "MOT - AB12CDE"
    assert event.decoded("DTSTART") == _dt(2026, 3, 10, 9, 30)
    assert event.decoded("DTEND") == _dt(2026, 3, 10, 10, 15)
    assert str(event["LOCATION"]) == "1 Test Street, London, London, E1 1AA"
    assert str(event["URL"]) == (
        f"http://localhost:5173/{garage.id}/appointments/{appointment.id}/overview"
    )
    assert "DTSTAMP" in event and "LAST-MODIFIED" in event
    assert int(event["SEQUENCE"]) == 0


def test_times_are_emitted_in_utc(authenticated_client, make_appointment):
    make_appointment(_dt(2026, 3, 10, 9))
    raw = _download(authenticated_client).get_data(as_text=True)
    assert "DTSTART:20260310T090000Z" in raw
    assert "VTIMEZONE" not in raw


def test_registration_omitted_without_vehicle(authenticated_client, make_appointment):
    appointment = make_appointment(_dt(2026, 3, 10, 9))
    _, events = _events(_download(authenticated_client))
    assert str(events[str(appointment.id)]["SUMMARY"]) == "MOT"


def test_special_characters_and_long_titles_round_trip(
    authenticated_client, make_appointment, session
):
    name = 'Brakes, discs; pads \\ "front" ' + "x" * 220
    appointment = make_appointment(_dt(2026, 3, 10, 9))
    appointment.appointment_type_name_at_booking = name[:100]
    session.commit()

    response = _download(authenticated_client)
    _, events = _events(response)

    assert str(events[str(appointment.id)]["SUMMARY"]) == name[:100]
    # RFC 5545 3.1: no content line longer than 75 octets.
    assert all(len(line) <= 75 for line in response.data.split(b"\r\n"))


def test_title_over_200_characters_is_folded(
    authenticated_client, make_appointment, session, user, vehicle
):
    service = "Service, full; " + "y" * 85
    user.first_name, user.last_name = "F" * 100, "L" * 100
    appointment = make_appointment(_dt(2026, 3, 10, 9))
    appointment.appointment_type_name_at_booking = service
    appointment.vehicle_id = vehicle.id
    session.commit()

    response = _download(authenticated_client, scope="ALL")
    _, events = _events(response)

    summary = str(events[str(appointment.id)]["SUMMARY"])
    assert len(summary) > 200
    assert summary == f"{service} - AB12CDE ({'F' * 100} {'L' * 100})"
    assert all(len(line) <= 75 for line in response.data.split(b"\r\n"))


def test_multi_day_and_midnight_crossing_events(authenticated_client, make_appointment):
    overnight = make_appointment(_dt(2026, 3, 10, 23, 30), minutes=90)
    multi_day = make_appointment(_dt(2026, 3, 12, 8), minutes=60 * 50)

    _, events = _events(_download(authenticated_client))

    assert events[str(overnight.id)].decoded("DTEND") == _dt(2026, 3, 11, 1, 0)
    assert events[str(multi_day.id)].decoded("DTEND") == _dt(2026, 3, 14, 10, 0)


def test_dst_boundary_is_correct_in_utc(authenticated_client, make_appointment, garage, session):
    # Europe/London springs forward at 01:00 UTC on 29 March 2026. 09:00 local
    # that day is 08:00 UTC; the day before it was 09:00 UTC.
    garage.timezone = "Europe/London"
    session.commit()
    before = make_appointment(_dt(2026, 3, 28, 9))
    after = make_appointment(_dt(2026, 3, 29, 8))
    # 00:30 UTC on the 29th is still the 29th locally (GMT) - in range.
    edge = make_appointment(_dt(2026, 3, 29, 0, 30))

    _, events = _events(_download(authenticated_client, start="2026-03-29", end="2026-03-29"))

    assert set(events) == {str(after.id), str(edge.id)}
    assert events[str(after.id)].decoded("DTSTART") == _dt(2026, 3, 29, 8)
    assert str(before.id) not in events


def test_uid_stable_and_sequence_increases_across_edits(
    authenticated_client, make_appointment, session
):
    appointment = make_appointment(_dt(2026, 3, 10, 9))
    # Back-date the row so the edit below lands in a later second than it
    # (LAST-MODIFIED has one-second resolution). Setting updated_at
    # explicitly stops the ORM's onupdate from overriding it.
    an_hour_ago = datetime.datetime.now(UTC) - datetime.timedelta(hours=1)
    appointment.created_at = appointment.updated_at = an_hour_ago
    appointment.garage.updated_at = an_hour_ago  # LOCATION's source row
    session.commit()
    _, first = _events(_download(authenticated_client))

    appointment.end_time = _dt(2026, 3, 10, 11)
    session.commit()
    _, second = _events(_download(authenticated_client))

    key = str(appointment.id)
    assert str(first[key]["UID"]) == str(second[key]["UID"])
    assert int(second[key]["SEQUENCE"]) > int(first[key]["SEQUENCE"])
    assert second[key].decoded("LAST-MODIFIED") > first[key].decoded("LAST-MODIFIED")


def test_cancelled_and_no_show_are_excluded(authenticated_client, make_appointment, session):
    kept = make_appointment(_dt(2026, 3, 10, 9))
    cancelled = make_appointment(_dt(2026, 3, 10, 11))
    make_appointment(_dt(2026, 3, 10, 13), status="NO_SHOW")
    _, events = _events(_download(authenticated_client))
    assert set(events) == {str(kept.id), str(cancelled.id)}

    cancelled.status = "CANCELLED"
    session.commit()
    _, events = _events(_download(authenticated_client))
    assert set(events) == {str(kept.id)}


def test_pending_booking_requests_are_not_exported(authenticated_client, booking_request):
    _, events = _events(_download(authenticated_client, start="2026-01-01", end="2026-12-31"))
    assert events == {}


def test_appointment_type_filter(authenticated_client, make_appointment, garage, session):
    other_type = GarageAppointmentType(garage_id=garage.id, name="Service", status="ACTIVE")
    session.add(other_type)
    session.commit()
    mot = make_appointment(_dt(2026, 3, 10, 9))
    service = make_appointment(_dt(2026, 3, 10, 11))
    service.appointment_type_id = other_type.id
    session.commit()

    _, events = _events(_download(authenticated_client, appointment_type_ids=[str(other_type.id)]))
    assert set(events) == {str(service.id)}

    _, events = _events(_download(authenticated_client))
    assert set(events) == {str(mot.id), str(service.id)}


# --------------------------------------------------------------------------
# Privacy regression
# --------------------------------------------------------------------------


def test_no_customer_details_ever_reach_the_calendar(
    authenticated_client, make_appointment, session, garage, vehicle, named_owner, staff
):
    customer = Customer(
        garage_id=garage.id,
        first_name="Zebedee",
        last_name="Quizzlewick",
        email="zebedee.quizzlewick@private.example",
        phone="+44 7700 912345",
    )
    session.add(customer)
    session.commit()
    start = datetime.datetime.now(UTC) + datetime.timedelta(days=1)
    appointment = make_appointment(start, employee=staff)
    appointment.customer_id = customer.id
    appointment.vehicle_id = vehicle.id
    appointment.notes = "Gate code 4417, spare key under the plant pot"
    session.commit()

    day = start.date().isoformat()
    download = _download(authenticated_client, start=day, end=day, scope="ALL").data
    feed = authenticated_client.post(FEEDS, json={"name": "All", "scope": "ALL"}).get_json()
    subscribed = authenticated_client.get(_feed_path(feed["subscription_url"])).data

    for body in (download, subscribed):
        # Unfold first so a value split across lines can't slip past.
        text = body.decode().replace("\r\n ", "").lower()
        assert str(appointment.id) in text
        for needle in (
            "zebedee",
            "quizzlewick",
            "private.example",
            "912345",
            "7700",
            "gate code",
            "plant pot",
            "4417",
        ):
            assert needle not in text, needle


# --------------------------------------------------------------------------
# Download authorization and limits
# --------------------------------------------------------------------------


def test_staff_can_export_only_their_own(staff_client, staff, make_appointment):
    mine = make_appointment(_dt(2026, 3, 10, 9), employee=staff)
    make_appointment(_dt(2026, 3, 10, 9))  # the owner's

    _, events = _events(_download(staff_client))  # scope defaults to ME
    assert set(events) == {str(mine.id)}
    # A single worker's own calendar: no worker name in the title.
    assert "(" not in str(events[str(mine.id)]["SUMMARY"])

    _, events = _events(_download(staff_client, scope="SELECTED", employee_ids=[str(staff.id)]))
    assert set(events) == {str(mine.id)}


def test_staff_requesting_other_workers_is_403(staff_client, user, staff):
    assert _download(staff_client, scope="ALL").status_code == 403
    response = _download(staff_client, scope="SELECTED", employee_ids=[str(user.id), str(staff.id)])
    assert response.status_code == 403


def test_owner_can_export_all_or_selected_with_worker_names(
    authenticated_client, named_owner, staff, make_appointment
):
    own = make_appointment(_dt(2026, 3, 10, 9))
    theirs = make_appointment(_dt(2026, 3, 10, 9), employee=staff)

    _, events = _events(_download(authenticated_client, scope="ALL"))
    assert str(events[str(own.id)]["SUMMARY"]) == "MOT (Olive Owner)"
    assert str(events[str(theirs.id)]["SUMMARY"]) == "MOT (Sam Spanner)"

    _, events = _events(
        _download(authenticated_client, scope="SELECTED", employee_ids=[str(staff.id)])
    )
    assert set(events) == {str(theirs.id)}


def test_all_includes_deactivated_workers_but_selected_rejects_them(
    authenticated_client, staff, make_appointment, session
):
    # Matches the calendar page: its unfiltered view lists every appointment,
    # while its worker picker only offers active employees.
    theirs = make_appointment(_dt(2026, 3, 10, 9), employee=staff)
    staff.is_active = False
    session.commit()

    _, events = _events(_download(authenticated_client, scope="ALL"))
    assert str(theirs.id) in events
    response = _download(authenticated_client, scope="SELECTED", employee_ids=[str(staff.id)])
    assert response.status_code == 422


def test_other_garage_ids_are_rejected(authenticated_client, second_user, second_garage, session):
    response = _download(authenticated_client, scope="SELECTED", employee_ids=[str(second_user.id)])
    assert response.status_code == 422

    foreign_type = GarageAppointmentType(garage_id=second_garage.id, name="MOT", status="ACTIVE")
    session.add(foreign_type)
    session.commit()
    response = _download(authenticated_client, appointment_type_ids=[str(foreign_type.id)])
    assert response.status_code == 422


def test_never_returns_another_garages_appointments(
    authenticated_client, second_garage, second_user, second_customer, session
):
    foreign_type = GarageAppointmentType(garage_id=second_garage.id, name="MOT", status="ACTIVE")
    session.add(foreign_type)
    session.commit()
    session.add(
        Appointment(
            garage_id=second_garage.id,
            employee_id=second_user.id,
            customer_id=second_customer.id,
            appointment_type_id=foreign_type.id,
            start_time=_dt(2026, 3, 10, 9),
            end_time=_dt(2026, 3, 10, 10),
        )
    )
    session.commit()

    _, events = _events(_download(authenticated_client, scope="ALL"))
    assert events == {}


def test_range_cap(authenticated_client):
    assert _download(authenticated_client, start="2026-01-01", end="2027-01-01").status_code == 200
    response = _download(authenticated_client, start="2026-01-01", end="2027-01-02")
    assert response.status_code == 422
    assert "366 days" in response.get_json()["message"]
    assert _download(authenticated_client, start="2026-03-02", end="2026-03-01").status_code == 422


def test_event_count_cap(app, authenticated_client, make_appointment):
    for hour in range(9, 12):
        make_appointment(_dt(2026, 3, 10, hour))
    app.config["CALENDAR_EXPORT_MAX_EVENTS"] = 2
    try:
        response = _download(authenticated_client)
    finally:
        app.config["CALENDAR_EXPORT_MAX_EVENTS"] = 5000
    assert response.status_code == 422
    assert "Narrow the date range" in response.get_json()["message"]


def test_download_requires_an_employee_token(client, customer_client):
    assert _download(client).status_code == 401
    assert _download(customer_client).status_code == 403


# --------------------------------------------------------------------------
# Feed CRUD
# --------------------------------------------------------------------------


def test_create_returns_url_once_and_list_never_does(authenticated_client):
    created = authenticated_client.post(FEEDS, json={"name": "Phone"})
    assert created.status_code == 201
    body = created.get_json()
    assert re.fullmatch(
        r"http://localhost:5001/api/calendar-feeds/[A-Za-z0-9_-]{43}\.ics", body["subscription_url"]
    )
    assert body["webcal_url"] == body["subscription_url"].replace("http://", "webcal://")
    assert body["scope"] == "ME" and body["appointment_type_ids"] is None

    token = body["subscription_url"].rsplit("/", 1)[1].removesuffix(".ics")
    listed = authenticated_client.get(FEEDS)
    assert listed.status_code == 200
    assert token not in listed.get_data(as_text=True)
    assert "subscription_url" not in listed.get_json()[0]
    # Stored hashed, never raw.
    assert CalendarFeed.query.one().token_hash != token


def test_feed_creation_permissions(staff_client, staff, user):
    assert staff_client.post(FEEDS, json={"name": "x", "scope": "ALL"}).status_code == 403
    response = staff_client.post(
        FEEDS, json={"name": "x", "scope": "SELECTED", "employee_ids": [str(user.id)]}
    )
    assert response.status_code == 403
    assert staff_client.post(FEEDS, json={"name": "x", "scope": "ME"}).status_code == 201


def test_feed_creation_rejects_other_garage_ids(authenticated_client, second_user):
    response = authenticated_client.post(
        FEEDS, json={"name": "x", "scope": "SELECTED", "employee_ids": [str(second_user.id)]}
    )
    assert response.status_code == 422


def test_feed_cap_per_employee(app, authenticated_client):
    app.config["CALENDAR_FEEDS_PER_EMPLOYEE"] = 2
    try:
        assert authenticated_client.post(FEEDS, json={"name": "1"}).status_code == 201
        second = authenticated_client.post(FEEDS, json={"name": "2"}).get_json()
        assert authenticated_client.post(FEEDS, json={"name": "3"}).status_code == 422
        # Revoking frees a slot.
        authenticated_client.delete(f"{FEEDS}/{second['id']}")
        assert authenticated_client.post(FEEDS, json={"name": "3"}).status_code == 201
    finally:
        app.config["CALENDAR_FEEDS_PER_EMPLOYEE"] = 10


def test_list_scoping(authenticated_client, staff_client, second_authenticated_client):
    authenticated_client.post(FEEDS, json={"name": "Owner's"})
    staff_client.post(FEEDS, json={"name": "Staff's"})
    second_authenticated_client.post(FEEDS, json={"name": "Other garage"})

    assert {f["name"] for f in authenticated_client.get(FEEDS).get_json()} == {"Owner's", "Staff's"}
    assert {f["name"] for f in staff_client.get(FEEDS).get_json()} == {"Staff's"}


def test_regenerate_rotates_and_old_url_stops(authenticated_client):
    created = authenticated_client.post(FEEDS, json={"name": "Phone"}).get_json()
    old_path = _feed_path(created["subscription_url"])
    assert authenticated_client.get(old_path).status_code == 200

    rotated = authenticated_client.post(f"{FEEDS}/{created['id']}/regenerate")
    assert rotated.status_code == 200
    new_path = _feed_path(rotated.get_json()["subscription_url"])

    assert new_path != old_path
    assert authenticated_client.get(old_path).status_code == 404
    assert authenticated_client.get(new_path).status_code == 200


def test_regenerate_and_revoke_permissions(
    authenticated_client, staff_client, second_authenticated_client
):
    staff_feed = staff_client.post(FEEDS, json={"name": "Staff's"}).get_json()
    owner_feed = authenticated_client.post(FEEDS, json={"name": "Owner's"}).get_json()

    # Staff can't see or touch the owner's feed; other garages see nothing.
    assert staff_client.delete(f"{FEEDS}/{owner_feed['id']}").status_code == 404
    assert staff_client.post(f"{FEEDS}/{owner_feed['id']}/regenerate").status_code == 404
    assert second_authenticated_client.delete(f"{FEEDS}/{staff_feed['id']}").status_code == 404

    # An owner may revoke anyone's link but only regenerate their own.
    response = authenticated_client.post(f"{FEEDS}/{staff_feed['id']}/regenerate")
    assert response.status_code == 403
    assert authenticated_client.delete(f"{FEEDS}/{staff_feed['id']}").status_code == 204
    assert authenticated_client.get(_feed_path(staff_feed["subscription_url"])).status_code == 404


def test_regenerate_refused_once_authority_is_gone(authenticated_client, user, session):
    created = authenticated_client.post(FEEDS, json={"name": "All", "scope": "ALL"}).get_json()
    user.roles = []
    session.commit()
    response = authenticated_client.post(f"{FEEDS}/{created['id']}/regenerate")
    assert response.status_code == 403


# --------------------------------------------------------------------------
# Feed fetch
# --------------------------------------------------------------------------


def test_feed_fetch_serves_a_rolling_window(client, authenticated_client, make_appointment):
    now = datetime.datetime.now(UTC).replace(microsecond=0)
    recent = make_appointment(now - datetime.timedelta(days=3))
    soon = make_appointment(now + datetime.timedelta(days=20))
    make_appointment(now - datetime.timedelta(days=10))  # before the window
    make_appointment(now + datetime.timedelta(days=40))  # after it

    created = authenticated_client.post(
        FEEDS, json={"name": "Phone", "days_back": 7, "days_forward": 30}
    ).get_json()
    response = client.get(_feed_path(created["subscription_url"]))

    calendar, events = _events(response)
    assert set(events) == {str(recent.id), str(soon.id)}
    assert str(calendar["X-WR-CALNAME"]) == "Garage A - Phone"
    assert calendar.decoded("REFRESH-INTERVAL") == datetime.timedelta(hours=1)
    assert str(calendar["X-PUBLISHED-TTL"]) == "PT1H"
    assert response.headers["Content-Type"] == "text/calendar; charset=utf-8"
    assert "max-age" in response.headers["Cache-Control"]
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_feed_etag_gives_304_and_records_last_access(client, authenticated_client, session):
    created = authenticated_client.post(FEEDS, json={"name": "Phone"}).get_json()
    assert created["last_accessed_at"] is None
    path = _feed_path(created["subscription_url"])

    first = client.get(path)
    etag = first.headers["ETag"]
    assert etag
    second = client.get(path, headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.data == b""

    listed = authenticated_client.get(FEEDS).get_json()[0]
    assert listed["last_accessed_at"] is not None


def test_cancelled_appointment_disappears_from_feed(
    client, authenticated_client, make_appointment, session
):
    appointment = make_appointment(datetime.datetime.now(UTC) + datetime.timedelta(days=1))
    path = _feed_path(
        authenticated_client.post(FEEDS, json={"name": "x"}).get_json()["subscription_url"]
    )
    _, events = _events(client.get(path))
    assert str(appointment.id) in events

    appointment.status = "CANCELLED"
    session.commit()
    _, events = _events(client.get(path))
    assert str(appointment.id) not in events


def test_feed_type_selection_is_honoured(
    client, authenticated_client, make_appointment, appointment_type, garage, session
):
    other_type = GarageAppointmentType(garage_id=garage.id, name="Service", status="ACTIVE")
    session.add(other_type)
    session.commit()
    start = datetime.datetime.now(UTC) + datetime.timedelta(days=1)
    mot = make_appointment(start)
    service = make_appointment(start + datetime.timedelta(hours=2))
    service.appointment_type_id = other_type.id
    session.commit()

    created = authenticated_client.post(
        FEEDS, json={"name": "MOTs", "appointment_type_ids": [str(appointment_type.id)]}
    ).get_json()
    assert created["appointment_type_ids"] == [str(appointment_type.id)]
    _, events = _events(client.get(_feed_path(created["subscription_url"])))
    assert set(events) == {str(mot.id)}


def test_unknown_revoked_rotated_and_malformed_tokens_are_identical_404s(
    client, authenticated_client
):
    revoked = authenticated_client.post(FEEDS, json={"name": "r"}).get_json()
    authenticated_client.delete(f"{FEEDS}/{revoked['id']}")
    rotated = authenticated_client.post(FEEDS, json={"name": "o"}).get_json()
    authenticated_client.post(f"{FEEDS}/{rotated['id']}/regenerate")

    paths = [
        f"/api/calendar-feeds/{secrets.token_urlsafe(32)}.ics",
        _feed_path(revoked["subscription_url"]),
        _feed_path(rotated["subscription_url"]),
        "/api/calendar-feeds/not-a-token.ics",
        f"/api/calendar-feeds/{uuid.uuid4()}.ics",
    ]
    responses = [client.get(path) for path in paths]
    assert {r.status_code for r in responses} == {404}
    bodies = [{k: v for k, v in r.get_json().items() if k != "request_id"} for r in responses]
    assert all(body == bodies[0] for body in bodies)


def test_feed_never_serves_another_garages_data(
    client,
    second_authenticated_client,
    second_garage,
    second_user,
    second_customer,
    make_appointment,
    session,
):
    make_appointment(datetime.datetime.now(UTC) + datetime.timedelta(days=1))  # garage A
    foreign_type = GarageAppointmentType(garage_id=second_garage.id, name="Tyres", status="ACTIVE")
    session.add(foreign_type)
    session.commit()
    theirs = Appointment(
        garage_id=second_garage.id,
        employee_id=second_user.id,
        customer_id=second_customer.id,
        appointment_type_id=foreign_type.id,
        start_time=datetime.datetime.now(UTC) + datetime.timedelta(days=1),
        end_time=datetime.datetime.now(UTC) + datetime.timedelta(days=1, hours=1),
    )
    session.add(theirs)
    session.commit()

    created = second_authenticated_client.post(FEEDS, json={"name": "B", "scope": "ALL"}).get_json()
    calendar, events = _events(client.get(_feed_path(created["subscription_url"])))
    assert set(events) == {str(theirs.id)}
    assert str(calendar["X-WR-CALNAME"]) == "Garage B - B"


def test_deactivated_creator_stops_the_feed(client, staff_client, staff, session):
    created = staff_client.post(FEEDS, json={"name": "Mine"}).get_json()
    path = _feed_path(created["subscription_url"])
    assert client.get(path).status_code == 200

    staff.is_active = False
    session.commit()
    assert client.get(path).status_code == 404


def test_demoted_owner_feed_spanning_others_stops(client, authenticated_client, user, session):
    everyone = authenticated_client.post(FEEDS, json={"name": "All", "scope": "ALL"}).get_json()
    own = authenticated_client.post(FEEDS, json={"name": "Me", "scope": "ME"}).get_json()

    user.roles = []
    session.commit()

    assert client.get(_feed_path(everyone["subscription_url"])).status_code == 404
    # Their own calendar is still theirs to see.
    assert client.get(_feed_path(own["subscription_url"])).status_code == 200


def test_suspended_garage_feed_stops(client, authenticated_client, garage, session):
    created = authenticated_client.post(FEEDS, json={"name": "x"}).get_json()
    garage.status = GARAGE_STATUS_SUSPENDED
    session.commit()
    assert client.get(_feed_path(created["subscription_url"])).status_code == 404


def test_feed_rate_limit_is_keyed_per_feed_not_per_ip(app):
    # End-to-end 429s can't run here (see the NOTE at the end of
    # test_public_booking.py: the suite runs with RATELIMIT_ENABLED=False, and
    # turning it on means a second app instance). What matters for this
    # endpoint is the key: per feed, so Google's shared fetch addresses can't
    # make one business's feeds throttle another's - and never the raw token.
    from app.calendar_export.routes import CalendarFeedFetch, _feed_rate_limit_key

    first, second = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    keys = []
    for token in (first, first, second):
        with app.test_request_context(
            f"/api/calendar-feeds/{token}.ics", environ_base={"REMOTE_ADDR": "203.0.113.9"}
        ) as ctx:
            ctx.request.view_args = {"token": token}
            keys.append(_feed_rate_limit_key())

    assert keys[0] == keys[1] != keys[2]
    assert all(first not in key and second not in key for key in keys)
    assert "203.0.113.9" not in keys[0]
    assert app.config["CALENDAR_FEED_RATELIMIT"]
    assert CalendarFeedFetch.get.__wrapped__ is not None  # limiter-decorated


def test_feed_vehicle_registration_shown(
    client, authenticated_client, make_appointment, garage, customer, session
):
    vehicle = Vehicle(garage_id=garage.id, customer_id=customer.id, registration_number="ZZ99ZZZ")
    session.add(vehicle)
    appointment = make_appointment(datetime.datetime.now(UTC) + datetime.timedelta(days=1))
    appointment.vehicle_id = vehicle.id
    session.commit()

    created = authenticated_client.post(FEEDS, json={"name": "x"}).get_json()
    _, events = _events(client.get(_feed_path(created["subscription_url"])))
    assert str(events[str(appointment.id)]["SUMMARY"]) == "MOT - ZZ99ZZZ"
    db.session.rollback()
