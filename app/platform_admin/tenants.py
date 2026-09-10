"""Cross-tenant read and lifecycle operations for Platform Admin.

The one place in the codebase that is *deliberately* not tenant-scoped: these
queries span every garage, because that is the whole job of an operator
console. Nothing here is reachable without a platform token
(``app/platform_admin/security.py``), and nothing here is imported by any
tenant-facing module - the garage API's own queries still derive their garage
from the caller's JWT exactly as before.

Business logic is borrowed, not re-implemented: contact-detail edits go
through ``app.garages.details.update_garage_details`` (the same allowlist the
onboarding CLI uses), so there is still exactly one definition of "which
tenant fields are editable and how".

"Dormant" is computed here rather than stored:
:func:`last_activity_expression` is the newest of a tenant's own appointments,
booking requests, communications and customers (falling back to its signup
date), which means a tenant that goes quiet and then comes back is simply
active again - no flag to remember to clear.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select

from app.extensions import db
from app.garages.details import update_garage_details
from app.models.appointments.appointment import Appointment
from app.models.booking_request import BookingRequest
from app.models.communications.communication_log import CommunicationLog
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.garage import (
    GARAGE_STATUS_ACTIVE,
    GARAGE_STATUS_SUSPENDED,
    GARAGE_STATUSES,
    Garage,
)
from app.models.platform.audit_log import (
    ACTION_TENANT_PLAN_CHANGE,
    ACTION_TENANT_REACTIVATE,
    ACTION_TENANT_SUSPEND,
    ACTION_TENANT_UPDATE,
)
from app.models.role import Role, employee_roles
from app.models.vehicle import Vehicle

from .audit import record_audit
from .features import UnknownPlanError, validate_plan
from .onboarding import STAGE_KEYS, onboarding_progress, onboarding_progress_bulk

#: A tenant with no activity for this long is reported as dormant. A read-time
#: threshold, not a stored state.
DEFAULT_DORMANT_DAYS = 30

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

#: Sort keys the tenant list accepts, mapped to their ORDER BY.
SORT_KEYS = ("name", "created_at", "last_activity", "status")


class TenantError(ValueError):
    """A rejected tenant operation (maps to 422)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def last_activity_expression():
    """SQL for "when did anything last happen in this tenant?".

    The newest ``created_at`` across the tables a working garage actually
    writes to, floored at the tenant's own signup date so a brand-new tenant
    is never reported as dormant on day one.
    """
    latest = [
        func.coalesce(
            select(func.max(model.created_at))
            .where(model.garage_id == Garage.id)
            .correlate(Garage)
            .scalar_subquery(),
            Garage.created_at,
        )
        for model in (Appointment, BookingRequest, CommunicationLog, Customer)
    ]
    return func.greatest(Garage.created_at, *latest)


def _search_filter(search: str):
    like = f"%{search.strip()}%"
    owner_match = (
        select(Employee.id)
        .where(Employee.garage_id == Garage.id, Employee.email.ilike(like))
        .correlate(Garage)
        .exists()
    )
    return or_(
        Garage.name.ilike(like),
        Garage.slug.ilike(like),
        Garage.email.ilike(like),
        Garage.phone.ilike(like),
        owner_match,
    )


def _owner_emails(garage_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """One OWNER email per tenant, for the list view - a single join rather
    than walking `garage.employees` per row."""
    rows = db.session.execute(
        select(Employee.garage_id, Employee.email)
        .join(employee_roles, employee_roles.c.employee_id == Employee.id)
        .join(Role, Role.id == employee_roles.c.role_id)
        .where(Role.name == "OWNER", Employee.garage_id.in_(garage_ids))
        .order_by(Employee.garage_id, Employee.created_at)
    ).all()
    owners: dict[uuid.UUID, str] = {}
    for garage_id, email in rows:
        owners.setdefault(garage_id, email)
    return owners


def _counts(model, garage_ids: list[uuid.UUID], *filters) -> dict[uuid.UUID, int]:
    rows = db.session.execute(
        select(model.garage_id, func.count())
        .where(model.garage_id.in_(garage_ids), *filters)
        .group_by(model.garage_id)
    ).all()
    return {row[0]: row[1] for row in rows}


def list_tenants(
    *,
    search: str | None = None,
    status: str | None = None,
    plan: str | None = None,
    activity: str | None = None,
    stage: str | None = None,
    sort: str = "created_at",
    descending: bool = True,
    page: int = 1,
    per_page: int = DEFAULT_PAGE_SIZE,
    dormant_days: int = DEFAULT_DORMANT_DAYS,
    now: datetime | None = None,
) -> dict:
    """One page of tenants with the headline numbers the list view shows.

    Filters: free-text ``search`` (name / slug / contact / owner email), exact
    ``status`` and ``plan``, and the two derived ones, ``activity``
    ("active" | "dormant") and ``stage`` (onboarding stage).

    ``stage`` is the one filter that cannot be a WHERE clause: onboarding
    progress is deliberately derived from a tenant's own data rather than
    stored (see ``app/platform_admin/onboarding.py``), so there is no column to
    filter on. Passing it therefore materialises the rows matching the *other*
    filters and pages in Python. That is a deliberate trade - the alternative
    is a stored progress column that can drift - and it costs a few grouped
    queries over the operator's own tenant list, not a scan of tenant data.
    """
    now = now or _utcnow()
    per_page = max(1, min(per_page or DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE))
    page = max(1, page or 1)

    last_activity = last_activity_expression().label("last_activity")
    query = select(Garage, last_activity)

    if search:
        query = query.where(_search_filter(search))
    if status:
        if status not in GARAGE_STATUSES:
            raise TenantError(
                f"Unknown status {status!r}. Expected one of {list(GARAGE_STATUSES)}."
            )
        query = query.where(Garage.status == status)
    if plan:
        query = query.where(Garage.plan == plan)

    if activity:
        cutoff = now - timedelta(days=dormant_days)
        if activity == "dormant":
            query = query.where(last_activity_expression() < cutoff)
        elif activity == "active":
            query = query.where(last_activity_expression() >= cutoff)
        else:
            raise TenantError(f"Unknown activity filter {activity!r}. Expected active or dormant.")

    order_column = {
        "name": Garage.name,
        "created_at": Garage.created_at,
        "status": Garage.status,
        "last_activity": last_activity,
    }.get(sort)
    if order_column is None:
        raise TenantError(f"Unknown sort {sort!r}. Expected one of {list(SORT_KEYS)}.")

    if stage is not None and stage not in STAGE_KEYS:
        raise TenantError(f"Unknown stage {stage!r}. Expected one of {list(STAGE_KEYS)}.")

    ordered = query.order_by(order_column.desc() if descending else order_column.asc(), Garage.id)

    if stage is None:
        total = db.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = db.session.execute(ordered.limit(per_page).offset((page - 1) * per_page)).all()
        onboarding = onboarding_progress_bulk([row[0] for row in rows])
    else:
        candidates = db.session.execute(ordered).all()
        onboarding = onboarding_progress_bulk([row[0] for row in candidates])
        rows = [row for row in candidates if onboarding[row[0].id]["stage"] == stage]
        total = len(rows)
        rows = rows[(page - 1) * per_page : page * per_page]

    garages = [row[0] for row in rows]
    activity_by_id = {row[0].id: row[1] for row in rows}
    garage_ids = [garage.id for garage in garages]

    if garage_ids:
        owners = _owner_emails(garage_ids)
        customers = _counts(Customer, garage_ids)
        vehicles = _counts(Vehicle, garage_ids)
        appointments = _counts(Appointment, garage_ids)
        pending = _counts(BookingRequest, garage_ids, BookingRequest.status == "PENDING")
        employees = _counts(Employee, garage_ids)
    else:
        owners = {}
        customers = vehicles = appointments = pending = employees = {}

    cutoff = now - timedelta(days=dormant_days)

    items = []
    for garage in garages:
        last_seen = activity_by_id.get(garage.id)
        items.append(
            {
                "garage": garage,
                "owner_email": owners.get(garage.id),
                "last_activity_at": last_seen,
                "is_dormant": bool(
                    last_seen
                    and _as_utc(last_seen) < cutoff
                    and garage.status != GARAGE_STATUS_SUSPENDED
                ),
                "customer_count": customers.get(garage.id, 0),
                "vehicle_count": vehicles.get(garage.id, 0),
                "appointment_count": appointments.get(garage.id, 0),
                "pending_booking_requests": pending.get(garage.id, 0),
                "employee_count": employees.get(garage.id, 0),
                "onboarding": onboarding.get(garage.id),
            }
        )

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    }


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def get_tenant(garage_id) -> Garage | None:
    return db.session.get(Garage, garage_id)


def tenant_detail(garage: Garage, *, dormant_days: int = DEFAULT_DORMANT_DAYS) -> dict:
    """The single-tenant record behind the detail page header."""
    last_seen = db.session.scalar(select(last_activity_expression()).where(Garage.id == garage.id))
    owner_emails = _owner_emails([garage.id])

    return {
        "garage": garage,
        "owner_email": owner_emails.get(garage.id),
        "last_activity_at": last_seen,
        "is_dormant": bool(
            last_seen
            and _as_utc(last_seen) < _utcnow() - timedelta(days=dormant_days)
            and garage.status != GARAGE_STATUS_SUSPENDED
        ),
        "customer_count": db.session.scalar(
            select(func.count()).select_from(Customer).where(Customer.garage_id == garage.id)
        ),
        "vehicle_count": db.session.scalar(
            select(func.count()).select_from(Vehicle).where(Vehicle.garage_id == garage.id)
        ),
        "appointment_count": db.session.scalar(
            select(func.count()).select_from(Appointment).where(Appointment.garage_id == garage.id)
        ),
        "pending_booking_requests": db.session.scalar(
            select(func.count())
            .select_from(BookingRequest)
            .where(BookingRequest.garage_id == garage.id, BookingRequest.status == "PENDING")
        ),
        "employee_count": db.session.scalar(
            select(func.count()).select_from(Employee).where(Employee.garage_id == garage.id)
        ),
        "onboarding": onboarding_progress(garage),
    }


# --------------------------------------------------------------------------
# Mutations - each one audited in the same transaction as the change itself
# --------------------------------------------------------------------------

#: Tenant fields Platform Admin may set that aren't business contact details.
PLATFORM_FIELDS = ("plan", "trial_ends_at", "internal_notes")


def update_tenant(*, admin, garage: Garage, changes: dict) -> Garage:
    """Apply a tenant configuration change and audit exactly what moved.

    Contact details are delegated to ``update_garage_details`` so the
    allowlist stays in one place; ``plan`` / ``trial_ends_at`` /
    ``internal_notes`` are platform-only and applied here. ``status`` is not
    accepted - suspension has its own audited operation.
    """
    detail_changes = {key: value for key, value in changes.items() if key not in PLATFORM_FIELDS}
    platform_changes = {key: value for key, value in changes.items() if key in PLATFORM_FIELDS}

    if not detail_changes and not platform_changes:
        raise TenantError("Nothing to update - pass at least one field.")

    before = {key: getattr(garage, key) for key in (*detail_changes, *platform_changes)}

    if detail_changes:
        try:
            update_garage_details(garage, commit=False, **detail_changes)
        except ValueError as exc:
            raise TenantError(str(exc)) from exc

    for key, value in platform_changes.items():
        if key == "plan":
            if value is None:
                continue
            try:
                validate_plan(value)
            except UnknownPlanError as exc:
                raise TenantError(str(exc)) from exc
        setattr(garage, key, value)

    changed = {
        key: {"from": _audit_value(before[key]), "to": _audit_value(getattr(garage, key))}
        for key in before
        if before[key] != getattr(garage, key)
    }

    record_audit(
        admin=admin,
        # A plan move is its own audited event - it is what a future
        # subscription/revenue report would read - even when it arrives in the
        # same request as other edits.
        action=ACTION_TENANT_PLAN_CHANGE if "plan" in changed else ACTION_TENANT_UPDATE,
        garage=garage,
        summary=(
            f"Updated {', '.join(sorted(changed))}"
            if changed
            else "Submitted an update that changed nothing"
        ),
        details={"changed": changed},
    )
    db.session.commit()
    return garage


def _audit_value(value):
    """Audit details go into a JSON column - dates and UUIDs need rendering."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def suspend_tenant(*, admin, garage: Garage, reason: str) -> Garage:
    """Suspend a tenant: staff lose access immediately, data is untouched.

    Existing staff tokens stop working on their next request (the JWT
    blocklist loader in ``app/__init__.py``) and login is refused
    (``app/auth/routes.py``). Nothing is deleted, and an active support
    impersonation is deliberately unaffected - that is how support gets in to
    fix whatever caused the suspension.
    """
    reason = (reason or "").strip()
    if not reason:
        raise TenantError("A suspension reason is required.")
    if garage.status == GARAGE_STATUS_SUSPENDED:
        raise TenantError("This tenant is already suspended.")

    previous = garage.status
    garage.status = GARAGE_STATUS_SUSPENDED
    garage.status_changed_at = _utcnow()
    garage.suspension_reason = reason

    record_audit(
        admin=admin,
        action=ACTION_TENANT_SUSPEND,
        garage=garage,
        summary=f"Suspended {garage.name}",
        details={"reason": reason, "previous_status": previous},
    )
    db.session.commit()
    return garage


def reactivate_tenant(*, admin, garage: Garage, status: str = GARAGE_STATUS_ACTIVE) -> Garage:
    """Lift a suspension, back to ACTIVE (or TRIAL)."""
    if status == GARAGE_STATUS_SUSPENDED or status not in GARAGE_STATUSES:
        raise TenantError(f"Reactivate expects ACTIVE or TRIAL, got {status!r}.")
    if garage.status != GARAGE_STATUS_SUSPENDED:
        raise TenantError("This tenant is not suspended.")

    garage.status = status
    garage.status_changed_at = _utcnow()
    previous_reason = garage.suspension_reason
    garage.suspension_reason = None

    record_audit(
        admin=admin,
        action=ACTION_TENANT_REACTIVATE,
        garage=garage,
        summary=f"Reactivated {garage.name} as {status}",
        details={"status": status, "previous_suspension_reason": previous_reason},
    )
    db.session.commit()
    return garage
