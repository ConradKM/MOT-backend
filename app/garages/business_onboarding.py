"""Reusable, idempotent business onboarding.

Wraps :func:`app.garages.onboarding.onboard_garage` (which stays the atomic
core - business row, statuses, schedule, MOT-reminder settings, OWNER/STAFF
roles, first OWNER login) and adds the two things a real demo needs on top:

* the business's **services** (``garage_appointment_types``),
* its **opening hours** when they differ from the seeded Mon-Fri 09:00-17:00,
* its **booking settings** (the seeded ``garage_schedule_settings`` row), and
* the platform-owned lifecycle fields a new tenant is sold on - plan, ACTIVE
  vs TRIAL, trial expiry, and the internal notes the platform team keeps.

Everything lands in the one transaction ``onboard_garage`` opens, so a
half-configured tenant is never left behind: either the business exists with
its services, hours, booking window and plan, or nothing was written.

Idempotency key: the **owner email** (globally unique on ``employees``). If an
account already exists for it, this is treated as "already onboarded" and
nothing is written - rerunning the same spec is a safe no-op.

No password lives in a spec. The caller passes a strong generated one in:
``scripts/onboard_business.py`` prints it once for the operator to hand over,
while Platform Admin (``app/platform_admin/provisioning.py``) discards it
unread and emails the owner a set-password invite instead.

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
from app.garages.layouts import validate_layout_variant
from app.garages.onboarding import GarageSpec, OnboardingError, OwnerSpec, onboard_garage
from app.models.appointments.appointment_type import (
    APPOINTMENT_TYPE_STATUSES,
    GarageAppointmentType,
)
from app.models.employee import Employee
from app.models.garage import (
    GARAGE_STATUS_ACTIVE,
    GARAGE_STATUS_TRIAL,
    Garage,
)
from app.models.garage_schedule import GarageOpeningHours, GarageScheduleSettings

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: The statuses a business may be *onboarded* in. SUSPENDED is deliberately
#: absent - suspending is its own audited operation, never an initial state.
ONBOARDING_STATUSES = (GARAGE_STATUS_ACTIVE, GARAGE_STATUS_TRIAL)

#: Booking settings a spec may set, mapped to (kind, minimum, maximum). The
#: names and ranges are exactly ``GarageScheduleSettings`` and the owner-facing
#: ``ScheduleSettingsSchema`` - this is the same row Settings > Availability
#: edits, seeded at onboarding rather than a second copy of it.
BOOKING_SETTING_FIELDS: dict[str, tuple[type, float, float]] = {
    "slot_interval_minutes": (int, 5, 240),
    "default_appointment_minutes": (int, 5, 480),
    "min_lead_time_hours": (int, 0, 24 * 90),
    "max_advance_days": (int, 1, 365),
    "capacity_per_slot": (int, 1, 100),
    "limited_threshold_ratio": (float, 0, 1),
}


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
    # Platform-chosen presentation variant (app/garages/layouts.py). None =
    # the shared default layout.
    layout_variant: str | None = None
    services: list[ServiceSpec] = field(default_factory=list)
    # weekday index 0-6 -> ("HH:MM", "HH:MM") open range, or None = closed.
    # `None` for the whole mapping = keep the seeded default hours.
    opening_hours: dict[int, tuple[str, str] | None] | None = None
    # Overrides for the seeded GarageScheduleSettings row - see
    # BOOKING_SETTING_FIELDS. `None` = keep the seeded defaults.
    booking_settings: dict[str, Any] | None = None
    # --- platform-owned lifecycle -----------------------------------------
    # Applied to the Garage in the same transaction, so a tenant is never live
    # for a moment on the wrong plan. `None` keeps the model default.
    plan: str | None = None
    status: str | None = None
    trial_ends_at: datetime | None = None
    #: Internal platform-team notes -> ``Garage.internal_notes``. Never shown
    #: to the tenant.
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


def _parse_booking_settings(raw: Any) -> dict[str, Any] | None:
    """Validate a mapping of :data:`BOOKING_SETTING_FIELDS` overrides.

    ``capacity_per_slot: null`` is meaningful - it is how a spec says "fall
    back to the garage's active employee count" - so it is kept, while an
    omitted key simply leaves the seeded default alone.
    """
    if raw in (None, {}):
        return None
    if not isinstance(raw, dict):
        raise BusinessSpecError("booking_settings must be an object.")

    unknown = set(raw) - set(BOOKING_SETTING_FIELDS)
    if unknown:
        raise BusinessSpecError(
            f"booking_settings: unknown {sorted(unknown)}. "
            f"Allowed: {sorted(BOOKING_SETTING_FIELDS)}."
        )

    out: dict[str, Any] = {}
    for key, value in raw.items():
        kind, low, high = BOOKING_SETTING_FIELDS[key]
        if value is None:
            if key != "capacity_per_slot":
                raise BusinessSpecError(f"booking_settings.{key}: must not be null.")
            out[key] = None
            continue
        # bool is an int subclass, and "3" is not a number here - be explicit.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BusinessSpecError(f"booking_settings.{key}: expected a number, got {value!r}.")
        if kind is int and not float(value).is_integer():
            raise BusinessSpecError(f"booking_settings.{key}: must be a whole number.")
        number = kind(value)
        if not low <= number <= high:
            raise BusinessSpecError(f"booking_settings.{key}: must be between {low} and {high}.")
        out[key] = number
    return out


def _parse_datetime(value: Any, ctx: str) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise BusinessSpecError(f"{ctx}: {value!r} is not an ISO-8601 date/time.") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


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
        layout_variant=_str_or_none(business.get("layout_variant")),
        services=[_parse_service(s, i) for i, s in enumerate(raw.get("services", []) or [])],
        opening_hours=_parse_opening_hours(raw.get("opening_hours")),
        booking_settings=_parse_booking_settings(raw.get("booking_settings")),
        plan=(_str_or_none(business.get("plan")) or "").upper() or None,
        status=(_str_or_none(business.get("status")) or "").upper() or None,
        trial_ends_at=_parse_datetime(business.get("trial_ends_at"), "business.trial_ends_at"),
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

    # Range-checked here rather than only in `_parse_opening_hours`, because a
    # spec built in code (Platform Admin) never passes through the parser and
    # a backwards range would otherwise reach the availability engine as a day
    # that is open for a negative length of time.
    for weekday, value in (spec.opening_hours or {}).items():
        if weekday not in range(7):
            raise BusinessSpecError(f"opening_hours: weekday must be 0-6, got {weekday!r}.")
        if value is None:
            continue
        day = _WEEKDAYS[weekday]
        opens = _parse_hhmm(str(value[0]), f"opening_hours.{day} open")
        closes = _parse_hhmm(str(value[1]), f"opening_hours.{day} close")
        if opens >= closes:
            raise BusinessSpecError(
                f"opening_hours.{day}: open {opens} is not before close {closes}."
            )

    seen: set[str] = set()
    for service in spec.services:
        key = service.name.casefold()
        if key in seen:
            raise BusinessSpecError(f"Duplicate service name in the spec: {service.name!r}.")
        seen.add(key)

    try:
        validate_layout_variant(spec.layout_variant)
    except ValueError as exc:
        raise BusinessSpecError(str(exc)) from exc

    if spec.plan is not None:
        # Imported here: app.platform_admin owns the plan matrix, and importing
        # it at module scope would make the onboarding CLI depend on the whole
        # Platform Admin package to validate one string.
        from app.platform_admin.features import PLANS

        if spec.plan not in PLANS:
            raise BusinessSpecError(
                f"business.plan: unknown plan {spec.plan!r}. Expected one of {list(PLANS)}."
            )

    if spec.status is not None and spec.status not in ONBOARDING_STATUSES:
        raise BusinessSpecError(
            f"business.status: expected one of {list(ONBOARDING_STATUSES)}, got {spec.status!r}."
        )

    # A trial with no end date is indistinguishable from one that never
    # expires, and an end date on a non-trial is a value nothing will ever
    # read - both are almost certainly an operator mistake, so refuse them.
    if spec.status == GARAGE_STATUS_TRIAL:
        if spec.trial_ends_at is None:
            raise BusinessSpecError("business.trial_ends_at is required when status is TRIAL.")
        if spec.trial_ends_at <= datetime.now(UTC):
            raise BusinessSpecError("business.trial_ends_at must be in the future.")
    elif spec.trial_ends_at is not None:
        raise BusinessSpecError("business.trial_ends_at is only valid when status is TRIAL.")


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


def _apply_booking_settings(garage: Garage, values: dict[str, Any], session) -> None:
    """Overwrite fields on the garage's seeded ``GarageScheduleSettings`` row.

    The row always exists by this point - ``onboard_garage`` seeds it through
    ``seed_default_schedule`` - but a garage onboarded before that seed existed
    would not have one, so this creates it rather than failing.
    """
    row = session.query(GarageScheduleSettings).filter_by(garage_id=garage.id).first()
    if row is None:
        row = GarageScheduleSettings(garage_id=garage.id)
        session.add(row)
    for key, value in values.items():
        setattr(row, key, value)
    session.flush()


def _apply_lifecycle(garage: Garage, spec: BusinessSpec) -> None:
    """Plan, status, trial expiry and internal notes - the platform-owned
    fields, set here so a tenant is never briefly live on the wrong plan."""
    if spec.plan is not None:
        garage.plan = spec.plan
    if spec.status is not None:
        garage.status = spec.status
        garage.status_changed_at = datetime.now(UTC)
    garage.trial_ends_at = spec.trial_ends_at
    if spec.notes is not None:
        garage.internal_notes = spec.notes


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

    # A spec built in code (Platform Admin) never passed through
    # `parse_business_spec`, so validate here too rather than trusting the
    # caller to have done it.
    validate_business_spec(spec)

    result = onboard_garage(
        garage=GarageSpec(
            name=spec.name,
            email=spec.email,
            phone=spec.phone,
            address=spec.address,
            postcode=spec.postcode,
            website=spec.website,
            layout_variant=spec.layout_variant,
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
    if spec.booking_settings:
        _apply_booking_settings(result.garage, spec.booking_settings, session)
    _apply_lifecycle(result.garage, spec)

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
