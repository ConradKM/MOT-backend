"""Services (``garage_appointment_types``): parse, match, create.

The one definition of a service row, shared by the onboarding spec
(``app/garages/business_onboarding.py``, and through it the CLI and Platform
Admin provisioning) and by bulk import from a pasted price list.

A service row may carry its add-ons (``add_ons``), which are created with it.

Re-running an import is safe: a row whose name matches an existing service of
the business (case-insensitive, trimmed) is reported as ``EXISTS`` and left
untouched, add-ons included. Nothing is ever updated by an import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func

from app.models.appointments.add_on import (
    ADD_ON_MAX_QUANTITY_LIMIT,
    ADD_ON_STATUSES,
    AddOn,
)
from app.models.appointments.appointment_type import (
    APPOINTMENT_TYPE_STATUSES,
    GarageAppointmentType,
)
from app.models.appointments.appointment_type_group import AppointmentTypeGroup

from .result import (
    MAX_SERVICES,
    STATUS_EXISTS,
    ImportResult,
    ItemResult,
    read_rows,
    text_field,
    unknown_key_warnings,
)

SERVICE_FIELDS = {
    "name",
    "description",
    "base_price",
    "default_duration_minutes",
    "status",
    "group",
    "add_ons",
}

ADD_ON_FIELDS = {
    "name",
    "description",
    "price_delta",
    "duration_delta_minutes",
    "max_quantity",
    "exclusivity_group",
    "status",
}

#: Column sizes on ``garage_appointment_types`` / ``appointment_type_groups``.
NAME_MAX = 100
DESCRIPTION_MAX = 500
GROUP_NAME_MAX = 100
#: Numeric(10, 2).
PRICE_MAX = Decimal("99999999.99")
DURATION_MAX = 24 * 60
#: ``add_ons.exclusivity_group`` is String(50).
EXCLUSIVITY_GROUP_MAX = 50
#: A sanity bound per service, like MAX_SERVICES per import.
MAX_ADD_ONS_PER_SERVICE = 50


@dataclass
class AddOnSpec:
    name: str
    description: str | None = None
    price_delta: str = "0.00"  # signed decimal string
    duration_delta_minutes: int = 0  # signed
    max_quantity: int = 1
    exclusivity_group: str | None = None
    status: str = "ACTIVE"


@dataclass
class ServiceSpec:
    name: str
    description: str | None = None
    base_price: str | None = None  # decimal string, e.g. "54.85"
    default_duration_minutes: int | None = None
    status: str = "ACTIVE"
    # The group this service belongs to, named rather than id'd - a spec is
    # written by hand before any row exists. Groups are created implicitly, in
    # first-appearance order; None leaves the service ungrouped.
    group: str | None = None
    # In display order. Created with the service; never added to an existing one.
    add_ons: list[AddOnSpec] = field(default_factory=list)


def name_key(name: str) -> str:
    """How two service (or group) names are compared: trimmed, case-folded."""
    return name.strip().casefold()


# --------------------------------------------------------------------------
# Parsing (pure)
# --------------------------------------------------------------------------


def parse_service_row(raw: Any, path: str, item: ItemResult) -> ServiceSpec | None:
    """Validate one service row, recording every problem on ``item``.

    Collects all field errors rather than stopping at the first, so a preview
    can show everything wrong with a row at once. Returns the spec only when
    the row has no errors.
    """
    if not isinstance(raw, dict):
        item.error(path, "Expected an object with at least a 'name'.")
        return None

    unknown_key_warnings(item, raw, SERVICE_FIELDS, path)

    errors_before = len(item.errors)
    name = text_field(item, raw, "name", path, max_length=NAME_MAX)
    if name is None and len(item.errors) == errors_before:
        item.error(f"{path}.name", "Required - every service needs a name.")
    description = text_field(item, raw, "description", path, max_length=DESCRIPTION_MAX)
    group = text_field(item, raw, "group", path, max_length=GROUP_NAME_MAX)

    price = money_field(item, raw, "base_price", path, signed=False)

    duration = raw.get("default_duration_minutes")
    if duration is not None and (
        isinstance(duration, bool)
        or not isinstance(duration, int)
        or not 0 < duration <= DURATION_MAX
    ):
        item.error(
            f"{path}.default_duration_minutes",
            f"must be a positive whole number of minutes, at most {DURATION_MAX}.",
        )
        duration = None

    status = "ACTIVE"
    raw_status = raw.get("status")
    if raw_status is not None:
        if not isinstance(raw_status, str):
            item.error(f"{path}.status", f"must be one of {list(APPOINTMENT_TYPE_STATUSES)}.")
        else:
            status = (raw_status.strip() or "ACTIVE").upper()
            if status not in APPOINTMENT_TYPE_STATUSES:
                item.error(f"{path}.status", f"{status!r} not in {APPOINTMENT_TYPE_STATUSES}.")

    add_ons = parse_add_on_rows(raw.get("add_ons"), f"{path}.add_ons", item)

    if item.errors or name is None:
        return None
    return ServiceSpec(
        name=name,
        description=description,
        base_price=price,
        default_duration_minutes=duration,
        status=status,
        group=group,
        add_ons=add_ons,
    )


def money_field(item: ItemResult, raw: dict, name: str, path: str, *, signed: bool) -> str | None:
    """A price as a decimal string, ``None`` when absent. Pounds and pence,
    within Numeric(10, 2); negative only when ``signed`` (an add-on's delta)."""
    field_path = f"{path}.{name}"
    value = raw.get(name)
    if isinstance(value, bool) or not (value is None or isinstance(value, (str, int, float))):
        item.error(field_path, 'Expected a price like "54.85".')
        return None
    price = None if value is None else (str(value).strip() or None)
    if price is None:
        return None
    try:
        amount = Decimal(price)
    except InvalidOperation:
        item.error(field_path, f'{price!r} is not a number, e.g. "54.85".')
        return None
    if not amount.is_finite():
        item.error(field_path, f'{price!r} is not a number, e.g. "54.85".')
    elif amount < 0 and not signed:
        item.error(field_path, "must not be negative.")
    elif abs(amount) > PRICE_MAX:
        item.error(field_path, f"must be at most {PRICE_MAX}.")
    elif amount != amount.quantize(Decimal("0.01")):
        item.error(field_path, "at most two decimal places (pounds and pence).")
    else:
        return price
    return None


def parse_add_on_rows(raw: Any, path: str, item: ItemResult) -> list[AddOnSpec]:
    """A service row's ``add_ons`` list, recording every problem on ``item``
    (the service's row - an add-on is part of it, not a row of its own)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        item.error(path, "Expected a list of add-ons.")
        return []
    if len(raw) > MAX_ADD_ONS_PER_SERVICE:
        item.error(path, f"At most {MAX_ADD_ONS_PER_SERVICE} add-ons per service.")
        return []

    specs = []
    seen: dict[str, int] = {}
    for index, entry in enumerate(raw):
        spec = parse_add_on_row(entry, f"{path}[{index}]", item)
        if spec is None:
            continue
        key = name_key(spec.name)
        if key in seen:
            item.error(
                f"{path}[{index}].name",
                f"Same name as {path}[{seen[key]}] - remove one of them.",
            )
            continue
        seen[key] = index
        specs.append(spec)
    return specs


def parse_add_on_row(raw: Any, path: str, item: ItemResult) -> AddOnSpec | None:
    if not isinstance(raw, dict):
        item.error(path, "Expected an object with at least a 'name'.")
        return None

    unknown_key_warnings(item, raw, ADD_ON_FIELDS, path)

    errors_before = len(item.errors)
    name = text_field(item, raw, "name", path, max_length=NAME_MAX)
    if name is None and len(item.errors) == errors_before:
        item.error(f"{path}.name", "Required - every add-on needs a name.")
    description = text_field(item, raw, "description", path, max_length=DESCRIPTION_MAX)
    exclusivity_group = text_field(
        item, raw, "exclusivity_group", path, max_length=EXCLUSIVITY_GROUP_MAX
    )
    price_delta = money_field(item, raw, "price_delta", path, signed=True)

    duration = raw.get("duration_delta_minutes")
    if duration is None:
        duration = 0
    elif (
        isinstance(duration, bool)
        or not isinstance(duration, int)
        or not -DURATION_MAX <= duration <= DURATION_MAX
    ):
        item.error(
            f"{path}.duration_delta_minutes",
            f"must be a whole number of minutes between -{DURATION_MAX} and {DURATION_MAX}.",
        )

    max_quantity = raw.get("max_quantity")
    if max_quantity is None:
        max_quantity = 1
    elif (
        isinstance(max_quantity, bool)
        or not isinstance(max_quantity, int)
        or not 1 <= max_quantity <= ADD_ON_MAX_QUANTITY_LIMIT
    ):
        item.error(
            f"{path}.max_quantity",
            f"must be a whole number from 1 to {ADD_ON_MAX_QUANTITY_LIMIT}.",
        )

    status = "ACTIVE"
    raw_status = raw.get("status")
    if raw_status is not None:
        if not isinstance(raw_status, str):
            item.error(f"{path}.status", f"must be one of {list(ADD_ON_STATUSES)}.")
        else:
            status = (raw_status.strip() or "ACTIVE").upper()
            if status not in ADD_ON_STATUSES:
                item.error(f"{path}.status", f"{status!r} not in {ADD_ON_STATUSES}.")

    if len(item.errors) > errors_before or name is None:
        return None
    return AddOnSpec(
        name=name,
        description=description,
        price_delta=price_delta or "0.00",
        duration_delta_minutes=duration,
        max_quantity=max_quantity,
        exclusivity_group=exclusivity_group,
        status=status,
    )


def service_normalised(spec: ServiceSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "description": spec.description,
        "base_price": None if spec.base_price is None else str(Decimal(spec.base_price)),
        "default_duration_minutes": spec.default_duration_minutes,
        "status": spec.status,
        "group": spec.group,
        "add_ons": [
            {
                "name": a.name,
                "description": a.description,
                "price_delta": str(Decimal(a.price_delta)),
                "duration_delta_minutes": a.duration_delta_minutes,
                "max_quantity": a.max_quantity,
                "exclusivity_group": a.exclusivity_group,
                "status": a.status,
            }
            for a in spec.add_ons
        ],
    }


# --------------------------------------------------------------------------
# Creation (needs a session)
# --------------------------------------------------------------------------


def ensure_service_groups(
    garage,
    names: list[str],
    session,
    declared: dict[str, tuple[str | None, str | None]] | None = None,
) -> dict[str, AppointmentTypeGroup]:
    """Every group in ``names``, keyed by :func:`name_key`, creating the ones
    the business doesn't have yet.

    An existing group with the same name (case-insensitive) is reused as it
    is. New groups are appended after the business's existing ones, in
    first-appearance order - the order a human wrote them in, and so the order
    they should be sold in. ``declared`` optionally carries a new group's
    ``(description, display_mode)``, which is how the onboarding spec's
    ``service_groups`` section sets them.
    """
    declared = {name_key(k): v for k, v in (declared or {}).items()}
    existing = (
        session.query(AppointmentTypeGroup)
        .filter(AppointmentTypeGroup.garage_id == garage.id)
        .all()
    )
    groups: dict[str, AppointmentTypeGroup] = {name_key(g.name): g for g in existing}
    next_order = max((g.order for g in existing), default=-1) + 1

    for name in names:
        key = name_key(name)
        if key in groups:
            continue
        description, display_mode = declared.get(key, (None, None))
        row = AppointmentTypeGroup(
            garage_id=garage.id,
            name=name,
            description=description,
            display_mode=display_mode,
            order=next_order,
        )
        next_order += 1
        session.add(row)
        groups[key] = row

    session.flush()
    return groups


def create_services(
    garage,
    specs: list[ServiceSpec],
    session,
    groups: dict[str, AppointmentTypeGroup] | None = None,
) -> list[GarageAppointmentType]:
    """Create one service per spec, in order, after the business's existing
    services in the same group - so spec order survives as display order."""
    groups = groups or {}

    # Position within the service's own group (or within the ungrouped list).
    next_order: dict[Any, int] = {
        group_id: (top or 0) + 1
        for group_id, top in session.query(
            GarageAppointmentType.group_id, func.max(GarageAppointmentType.order)
        )
        .filter(GarageAppointmentType.garage_id == garage.id)
        .group_by(GarageAppointmentType.group_id)
        .all()
    }

    created = []
    for s in specs:
        group = groups.get(name_key(s.group)) if s.group else None
        group_id = None if group is None else group.id
        order = next_order.get(group_id, 0)
        next_order[group_id] = order + 1

        row = GarageAppointmentType(
            garage_id=garage.id,
            name=s.name,
            description=s.description,
            base_price=None if s.base_price is None else Decimal(s.base_price),
            default_duration_minutes=s.default_duration_minutes,
            status=s.status,
            group_id=group_id,
            order=order,
        )
        row.add_ons = [
            AddOn(
                garage_id=garage.id,
                name=a.name,
                description=a.description,
                price_delta=Decimal(a.price_delta),
                duration_delta_minutes=a.duration_delta_minutes,
                max_quantity=a.max_quantity,
                exclusivity_group=a.exclusivity_group,
                status=a.status,
                order=add_on_order,
            )
            for add_on_order, a in enumerate(s.add_ons)
        ]
        session.add(row)
        created.append(row)
    session.flush()
    return created


def import_services(garage, payload: Any, session) -> ImportResult:
    """Validate a pasted ``{"services": [...]}`` and create the new ones.

    Runs in the caller's transaction and only flushes: the caller commits or,
    for a preview, rolls back - so a preview is this exact function.
    """
    rows, payload_warnings = read_rows(payload, "services", MAX_SERVICES)
    result = ImportResult(kind="services", warnings=payload_warnings)

    existing = {
        name_key(service.name): service
        for service in session.query(GarageAppointmentType)
        .filter(GarageAppointmentType.garage_id == garage.id)
        .all()
    }
    seen_in_payload: dict[str, int] = {}
    to_create: list[tuple[ItemResult, ServiceSpec]] = []

    for index, raw in enumerate(rows):
        item = ItemResult(index=index)
        result.items.append(item)
        path = f"services[{index}]"
        spec = parse_service_row(raw, path, item)
        if spec is None:
            continue
        item.normalised = service_normalised(spec)

        key = name_key(spec.name)
        match = existing.get(key)
        if match is not None:
            item.status = STATUS_EXISTS
            item.normalised["existing"] = {"id": str(match.id), "name": match.name}
            if spec.add_ons:
                item.normalised["add_ons"] = []
                item.warn(
                    f"{path}.add_ons",
                    "This service already exists, so its add-ons are not imported - "
                    "add them on the service instead.",
                )
            continue
        if key in seen_in_payload:
            item.error(
                f"{path}.name",
                f"Same name as services[{seen_in_payload[key]}] - remove one of them.",
            )
            continue
        seen_in_payload[key] = index
        to_create.append((item, spec))

    if to_create:
        known_groups = {
            name_key(g.name)
            for g in session.query(AppointmentTypeGroup)
            .filter(AppointmentTypeGroup.garage_id == garage.id)
            .all()
        }
        group_names = [spec.group for _, spec in to_create if spec.group]
        for item, spec in to_create:
            item.normalised["group_is_new"] = bool(
                spec.group and name_key(spec.group) not in known_groups
            )
        groups = ensure_service_groups(garage, group_names, session)
        create_services(garage, [spec for _, spec in to_create], session, groups)

    return result
