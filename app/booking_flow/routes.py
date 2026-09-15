from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.appointments.image_schemas import ReorderSchema
from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_flow.field import BookingFlowField
from app.models.booking_flow.section import BookingFlowSection

from .presets import PRESETS, apply_preset
from .schemas import (
    ApplyPresetSchema,
    BookingFlowFieldSchema,
    BookingFlowFieldUpdateSchema,
    BookingFlowQueryArgsSchema,
    BookingFlowSectionSchema,
    BookingFlowSectionUpdateSchema,
)

booking_flow_blp = Blueprint(
    "booking-flow",
    "booking-flow",
    url_prefix="/api/booking-flow",
    description="What a business asks its customers while they book - "
    "configurable sections and fields, with an optional per-service override",
)


def _owned_section(section_id, garage_id):
    section = BookingFlowSection.query.filter_by(id=section_id, garage_id=garage_id).first()
    if not section:
        abort(404, message="Booking flow section not found")
    return section


def _owned_field(field_id, garage_id):
    field = BookingFlowField.query.filter_by(id=field_id, garage_id=garage_id).first()
    if not field:
        abort(404, message="Booking flow field not found")
    return field


def _validate_appointment_type(appointment_type_id, garage_id):
    """Reject a service id that isn't this business's own - otherwise a
    section could be scoped to another tenant's service."""
    if appointment_type_id is None:
        return
    exists = GarageAppointmentType.query.filter_by(
        id=appointment_type_id, garage_id=garage_id
    ).first()
    if exists is None:
        abort(422, message="appointment_type_id is not a service for this business.")


@booking_flow_blp.route("/sections")
class BookingFlowSectionList(MethodView):
    @jwt_required()
    @booking_flow_blp.arguments(BookingFlowQueryArgsSchema, location="query")
    @booking_flow_blp.response(200, BookingFlowSectionSchema(many=True))
    def get(self, args):
        garage_id = get_current_employee().garage_id

        query = BookingFlowSection.query.filter_by(garage_id=garage_id)
        if args.get("default_only"):
            query = query.filter(BookingFlowSection.appointment_type_id.is_(None))
        elif args.get("appointment_type_id") is not None:
            query = query.filter(
                BookingFlowSection.appointment_type_id == args["appointment_type_id"]
            )

        # Inactive sections are included: the settings editor has to be able
        # to see and re-enable one it switched off.
        return query.order_by(BookingFlowSection.order, BookingFlowSection.title).all()

    @jwt_required()
    @owner_required
    @booking_flow_blp.arguments(BookingFlowSectionSchema)
    @booking_flow_blp.response(201, BookingFlowSectionSchema)
    def post(self, data):
        garage_id = get_current_employee().garage_id
        _validate_appointment_type(data.get("appointment_type_id"), garage_id)

        section = BookingFlowSection(
            garage_id=garage_id,
            appointment_type_id=data.get("appointment_type_id"),
            title=data["title"],
            description=data.get("description"),
            order=data.get("order") or 0,
            is_active=data.get("is_active", True),
        )

        db.session.add(section)
        db.session.commit()

        return section


@booking_flow_blp.route("/sections/order")
class BookingFlowSectionReorder(MethodView):
    @jwt_required()
    @owner_required
    @booking_flow_blp.arguments(ReorderSchema)
    @booking_flow_blp.response(204)
    def put(self, data):
        """Reorder one workflow's sections in a single transaction.

        The ids must all belong to the same workflow (the business default, or
        one service's override) and must be all of it: ordering is only
        meaningful within a workflow, and a partial list would leave the
        omitted sections at stale positions.
        """
        garage_id = get_current_employee().garage_id
        ids = data["ids"]

        sections = BookingFlowSection.query.filter(
            BookingFlowSection.garage_id == garage_id,
            BookingFlowSection.id.in_(ids),
        ).all()
        if len(sections) != len(set(ids)):
            abort(422, message="ids must all be sections for this business.")

        scopes = {s.appointment_type_id for s in sections}
        if len(scopes) > 1:
            abort(422, message="ids must all belong to the same workflow.")

        scope = scopes.pop() if scopes else None
        siblings = BookingFlowSection.query.filter_by(
            garage_id=garage_id, appointment_type_id=scope
        ).all()
        if len(siblings) != len(set(ids)):
            abort(422, message="ids must list every section in this workflow exactly once.")

        by_id = {s.id: s for s in sections}
        for position, section_id in enumerate(ids):
            by_id[section_id].order = position

        db.session.commit()

        return ""


@booking_flow_blp.route("/sections/<uuid:section_id>")
class BookingFlowSectionResource(MethodView):
    @jwt_required()
    @booking_flow_blp.response(200, BookingFlowSectionSchema)
    def get(self, section_id):
        return _owned_section(section_id, get_current_employee().garage_id)

    @jwt_required()
    @owner_required
    @booking_flow_blp.arguments(BookingFlowSectionUpdateSchema)
    @booking_flow_blp.response(200, BookingFlowSectionSchema)
    def patch(self, data, section_id):
        garage_id = get_current_employee().garage_id
        section = _owned_section(section_id, garage_id)

        if "appointment_type_id" in data:
            _validate_appointment_type(data["appointment_type_id"], garage_id)

        for field, value in data.items():
            setattr(section, field, value)

        db.session.commit()

        return section

    @jwt_required()
    @owner_required
    @booking_flow_blp.response(204)
    def delete(self, section_id):
        garage_id = get_current_employee().garage_id
        section = _owned_section(section_id, garage_id)

        # No 409 for a section with answers against it: the answers are
        # snapshots (see app/models/booking_flow/answer.py), so past bookings
        # keep rendering exactly as they were submitted. Their FK just goes
        # null. A business must be able to stop asking a question.
        db.session.delete(section)
        db.session.commit()

        return ""


@booking_flow_blp.route("/sections/<uuid:section_id>/fields")
class BookingFlowFieldList(MethodView):
    @jwt_required()
    @owner_required
    @booking_flow_blp.arguments(BookingFlowFieldSchema)
    @booking_flow_blp.response(201, BookingFlowFieldSchema)
    def post(self, data, section_id):
        garage_id = get_current_employee().garage_id
        section = _owned_section(section_id, garage_id)

        field = BookingFlowField(
            garage_id=garage_id,
            booking_flow_section_id=section.id,
            label=data["label"],
            help_text=data.get("help_text"),
            placeholder=data.get("placeholder"),
            field_type=data.get("field_type") or "TEXT",
            is_required=data.get("is_required", False),
            options=data.get("options") or [],
            order=data.get("order") or 0,
            min_value=data.get("min_value"),
            max_value=data.get("max_value"),
            max_length=data.get("max_length"),
            binds_to=data.get("binds_to"),
        )

        db.session.add(field)
        db.session.commit()

        return field


@booking_flow_blp.route("/sections/<uuid:section_id>/fields/order")
class BookingFlowFieldReorder(MethodView):
    @jwt_required()
    @owner_required
    @booking_flow_blp.arguments(ReorderSchema)
    @booking_flow_blp.response(204)
    def put(self, data, section_id):
        garage_id = get_current_employee().garage_id
        section = _owned_section(section_id, garage_id)

        by_id = {f.id: f for f in section.fields}
        ids = data["ids"]
        if len(set(ids)) != len(ids) or set(ids) != set(by_id):
            abort(422, message="ids must list every field in this section exactly once.")

        for position, field_id in enumerate(ids):
            by_id[field_id].order = position

        db.session.commit()

        return ""


@booking_flow_blp.route("/fields/<uuid:field_id>")
class BookingFlowFieldResource(MethodView):
    @jwt_required()
    @owner_required
    @booking_flow_blp.arguments(BookingFlowFieldUpdateSchema)
    @booking_flow_blp.response(200, BookingFlowFieldSchema)
    def patch(self, data, field_id):
        garage_id = get_current_employee().garage_id
        field = _owned_field(field_id, garage_id)

        for attr, value in data.items():
            setattr(field, attr, value)

        db.session.commit()

        return field

    @jwt_required()
    @owner_required
    @booking_flow_blp.response(204)
    def delete(self, field_id):
        garage_id = get_current_employee().garage_id
        field = _owned_field(field_id, garage_id)

        # Same reasoning as deleting a section - answers are snapshots.
        db.session.delete(field)
        db.session.commit()

        return ""


@booking_flow_blp.route("/presets")
class BookingFlowPresets(MethodView):
    @jwt_required()
    @booking_flow_blp.response(200)
    def get(self):
        """The starting workflows a business can adopt. A preset is only ever
        seed data - once applied it is ordinary, editable configuration."""
        return {
            "presets": [
                {"key": key, "sections": [title for title, _, _ in spec]}
                for key, spec in PRESETS.items()
            ]
        }

    @jwt_required()
    @owner_required
    @booking_flow_blp.arguments(ApplyPresetSchema)
    @booking_flow_blp.response(201, BookingFlowSectionSchema(many=True))
    def post(self, data):
        """Seed the business's *default* workflow from a preset.

        Refuses when one already exists rather than merging or replacing:
        silently duplicating every section, or discarding configuration the
        business has already built, are both worse than saying no.
        """
        garage_id = get_current_employee().garage_id

        if data["preset"] not in PRESETS:
            abort(422, message=f"Unknown preset. Choose one of: {', '.join(PRESETS)}.")

        existing = BookingFlowSection.query.filter_by(
            garage_id=garage_id, appointment_type_id=None
        ).count()
        if existing:
            abort(409, message="This business already has a default booking workflow.")

        sections = apply_preset(garage_id, data["preset"])
        db.session.commit()

        return sections
