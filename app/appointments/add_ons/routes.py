from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.appointments.types.routes import get_owned_appointment_type
from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.appointments.add_on import AddOn

from .schemas import AddOnSchema, AddOnUpdateSchema

add_ons_blp = Blueprint(
    "add-ons",
    "add-ons",
    url_prefix="/api/appointment-types/<uuid:appointment_type_id>/add-ons",
    description="Optional extras on an appointment type that adjust an appointment's "
    "price and/or duration",
)


def _normalise_group(data):
    # "" and "  " mean "no group", not a group literally named blank - which
    # would otherwise make every blank-grouped add-on mutually exclusive.
    if "exclusivity_group" in data and data["exclusivity_group"] is not None:
        data["exclusivity_group"] = data["exclusivity_group"].strip() or None
    return data


@add_ons_blp.route("")
class AddOnList(MethodView):
    @jwt_required()
    @add_ons_blp.response(200, AddOnSchema(many=True))
    def get(self, appointment_type_id):
        garage_id = get_current_employee().garage_id
        return get_owned_appointment_type(appointment_type_id, garage_id).add_ons

    @jwt_required()
    @owner_required
    @add_ons_blp.arguments(AddOnSchema)
    @add_ons_blp.response(201, AddOnSchema)
    def post(self, data, appointment_type_id):
        garage_id = get_current_employee().garage_id
        appointment_type = get_owned_appointment_type(appointment_type_id, garage_id)

        add_on = AddOn(
            garage_id=garage_id, appointment_type_id=appointment_type.id, **_normalise_group(data)
        )
        db.session.add(add_on)
        db.session.commit()

        return add_on


@add_ons_blp.route("/<uuid:add_on_id>")
class AddOnResource(MethodView):
    def _get_owned_add_on(self, appointment_type_id, garage_id, add_on_id):
        get_owned_appointment_type(appointment_type_id, garage_id)
        add_on = AddOn.query.filter_by(
            id=add_on_id, appointment_type_id=appointment_type_id, garage_id=garage_id
        ).first()

        if not add_on:
            abort(404, message="Add-on not found")

        return add_on

    @jwt_required()
    @add_ons_blp.response(200, AddOnSchema)
    def get(self, appointment_type_id, add_on_id):
        garage_id = get_current_employee().garage_id
        return self._get_owned_add_on(appointment_type_id, garage_id, add_on_id)

    @jwt_required()
    @owner_required
    @add_ons_blp.arguments(AddOnUpdateSchema)
    @add_ons_blp.response(200, AddOnSchema)
    def patch(self, data, appointment_type_id, add_on_id):
        garage_id = get_current_employee().garage_id
        add_on = self._get_owned_add_on(appointment_type_id, garage_id, add_on_id)

        # Existing bookings keep their snapshotted name/price/duration, so an
        # edit here only affects selections made from now on.
        for field, value in _normalise_group(data).items():
            setattr(add_on, field, value)
        db.session.commit()

        return add_on

    @jwt_required()
    @owner_required
    @add_ons_blp.response(204)
    def delete(self, appointment_type_id, add_on_id):
        garage_id = get_current_employee().garage_id
        add_on = self._get_owned_add_on(appointment_type_id, garage_id, add_on_id)

        # Safe even when in use: applied rows hold their own snapshot and
        # their add_on_id is SET NULL. Hide it instead to keep the link.
        db.session.delete(add_on)
        db.session.commit()

        return ""
