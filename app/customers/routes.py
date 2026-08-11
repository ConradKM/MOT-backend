from flask_jwt_extended import jwt_required, get_jwt_identity
from flask_smorest import Blueprint
from flask import abort
from flask.views import MethodView

from app.extensions import db
from app.models.customer import Customer

from .schemas import CustomerSchema, CustomerUpdateSchema


customers_blp = Blueprint(
    "customers",
    "customers",
    url_prefix="/api/customers",
    description="Customer management",
)


@customers_blp.route("/")
class CustomerList(MethodView):

    @jwt_required()
    @customers_blp.response(200, CustomerSchema(many=True))
    def get(self):
        garage_id = get_jwt_identity()

        return Customer.query.filter_by(
            garage_id=garage_id
        ).all()

    @jwt_required()
    @customers_blp.arguments(CustomerSchema)
    @customers_blp.response(201, CustomerSchema)
    def post(self, data):
        garage_id = get_jwt_identity()

        customer = Customer(
            garage_id=garage_id,
            first_name=data["first_name"],
            last_name=data["last_name"],
            email=data.get("email"),
            phone=data.get("phone"),
        )

        db.session.add(customer)
        db.session.commit()

        return customer


@customers_blp.route("/<int:customer_id>")
class CustomerResource(MethodView):

    @jwt_required()
    @customers_blp.response(200, CustomerSchema)
    def get(self, customer_id):
        garage_id = get_jwt_identity()

        customer = Customer.query.filter_by(
            id=customer_id,
            garage_id=garage_id,
        ).first()

        if not customer:
            abort(404, description="Customer not found")

        return customer

    @jwt_required()
    @customers_blp.arguments(CustomerUpdateSchema)
    @customers_blp.response(200, CustomerSchema)
    def patch(self, data, customer_id):
        garage_id = get_jwt_identity()

        customer = Customer.query.filter_by(
            id=customer_id,
            garage_id=garage_id,
        ).first()

        if not customer:
            abort(404, description="Customer not found")

        for field, value in data.items():
            setattr(customer, field, value)

        db.session.commit()

        return customer

    @jwt_required()
    def delete(self, customer_id):
        garage_id = get_jwt_identity()

        customer = Customer.query.filter_by(
            id=customer_id,
            garage_id=garage_id,
        ).first()

        if not customer:
            abort(404, description="Customer not found")

        db.session.delete(customer)
        db.session.commit()

        return "", 204