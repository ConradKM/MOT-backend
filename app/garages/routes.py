from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.garage import Garage

from .capacity import capacity_summary
from .schemas import (
    CapacitySummarySchema,
    GarageSchema,
    PublicGarageSchema,
)

garages_blp = Blueprint(
    "garage",
    "garage",
    url_prefix="/api/garage",
    description="Garage (tenant) management",
)

public_garages_blp = Blueprint(
    "public_garages",
    "public_garages",
    url_prefix="/api/public/garages",
    description="Unauthenticated garage lookup for the public customer booking flow",
)


@garages_blp.route("")
class GarageResource(MethodView):

    @jwt_required()
    @garages_blp.response(200, GarageSchema)
    def get(self):
        """The caller's garage (business details) - read-only."""
        return get_current_employee().garage

    @jwt_required()
    @garages_blp.response(403)
    def patch(self):
        """Business details are platform-managed.

        Garage users - owners included - cannot edit them here; a developer
        updates a tenant with `flask update-garage-details` (see
        docs/GARAGE_ONBOARDING.md). Kept as an explicit 403 so the block is
        obvious rather than a bare 405.
        """
        abort(
            403,
            message="Garage details are managed by the platform administrator. "
            "Please contact them to change your business information.",
        )


@garages_blp.route("/capacity/summary")
class GarageCapacitySummary(MethodView):

    @jwt_required()
    @garages_blp.response(200, CapacitySummarySchema)
    def get(self):
        garage = get_current_employee().garage
        return capacity_summary(garage)


@public_garages_blp.route("/")
class PublicGarageList(MethodView):

    @public_garages_blp.response(200, PublicGarageSchema(many=True))
    def get(self):
        return Garage.query.order_by(Garage.name).all()


@public_garages_blp.route("/<uuid:garage_id>")
class PublicGarageResource(MethodView):

    @public_garages_blp.response(200, PublicGarageSchema)
    def get(self, garage_id):
        garage = db.session.get(Garage, garage_id)

        if not garage:
            abort(404, message="Garage not found")

        return garage
