"""Creating and correcting a tenant's onboarding configuration.

The Platform Admin entry point for "onboard a business" - what
``scripts/onboard_business.py`` does from a shell, done from the console
instead, with the same code underneath.

Nothing here re-implements onboarding. :func:`provision_tenant` builds a
:class:`~app.garages.business_onboarding.BusinessSpec` from a validated request
body and hands it to
:func:`~app.garages.business_onboarding.onboard_business`, which wraps
:func:`~app.garages.onboarding.onboard_garage` - still the single atomic
definition of how a tenant comes into existence. Services, opening hours,
booking settings and the platform lifecycle fields are applied by the same
spec, inside the same transaction, so a failure anywhere leaves *no* tenant
rather than a half-built one.

**The owner's password is never known to anyone.** Onboarding needs an
Employee row and an Employee row needs a password hash, so one is generated
from ``secrets``, hashed, and dropped on the floor unread. What the owner
actually receives is a set-password invite - the existing single-use
:class:`~app.models.password_reset_token.PasswordResetToken` from
``app/auth/reset.py``, on a longer TTL (``OWNER_INVITE_TOKEN_HOURS``) because a
first login is handed over out of band rather than clicked within the half hour
a self-service reset assumes. No plaintext password is returned by this API, is
stored anywhere, or reaches the audit trail.

Invite state is *derived* from those token rows (:func:`owner_invite_status`) -
"sent", "accepted", "expired" - so there is no new column to keep in sync and
no way for it to disagree with whether the link actually still works.

Duplicate submission is handled by the database, not by a nonce: owner email is
globally unique on ``employees``, so a second concurrent submit of the same
form loses the unique constraint inside its own transaction and comes back 409
having written nothing.

The correction functions below (services, opening hours, booking settings,
resending an invite) exist so a mistake caught after creation doesn't send an
operator back to the CLI. Each one goes through the same models the tenant's
own Settings screens use, is superadmin-only at the route, and writes an audit
row in its own transaction. This is deliberately *not* a general database
editor: only the fields onboarding collects are reachable.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Any

from flask import current_app
from sqlalchemy import func, select

from app.auth.reset import issue_reset_token
from app.branding import PLATFORM_NAME
from app.email import send_email
from app.extensions import db
from app.garages.business_onboarding import (
    BOOKING_SETTING_FIELDS,
    BusinessSpec,
    BusinessSpecError,
    ServiceSpec,
    onboard_business,
)
from app.garages.onboarding import OWNER_ROLE_NAME, OnboardingEmailInUse, OnboardingError
from app.garages.schedule.defaults import seed_default_schedule
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.employee import Employee
from app.models.garage import Garage
from app.models.garage_schedule import GarageOpeningHours, GarageScheduleSettings
from app.models.password_reset_token import PasswordResetToken
from app.models.platform.audit_log import (
    ACTION_TENANT_BOOKING_SETTINGS_UPDATE,
    ACTION_TENANT_CREATE,
    ACTION_TENANT_OPENING_HOURS_UPDATE,
    ACTION_TENANT_OWNER_INVITE,
    ACTION_TENANT_SERVICE_CREATE,
    ACTION_TENANT_SERVICE_DELETE,
    ACTION_TENANT_SERVICE_UPDATE,
)
from app.models.role import Role, employee_roles

from .audit import record_audit
from .onboarding import onboarding_progress

#: The follow-up work that is *not* part of core onboarding: a business is
#: fully created, bookable and signed in to without any of it. Reported so the
#: console can show the operator what is left, and so "what still needs doing"
#: has one definition rather than one per screen.
NEXT_TASK_WHATSAPP = "whatsapp"
NEXT_TASK_PHONE = "phone"
NEXT_TASK_MOT_REMINDERS = "mot_reminders"
NEXT_TASK_TEST_BOOKING = "test_booking"
NEXT_TASK_OWNER_LOGIN = "owner_login"
NEXT_TASK_QR_CODE = "qr_code"
NEXT_TASK_LAUNCH = "launch"


class ProvisioningError(ValueError):
    """A rejected provisioning request (maps to 422)."""


class DuplicateOwnerError(ProvisioningError):
    """The owner email already belongs to an account (maps to 409)."""


# --------------------------------------------------------------------------
# Reading a tenant's onboarding configuration
# --------------------------------------------------------------------------


def public_booking_url(garage: Garage) -> str:
    """The customer-facing booking link for ``garage``.

    Keyed on the immutable ``id``, matching the frontend's ``/book/:garageId``
    route and the QR code built from it - not on the slug, which the public
    API resolves but no customer-facing URL carries.
    """
    base = str(current_app.config.get("BOOKING_BASE_URL") or "").rstrip("/")
    return f"{base}/book/{garage.id}"


def find_owner(garage: Garage) -> Employee | None:
    """The tenant's first OWNER employee - the account onboarding created."""
    return db.session.execute(
        select(Employee)
        .join(employee_roles, employee_roles.c.employee_id == Employee.id)
        .join(Role, Role.id == employee_roles.c.role_id)
        .where(Role.name == OWNER_ROLE_NAME, Employee.garage_id == garage.id)
        .order_by(Employee.created_at, Employee.id)
        .limit(1)
    ).scalar_one_or_none()


def owner_invite_status(owner: Employee | None) -> dict:
    """Where this owner's set-password invite got to, read from its tokens.

    ``none`` - no invite was ever issued (an owner onboarded before this flow,
    or by the CLI with a temporary password). ``accepted`` - the link was used,
    so the owner has set their own password and can sign in. ``sent`` - a live
    link is outstanding. ``expired`` - the outstanding link lapsed unused, and
    the owner needs a fresh one.

    Only the **newest** token is read, and that is the whole story: issuing an
    invite first voids every outstanding one by stamping its ``used_at``, so a
    used flag on an older row means "superseded", not "accepted". At most one
    unused token can exist at a time, and consuming one only ever consumes the
    newest.
    """
    if owner is None:
        return {"state": "none", "sent_at": None, "expires_at": None, "accepted_at": None}

    latest = db.session.execute(
        select(PasswordResetToken)
        .where(PasswordResetToken.employee_id == owner.id)
        .order_by(PasswordResetToken.created_at.desc(), PasswordResetToken.id.desc())
        .limit(1)
    ).scalar_one_or_none()

    if latest is None:
        return {"state": "none", "sent_at": None, "expires_at": None, "accepted_at": None}

    if latest.used_at is not None:
        return {
            "state": "accepted",
            "sent_at": latest.created_at,
            "expires_at": latest.expires_at,
            "accepted_at": latest.used_at,
        }

    live = _as_utc(latest.expires_at) > datetime.now(UTC)
    return {
        "state": "sent" if live else "expired",
        "sent_at": latest.created_at,
        "expires_at": latest.expires_at,
        "accepted_at": None,
    }


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def communications_status(garage: Garage) -> dict:
    """Whether this tenant's Twilio wiring exists and is live.

    Read-only here on purpose: allocating numbers and senders is
    ``app/communications/cli.py``'s job, and onboarding only needs to report
    what is left to do.
    """
    settings = garage.communication_settings
    return {
        "configured": bool(settings and (settings.voice_phone_number or settings.whatsapp_sender)),
        "enabled": bool(settings and settings.communications_enabled),
        "voice_phone_number": settings.voice_phone_number if settings else None,
        "whatsapp_sender": settings.whatsapp_sender if settings else None,
        "twilio_subaccount_sid": settings.twilio_subaccount_sid if settings else None,
    }


def _opening_hours(garage: Garage) -> list[GarageOpeningHours]:
    return list(
        db.session.execute(
            select(GarageOpeningHours)
            .where(GarageOpeningHours.garage_id == garage.id)
            .order_by(GarageOpeningHours.weekday)
        )
        .scalars()
        .all()
    )


def _booking_settings(garage: Garage) -> GarageScheduleSettings:
    """The tenant's schedule settings, seeding them if it predates the seed."""
    row = db.session.execute(
        select(GarageScheduleSettings).where(GarageScheduleSettings.garage_id == garage.id)
    ).scalar_one_or_none()
    if row is None:
        seed_default_schedule(garage.id, db.session)
        db.session.commit()
        row = db.session.execute(
            select(GarageScheduleSettings).where(GarageScheduleSettings.garage_id == garage.id)
        ).scalar_one()
    return row


def _services(garage: Garage) -> list[GarageAppointmentType]:
    return list(
        db.session.execute(
            select(GarageAppointmentType)
            .where(GarageAppointmentType.garage_id == garage.id)
            .order_by(GarageAppointmentType.name)
        )
        .scalars()
        .all()
    )


def tenant_configuration(garage: Garage) -> dict:
    """Everything onboarding configured for one tenant, in one payload.

    The read behind the console's Onboarding tab: identity, owner and invite
    state, plan/status, services, opening hours, booking settings,
    communications, the public booking URL, and the derived checklist.
    """
    owner = find_owner(garage)
    return {
        "garage": garage,
        "owner": owner,
        "owner_invite": owner_invite_status(owner),
        "services": _services(garage),
        "opening_hours": _opening_hours(garage),
        "booking_settings": _booking_settings(garage),
        "communications": communications_status(garage),
        "public_booking_url": public_booking_url(garage),
        "onboarding": onboarding_progress(garage),
    }


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------


def _spec_from_request(data: dict) -> BusinessSpec:
    """Turn a validated request body into a :class:`BusinessSpec`.

    Shape-checking has already happened in the marshmallow schema; the *rules*
    (plan exists, TRIAL needs a future end date, opens before closes, no
    duplicate service names) live in ``validate_business_spec`` and run once,
    for this caller and the CLI alike.
    """
    business = data["business"]
    owner = data["owner"]

    opening_hours: dict[int, tuple[str, str] | None] | None = None
    if data.get("opening_hours") is not None:
        opening_hours = {}
        for entry in data["opening_hours"]:
            weekday = entry["weekday"]
            if entry.get("is_closed"):
                opening_hours[weekday] = None
                continue
            opens, closes = entry.get("opens_at"), entry.get("closes_at")
            if opens is None or closes is None:
                raise ProvisioningError(
                    f"opening_hours[{weekday}]: an open day needs both an opening "
                    "and a closing time."
                )
            opening_hours[weekday] = (_hhmm(opens), _hhmm(closes))

    return BusinessSpec(
        name=business["name"],
        owner_email=owner["email"],
        owner_first_name=owner.get("first_name"),
        owner_last_name=owner.get("last_name"),
        email=business.get("email"),
        phone=business.get("phone"),
        address=business.get("address"),
        postcode=business.get("postcode"),
        website=business.get("website"),
        layout_variant=business.get("layout_variant"),
        services=[
            ServiceSpec(
                name=service["name"],
                description=service.get("description"),
                base_price=(
                    None if service.get("base_price") is None else str(service["base_price"])
                ),
                default_duration_minutes=service.get("default_duration_minutes"),
                status=service.get("status") or "ACTIVE",
            )
            for service in data.get("services") or []
        ],
        opening_hours=opening_hours,
        booking_settings=data.get("booking_settings") or None,
        plan=business.get("plan"),
        status=business.get("status"),
        trial_ends_at=business.get("trial_ends_at"),
        notes=business.get("internal_notes"),
    )


def _hhmm(value: time | str) -> str:
    return value if isinstance(value, str) else f"{value.hour:02d}:{value.minute:02d}"


def provision_tenant(*, admin, data: dict) -> dict:
    """Onboard a business from Platform Admin. One transaction, fully audited.

    The tenant, its owner, roles, appointment statuses, schedule, MOT reminder
    settings, services, opening hours, booking settings and plan all commit
    together or not at all. The owner's invite is issued in the same
    transaction; the email itself is sent *after* the commit, because a mail
    provider being down must not roll back a business that was created
    correctly - the invite can always be resent.
    """
    spec = _spec_from_request(data)

    try:
        result = onboard_business(
            spec,
            # Never read, never returned, never stored in plaintext: the owner
            # sets their own password through the invite below. It exists only
            # because an Employee row needs a hash.
            temp_password=secrets.token_urlsafe(32),
            commit=False,
        )
    except OnboardingEmailInUse as exc:
        db.session.rollback()
        raise DuplicateOwnerError(str(exc)) from exc
    except (BusinessSpecError, OnboardingError) as exc:
        db.session.rollback()
        raise ProvisioningError(str(exc)) from exc

    if not result.created:
        # `onboard_business` is idempotent on owner email and would quietly
        # return the existing tenant. From the console that is a duplicate
        # submission, not a success - say so rather than showing a second
        # "created" screen for a business that already existed.
        db.session.rollback()
        raise DuplicateOwnerError(
            f"An account already exists for {spec.owner_email} (business {result.garage.name!r})."
        )

    garage, owner = result.garage, result.owner
    raw_token = issue_reset_token(owner, minutes=_invite_minutes())

    record_audit(
        admin=admin,
        action=ACTION_TENANT_CREATE,
        garage=garage,
        summary=f"Onboarded {garage.name}",
        details={
            "slug": garage.slug,
            "owner_email": owner.email,
            "plan": garage.plan,
            "status": garage.status,
            "trial_ends_at": garage.trial_ends_at.isoformat() if garage.trial_ends_at else None,
            "services": [service.name for service in result.services],
            "custom_opening_hours": not result.used_default_hours,
            "custom_booking_settings": sorted(spec.booking_settings or {}),
        },
    )
    db.session.commit()

    invite_sent = _send_owner_invite(garage, owner, raw_token)

    return {
        "created": True,
        "invite_sent": invite_sent,
        **tenant_configuration(garage),
        "next_tasks": next_tasks(garage),
    }


def _invite_minutes() -> int:
    return int(current_app.config.get("OWNER_INVITE_TOKEN_HOURS", 168)) * 60


def _send_owner_invite(garage: Garage, owner: Employee, raw_token: str) -> bool:
    """Email the owner their set-password link. Never raises.

    A failure here is reported, not fatal: the business exists and is correct,
    and the console offers "resend invite" for exactly this case.
    """
    base = str(current_app.config.get("APP_BASE_URL") or "").rstrip("/")
    hours = int(current_app.config.get("OWNER_INVITE_TOKEN_HOURS", 168))
    url = f"{base}/reset-password?token={raw_token}"
    days = max(1, round(hours / 24))

    try:
        send_email(
            to=owner.email,
            subject=f"Set up your {garage.name} account",
            body=(
                f"Your {garage.name} account on {PLATFORM_NAME} is ready.\n\n"
                f"Choose your password to sign in for the first time "
                f"(link valid for {days} day{'s' if days != 1 else ''}):\n{url}\n\n"
                "Your booking page for customers:\n"
                f"{public_booking_url(garage)}\n\n"
                "If you weren't expecting this, you can ignore this email."
            ),
            from_name=garage.name,
        )
        return True
    except Exception:  # pragma: no cover - provider-specific failures
        current_app.logger.exception(
            "[platform-admin] owner invite email failed for %s", owner.email
        )
        return False


def resend_owner_invite(*, admin, garage: Garage) -> dict:
    """Issue a fresh set-password link for the tenant's owner and email it.

    Voids any outstanding link, by the same rule as a self-service reset - so
    an invite forwarded to the wrong address stops working the moment a new one
    is sent.
    """
    owner = find_owner(garage)
    if owner is None:
        raise ProvisioningError("This business has no owner account to invite.")

    raw_token = issue_reset_token(owner, minutes=_invite_minutes())
    record_audit(
        admin=admin,
        action=ACTION_TENANT_OWNER_INVITE,
        garage=garage,
        target_type="employee",
        target_id=owner.id,
        summary=f"Sent {owner.email} a set-password invite",
    )
    db.session.commit()

    sent = _send_owner_invite(garage, owner, raw_token)
    return {"invite_sent": sent, "owner": owner, "owner_invite": owner_invite_status(owner)}


# --------------------------------------------------------------------------
# Next setup tasks - follow-ups, never blockers
# --------------------------------------------------------------------------


def next_tasks(garage: Garage) -> list[dict]:
    """What is left to do after a business is onboarded.

    Each entry reports whether it is already done where that is knowable from
    the tenant's own data; the rest are things a human verifies. None of them
    gate onboarding being complete.
    """
    comms = communications_status(garage)
    reminders = garage.mot_reminder_settings
    owner = find_owner(garage)
    invite = owner_invite_status(owner)
    progress = onboarding_progress(garage)

    return [
        {
            "key": NEXT_TASK_WHATSAPP,
            "label": "Configure WhatsApp / Twilio",
            "description": "Allocate a Twilio subaccount and WhatsApp sender for this business.",
            "complete": bool(comms["whatsapp_sender"]),
        },
        {
            "key": NEXT_TASK_PHONE,
            "label": "Configure phone number / forwarding",
            "description": "Point a voice number at this business and set call forwarding.",
            "complete": bool(comms["voice_phone_number"]),
        },
        {
            "key": NEXT_TASK_MOT_REMINDERS,
            "label": "Configure MOT reminders",
            "description": "Reminder stages are enabled for this business.",
            # Seeded at onboarding and on by default, so this normally arrives
            # already done - it is here so an operator who turned reminders off
            # for a tenant sees that reflected, not as busywork.
            "complete": bool(
                reminders
                and (
                    reminders.stage1_enabled or reminders.stage2_enabled or reminders.stage3_enabled
                )
            ),
        },
        {
            "key": NEXT_TASK_TEST_BOOKING,
            "label": "Test public booking",
            "description": "Submit a booking on the public page and approve it.",
            "complete": next(
                (
                    step["complete"]
                    for step in progress["steps"]
                    if step["key"] == "booking_requests"
                ),
                False,
            ),
        },
        {
            "key": NEXT_TASK_OWNER_LOGIN,
            "label": "Test owner login",
            "description": "The owner has set their password and signed in.",
            "complete": invite["state"] == "accepted",
        },
        {
            "key": NEXT_TASK_QR_CODE,
            "label": "Generate / access QR code",
            "description": "Share the booking QR code with the business.",
            "complete": False,
        },
        {
            "key": NEXT_TASK_LAUNCH,
            "label": "Mark business ready for launch",
            "description": "Reached once communications are live for this business.",
            "complete": progress["stage"] == "ready_for_launch",
        },
    ]


# --------------------------------------------------------------------------
# Correcting the configuration afterwards
# --------------------------------------------------------------------------


def _service_payload(service: GarageAppointmentType) -> dict:
    return {
        "name": service.name,
        "description": service.description,
        "base_price": None if service.base_price is None else str(service.base_price),
        "default_duration_minutes": service.default_duration_minutes,
        "status": service.status,
    }


def _validate_service(garage: Garage, data: dict, *, exclude_id=None) -> None:
    """Reject a name that another service on this tenant already uses.

    Enforced here rather than by a constraint because the table has never had
    one, and adding it would need a migration that could fail on tenants with
    existing duplicates.
    """
    name = (data.get("name") or "").strip()
    if not name:
        return
    clash = db.session.execute(
        select(GarageAppointmentType.id).where(
            GarageAppointmentType.garage_id == garage.id,
            func.lower(GarageAppointmentType.name) == name.lower(),
            *([GarageAppointmentType.id != exclude_id] if exclude_id else []),
        )
    ).first()
    if clash is not None:
        raise ProvisioningError(f"This business already has a service called {name!r}.")


def create_service(*, admin, garage: Garage, data: dict) -> GarageAppointmentType:
    """Add one service to an already-onboarded tenant."""
    _validate_service(garage, data)
    service = GarageAppointmentType(
        garage_id=garage.id,
        name=data["name"].strip(),
        description=data.get("description"),
        base_price=(None if data.get("base_price") is None else Decimal(str(data["base_price"]))),
        default_duration_minutes=data.get("default_duration_minutes"),
        status=data.get("status") or "ACTIVE",
    )
    db.session.add(service)
    db.session.flush()

    record_audit(
        admin=admin,
        action=ACTION_TENANT_SERVICE_CREATE,
        garage=garage,
        target_type="appointment_type",
        target_id=service.id,
        summary=f"Added service {service.name}",
        details=_service_payload(service),
    )
    db.session.commit()
    return service


def get_service(garage: Garage, service_id: uuid.UUID) -> GarageAppointmentType | None:
    """One of *this* tenant's services - the garage filter is the isolation."""
    return db.session.execute(
        select(GarageAppointmentType).where(
            GarageAppointmentType.id == service_id,
            GarageAppointmentType.garage_id == garage.id,
        )
    ).scalar_one_or_none()


def update_service(
    *, admin, garage: Garage, service: GarageAppointmentType, changes: dict
) -> GarageAppointmentType:
    if not changes:
        raise ProvisioningError("Nothing to update - pass at least one field.")
    _validate_service(garage, changes, exclude_id=service.id)

    before = _service_payload(service)
    for key, value in changes.items():
        if key == "base_price":
            value = None if value is None else Decimal(str(value))
        if key == "name" and value is not None:
            value = value.strip()
        setattr(service, key, value)
    db.session.flush()

    record_audit(
        admin=admin,
        action=ACTION_TENANT_SERVICE_UPDATE,
        garage=garage,
        target_type="appointment_type",
        target_id=service.id,
        summary=f"Updated service {service.name}",
        details={"from": before, "to": _service_payload(service)},
    )
    db.session.commit()
    return service


def delete_service(*, admin, garage: Garage, service: GarageAppointmentType) -> None:
    """Remove a service added by mistake.

    Refused once appointments reference it - deleting would orphan real
    history. Retiring it (``status: DEPRECATED``) is the supported way to stop
    offering a service customers have already booked.
    """
    if service.appointments:
        raise ProvisioningError(
            f"{service.name!r} has appointments booked against it and cannot be deleted. "
            "Set its status to HIDDEN or DEPRECATED instead."
        )

    payload = _service_payload(service)
    db.session.delete(service)
    record_audit(
        admin=admin,
        action=ACTION_TENANT_SERVICE_DELETE,
        garage=garage,
        target_type="appointment_type",
        target_id=service.id,
        summary=f"Removed service {payload['name']}",
        details=payload,
    )
    db.session.commit()


def update_opening_hours(*, admin, garage: Garage, entries: list[dict]) -> list[GarageOpeningHours]:
    """Replace this tenant's weekday opening hours.

    Writes the same ``garage_opening_hours`` rows Settings > Availability
    writes, which is what makes the hours set here immediately real to the
    public booking calendar, the availability API and the WhatsApp assistant -
    there is no separate onboarding copy of a garage's hours.
    """
    rows = {row.weekday: row for row in _opening_hours(garage)}
    before = {row.weekday: _hours_payload(row) for row in rows.values()}

    for entry in entries:
        weekday = entry["weekday"]
        is_closed = bool(entry.get("is_closed"))
        opens_at, closes_at = entry.get("opens_at"), entry.get("closes_at")

        if not is_closed:
            if opens_at is None or closes_at is None:
                raise ProvisioningError(
                    f"weekday {weekday}: an open day needs both an opening and a closing time."
                )
            if opens_at >= closes_at:
                raise ProvisioningError(f"weekday {weekday}: opens_at must be before closes_at.")

        row = rows.get(weekday)
        if row is None:
            row = GarageOpeningHours(garage_id=garage.id, weekday=weekday)
            db.session.add(row)
            rows[weekday] = row
        row.is_closed = is_closed
        if opens_at is not None:
            row.opens_at = opens_at
        if closes_at is not None:
            row.closes_at = closes_at

    db.session.flush()
    after = {row.weekday: _hours_payload(row) for row in rows.values()}
    changed = sorted(key for key in after if before.get(key) != after[key])

    record_audit(
        admin=admin,
        action=ACTION_TENANT_OPENING_HOURS_UPDATE,
        garage=garage,
        summary=(
            f"Updated opening hours for {len(changed)} day(s)"
            if changed
            else "Submitted opening hours that changed nothing"
        ),
        details={"changed_weekdays": changed, "opening_hours": after},
    )
    db.session.commit()
    return _opening_hours(garage)


def _hours_payload(row: GarageOpeningHours) -> dict:
    return {
        "is_closed": row.is_closed,
        "opens_at": row.opens_at.isoformat() if row.opens_at else None,
        "closes_at": row.closes_at.isoformat() if row.closes_at else None,
    }


def update_booking_settings(*, admin, garage: Garage, changes: dict) -> GarageScheduleSettings:
    """Change the booking window and capacity rules for one tenant.

    The same ``garage_schedule_settings`` row the owner edits in Settings >
    Availability, restricted to the fields onboarding collects.
    """
    unknown = set(changes) - set(BOOKING_SETTING_FIELDS)
    if unknown:
        raise ProvisioningError(f"Not a booking setting: {sorted(unknown)}.")
    if not changes:
        raise ProvisioningError("Nothing to update - pass at least one setting.")

    settings = _booking_settings(garage)
    before = {key: getattr(settings, key) for key in changes}
    for key, value in changes.items():
        setattr(settings, key, value)
    db.session.flush()

    changed = {
        key: {"from": _plain(before[key]), "to": _plain(getattr(settings, key))}
        for key in before
        if before[key] != getattr(settings, key)
    }
    record_audit(
        admin=admin,
        action=ACTION_TENANT_BOOKING_SETTINGS_UPDATE,
        garage=garage,
        summary=(
            f"Updated {', '.join(sorted(changed))}"
            if changed
            else "Submitted booking settings that changed nothing"
        ),
        details={"changed": changed},
    )
    db.session.commit()
    return settings


def _plain(value: Any) -> Any:
    """Audit details are a JSON column; Decimal isn't JSON."""
    return float(value) if isinstance(value, Decimal) else value
