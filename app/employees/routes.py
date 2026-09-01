from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort
from werkzeug.security import generate_password_hash

from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.employee import Employee
from app.models.role import Role

from .schemas import EmployeeSchema, EmployeeUpdateSchema

employees_blp = Blueprint(
    "employees",
    "employees",
    url_prefix="/api/employees",
    description="Staff management",
)


def _resolve_roles(role_ids, garage_id):
    """Look up Role rows by id, scoped to this garage - abort if any are unknown."""
    if not role_ids:
        return []

    roles = Role.query.filter(Role.garage_id == garage_id, Role.id.in_(role_ids)).all()

    if len(roles) != len(set(role_ids)):
        abort(422, message="One or more role_ids are invalid.")

    return roles


@employees_blp.route("/")
class EmployeeList(MethodView):

    @jwt_required()
    @employees_blp.response(200, EmployeeSchema(many=True))
    def get(self):
        garage_id = get_current_employee().garage_id

        return (
            Employee.query.filter_by(garage_id=garage_id)
            .order_by(Employee.email)
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

        role_ids = data.get("role_ids") or []

        if role_ids:
            roles = _resolve_roles(role_ids, garage_id)
        else:
            # No roles specified - default to the garage's STAFF role, same as before
            # roles existed, unless it's been renamed/deleted.
            default_role = Role.query.filter_by(garage_id=garage_id, name="STAFF").first()
            roles = [default_role] if default_role else []

        employee = Employee(
            garage_id=garage_id,
            email=data["email"],
            first_name=data.get("first_name"),
            last_name=data.get("last_name"),
            password_hash=generate_password_hash(data["password"]),
            roles=roles,
        )

        db.session.add(employee)
        db.session.commit()

        return employee


@employees_blp.route("/<uuid:employee_id>")
class EmployeeResource(MethodView):

    @jwt_required()
    @employees_blp.response(200, EmployeeSchema)
    def get(self, employee_id):
        garage_id = get_current_employee().garage_id

        employee = Employee.query.filter_by(id=employee_id, garage_id=garage_id).first()

        if not employee:
            abort(404, message="Employee not found")

        return employee

    @jwt_required()
    @owner_required
    @employees_blp.arguments(EmployeeUpdateSchema)
    @employees_blp.response(200, EmployeeSchema)
    def patch(self, data, employee_id):
        garage_id = get_current_employee().garage_id

        employee = Employee.query.filter_by(id=employee_id, garage_id=garage_id).first()

        if not employee:
            abort(404, message="Employee not found")

        if "role_ids" in data:
            new_roles = _resolve_roles(data["role_ids"], garage_id)

            removing_own_ownership = employee.id == get_current_employee().id and not any(
                role.name == "OWNER" for role in new_roles
            )
            if removing_own_ownership:
                other_owners = (
                    Employee.query.join(Employee.roles)
                    .filter(
                        Employee.garage_id == garage_id,
                        Role.name == "OWNER",
                        Employee.id != employee.id,
                    )
                    .count()
                )
                if other_owners == 0:
                    abort(409, message="Cannot remove the last owner's OWNER role.")

            employee.roles = new_roles

        if "email" in data:
            existing_employee = Employee.query.filter(
                Employee.email == data["email"], Employee.id != employee.id
            ).first()

            if existing_employee:
                abort(409, message="An employee with this email already exists.")

            employee.email = data["email"]

        if "first_name" in data:
            employee.first_name = data["first_name"]

        if "last_name" in data:
            employee.last_name = data["last_name"]

        db.session.commit()

        return employee
