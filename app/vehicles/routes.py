from flask import abort
from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint
from sqlalchemy.exc import IntegrityError

from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.customer import Customer
from app.models.vehicle import Vehicle

from .schemas import VehicleQueryArgsSchema, VehicleSchema, VehicleUpdateSchema

vehicles_blp = Blueprint(
    "vehicles",
    "vehicles",
    url_prefix="/api/vehicles",
    description="Vehicle management",
)


def _get_owned_customer(customer_id, garage_id):
    customer = Customer.query.filter_by(id=customer_id, garage_id=garage_id).first()

    if not customer:
        abort(422, description="customer_id does not belong to your garage.")

    return customer


@vehicles_blp.route("/")
class VehicleList(MethodView):

    @jwt_required()
    @vehicles_blp.arguments(VehicleQueryArgsSchema, location="query")
    @vehicles_blp.response(200, VehicleSchema(many=True))
    def get(self, args):
        garage_id = get_current_employee().garage_id

        query = Vehicle.query.filter_by(garage_id=garage_id)

        if args.get("registration"):
            pattern = f"%{args['registration'].strip().upper().replace(' ', '')}%"
            query = query.filter(Vehicle.registration_number.ilike(pattern))

        if args.get("customer_id") is not None:
            query = query.filter(Vehicle.customer_id == args["customer_id"])

        if args.get("mot_expiry_date") is not None:
            query = query.filter(Vehicle.mot_expiry_date == args["mot_expiry_date"])

        return query.order_by(Vehicle.registration_number).all()

    @jwt_required()
    @vehicles_blp.arguments(VehicleSchema)
    @vehicles_blp.response(201, VehicleSchema)
    def post(self, data):
        garage_id = get_current_employee().garage_id

        _get_owned_customer(data["customer_id"], garage_id)

        vehicle = Vehicle(
            garage_id=garage_id,
            customer_id=data["customer_id"],
            registration_number=data["registration_number"],
            make=data.get("make"),
            model=data.get("model"),
            year=data.get("year"),
            current_mileage=data.get("current_mileage"),
            mot_expiry_date=data.get("mot_expiry_date"),
        )

        db.session.add(vehicle)

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            abort(409, description="A vehicle with this registration number already exists.")

        return vehicle


@vehicles_blp.route("/<int:vehicle_id>")
class VehicleResource(MethodView):

    @jwt_required()
    @vehicles_blp.response(200, VehicleSchema)
    def get(self, vehicle_id):
        garage_id = get_current_employee().garage_id

        vehicle = Vehicle.query.filter_by(id=vehicle_id, garage_id=garage_id).first()

        if not vehicle:
            abort(404, description="Vehicle not found")

        return vehicle

    @jwt_required()
    @vehicles_blp.arguments(VehicleUpdateSchema)
    @vehicles_blp.response(200, VehicleSchema)
    def patch(self, data, vehicle_id):
        garage_id = get_current_employee().garage_id

        vehicle = Vehicle.query.filter_by(id=vehicle_id, garage_id=garage_id).first()

        if not vehicle:
            abort(404, description="Vehicle not found")

        if "customer_id" in data:
            _get_owned_customer(data["customer_id"], garage_id)

        for field, value in data.items():
            setattr(vehicle, field, value)

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            abort(409, description="A vehicle with this registration number already exists.")

        return vehicle

    @jwt_required()
    @vehicles_blp.response(204)
    def delete(self, vehicle_id):
        garage_id = get_current_employee().garage_id

        vehicle = Vehicle.query.filter_by(id=vehicle_id, garage_id=garage_id).first()

        if not vehicle:
            abort(404, description="Vehicle not found")

        db.session.delete(vehicle)
        db.session.commit()

        return ""
