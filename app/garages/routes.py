from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint

from app.auth.decorators import owner_required
from app.auth.utils import get_current_user
from app.extensions import db

from .schemas import GarageSchema, GarageUpdateSchema

garages_blp = Blueprint(
    "garage",
    "garage",
    url_prefix="/api/garage",
    description="Garage (tenant) management",
)


@garages_blp.route("")
class GarageResource(MethodView):

    @jwt_required()
    @garages_blp.response(200, GarageSchema)
    def get(self):
        return get_current_user().garage

    @jwt_required()
    @owner_required
    @garages_blp.arguments(GarageUpdateSchema)
    @garages_blp.response(200, GarageSchema)
    def patch(self, data):
        garage = get_current_user().garage

        for field, value in data.items():
            setattr(garage, field, value)

        db.session.commit()

        return garage
