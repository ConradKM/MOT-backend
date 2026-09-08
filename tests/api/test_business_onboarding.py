"""Reusable, idempotent business onboarding (app/garages/business_onboarding.py
+ scripts/onboard_business.py). Covers ConradKM/MOT-backend#52."""

import json

import pytest
from werkzeug.security import check_password_hash

from app.garages.business_onboarding import (
    BusinessSpecError,
    onboard_business,
    parse_business_spec,
    reset_owner_password,
)
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.employee import Employee
from app.models.garage import Garage
from app.models.garage_schedule import GarageOpeningHours

TEMP_PW = "temp-pw-abc123"

SPEC = {
    "business": {
        "name": "Tints on Demand",
        "phone": "+44 20 3971 1619",
        "address": "London, E16 2ES",
        "postcode": "E16 2ES",
    },
    "owner": {"email": "owner@tints.example", "first_name": "Mubz"},
    "services": [
        {"name": "Window tints", "base_price": "120.00", "default_duration_minutes": 60},
        {"name": "Ceramic coating", "default_duration_minutes": 90},
    ],
    "opening_hours": {
        "mon": ["08:00", "17:00"],
        "sat": None,
        "sun": None,
    },
    "notes": "demo",
}


# --------------------------------------------------------------------------
# Parsing / validation (pure)
# --------------------------------------------------------------------------


def test_parse_valid_spec():
    spec = parse_business_spec(SPEC)
    assert spec.name == "Tints on Demand"
    assert spec.owner_email == "owner@tints.example"
    assert [s.name for s in spec.services] == ["Window tints", "Ceramic coating"]
    assert spec.opening_hours[0] == ("08:00", "17:00")  # Monday
    assert spec.opening_hours[5] is None  # Saturday closed


def test_spec_with_a_slug_or_password_is_rejected():
    for bad in ({"business": {"slug": "x"}}, {"owner": {"password": "x"}}):
        raw = {**SPEC, **{k: {**SPEC.get(k, {}), **v} for k, v in bad.items()}}
        with pytest.raises(BusinessSpecError):
            parse_business_spec(raw)


def test_spec_rejects_bad_email_bad_price_bad_time_and_dupes():
    with pytest.raises(BusinessSpecError):
        parse_business_spec({**SPEC, "owner": {"email": "not-an-email"}})
    with pytest.raises(BusinessSpecError):
        parse_business_spec({**SPEC, "services": [{"name": "x", "base_price": "-5"}]})
    with pytest.raises(BusinessSpecError):
        parse_business_spec({**SPEC, "opening_hours": {"mon": ["25:00", "26:00"]}})
    with pytest.raises(BusinessSpecError):
        parse_business_spec({**SPEC, "services": [{"name": "A"}, {"name": "a"}]})


# --------------------------------------------------------------------------
# Creation + idempotency (needs the DB)
# --------------------------------------------------------------------------


def test_onboard_creates_business_owner_services_and_custom_hours(app, session):
    result = onboard_business(parse_business_spec(SPEC), temp_password=TEMP_PW)

    assert result.created is True
    assert result.temp_password == TEMP_PW
    assert result.garage.slug.startswith("tints-on-demand-")

    owner = session.query(Employee).filter_by(email="owner@tints.example").one()
    assert owner.has_role("OWNER")
    assert check_password_hash(owner.password_hash, TEMP_PW)

    names = {
        t.name for t in session.query(GarageAppointmentType).filter_by(garage_id=result.garage.id)
    }
    assert names == {"Window tints", "Ceramic coating"}

    hours = {
        h.weekday: h
        for h in session.query(GarageOpeningHours).filter_by(garage_id=result.garage.id)
    }
    assert hours[0].opens_at.strftime("%H:%M") == "08:00" and hours[0].is_closed is False
    assert hours[5].is_closed is True  # Saturday
    # Tuesday was not in the spec -> keeps the seeded default.
    assert hours[1].opens_at.strftime("%H:%M") == "09:00"


def test_rerunning_the_same_spec_is_a_no_op(app, session):
    first = onboard_business(parse_business_spec(SPEC), temp_password=TEMP_PW)
    second = onboard_business(parse_business_spec(SPEC), temp_password="different-pw-99")

    assert second.created is False
    assert second.temp_password is None
    assert second.garage.id == first.garage.id
    assert session.query(Garage).filter_by(name="Tints on Demand").count() == 1
    assert session.query(Employee).filter_by(email="owner@tints.example").count() == 1
    # The second run's password was ignored - the original still works.
    owner = session.query(Employee).filter_by(email="owner@tints.example").one()
    assert check_password_hash(owner.password_hash, TEMP_PW)


def test_default_hours_are_kept_when_the_spec_omits_them(app, session):
    spec = parse_business_spec({**SPEC, "opening_hours": None})
    result = onboard_business(spec, temp_password=TEMP_PW)

    assert result.used_default_hours is True
    hours = session.query(GarageOpeningHours).filter_by(garage_id=result.garage.id).all()
    weekdays_open = {h.weekday for h in hours if not h.is_closed}
    assert weekdays_open == {0, 1, 2, 3, 4}  # Mon-Fri default


def test_reset_owner_password_sets_a_new_hash_and_invalidates_sessions(app, session):
    result = onboard_business(parse_business_spec(SPEC), temp_password=TEMP_PW)
    reset_owner_password(result.owner, "brand-new-pw-1")

    owner = session.query(Employee).filter_by(email="owner@tints.example").one()
    assert check_password_hash(owner.password_hash, "brand-new-pw-1")
    assert not check_password_hash(owner.password_hash, TEMP_PW)
    assert owner.tokens_valid_from is not None


# --------------------------------------------------------------------------
# The CLI
# --------------------------------------------------------------------------


def test_cli_validate_is_offline_and_reports_ok(tmp_path, capsys):
    from scripts.onboard_business import main

    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(SPEC), encoding="utf-8")

    rc = main([str(spec_file), "--validate"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "spec is valid" in out
    assert "Window tints" in out


def test_cli_rejects_a_bad_spec(tmp_path, capsys):
    from scripts.onboard_business import main

    spec_file = tmp_path / "bad.json"
    spec_file.write_text(json.dumps({"business": {"name": ""}, "owner": {}}), encoding="utf-8")

    rc = main([str(spec_file), "--validate"])
    assert rc == 1
