"""The local-development helpers: `flask seed-dev`, `flask dev-info`, the
idempotent slug-preserving re-seed, and the development-only gate.

Covers ConradKM/MOT-backend#41.
"""

import pytest
from werkzeug.security import check_password_hash

from app.dev.guard import NotDevelopmentError, require_development
from app.dev.info import collect_local_garages
from app.dev.seed import (
    DEV_ACCOUNTS,
    DEV_GARAGE_NAME,
    DEV_LEGACY_NAMES,
    DEV_PASSWORD,
    DEV_PRIMARY_ACCOUNT,
    seed_dev_garage,
)
from app.extensions import db
from app.garages.onboarding import GarageSpec, OwnerSpec, onboard_garage
from app.models.employee import Employee
from app.models.garage import Garage

# --------------------------------------------------------------------------
# seed-dev: idempotent, and it does not churn the random slug
# --------------------------------------------------------------------------


def test_first_seed_creates_one_garage_with_a_randomised_slug(app):
    result = seed_dev_garage()

    assert result.created is True
    assert result.name == DEV_GARAGE_NAME
    garages = Garage.query.filter_by(name=DEV_GARAGE_NAME).all()
    assert len(garages) == 1
    # slugify_unique: "<stem>-<6 char suffix>"
    assert garages[0].slug.startswith("test-garage-101-")
    assert garages[0].slug != "test-garage-101-"


def test_reseed_reuses_the_same_garage_id_and_slug(app):
    first = seed_dev_garage()
    second = seed_dev_garage()
    third = seed_dev_garage()

    assert first.created is True
    assert second.created is False and third.created is False
    assert second.garage_id == first.garage_id == third.garage_id
    assert second.slug == first.slug == third.slug
    assert Garage.query.filter_by(name=DEV_GARAGE_NAME).count() == 1
    # No duplicate accounts either.
    assert Employee.query.filter_by(email=DEV_PRIMARY_ACCOUNT).count() == 1


def test_reseed_adopts_a_business_still_under_a_previous_name(app, session):
    # Simulate a database seeded before the rename.
    first = seed_dev_garage()
    legacy_name = DEV_LEGACY_NAMES[0]
    garage = db.session.get(Garage, first.garage_id)
    garage.name = legacy_name
    session.commit()

    second = seed_dev_garage()

    assert second.created is False
    assert second.garage_id == first.garage_id  # same row, renamed in place
    assert second.slug == first.slug  # slug survives the rename
    assert Garage.query.filter_by(name=DEV_GARAGE_NAME).count() == 1
    assert Garage.query.filter_by(name=legacy_name).count() == 0
    assert Employee.query.filter_by(email=DEV_PRIMARY_ACCOUNT).count() == 1


def test_reseed_resets_the_dev_password(app):
    seed_dev_garage()
    owner = Employee.query.filter_by(email=DEV_PRIMARY_ACCOUNT).one()
    owner.password_hash = "not-a-valid-hash"
    db.session.commit()

    seed_dev_garage()

    owner = Employee.query.filter_by(email=DEV_PRIMARY_ACCOUNT).one()
    assert check_password_hash(owner.password_hash, DEV_PASSWORD)


def test_fresh_mints_a_new_slug(app):
    first = seed_dev_garage()
    after_fresh = seed_dev_garage(fresh=True)

    assert after_fresh.created is True
    assert after_fresh.slug != first.slug
    assert Garage.query.filter_by(name=DEV_GARAGE_NAME).count() == 1


# --------------------------------------------------------------------------
# dev-info: correct booking identifier, seeded logins, no leakage
# --------------------------------------------------------------------------


def test_dev_info_booking_url_uses_the_garage_uuid_not_the_slug(app):
    result = seed_dev_garage()
    [info] = collect_local_garages()

    assert info.booking_url.endswith(f"/book/{result.garage_id}")
    assert result.slug not in info.booking_url
    # The public API, by contrast, is slug-addressed.
    assert info.public_api_url.endswith(f"/api/public/{result.slug}")
    assert info.login_url.endswith("/login")


def test_dev_info_shows_the_seeded_login_with_its_known_password(app):
    seed_dev_garage()
    [info] = collect_local_garages()

    assert info.is_seeded is True
    by_email = {a.email: a for a in info.accounts}
    assert set(by_email) == {email for email, _ in DEV_ACCOUNTS}
    assert by_email[DEV_PRIMARY_ACCOUNT].password == DEV_PASSWORD


def test_dev_info_never_shows_a_password_for_an_onboarded_garage(app):
    seed_dev_garage()
    onboard_garage(
        garage=GarageSpec(name="Bob's Autocentre"),
        owner=OwnerSpec(email="bob@bobs-autocentre.example", password="s3cret-pass-1"),
    )

    # Default: only the DEV-seeded garage.
    names = {i.name for i in collect_local_garages()}
    assert names == {DEV_GARAGE_NAME}

    # --all: the onboarded garage appears, but with no plaintext password.
    all_infos = {i.name: i for i in collect_local_garages(include_all=True)}
    assert "Bob's Autocentre" in all_infos
    bob = all_infos["Bob's Autocentre"]
    assert bob.is_seeded is False
    assert bob.accounts and all(a.password is None for a in bob.accounts)


# --------------------------------------------------------------------------
# Development-only gate
# --------------------------------------------------------------------------


def test_require_development_raises_outside_development(app, monkeypatch):
    # monkeypatch.setitem so the session-scoped app config is restored after.
    monkeypatch.setitem(app.config, "APP_ENV", "production")
    with pytest.raises(NotDevelopmentError):
        require_development()


def test_seed_dev_refuses_outside_development(app, monkeypatch):
    monkeypatch.setitem(app.config, "APP_ENV", "production")
    with pytest.raises(NotDevelopmentError):
        seed_dev_garage()
    assert Garage.query.filter_by(name=DEV_GARAGE_NAME).count() == 0


@pytest.mark.parametrize("command", ["dev-info", "seed-dev"])
def test_cli_commands_refuse_outside_development(app, monkeypatch, command):
    monkeypatch.setitem(app.config, "APP_ENV", "production")
    result = app.test_cli_runner().invoke(args=[command])

    assert result.exit_code != 0
    assert "development-only" in result.output


def test_dev_info_cli_prints_the_booking_url(app):
    result = seed_dev_garage()
    cli = app.test_cli_runner().invoke(args=["dev-info"])

    assert cli.exit_code == 0
    assert f"/book/{result.garage_id}" in cli.output
    assert DEV_PASSWORD in cli.output


def test_seed_dev_cli_reports_reuse_on_the_second_run(app):
    first = app.test_cli_runner().invoke(args=["seed-dev"])
    second = app.test_cli_runner().invoke(args=["seed-dev"])

    assert first.exit_code == 0 and second.exit_code == 0
    assert "id and slug unchanged" in second.output
