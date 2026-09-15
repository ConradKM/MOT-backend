"""Validating and storing what a customer answered.

The client renders the form from the same configuration this module
validates against, but that is a convenience and never the authority: a
direct POST must not be able to skip a required field, invent a field, or
answer a SELECT with something that was never on offer.

Answers are stored as snapshots (see
``app/models/booking_flow/answer.py``) and, where a field declares a
binding, additionally written into the real record the business keeps.
"""

from __future__ import annotations

import uuid
from datetime import date, time

from marshmallow import ValidationError

from app.models.booking_flow.answer import BookingRequestAnswer
from app.models.booking_flow.field import (
    BINDING_ITEM_MAKE,
    BINDING_ITEM_MODEL,
    BINDING_ITEM_REFERENCE,
    BINDING_ITEM_USAGE,
    BINDING_ITEM_YEAR,
    MULTI_VALUE_FIELD_TYPES,
)

from .resolve import resolve_sections

_EMAIL_RE_MESSAGE = "Enter a valid email address."


class AnswerError(ValidationError):
    """A rejected set of answers.

    A marshmallow ValidationError so the route can report it as a 422 with
    per-field messages, exactly like any other bad input. Use :func:`reject`
    to raise one rather than constructing it directly - marshmallow leaves a
    bare string message as a string, while every other 422 this API produces
    carries a *list* per key, and clients rely on that.
    """


def reject(key, message: str) -> None:
    """Raise an AnswerError keyed by field id, in the list-per-key shape the
    rest of the API's 422s use."""
    raise AnswerError({str(key): [message]})


def _as_text(raw) -> str:
    return "" if raw is None else str(raw).strip()


def _validate_one(field, raw_value, raw_values) -> tuple[str | None, list[str]]:
    """Check one answer against its field and normalise it for storage.

    Returns ``(value, value_list)`` - only one of the two is ever populated,
    decided by the field's own type rather than by what the client sent.
    """
    label = field.label

    if field.field_type in MULTI_VALUE_FIELD_TYPES:
        values = [_as_text(v) for v in (raw_values or []) if _as_text(v)]
        if field.is_required and not values:
            reject(field.id, f"{label} is required.")
        unknown = [v for v in values if v not in field.options]
        if unknown:
            reject(field.id, f"{label}: {unknown[0]!r} is not an option.")
        return None, values

    value = _as_text(raw_value)

    if not value:
        if field.is_required:
            reject(field.id, f"{label} is required.")
        # An optional field left blank is stored as an explicit blank answer
        # rather than dropped, so the staff screen can show "asked, not
        # answered" instead of silently omitting the question.
        return None, []

    if field.field_type == "CHECKBOX":
        # Anything truthy the client can express; normalised so staff never
        # see "on"/"true"/"1" depending on which client submitted it.
        return ("Yes" if value.lower() in {"true", "yes", "on", "1"} else "No"), []

    if field.field_type == "SELECT":
        if value not in field.options:
            reject(field.id, f"{label}: {value!r} is not an option.")
        return value, []

    if field.field_type == "NUMBER":
        try:
            number = int(value)
        except ValueError:
            raise AnswerError({str(field.id): [f"{label} must be a whole number."]}) from None
        if field.min_value is not None and number < field.min_value:
            reject(field.id, f"{label} must be at least {field.min_value}.")
        if field.max_value is not None and number > field.max_value:
            reject(field.id, f"{label} must be at most {field.max_value}.")
        return str(number), []

    if field.field_type == "DATE":
        try:
            date.fromisoformat(value)
        except ValueError:
            raise AnswerError(
                {str(field.id): [f"{label} must be a date (YYYY-MM-DD)."]}
            ) from None
        return value, []

    if field.field_type == "TIME":
        try:
            time.fromisoformat(value)
        except ValueError:
            raise AnswerError({str(field.id): [f"{label} must be a time (HH:MM)."]}) from None
        return value, []

    if field.field_type == "EMAIL" and ("@" not in value or "." not in value.split("@")[-1]):
        reject(field.id, f"{label}: {_EMAIL_RE_MESSAGE}")

    if field.max_length is not None and len(value) > field.max_length:
        reject(field.id, f"{label} must be {field.max_length} characters or fewer.")

    return value, []


def validate_answers(
    garage_id: uuid.UUID, appointment_type_id: uuid.UUID | None, submitted: list[dict]
) -> list[dict]:
    """Check a submission against the workflow that actually applies to it.

    Returns one normalised record per configured field, in the order the
    customer saw them - including fields they left blank, so the staff screen
    can distinguish "not asked" from "asked and skipped".
    """
    sections = resolve_sections(garage_id, appointment_type_id)
    by_id = {str(f.id): f for section in sections for f in section.fields}

    incoming: dict[str, dict] = {}
    for entry in submitted or []:
        field_id = str(entry.get("field_id"))
        if field_id not in by_id:
            # Not merely ignored: a client sending a field that isn't in this
            # workflow is either stale or probing, and silently dropping it
            # would hide a real integration bug.
            reject("answers", f"{field_id} is not a field on this booking form.")
        incoming[field_id] = entry

    resolved = []
    order = 0
    for section in sections:
        for field in section.fields:
            entry = incoming.get(str(field.id), {})
            value, value_list = _validate_one(field, entry.get("value"), entry.get("values"))
            resolved.append(
                {
                    "field": field,
                    "order": order,
                    "section_title": section.title,
                    "label": field.label,
                    "field_type": field.field_type,
                    "value": value,
                    "value_list": value_list,
                }
            )
            order += 1

    return resolved


def persist_answers(booking_request, resolved: list[dict]) -> list[BookingRequestAnswer]:
    """Turn validated answers into snapshot rows on ``booking_request``."""
    rows = [
        BookingRequestAnswer(
            garage_id=booking_request.garage_id,
            booking_request_id=booking_request.id,
            booking_flow_field_id=r["field"].id,
            order=r["order"],
            section_title=r["section_title"],
            label=r["label"],
            field_type=r["field_type"],
            value=r["value"],
            value_list=r["value_list"],
        )
        for r in resolved
    ]
    booking_request.answers.extend(rows)
    return rows


def bound_values(resolved: list[dict]) -> dict[str, str | None]:
    """The subset of answers that also populate a real record, keyed by
    binding. Only bindings with an actual answer appear."""
    out: dict[str, str | None] = {}
    for r in resolved:
        binding = r["field"].binds_to
        if binding and r["value"]:
            out[binding] = r["value"]
    return out


def tracked_item_kwargs(bindings: dict[str, str | None]) -> dict:
    """Map bindings onto the columns of the tracked-item record.

    Returns an empty dict when the business collected no identifier for the
    item - which is the normal case for a business that doesn't track one, and
    is what lets a booking exist with no item at all.
    """
    if not bindings.get(BINDING_ITEM_REFERENCE):
        return {}

    def as_int(binding):
        raw = bindings.get(binding)
        # Already validated as a NUMBER field at this point; the guard is for
        # a binding placed on a non-numeric field, which the field schema
        # rejects but which a hand-written migration could still produce.
        try:
            return None if raw is None else int(raw)
        except (TypeError, ValueError):
            return None

    return {
        "reference": bindings[BINDING_ITEM_REFERENCE],
        "make": bindings.get(BINDING_ITEM_MAKE),
        "model": bindings.get(BINDING_ITEM_MODEL),
        "year": as_int(BINDING_ITEM_YEAR),
        "usage": as_int(BINDING_ITEM_USAGE),
    }
