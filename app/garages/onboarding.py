"""Developer-controlled garage onboarding.

The single, authoritative way a tenant comes into existence. Used by:

* the ``flask onboard-garage`` CLI (``app/garages/cli.py`` /
  ``scripts/onboard_garage.py``) - the supported, developer-run path, and
* ``POST /api/auth/register`` - the HTTP path, kept for the in-repo onboarding
  form and switchable off with ``ONBOARDING_HTTP_ENABLED=false``.

Both call :func:`onboard_garage`, which - in one transaction, rolled back
completely on any error - creates:

* the ``Garage`` (with a generated, immutable :mod:`slug <app.garages.slug>`
  and an optional platform-chosen ``layout_variant``),
* its default appointment statuses and schedule,
* the ``OWNER`` and ``STAFF`` roles, and
* the first ``OWNER`` employee login.

A half-created garage is never left behind. The caller supplies the display
name and owner details; it may **not** supply a slug or any identifier - those
belong to the platform.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.appointments.statuses.defaults import seed_default_statuses
from app.employees.service import (
    build_employee_account,
    email_format_error,
    email_in_use,
    password_policy_error,
)
from app.extensions import db
from app.garages.layouts import validate_layout_variant
from app.garages.schedule.defaults import seed_default_schedule
from app.garages.slug import slugify_unique
from app.models.employee import Employee
from app.models.garage import Garage
from app.models.role import Role

OWNER_ROLE_NAME = "OWNER"
DEFAULT_STAFF_ROLE_NAME = "STAFF"

# A spec file must never carry these - the slug is generated and identifiers
# are assigned by the platform.
FORBIDDEN_SPEC_KEYS = {"slug", "id", "garage_id"}


class OnboardingError(ValueError):
    """A spec or validation problem. The CLI prints it and exits non-zero;
    the HTTP route maps it to a 4xx."""


class OnboardingEmailInUse(OnboardingError):
    """The chosen owner email already belongs to an account (maps to 409)."""


@dataclass
class OwnerSpec:
    email: str
    password: str
    first_name: str | None = None
    last_name: str | None = None


@dataclass
class GarageSpec:
    name: str
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    layout_variant: str | None = None


@dataclass
class OnboardingResult:
    garage: Garage
    owner: Employee


# --------------------------------------------------------------------------
# Structured input
# --------------------------------------------------------------------------


def _require(mapping: dict, key: str, ctx: str):
    if not isinstance(mapping, dict) or mapping.get(key) in (None, ""):
        raise OnboardingError(f"{ctx}: missing required field {key!r}.")
    return mapping[key]


def parse_spec(raw: Any) -> tuple[GarageSpec, OwnerSpec]:
    """Turn a decoded JSON/YAML mapping into a (GarageSpec, OwnerSpec).

    Accepts either a nested shape ``{"garage": {...}, "owner": {...}}`` or a
    flat one ``{"name": ..., "owner": {...}}``. Rejects any spec that tries to
    pin a slug or identifier.
    """
    if not isinstance(raw, dict):
        raise OnboardingError("Onboarding spec must be a mapping/object.")

    garage_raw = raw.get("garage", raw)
    nested = raw.get("garage") if isinstance(raw.get("garage"), dict) else {}
    forbidden = FORBIDDEN_SPEC_KEYS & (set(raw) | set(nested))
    if forbidden:
        raise OnboardingError(
            f"Onboarding spec must not set {sorted(forbidden)} - the slug is "
            "generated and identifiers are assigned by the platform."
        )

    owner_raw = raw.get("owner")
    if not isinstance(owner_raw, dict):
        raise OnboardingError("Onboarding spec: missing 'owner' object.")

    garage = GarageSpec(
        name=_require(garage_raw, "name", "garage"),
        email=garage_raw.get("email"),
        phone=garage_raw.get("phone"),
        address=garage_raw.get("address"),
        layout_variant=garage_raw.get("layout_variant"),
    )
    owner = OwnerSpec(
        email=_require(owner_raw, "email", "owner"),
        password=_require(owner_raw, "password", "owner"),
        first_name=owner_raw.get("first_name"),
        last_name=owner_raw.get("last_name"),
    )
    return garage, owner


def load_spec_file(path: str | Path) -> tuple[GarageSpec, OwnerSpec]:
    """Read + parse a ``.json`` (or ``.yaml``/``.yml``, if PyYAML is
    installed) onboarding spec."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")

    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ModuleNotFoundError as exc:  # pragma: no cover - env dependent
            raise OnboardingError(
                "PyYAML isn't installed - use a .json spec, or "
                "`pip install pyyaml`."
            ) from exc
        raw = yaml.safe_load(text)
    else:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OnboardingError(f"{path}: invalid JSON - {exc}") from exc

    return parse_spec(raw)


# --------------------------------------------------------------------------
# Validation + creation
# --------------------------------------------------------------------------


def validate(garage: GarageSpec, owner: OwnerSpec) -> None:
    """Raise :class:`OnboardingError` if the spec can't be onboarded. Pure -
    no writes."""
    if not garage.name or not garage.name.strip():
        raise OnboardingError("garage.name must not be blank.")

    email_err = email_format_error(owner.email)
    if email_err:
        raise OnboardingError(f"owner.email: {email_err}")

    password_err = password_policy_error(owner.password)
    if password_err:
        raise OnboardingError(f"owner.password: {password_err}")

    try:
        validate_layout_variant(garage.layout_variant)
    except ValueError as exc:
        raise OnboardingError(str(exc)) from exc

    if email_in_use(owner.email):
        raise OnboardingEmailInUse(
            f"owner.email {owner.email!r} is already in use by another account."
        )


def onboard_garage(
    *,
    garage: GarageSpec,
    owner: OwnerSpec,
    session=None,
    commit: bool = True,
) -> OnboardingResult:
    """Create the tenant and its first OWNER, atomically.

    Any failure rolls the whole unit of work back - there is no partially
    onboarded garage. Pass ``commit=False`` to leave the transaction open for
    the caller (used by ``--dry-run`` style callers and tests).
    """
    session = session or db.session
    validate(garage, owner)

    try:
        record = Garage(
            name=garage.name.strip(),
            slug=slugify_unique(garage.name, session),
            layout_variant=garage.layout_variant,
            email=garage.email,
            phone=garage.phone,
            address=garage.address,
        )
        session.add(record)
        session.flush()

        # New garages ship with the built-in appointment statuses + default
        # scheduling; they define their own appointment types later.
        seed_default_statuses(record.id, session)
        seed_default_schedule(record.id, session)

        owner_role = Role(garage_id=record.id, name=OWNER_ROLE_NAME)
        session.add(owner_role)
        session.add(Role(garage_id=record.id, name=DEFAULT_STAFF_ROLE_NAME))
        session.flush()

        owner_employee = build_employee_account(
            garage_id=record.id,
            email=owner.email,
            password=owner.password,
            first_name=owner.first_name,
            last_name=owner.last_name,
            roles=[owner_role],
        )

        if commit:
            session.commit()
        else:
            session.flush()
    except IntegrityError as exc:
        session.rollback()
        # The only user-supplied unique value is the owner email.
        raise OnboardingEmailInUse(
            f"owner.email {owner.email!r} is already in use by another account."
        ) from exc
    except Exception:
        session.rollback()
        raise

    return OnboardingResult(garage=record, owner=owner_employee)
