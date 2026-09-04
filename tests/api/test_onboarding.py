"""Developer-controlled onboarding: slug generation, the transactional
onboarding service, structured-spec parsing, the ``flask onboard-garage`` CLI,
the HTTP on/off switch, and the platform-vs-operational settings split.

Covers ConradKM/MOT-backend#30.
"""

import json
import re

import pytest

from app.garages import slug as slug_mod
from app.garages.layouts import (
    DEFAULT_LAYOUT,
    LAYOUT_VARIANTS,
    resolve_layout,
    validate_layout_variant,
)
from app.garages.onboarding import (
    GarageSpec,
    OnboardingEmailInUse,
    OnboardingError,
    OwnerSpec,
    load_spec_file,
    onboard_garage,
    parse_spec,
)
from app.models.appointments.appointment_status import GarageAppointmentStatus
from app.models.employee import Employee
from app.models.garage import Garage
from app.models.garage_schedule import GarageOpeningHours, GarageScheduleSettings
from app.models.role import Role

VALID_GARAGE = {"name": "Kingsway MOT & Service Centre"}
VALID_OWNER = {
    "email": "owner@kingswaymot.example",
    "password": "s3cret-pass-1",
    "first_name": "Jo",
    "last_name": "Kingsway",
}


def _garage_spec(**over):
    return GarageSpec(**{**VALID_GARAGE, **over})


def _owner_spec(**over):
    return OwnerSpec(**{**VALID_OWNER, **over})


# --------------------------------------------------------------------------
# Randomised, immutable slug
# --------------------------------------------------------------------------


def test_slugify_is_lowercase_hyphenated_and_trimmed():
    assert slug_mod.slugify("  Bob's Tyres & Exhausts!  ") == "bob-s-tyres-exhausts"
    assert slug_mod.slugify("!!!") == "garage"


def test_slugify_unique_is_stem_plus_random_suffix(session):
    result = slug_mod.slugify_unique("Kingsway MOT & Service Centre", session)

    assert re.fullmatch(r"kingsway-mot-service-centre-[a-z0-9]{6}", result)


def test_slugify_unique_suffix_uses_the_unambiguous_alphabet(session):
    suffixes = {slug_mod.slugify_unique("X", session).rsplit("-", 1)[1] for _ in range(30)}

    for suffix in suffixes:
        assert set(suffix) <= set(slug_mod._SUFFIX_ALPHABET)
        assert not (set(suffix) & set("01ilo"))


def test_slugify_unique_two_calls_same_name_differ(session):
    a = slug_mod.slugify_unique("City Motors", session)
    b = slug_mod.slugify_unique("City Motors", session)

    assert a != b
    assert a.startswith("city-motors-") and b.startswith("city-motors-")


def test_slugify_unique_total_length_capped_at_120(session):
    result = slug_mod.slugify_unique("A" * 400, session)

    assert len(result) <= 120


def test_slugify_unique_retries_on_a_collision(session, monkeypatch):
    existing = Garage(name="Taken", slug="city-motors-aaaaaa")
    session.add(existing)
    session.flush()

    # First draw collides with the row above; second is free.
    draws = iter(["aaaaaa", "bbbbbb"])
    monkeypatch.setattr(slug_mod, "random_suffix", lambda *a, **k: next(draws))

    assert slug_mod.slugify_unique("City Motors", session) == "city-motors-bbbbbb"


# --------------------------------------------------------------------------
# onboard_garage(): one transaction, all-or-nothing
# --------------------------------------------------------------------------


def test_onboard_creates_garage_owner_roles_and_defaults(session):
    result = onboard_garage(garage=_garage_spec(), owner=_owner_spec())

    garage = result.garage
    assert garage.id is not None
    assert garage.name == "Kingsway MOT & Service Centre"
    assert garage.slug.startswith("kingsway-mot-service-centre-")

    owner = result.owner
    assert owner.email == VALID_OWNER["email"]
    assert owner.garage_id == garage.id
    assert owner.is_active is True
    assert owner.has_role("OWNER")

    role_names = {r.name for r in Role.query.filter_by(garage_id=garage.id)}
    assert role_names == {"OWNER", "STAFF"}

    assert GarageAppointmentStatus.query.filter_by(garage_id=garage.id).count() == 7
    assert GarageScheduleSettings.query.filter_by(garage_id=garage.id).count() == 1
    assert GarageOpeningHours.query.filter_by(garage_id=garage.id).count() == 7


def test_onboard_never_stores_the_plaintext_password(session):
    result = onboard_garage(garage=_garage_spec(), owner=_owner_spec())

    assert result.owner.password_hash != VALID_OWNER["password"]
    assert VALID_OWNER["password"] not in result.owner.password_hash


def test_onboard_defaults_layout_variant_to_null(session):
    result = onboard_garage(garage=_garage_spec(), owner=_owner_spec())
    assert result.garage.layout_variant is None


def test_onboard_accepts_a_registered_layout_variant(session):
    result = onboard_garage(
        garage=_garage_spec(layout_variant="default"), owner=_owner_spec()
    )
    assert result.garage.layout_variant == "default"


def test_onboard_rejects_an_unknown_layout_variant(session):
    with pytest.raises(OnboardingError):
        onboard_garage(
            garage=_garage_spec(layout_variant="bespoke-nope"), owner=_owner_spec()
        )
    assert Garage.query.count() == 0


def test_onboard_rejects_a_weak_owner_password(session):
    with pytest.raises(OnboardingError):
        onboard_garage(garage=_garage_spec(), owner=_owner_spec(password="short"))
    assert Garage.query.count() == 0


def test_onboard_rejects_an_invalid_owner_email(session):
    with pytest.raises(OnboardingError):
        onboard_garage(garage=_garage_spec(), owner=_owner_spec(email="not-an-email"))
    assert Garage.query.count() == 0


def test_onboard_rejects_a_blank_garage_name(session):
    with pytest.raises(OnboardingError):
        onboard_garage(garage=_garage_spec(name="   "), owner=_owner_spec())
    assert Garage.query.count() == 0


def test_onboard_duplicate_owner_email_leaves_no_orphan_garage(session):
    onboard_garage(garage=_garage_spec(), owner=_owner_spec())
    before = Garage.query.count()

    with pytest.raises(OnboardingEmailInUse):
        onboard_garage(
            garage=_garage_spec(name="Second Garage"), owner=_owner_spec()
        )

    assert Garage.query.count() == before
    assert Garage.query.filter_by(name="Second Garage").first() is None


def test_onboard_rolls_back_completely_on_a_late_failure(session, monkeypatch):
    import app.garages.onboarding as onboarding_mod

    def boom(**_kwargs):
        raise RuntimeError("simulated failure after the garage row was flushed")

    monkeypatch.setattr(onboarding_mod, "build_employee_account", boom)

    with pytest.raises(RuntimeError):
        onboard_garage(garage=_garage_spec(), owner=_owner_spec())

    assert Garage.query.count() == 0
    assert Role.query.count() == 0
    assert GarageScheduleSettings.query.count() == 0


def test_onboard_two_tenants_are_isolated(session):
    a = onboard_garage(garage=_garage_spec(name="Alpha Autos"), owner=_owner_spec())
    b = onboard_garage(
        garage=_garage_spec(name="Beta Motors"),
        owner=_owner_spec(email="owner@beta.example"),
    )

    assert a.garage.id != b.garage.id
    assert a.garage.slug != b.garage.slug
    assert a.owner.garage_id == a.garage.id
    assert b.owner.garage_id == b.garage.id
    assert (
        Role.query.filter_by(garage_id=a.garage.id, name="OWNER").count() == 1
    )


# --------------------------------------------------------------------------
# Structured spec input (JSON/YAML); the slug is never part of a spec
# --------------------------------------------------------------------------


def test_parse_spec_accepts_the_nested_shape():
    garage, owner = parse_spec(
        {
            "garage": {"name": "Nested Garage", "phone": "+44 20 7946 0000"},
            "owner": {"email": "o@nested.example", "password": "longenough1"},
        }
    )
    assert garage.name == "Nested Garage"
    assert garage.phone == "+44 20 7946 0000"
    assert owner.email == "o@nested.example"


def test_parse_spec_accepts_the_flat_shape():
    garage, owner = parse_spec(
        {"name": "Flat Garage", "owner": {"email": "o@flat.example", "password": "longenough1"}}
    )
    assert garage.name == "Flat Garage"
    assert owner.password == "longenough1"


def test_parse_spec_rejects_a_top_level_slug():
    with pytest.raises(OnboardingError):
        parse_spec(
            {"name": "X", "slug": "hand-picked", "owner": {"email": "a@b.co", "password": "longenough1"}}
        )


def test_parse_spec_rejects_a_nested_slug_or_id():
    with pytest.raises(OnboardingError):
        parse_spec(
            {
                "garage": {"name": "X", "slug": "hand-picked"},
                "owner": {"email": "a@b.co", "password": "longenough1"},
            }
        )
    with pytest.raises(OnboardingError):
        parse_spec(
            {
                "garage": {"name": "X", "id": "00000000-0000-0000-0000-000000000000"},
                "owner": {"email": "a@b.co", "password": "longenough1"},
            }
        )


def test_parse_spec_requires_owner_and_name():
    with pytest.raises(OnboardingError):
        parse_spec({"name": "No Owner Here"})
    with pytest.raises(OnboardingError):
        parse_spec({"owner": {"email": "a@b.co", "password": "longenough1"}})


def test_load_spec_file_reads_json(tmp_path):
    spec = tmp_path / "new_garage.json"
    spec.write_text(
        json.dumps(
            {
                "garage": {"name": "File Garage"},
                "owner": {"email": "o@file.example", "password": "longenough1"},
            }
        ),
        encoding="utf-8",
    )

    garage, owner = load_spec_file(spec)
    assert garage.name == "File Garage"
    assert owner.email == "o@file.example"


def test_load_spec_file_rejects_invalid_json(tmp_path):
    spec = tmp_path / "broken.json"
    spec.write_text("{not json", encoding="utf-8")

    with pytest.raises(OnboardingError):
        load_spec_file(spec)


# --------------------------------------------------------------------------
# flask onboard-garage
# --------------------------------------------------------------------------


def _write_spec(tmp_path, **over):
    body = {
        "garage": {"name": "CLI Garage", **over.get("garage", {})},
        "owner": {
            "email": over.get("email", "owner@cli.example"),
            "password": "longenough1",
        },
    }
    if "slug" in over:
        body["garage"]["slug"] = over["slug"]
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_cli_onboards_from_a_spec_file(app, session, tmp_path):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["onboard-garage", "--file", str(_write_spec(tmp_path))])

    assert result.exit_code == 0, result.output
    garage = Garage.query.filter_by(name="CLI Garage").first()
    assert garage is not None
    assert garage.slug in result.output
    assert Employee.query.filter_by(email="owner@cli.example").first().has_role("OWNER")


def test_cli_dry_run_writes_nothing(app, session, tmp_path):
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=["onboard-garage", "--file", str(_write_spec(tmp_path)), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert Garage.query.count() == 0


def test_cli_rejects_a_spec_with_a_slug_and_writes_nothing(app, session, tmp_path):
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=["onboard-garage", "--file", str(_write_spec(tmp_path, slug="hand-picked"))]
    )

    assert result.exit_code != 0
    assert Garage.query.count() == 0


def test_cli_onboards_from_inline_options(app, session):
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=[
            "onboard-garage",
            "--name", "Inline Garage",
            "--owner-email", "owner@inline.example",
            "--owner-password", "longenough1",
        ]
    )

    assert result.exit_code == 0, result.output
    assert Garage.query.filter_by(name="Inline Garage").first() is not None


def test_cli_requires_file_or_inline_details(app, session):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["onboard-garage", "--name", "Only A Name"])

    assert result.exit_code != 0
    assert Garage.query.count() == 0


# --------------------------------------------------------------------------
# HTTP onboarding: same service, switchable
# --------------------------------------------------------------------------


def test_http_register_still_onboards_by_default(client, session):
    resp = client.post(
        "/api/auth/register",
        json={
            "garage_name": "HTTP Garage",
            "email": "owner@http.example",
            "password": "longenough1",
        },
    )

    assert resp.status_code == 201
    assert resp.get_json()["access_token"]
    assert Garage.query.filter_by(name="HTTP Garage").first() is not None


def test_http_register_blocked_when_onboarding_http_disabled(client, session, app):
    app.config["ONBOARDING_HTTP_ENABLED"] = False
    try:
        resp = client.post(
            "/api/auth/register",
            json={
                "garage_name": "Blocked Garage",
                "email": "owner@blocked.example",
                "password": "longenough1",
            },
        )
    finally:
        app.config["ONBOARDING_HTTP_ENABLED"] = True

    assert resp.status_code == 404
    assert Garage.query.filter_by(name="Blocked Garage").first() is None


# --------------------------------------------------------------------------
# Platform-controlled vs operational settings
# --------------------------------------------------------------------------


def test_get_garage_exposes_layout_variant(authenticated_user):
    body = authenticated_user.client.get("/api/garage").get_json()
    assert "layout_variant" in body


def test_patch_garage_rejects_layout_variant(authenticated_user):
    resp = authenticated_user.client.patch(
        "/api/garage", json={"layout_variant": "bespoke"}
    )
    assert resp.status_code == 422
    assert "layout_variant" in resp.get_json()["errors"]["json"]


def test_patch_garage_rejects_slug(authenticated_user):
    resp = authenticated_user.client.patch("/api/garage", json={"slug": "new-slug"})
    assert resp.status_code == 422
    assert "slug" in resp.get_json()["errors"]["json"]


def test_renaming_a_garage_does_not_change_its_slug(authenticated_user, session):
    original_slug = authenticated_user.garage.slug

    authenticated_user.client.patch("/api/garage", json={"name": "Totally New Name"})

    session.refresh(authenticated_user.garage)
    assert authenticated_user.garage.name == "Totally New Name"
    assert authenticated_user.garage.slug == original_slug


# --------------------------------------------------------------------------
# Layout registry - resolved by key, never by garage identity
# --------------------------------------------------------------------------


def test_layout_registry_is_keyed_by_strings():
    assert all(isinstance(k, str) for k in LAYOUT_VARIANTS)
    assert DEFAULT_LAYOUT in LAYOUT_VARIANTS


def test_resolve_layout_falls_back_to_default():
    assert resolve_layout(GarageSpec(name="x", layout_variant=None)) == DEFAULT_LAYOUT
    assert resolve_layout(GarageSpec(name="x", layout_variant="ghost")) == DEFAULT_LAYOUT
    assert resolve_layout(GarageSpec(name="x", layout_variant="default")) == "default"


def test_validate_layout_variant_allows_none_and_registered_only():
    validate_layout_variant(None)
    validate_layout_variant("default")
    with pytest.raises(ValueError):
        validate_layout_variant("not-registered")
