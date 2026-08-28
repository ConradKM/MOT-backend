from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort
from werkzeug.security import generate_password_hash

from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.employee import Employee

from .schemas import EmployeeSchema

employees_blp = Blueprint(
    "employees",
    "employees",
    url_prefix="/api/employees",
    description="Staff management",
)


@employees_blp.route("/")
class EmployeeList(MethodView):

    @jwt_required()
    @employees_blp.response(200, EmployeeSchema(many=True))
    def get(self):
        garage_id = get_current_employee().garage_id

        return (
            Employee.query.filter_by(garage_id=garage_id)
            .order_by(Employee.role, Employee.email)
            .all()
        )

    @jwt_required()
    @owner_required
    @employees_blp.arguments(EmployeeSchema)
    @employees_blp.response(201, EmployeeSchema)
    def post(self, data):
        garage_id = get_current_employee().garage_id

        existing_employee = Employee.query.filter_by(email=data["email"]).first()

        if existing_employee:
            abort(409, message="An employee with this email already exists.")

        employee = Employee(
            garage_id=garage_id,
            email=data["email"],
            password_hash=generate_password_hash(data["password"]),
            role=data.get("role") or "STAFF",
        )

        db.session.add(employee)
        db.session.commit()

        return employee
