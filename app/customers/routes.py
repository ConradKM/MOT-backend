from flask import abort
from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint

from app.auth.utils import get_current_user
from app.extensions import db
from app.models.customer import Customer

from .schemas import CustomerQueryArgsSchema, CustomerSchema, CustomerUpdateSchema

customers_blp = Blueprint(
    "customers",
    "customers",
    url_prefix="/api/customers",
    description="Customer management",
)


@customers_blp.route("/")
class CustomerList(MethodView):

    @jwt_required()
    @customers_blp.arguments(CustomerQueryArgsSchema, location="query")
    @customers_blp.response(200, CustomerSchema(many=True))
    def get(self, args):
        garage_id = get_current_user().garage_id

        query = Customer.query.filter_by(garage_id=garage_id)

        search = args.get("search")
        if search:
            pattern = f"%{search}%"
            query = query.filter(
                db.or_(
                    Customer.first_name.ilike(pattern),
                    Customer.last_name.ilike(pattern),
                    Customer.email.ilike(pattern),
                )
            )

        return query.order_by(Customer.last_name, Customer.first_name).all()

    @jwt_required()
    @customers_blp.arguments(CustomerSchema)
    @customers_blp.response(201, CustomerSchema)
    def post(self, data):
        garage_id = get_current_user().garage_id

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
        garage_id = get_current_user().garage_id

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
        garage_id = get_current_user().garage_id

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
    @customers_blp.response(204)
    def delete(self, customer_id):
        garage_id = get_current_user().garage_id

        customer = Customer.query.filter_by(
            id=customer_id,
            garage_id=garage_id,
        ).first()

        if not customer:
            abort(404, description="Customer not found")

        db.session.delete(customer)
        db.session.commit()

        return ""
