from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.garage import Garage

from .capacity import capacity_summary
from .details import update_garage_details
from .schemas import (
    CapacitySummarySchema,
    GarageDetailsUpdateSchema,
    GarageSchema,
    PublicGarageSchema,
)

garages_blp = Blueprint(
    "garage",
    "garage",
    url_prefix="/api/garage",
    description="Business (tenant) management",
)

public_garages_blp = Blueprint(
    "public_garages",
    "public_garages",
    url_prefix="/api/public/garages",
    description="Unauthenticated business lookup for the public customer booking flow",
)


@garages_blp.route("")
class GarageResource(MethodView):
    @jwt_required()
    @garages_blp.response(200, GarageSchema)
    def get(self):
        """The caller's business record (contact details) - read-only."""
        return get_current_employee().garage

    @jwt_required()
    @owner_required
    @garages_blp.arguments(GarageDetailsUpdateSchema)
    @garages_blp.response(200, GarageSchema)
    def patch(self, data):
        """Update this business's own contact / profile details. OWNER only.

        Strict allowlist: ``name``, ``email``, ``phone``, ``address``,
        ``postcode``, ``website`` (``GarageDetailsUpdateSchema``). The public
        ``slug``, the internal ``id``, ``layout_variant`` and every system
        field stay platform-controlled - they are not in the schema, so a
        request that names one is rejected (422). An empty body is a 422 too.
        The business is always the caller's own (from the JWT).
        """
        garage = get_current_employee().garage
        try:
            update_garage_details(garage, **data)
        except ValueError as exc:
            abort(422, message=str(exc))
        return garage


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
