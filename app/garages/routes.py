from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.garage import Garage
from app.public_booking.payload import public_garage_payload
from app.public_booking.schemas import PublicGarageDetailSchema

from .capacity import capacity_summary
from .details import update_garage_details
from .schemas import (
    BookingRequestAutoAcceptSchema,
    BookingRequestSettingsSchema,
    CapacitySummarySchema,
    GarageDetailsUpdateSchema,
    GarageSchema,
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


@garages_blp.route("/booking-request-settings")
class BookingRequestSettings(MethodView):
    @jwt_required()
    @garages_blp.response(200, GarageSchema)
    def get(self):
        return get_current_employee().garage

    @jwt_required()
    @owner_required
    @garages_blp.arguments(BookingRequestSettingsSchema)
    @garages_blp.response(200, GarageSchema)
    def put(self, data):
        garage = get_current_employee().garage
        garage.auto_accept_booking_requests = data["auto_accept_booking_requests"]
        if not garage.auto_accept_booking_requests:
            garage.auto_accept_booking_requests_enabled = False
        db.session.commit()
        return garage


@garages_blp.route("/booking-request-settings/auto-accept")
class BookingRequestAutoAccept(MethodView):
    @jwt_required()
    @owner_required
    @garages_blp.arguments(BookingRequestAutoAcceptSchema)
    @garages_blp.response(200, GarageSchema)
    def put(self, data):
        garage = get_current_employee().garage
        if not garage.auto_accept_booking_requests:
            abort(409, message="Allow automatic acceptance before enabling auto-accept.")
        garage.auto_accept_booking_requests_enabled = data["auto_accept_booking_requests_enabled"]
        db.session.commit()
        return garage


@public_garages_blp.route("/")
class PublicGarageList(MethodView):
    @public_garages_blp.response(200, PublicGarageDetailSchema(many=True))
    def get(self):
        return [public_garage_payload(g) for g in Garage.query.order_by(Garage.name).all()]


@public_garages_blp.route("/<uuid:garage_id>")
class PublicGarageResource(MethodView):
    @public_garages_blp.response(200, PublicGarageDetailSchema)
    def get(self, garage_id):
        """The same payload as GET /api/public/<slug>, by id instead.

        This is the /book/:garageId entry point - the one the QR codes and
        onboarding emails point at - so it must not be a second, slightly
        different view of the same page. Both call
        app/public_booking/payload.py.
        """
        garage = db.session.get(Garage, garage_id)

        if not garage:
            abort(404, message="Garage not found")

        return public_garage_payload(garage)
