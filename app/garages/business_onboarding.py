"""Reusable, idempotent business onboarding.

Wraps :func:`app.garages.onboarding.onboard_garage` (which stays the atomic
core - business row, statuses, schedule, MOT-reminder settings, OWNER/STAFF
roles, first OWNER login) and adds the two things a real demo needs on top:

* the business's **services** (``garage_appointment_types``), and
* its **opening hours** when they differ from the seeded Mon-Fri 09:00-17:00.

Idempotency key: the **owner email** (globally unique on ``employees``). If an
account already exists for it, this is treated as "already onboarded" and
nothing is written - rerunning the same spec is a safe no-op.

No password lives in a spec. The caller (``scripts/onboard_business.py``)
generates a strong temporary one, passes it in here, and prints it once; the
owner must change it on first login.

Spec parsing/validation (:func:`parse_business_spec` / :func:`validate_business_spec`)
is pure - no app context, no database - so it can run fully offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from app.employees.service import email_format_error, password_policy_error
from app.extensions import db
from app.garages.onboarding import GarageSpec, OnboardingError, OwnerSpec, onboard_garage
from app.models.appointments.appointment_type import (
    APPOINTMENT_TYPE_STATUSES,
    GarageAppointmentType,
)
from app.models.employee import Employee
from app.models.garage import Garage
from app.models.garage_schedule import GarageOpeningHours

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class BusinessSpecError(OnboardingError):
    """A malformed or invalid business spec."""


@dataclass
class ServiceSpec:
    name: str
    description: str | None = None
    base_price: str | None = None  # decimal string, e.g. "54.85"
    default_duration_minutes: int | None = None
    status: str = "ACTIVE"


@dataclass
class BusinessSpec:
    name: str
    owner_email: str
    owner_first_name: str | None = None
    owner_last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    postcode: str | None = None
    website: str | None = None
    services: list[ServiceSpec] = field(default_factory=list)
    # weekday index 0-6 -> ("HH:MM", "HH:MM") open range, or None = closed.
    # `None` for the whole mapping = keep the seeded default hours.
    opening_hours: dict[int, tuple[str, str] | None] | None = None
    notes: str | None = None


@dataclass
class BusinessOnboardingResult:
    created: bool
    garage: Garage
    owner: Employee
    #: The plaintext temp password - only when this call created the account
    #: (or reset it). ``None`` on an idempotent no-op.
    temp_password: str | None
    services: list[GarageAppointmentType]
    used_default_hours: bool


# --------------------------------------------------------------------------
# Parsing + validation (pure - safe offline)
# --------------------------------------------------------------------------


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_hhmm(value: str, ctx: str) -> str:
    try:
        hh, mm = value.split(":")
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError) as exc:
        raise BusinessSpecError(f"{ctx}: expected 'HH:MM', got {value!r}.") from exc
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise BusinessSpecError(f"{ctx}: {value!r} is not a valid time of day.")
    return f"{h:02d}:{m:02d}"


def _parse_opening_hours(raw: Any) -> dict[int, tuple[str, str] | None] | None:
    if raw in (None, {}):
        return None
    if not isinstance(raw, dict):
        raise BusinessSpecError("opening_hours must be an object keyed by weekday.")

    out: dict[int, tuple[str, str] | None] = {}
    for key, value in raw.items():
        name = str(key).strip().lower()[:3]
        if name not in _WEEKDAYS:
            raise BusinessSpecError(f"opening_hours: unknown day {key!r}.")
        idx = _WEEKDAYS.index(name)
        if value in (None, False, "closed"):
            out[idx] = None
            continue
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise BusinessSpecError(f'opening_hours.{name}: expected ["HH:MM", "HH:MM"] or null.')
        opens = _parse_hhmm(str(value[0]), f"opening_hours.{name} open")
        closes = _parse_hhmm(str(value[1]), f"opening_hours.{name} close")
        if opens >= closes:
            raise BusinessSpecError(
                f"opening_hours.{name}: open {opens} is not before close {closes}."
            )
        out[idx] = (opens, closes)
    return out


def _parse_service(raw: Any, i: int) -> ServiceSpec:
    if not isinstance(raw, dict) or not _str_or_none(raw.get("name")):
        raise BusinessSpecError(f"services[{i}]: needs a non-empty 'name'.")
    price = _str_or_none(raw.get("base_price"))
    if price is not None:
        try:
            if Decimal(price) < 0:
                raise BusinessSpecError(f"services[{i}].base_price: must not be negative.")
        except InvalidOperation as exc:
            raise BusinessSpecError(
                f"services[{i}].base_price: {price!r} is not a number."
            ) from exc
    duration = raw.get("default_duration_minutes")
    if duration is not None and (not isinstance(duration, int) or duration <= 0):
        raise BusinessSpecError(
            f"services[{i}].default_duration_minutes: must be a positive whole number."
        )
    status = (_str_or_none(raw.get("status")) or "ACTIVE").upper()
    if status not in APPOINTMENT_TYPE_STATUSES:
        raise BusinessSpecError(
            f"services[{i}].status: {status!r} not in {APPOINTMENT_TYPE_STATUSES}."
        )
    return ServiceSpec(
        name=_str_or_none(raw["name"]),  # type: ignore[arg-type]
        description=_str_or_none(raw.get("description")),
        base_price=price,
        default_duration_minutes=duration,
        status=status,
    )


def parse_business_spec(raw: Any) -> BusinessSpec:
    """Turn a decoded JSON/YAML mapping into a :class:`BusinessSpec`. Pure."""
    if not isinstance(raw, dict):
        raise BusinessSpecError("A business spec must be a JSON object.")

    business = raw.get("business", raw)
    owner = raw.get("owner")
    if not isinstance(owner, dict):
        raise BusinessSpecError("Spec is missing an 'owner' object.")
    if not isinstance(business, dict):
        raise BusinessSpecError("Spec 'business' must be an object.")

    forbidden = {"slug", "id", "garage_id", "password"} & (set(business) | set(owner))
    if forbidden:
        raise BusinessSpecError(
            f"Spec must not contain {sorted(forbidden)} - the slug is generated, "
            "ids are assigned by the platform, and passwords are never stored in a spec."
        )

    name = _str_or_none(business.get("name"))
    if not name:
        raise BusinessSpecError("business.name is required.")
    owner_email = _str_or_none(owner.get("email"))
    if not owner_email:
        raise BusinessSpecError("owner.email is required.")

    spec = BusinessSpec(
        name=name,
        owner_email=owner_email,
        owner_first_name=_str_or_none(owner.get("first_name")),
        owner_last_name=_str_or_none(owner.get("last_name")),
        email=_str_or_none(business.get("email")),
        phone=_str_or_none(business.get("phone")),
        address=_str_or_none(business.get("address")),
        postcode=_str_or_none(business.get("postcode")),
        website=_str_or_none(business.get("website")),
        services=[_parse_service(s, i) for i, s in enumerate(raw.get("services", []) or [])],
        opening_hours=_parse_opening_hours(raw.get("opening_hours")),
        notes=_str_or_none(raw.get("notes")),
    )
    validate_business_spec(spec)
    return spec


def validate_business_spec(spec: BusinessSpec) -> None:
    """Raise :class:`BusinessSpecError` on an invalid spec. Pure - no DB."""
    err = email_format_error(spec.owner_email)
    if err:
        raise BusinessSpecError(f"owner.email: {err}")
    if spec.email and email_format_error(spec.email):
        raise BusinessSpecError("business.email: enter a valid email address, or omit it.")

    seen: set[str] = set()
    for service in spec.services:
        key = service.name.casefold()
        if key in seen:
            raise BusinessSpecError(f"Duplicate service name in the spec: {service.name!r}.")
        seen.add(key)


# --------------------------------------------------------------------------
# Creation (needs an app context + DB)
# --------------------------------------------------------------------------


def find_existing_business(owner_email: str, session=None) -> tuple[Employee, Garage] | None:
    """The (owner, business) already onboarded for ``owner_email``, or None."""
    session = session or db.session
    owner = session.query(Employee).filter_by(email=owner_email).first()
    if owner is None:
        return None
    return owner, owner.garage


def _apply_services(
    garage: Garage, specs: list[ServiceSpec], session
) -> list[GarageAppointmentType]:
    created = []
    for s in specs:
        row = GarageAppointmentType(
            garage_id=garage.id,
            name=s.name,
            description=s.description,
            base_price=None if s.base_price is None else Decimal(s.base_price),
            default_duration_minutes=s.default_duration_minutes,
            status=s.status,
        )
        session.add(row)
        created.append(row)
    session.flush()
    return created


def _apply_opening_hours(garage: Garage, hours: dict[int, tuple[str, str] | None], session) -> None:
    rows = {
        row.weekday: row
        for row in session.query(GarageOpeningHours).filter_by(garage_id=garage.id).all()
    }
    for weekday, value in hours.items():
        row = rows.get(weekday)
        if row is None:
            row = GarageOpeningHours(
                garage_id=garage.id,
                weekday=weekday,
                opens_at=time(9, 0),
                closes_at=time(17, 0),
            )
            session.add(row)
        if value is None:
            row.is_closed = True
        else:
            opens, closes = value
            oh, om = (int(x) for x in opens.split(":"))
            ch, cm = (int(x) for x in closes.split(":"))
            row.is_closed = False
            row.opens_at = time(oh, om)
            row.closes_at = time(ch, cm)
    session.flush()


def onboard_business(
    spec: BusinessSpec,
    *,
    temp_password: str,
    session=None,
    commit: bool = True,
) -> BusinessOnboardingResult:
    """Idempotently onboard ``spec``.

    If an account already exists for ``spec.owner_email`` this is a no-op:
    the existing business is returned with ``created=False`` and no
    ``temp_password``. Otherwise the full tenant is created (atomically, via
    :func:`onboard_garage`), then the spec's services and any non-default
    opening hours are applied in the same transaction.
    """
    session = session or db.session

    existing = find_existing_business(spec.owner_email, session)
    if existing is not None:
        owner, garage = existing
        return BusinessOnboardingResult(
            created=False,
            garage=garage,
            owner=owner,
            temp_password=None,
            services=list(garage.appointment_types),
            used_default_hours=True,
        )

    err = password_policy_error(temp_password)
    if err:
        raise BusinessSpecError(f"temp_password: {err}")

    result = onboard_garage(
        garage=GarageSpec(
            name=spec.name,
            email=spec.email,
            phone=spec.phone,
            address=spec.address,
            postcode=spec.postcode,
            website=spec.website,
        ),
        owner=OwnerSpec(
            email=spec.owner_email,
            password=temp_password,
            first_name=spec.owner_first_name,
            last_name=spec.owner_last_name,
        ),
        session=session,
        commit=False,
    )

    services = _apply_services(result.garage, spec.services, session)
    used_default_hours = spec.opening_hours is None
    if spec.opening_hours is not None:
        _apply_opening_hours(result.garage, spec.opening_hours, session)

    if commit:
        session.commit()
    else:
        session.flush()

    return BusinessOnboardingResult(
        created=True,
        garage=result.garage,
        owner=result.owner,
        temp_password=temp_password,
        services=services,
        used_default_hours=used_default_hours,
    )


def reset_owner_password(
    owner: Employee, new_password: str, *, session=None, commit: bool = True
) -> None:
    """Set a fresh password for an existing owner and invalidate live sessions."""
    err = password_policy_error(new_password)
    if err:
        raise BusinessSpecError(f"new password: {err}")
    from werkzeug.security import generate_password_hash

    session = session or db.session
    owner.password_hash = generate_password_hash(new_password)
    owner.tokens_valid_from = datetime.now(UTC)
    if commit:
        session.commit()
    else:
        session.flush()
