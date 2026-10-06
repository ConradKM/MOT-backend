"""Resolving and pricing a booking's add-on selection.

One place for the rules every entry point shares - staff create/edit, public
booking (plain and deposit), and public availability - so none of them can
disagree about what a selection costs or how long it takes:

- every add-on must belong to the booking's appointment type (and so to the
  caller's garage), and be ACTIVE;
- quantity is 1..the add-on's own max_quantity;
- at most one add-on per exclusivity group.

When editing, add-ons already on the booking are grandfathered: the catalogue
may have hidden them, lowered their maximum or regrouped them since, and that
must not block an unrelated change to the booking.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from flask_smorest import abort

from app.models.appointments.add_on import AddOn

if TYPE_CHECKING:
    from app.models.appointments.applied_add_on import AppliedAddOnMixin
    from app.models.appointments.appointment_type import GarageAppointmentType


@dataclass(frozen=True)
class ResolvedAddOn:
    add_on: AddOn
    quantity: int


def resolve_selection(
    appointment_type: GarageAppointmentType,
    selection: Sequence[Mapping[str, Any]] | None,
    *,
    already_applied: Mapping[UUID, int] | None = None,
) -> list[ResolvedAddOn]:
    """Validate ``[{add_on_id, quantity}, ...]`` for ``appointment_type``,
    aborting 422 on any rule violation. ``already_applied`` (add-on id ->
    quantity currently on the booking being edited) grandfathers those
    add-ons - see the module docstring."""
    if not selection:
        return []

    applied = already_applied or {}
    by_id = {a.id: a for a in appointment_type.add_ons}
    resolved: list[ResolvedAddOn] = []
    seen: set[UUID] = set()
    groups: dict[str, AddOn] = {}

    for entry in selection:
        add_on_id = entry["add_on_id"]
        quantity = entry.get("quantity", 1)
        add_on = by_id.get(add_on_id)
        if add_on is None:
            abort(422, message="add_ons contains an add-on that is not offered for this service.")
        assert add_on is not None
        if add_on.status != "ACTIVE" and add_on.id not in applied:
            abort(422, message=f"{add_on.name} is no longer available.")
        if add_on_id in seen:
            abort(422, message=f"{add_on.name} is listed more than once - use quantity instead.")
        seen.add(add_on_id)
        limit = max(add_on.max_quantity, applied.get(add_on.id, 0))
        if quantity < 1 or quantity > limit:
            abort(422, message=f"{add_on.name} can be added at most {limit} time(s).")
        if add_on.exclusivity_group:
            other = groups.get(add_on.exclusivity_group)
            # Two add-ons that were already combined on this booking stay
            # combined; only a newly added one can create a conflict.
            if other is not None and not (other.id in applied and add_on.id in applied):
                abort(
                    422,
                    message=f"{other.name} and {add_on.name} cannot be combined - choose one.",
                    errors={"reason": "add_on_exclusivity_conflict"},
                )
            groups[add_on.exclusivity_group] = add_on
        resolved.append(ResolvedAddOn(add_on=add_on, quantity=quantity))

    return resolved


def price_delta(rows: Iterable[ResolvedAddOn | AppliedAddOnMixin]) -> Decimal:
    total = Decimal("0.00")
    for row in rows:
        unit = row.add_on.price_delta if isinstance(row, ResolvedAddOn) else row.price_delta
        total += unit * row.quantity
    return total


def duration_delta(rows: Iterable[ResolvedAddOn | AppliedAddOnMixin]) -> int:
    total = 0
    for row in rows:
        unit = (
            row.add_on.duration_delta_minutes
            if isinstance(row, ResolvedAddOn)
            else row.duration_delta_minutes
        )
        total += unit * row.quantity
    return total


def total_price(base_price: Decimal | None, delta: Decimal) -> Decimal | None:
    """Base plus add-ons. An unpriced service stays unpriced: showing just the
    add-ons' sum as though it were the whole cost would understate it."""
    if base_price is None:
        return None
    total = base_price + delta
    if total < 0:
        abort(422, message="The selected add-ons would reduce the price below zero.")
    return total


def total_duration(base_minutes: int, delta: int) -> int:
    total = base_minutes + delta
    if total < 1:
        abort(422, message="The selected add-ons would reduce the duration below one minute.")
    return total


def snapshot_kwargs(row: ResolvedAddOn, garage_id: UUID) -> dict[str, Any]:
    """Column values for a new AppointmentAddOn/BookingRequestAddOn."""
    return {
        "garage_id": garage_id,
        "add_on_id": row.add_on.id,
        "name": row.add_on.name,
        "quantity": row.quantity,
        "price_delta": row.add_on.price_delta,
        "duration_delta_minutes": row.add_on.duration_delta_minutes,
    }


def selection_signature(
    rows: Iterable[ResolvedAddOn | AppliedAddOnMixin],
) -> tuple[tuple[str, int], ...]:
    """Order-independent identity of a selection - for idempotent retries,
    where the same attempt must not be replayed with a different selection."""
    pairs = []
    for row in rows:
        add_on_id = row.add_on.id if isinstance(row, ResolvedAddOn) else row.add_on_id
        pairs.append((str(add_on_id), row.quantity))
    return tuple(sorted(pairs))


def parse_query_selection(raw: str | None) -> list[dict[str, Any]]:
    """Parse the availability endpoints' ``add_ons=<uuid>[:qty],...`` query
    form (a list of objects has no clean query-string encoding)."""
    if not raw:
        return []
    selection = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        id_str, _, qty_str = part.partition(":")
        try:
            selection.append(
                {"add_on_id": UUID(id_str), "quantity": int(qty_str) if qty_str else 1}
            )
        except ValueError:
            abort(422, message="add_ons must be a comma-separated list of id[:quantity].")
    return selection
