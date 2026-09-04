from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.appointments.appointment import Appointment
from app.models.customer import Customer
from app.models.vehicle import Vehicle

from .schemas import (
    CustomerDeleteResultSchema,
    CustomerQueryArgsSchema,
    CustomerSchema,
    CustomerUpdateSchema,
)

customers_blp = Blueprint(
    "customers",
    "customers",
    url_prefix="/api/customers",
    description="Customer management",
)


def _is_referenced(customer_id) -> bool:
    """True if deleting this customer would cascade away real history
    (vehicles and/or appointments) - see CustomerResource.delete."""
    has_vehicle = Vehicle.query.filter_by(customer_id=customer_id).first() is not None
    has_appointment = (
        Appointment.query.filter_by(customer_id=customer_id).first() is not None
    )
    return has_vehicle or has_appointment


@customers_blp.route("/")
class CustomerList(MethodView):

    @jwt_required()
    @customers_blp.arguments(CustomerQueryArgsSchema, location="query")
    @customers_blp.response(200, CustomerSchema(many=True))
    def get(self, args):
        garage_id = get_current_employee().garage_id

        query = Customer.query.filter_by(garage_id=garage_id)

        if not args["include_inactive"]:
            query = query.filter(Customer.is_active.is_(True))

        search = args.get("search")
        if search:
            pattern = f"%{search}%"
            query = query.filter(
                db.or_(
                    Customer.first_name.ilike(pattern),
                    Customer.last_name.ilike(pattern),
                    Customer.email.ilike(pattern),
                    Customer.phone.ilike(pattern),
                )
            )

        return query.order_by(Customer.last_name, Customer.first_name).all()

    @jwt_required()
    @customers_blp.arguments(CustomerSchema)
    @customers_blp.response(201, CustomerSchema)
    def post(self, data):
        garage_id = get_current_employee().garage_id

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


@customers_blp.route("/<uuid:customer_id>")
class CustomerResource(MethodView):

    @jwt_required()
    @customers_blp.response(200, CustomerSchema)
    def get(self, customer_id):
        garage_id = get_current_employee().garage_id

        # Not filtered by is_active - an archived customer must stay
        # reachable by id (e.g. from their own vehicle/appointment history)
        # even though the main list hides them.
        customer = Customer.query.filter_by(
            id=customer_id,
            garage_id=garage_id,
        ).first()

        if not customer:
            abort(404, message="Customer not found")

        return customer

    @jwt_required()
    @customers_blp.arguments(CustomerUpdateSchema)
    @customers_blp.response(200, CustomerSchema)
    def patch(self, data, customer_id):
        garage_id = get_current_employee().garage_id

        customer = Customer.query.filter_by(
            id=customer_id,
            garage_id=garage_id,
        ).first()

        if not customer:
            abort(404, message="Customer not found")

        for field, value in data.items():
            setattr(customer, field, value)

        db.session.commit()

        return customer

    @jwt_required()
    @customers_blp.response(200, CustomerDeleteResultSchema)
    def delete(self, customer_id):
        garage_id = get_current_employee().garage_id

        customer = Customer.query.filter_by(
            id=customer_id,
            garage_id=garage_id,
        ).first()

        if not customer:
            abort(404, message="Customer not found")

        # A customer with any vehicle or appointment history is archived, not
        # deleted - both relationships cascade-delete, which would otherwise
        # silently wipe out real appointment/MOT history. A never-referenced
        # customer can still be removed outright.
        if _is_referenced(customer.id):
            customer.is_active = False
            db.session.commit()
            return {"archived": True, "deleted": False}

        db.session.delete(customer)
        db.session.commit()

        return {"archived": False, "deleted": True}
