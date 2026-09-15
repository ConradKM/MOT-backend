"""API tests for the configurable booking workflow.

/api/booking-flow/* (staff configuration), GET /api/public/<slug>/booking-flow
(what the booking page renders), and the answers half of
POST /api/public/<slug>/booking-requests.
"""

import datetime
import uuid

from app.models.booking_flow.field import BookingFlowField
from app.models.booking_flow.section import BookingFlowSection
from app.models.booking_request import BookingRequest
from app.models.vehicle import Vehicle


def _future_weekday(min_days=5):
    d = datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=min_days)
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d


FUTURE_DATE = _future_weekday()


def _payload(**overrides):
    """A submission carrying only what the platform itself requires - no
    vehicle keys at all. This is the shape a business that tracks nothing
    submits, and it must be enough on its own."""
    base = {
        "customer_first_name": "Ada",
        "customer_last_name": "Lovelace",
        "customer_email": "ada@example.com",
        "customer_phone": "07123456789",
        "preferred_date": FUTURE_DATE.isoformat(),
    }
    base.update(overrides)
    return base


def _section(session, garage, title="Extra details", appointment_type=None, order=0):
    s = BookingFlowSection(
        garage_id=garage.id,
        title=title,
        order=order,
        appointment_type_id=None if appointment_type is None else appointment_type.id,
    )
    session.add(s)
    session.commit()
    return s


def _field(session, garage, section, label="Question", **kwargs):
    kwargs.setdefault("field_type", "TEXT")
    kwargs.setdefault("options", [])
    f = BookingFlowField(
        garage_id=garage.id,
        booking_flow_section_id=section.id,
        label=label,
        **kwargs,
    )
    session.add(f)
    session.commit()
    return f


# --------------------------------------------------------------------------
# A business that tracks nothing
# --------------------------------------------------------------------------


def test_a_booking_with_no_tracked_item_succeeds(client, session, garage):
    """The headline of this change: a salon, a clinic, a studio - no vehicle,
    no registration, no item of any kind - can take a booking."""
    resp = client.post(f"/api/public/{garage.slug}/booking-requests", json=_payload())

    assert resp.status_code == 201
    request = session.query(BookingRequest).one()
    assert request.vehicle_registration is None
    assert request.vehicle_id is None
    # The customer account is still created - that part is never optional.
    assert request.customer_id is not None


def test_no_item_record_is_created_when_nothing_identifies_one(client, session, garage):
    client.post(f"/api/public/{garage.slug}/booking-requests", json=_payload())

    assert session.query(Vehicle).count() == 0


def test_the_legacy_client_shape_still_works(client, session, garage):
    """The pre-workflow booking page posts top-level vehicle_* keys. It has to
    keep working until the new one ships."""
    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(vehicle_registration="AB12 CDE", vehicle_make="Ford"),
    )

    assert resp.status_code == 201
    assert session.query(Vehicle).one().make == "Ford"


# --------------------------------------------------------------------------
# Answers: validation
# --------------------------------------------------------------------------


def test_a_missing_required_answer_is_rejected(client, session, garage):
    section = _section(session, garage)
    _field(session, garage, section, "Hair length", is_required=True)

    resp = client.post(f"/api/public/{garage.slug}/booking-requests", json=_payload())

    assert resp.status_code == 422
    assert session.query(BookingRequest).count() == 0


def test_a_required_answer_that_is_only_whitespace_is_rejected(client, session, garage):
    section = _section(session, garage)
    field = _field(session, garage, section, "Hair length", is_required=True)

    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(answers=[{"field_id": str(field.id), "value": "   "}]),
    )

    assert resp.status_code == 422


def test_an_unknown_field_id_is_rejected(client, session, garage):
    """Silently dropping it would hide a stale or probing client."""
    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(answers=[{"field_id": str(uuid.uuid4()), "value": "x"}]),
    )

    assert resp.status_code == 422


def test_a_field_from_another_business_is_rejected(client, session, garage, second_garage):
    foreign_section = _section(session, second_garage)
    foreign_field = _field(session, second_garage, foreign_section, "Theirs")

    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(answers=[{"field_id": str(foreign_field.id), "value": "x"}]),
    )

    assert resp.status_code == 422


def test_a_select_answer_must_be_one_of_the_options(client, session, garage):
    section = _section(session, garage)
    field = _field(
        session, garage, section, "Length", field_type="SELECT", options=["Short", "Long"]
    )

    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(answers=[{"field_id": str(field.id), "value": "Medium"}]),
    )

    assert resp.status_code == 422


def test_a_number_answer_is_range_checked(client, session, garage):
    section = _section(session, garage)
    field = _field(
        session, garage, section, "Guests", field_type="NUMBER", min_value=1, max_value=8
    )

    too_many = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(answers=[{"field_id": str(field.id), "value": "9"}]),
    )
    not_a_number = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(answers=[{"field_id": str(field.id), "value": "lots"}]),
    )

    assert too_many.status_code == 422
    assert not_a_number.status_code == 422


def test_a_date_answer_must_be_a_date(client, session, garage):
    section = _section(session, garage)
    field = _field(session, garage, section, "Event date", field_type="DATE")

    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(answers=[{"field_id": str(field.id), "value": "next tuesday"}]),
    )

    assert resp.status_code == 422


def test_a_multi_select_answer_must_be_from_the_options(client, session, garage):
    section = _section(session, garage)
    field = _field(
        session,
        garage,
        section,
        "Add-ons",
        field_type="MULTI_SELECT",
        options=["Wash", "Wax"],
    )

    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(answers=[{"field_id": str(field.id), "values": ["Wash", "Polish"]}]),
    )

    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Answers: storage
# --------------------------------------------------------------------------


def test_answers_are_stored_in_the_order_the_customer_saw_them(client, session, garage):
    section = _section(session, garage, "About you")
    second = _field(session, garage, section, "Second", order=1)
    first = _field(session, garage, section, "First", order=0)

    client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(
            answers=[
                {"field_id": str(second.id), "value": "b"},
                {"field_id": str(first.id), "value": "a"},
            ]
        ),
    )

    request = session.query(BookingRequest).one()
    assert [a.label for a in request.answers] == ["First", "Second"]
    assert [a.value for a in request.answers] == ["a", "b"]


def test_an_unanswered_optional_field_is_still_recorded(client, session, garage):
    """ "Asked and skipped" and "never asked" must not look the same to staff."""
    section = _section(session, garage)
    _field(session, garage, section, "Anything else")

    client.post(f"/api/public/{garage.slug}/booking-requests", json=_payload())

    request = session.query(BookingRequest).one()
    assert len(request.answers) == 1
    assert request.answers[0].value is None


def test_a_multi_select_answer_is_stored_as_a_list(client, session, garage):
    section = _section(session, garage)
    field = _field(
        session, garage, section, "Add-ons", field_type="MULTI_SELECT", options=["Wash", "Wax"]
    )

    client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(answers=[{"field_id": str(field.id), "values": ["Wax", "Wash"]}]),
    )

    answer = session.query(BookingRequest).one().answers[0]
    assert answer.value is None
    assert answer.value_list == ["Wax", "Wash"]


def test_answers_survive_the_field_being_renamed(client, session, garage):
    """The snapshot exists so a business renaming a question does not rewrite
    what a customer was asked weeks ago."""
    section = _section(session, garage)
    field = _field(session, garage, section, "Colour preference")

    client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(answers=[{"field_id": str(field.id), "value": "Ash blonde"}]),
    )

    field.label = "Shade"
    session.commit()
    session.expire_all()

    assert session.query(BookingRequest).one().answers[0].label == "Colour preference"


def test_answers_survive_the_field_being_deleted(client, session, garage):
    section = _section(session, garage)
    field = _field(session, garage, section, "Colour preference")

    client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(answers=[{"field_id": str(field.id), "value": "Ash blonde"}]),
    )

    session.delete(field)
    session.commit()
    session.expire_all()

    answer = session.query(BookingRequest).one().answers[0]
    assert answer.label == "Colour preference"
    assert answer.value == "Ash blonde"
    assert answer.booking_flow_field_id is None


# --------------------------------------------------------------------------
# Bindings
# --------------------------------------------------------------------------


def test_a_bound_field_populates_the_real_record(client, session, garage):
    """What keeps a business that *does* track an item - and everything built
    on those records - working after the form became configurable."""
    section = _section(session, garage, "Vehicle details")
    reference = _field(
        session, garage, section, "Registration", is_required=True, binds_to="ITEM_REFERENCE"
    )
    make = _field(session, garage, section, "Make", order=1, binds_to="ITEM_MAKE")
    year = _field(
        session, garage, section, "Year", order=2, field_type="NUMBER", binds_to="ITEM_YEAR"
    )

    client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(
            answers=[
                {"field_id": str(reference.id), "value": "AB12 CDE"},
                {"field_id": str(make.id), "value": "Ford"},
                {"field_id": str(year.id), "value": "2015"},
            ]
        ),
    )

    item = session.query(Vehicle).one()
    # Normalised by the model itself (Vehicle.normalize_registration_number),
    # exactly as it always has been - a binding changes where the value comes
    # from, never how the record stores it.
    assert item.registration_number == "AB12CDE"
    assert item.make == "Ford"
    assert item.year == 2015

    request = session.query(BookingRequest).one()
    assert request.vehicle_id == item.id
    # Denormalised onto the request too, so staff review reads what was
    # submitted even if the item is edited later.
    assert request.vehicle_registration == "AB12 CDE"


def test_an_unbound_field_creates_no_record(client, session, garage):
    section = _section(session, garage)
    field = _field(session, garage, section, "Anything else")

    client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(answers=[{"field_id": str(field.id), "value": "Please call first"}]),
    )

    assert session.query(Vehicle).count() == 0


def test_a_bound_answer_wins_over_the_legacy_key(client, session, garage):
    section = _section(session, garage)
    reference = _field(session, garage, section, "Registration", binds_to="ITEM_REFERENCE")

    client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(
            vehicle_registration="OLD123",
            answers=[{"field_id": str(reference.id), "value": "NEW456"}],
        ),
    )

    assert session.query(Vehicle).one().registration_number == "NEW456"


# --------------------------------------------------------------------------
# Resolution: business default vs per-service override
# --------------------------------------------------------------------------


def test_the_business_default_applies_when_a_service_has_no_override(
    client, session, garage, appointment_type
):
    section = _section(session, garage, "Business default")
    _field(session, garage, section, "Default question")

    body = client.get(
        f"/api/public/{garage.slug}/booking-flow",
        query_string={"appointment_type_id": str(appointment_type.id)},
    ).get_json()

    assert [s["title"] for s in body["sections"]] == ["Business default"]


def test_a_service_override_replaces_the_default_rather_than_adding_to_it(
    client, session, garage, appointment_type
):
    """The one rule about this model that can surprise a business, so it is
    pinned: an override is the whole form, not an addition to it."""
    default = _section(session, garage, "Business default")
    _field(session, garage, default, "Default question")
    override = _section(session, garage, "Just for this service", appointment_type=appointment_type)
    _field(session, garage, override, "Override question")

    body = client.get(
        f"/api/public/{garage.slug}/booking-flow",
        query_string={"appointment_type_id": str(appointment_type.id)},
    ).get_json()

    assert [s["title"] for s in body["sections"]] == ["Just for this service"]


def test_an_override_does_not_leak_into_another_service(client, session, garage, appointment_type):
    from app.models.appointments.appointment_type import GarageAppointmentType

    other = GarageAppointmentType(garage_id=garage.id, name="Other", status="ACTIVE")
    session.add(other)
    session.commit()

    default = _section(session, garage, "Business default")
    _field(session, garage, default, "Default question")
    override = _section(session, garage, "Just for this service", appointment_type=appointment_type)
    _field(session, garage, override, "Override question")

    body = client.get(
        f"/api/public/{garage.slug}/booking-flow",
        query_string={"appointment_type_id": str(other.id)},
    ).get_json()

    assert [s["title"] for s in body["sections"]] == ["Business default"]


def test_an_inactive_section_is_not_asked(client, session, garage):
    section = _section(session, garage, "Paused")
    _field(session, garage, section, "Question")
    section.is_active = False
    session.commit()

    body = client.get(f"/api/public/{garage.slug}/booking-flow").get_json()

    assert body["sections"] == []


def test_a_required_field_in_an_inactive_section_does_not_block_a_booking(client, session, garage):
    section = _section(session, garage, "Paused")
    _field(session, garage, section, "Question", is_required=True)
    section.is_active = False
    session.commit()

    resp = client.post(f"/api/public/{garage.slug}/booking-requests", json=_payload())

    assert resp.status_code == 201


def test_the_public_flow_never_exposes_bindings(client, session, garage):
    """Which internal record an answer populates is the business's concern,
    never the customer's."""
    section = _section(session, garage)
    _field(session, garage, section, "Registration", binds_to="ITEM_REFERENCE")

    body = client.get(f"/api/public/{garage.slug}/booking-flow").get_json()

    assert "binds_to" not in body["sections"][0]["fields"][0]


# --------------------------------------------------------------------------
# Staff configuration
# --------------------------------------------------------------------------


def test_create_a_section_and_a_field(authenticated_user):
    section = authenticated_user.client.post(
        "/api/booking-flow/sections", json={"title": "About your pet"}
    ).get_json()

    resp = authenticated_user.client.post(
        f"/api/booking-flow/sections/{section['id']}/fields",
        json={"label": "Breed", "field_type": "TEXT", "is_required": True},
    )

    assert resp.status_code == 201
    assert resp.get_json()["label"] == "Breed"


def test_a_select_field_needs_options(authenticated_user):
    section = authenticated_user.client.post(
        "/api/booking-flow/sections", json={"title": "Details"}
    ).get_json()

    resp = authenticated_user.client.post(
        f"/api/booking-flow/sections/{section['id']}/fields",
        json={"label": "Size", "field_type": "SELECT", "options": []},
    )

    assert resp.status_code == 422


def test_a_numeric_binding_cannot_be_put_on_a_text_field(authenticated_user):
    """It would write non-numeric text into a real integer column."""
    section = authenticated_user.client.post(
        "/api/booking-flow/sections", json={"title": "Details"}
    ).get_json()

    resp = authenticated_user.client.post(
        f"/api/booking-flow/sections/{section['id']}/fields",
        json={"label": "Year", "field_type": "TEXT", "binds_to": "ITEM_YEAR"},
    )

    assert resp.status_code == 422


def test_staff_cannot_configure_the_workflow(authenticated_user, session):
    authenticated_user.user.roles = []
    session.commit()

    resp = authenticated_user.client.post(
        "/api/booking-flow/sections", json={"title": "About your pet"}
    )

    assert resp.status_code == 403


def test_sections_from_another_business_are_not_listed(authenticated_user, session, second_garage):
    _section(session, authenticated_user.garage, "Mine")
    _section(session, second_garage, "Theirs")

    listed = authenticated_user.client.get("/api/booking-flow/sections").get_json()

    assert [s["title"] for s in listed] == ["Mine"]


def test_a_section_cannot_be_scoped_to_another_businesss_service(
    authenticated_user, session, second_garage
):
    from app.models.appointments.appointment_type import GarageAppointmentType

    foreign = GarageAppointmentType(garage_id=second_garage.id, name="Theirs", status="ACTIVE")
    session.add(foreign)
    session.commit()

    resp = authenticated_user.client.post(
        "/api/booking-flow/sections",
        json={"title": "Mine", "appointment_type_id": str(foreign.id)},
    )

    assert resp.status_code == 422


def test_reorder_fields_within_a_section(authenticated_user, session):
    garage = authenticated_user.garage
    section = _section(session, garage)
    a = _field(session, garage, section, "A", order=0)
    b = _field(session, garage, section, "B", order=1)

    resp = authenticated_user.client.put(
        f"/api/booking-flow/sections/{section.id}/fields/order",
        json={"ids": [str(b.id), str(a.id)]},
    )

    assert resp.status_code == 204
    session.expire_all()
    assert session.get(BookingFlowField, b.id).order == 0


def test_reorder_rejects_sections_from_two_different_workflows(
    authenticated_user, session, appointment_type
):
    """Ordering is only meaningful within one workflow."""
    garage = authenticated_user.garage
    default = _section(session, garage, "Default")
    override = _section(session, garage, "Override", appointment_type=appointment_type)

    resp = authenticated_user.client.put(
        "/api/booking-flow/sections/order",
        json={"ids": [str(default.id), str(override.id)]},
    )

    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------


def test_applying_a_preset_seeds_an_editable_workflow(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/booking-flow/presets", json={"preset": "automotive"}
    )

    assert resp.status_code == 201
    titles = [s["title"] for s in resp.get_json()]
    assert "Vehicle details" in titles
    # Seeded as ordinary configuration - the reference field is bound, so an
    # automotive business keeps its item records.
    fields = resp.get_json()[0]["fields"]
    assert any(f["binds_to"] == "ITEM_REFERENCE" for f in fields)


def test_the_appointments_preset_binds_nothing(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/booking-flow/presets", json={"preset": "appointments"}
    )

    assert resp.status_code == 201
    every_field = [f for s in resp.get_json() for f in s["fields"]]
    assert every_field
    assert all(f["binds_to"] is None for f in every_field)


def test_a_preset_is_refused_when_a_workflow_already_exists(authenticated_user, session):
    _section(session, authenticated_user.garage, "Already here")

    resp = authenticated_user.client.post("/api/booking-flow/presets", json={"preset": "generic"})

    assert resp.status_code == 409


def test_an_unknown_preset_is_422(authenticated_user):
    resp = authenticated_user.client.post("/api/booking-flow/presets", json={"preset": "dental"})

    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Staff review
# --------------------------------------------------------------------------


def test_the_review_screen_shows_the_answers(authenticated_user, client, session):
    garage = authenticated_user.garage
    section = _section(session, garage, "About you")
    field = _field(session, garage, section, "Hair length")

    client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(answers=[{"field_id": str(field.id), "value": "Shoulder"}]),
    )

    listed = authenticated_user.client.get("/api/booking-requests/").get_json()

    assert listed[0]["answers"][0]["label"] == "Hair length"
    assert listed[0]["answers"][0]["value"] == "Shoulder"
    assert listed[0]["answers_collected"] is True


def test_a_conversational_request_is_flagged_as_not_collected(authenticated_user, session):
    """Staff must be able to tell "this business asks nothing" apart from
    "this channel could not ask"."""
    garage = authenticated_user.garage
    session.add(
        BookingRequest(
            garage_id=garage.id,
            status="PENDING",
            source="CONVERSATION",
            customer_first_name="Phone",
            customer_last_name="Caller",
            preferred_date=FUTURE_DATE,
        )
    )
    session.commit()

    listed = authenticated_user.client.get("/api/booking-requests/").get_json()

    assert listed[0]["answers"] == []
    assert listed[0]["answers_collected"] is False


def test_a_web_request_defaults_to_the_web_source(client, session, garage):
    client.post(f"/api/public/{garage.slug}/booking-requests", json=_payload())

    assert session.query(BookingRequest).one().source == "WEB"
