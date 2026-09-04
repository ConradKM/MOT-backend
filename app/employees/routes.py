from datetime import UTC, datetime

from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.employee import Employee
from app.models.role import Role

from .schemas import EmployeeSchema, EmployeeUpdateSchema
from .service import create_employee_account


def _active_owner_count(garage_id, exclude_id=None):
    q = (
        Employee.query.join(Employee.roles)
        .filter(
            Employee.garage_id == garage_id,
            Employee.is_active.is_(True),
            Role.name == "OWNER",
        )
    )
    if exclude_id is not None:
        q = q.filter(Employee.id != exclude_id)
    return q.count()

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
        # Garage always comes from the owner's token - never the request body.
        garage_id = get_current_employee().garage_id

        role_ids = data.get("role_ids") or []
        if role_ids:
            roles = _resolve_roles(role_ids, garage_id)
        else:
            # No roles specified - default to the garage's STAFF role, same as
            # before roles existed, unless it's been renamed/deleted.
            default_role = Role.query.filter_by(
                garage_id=garage_id, name="STAFF"
            ).first()
            roles = [default_role] if default_role else []

        employee = create_employee_account(
            garage_id=garage_id,
            email=data["email"],
            password=data["password"],
            first_name=data.get("first_name"),
            last_name=data.get("last_name"),
            roles=roles,
        )
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

            was_owner = employee.has_role("OWNER")
            will_be_owner = any(role.name == "OWNER" for role in new_roles)
            if (
                was_owner
                and not will_be_owner
                and employee.is_active
                and _active_owner_count(garage_id, exclude_id=employee.id) == 0
            ):
                abort(409, message="Cannot remove the last active owner's OWNER role.")

            employee.roles = new_roles

        if "is_active" in data and data["is_active"] != employee.is_active:
            if (
                not data["is_active"]
                and employee.has_role("OWNER")
                and _active_owner_count(garage_id, exclude_id=employee.id) == 0
            ):
                abort(
                    409,
                    message="Cannot deactivate the garage's only active owner.",
                )
            employee.is_active = data["is_active"]
            if not data["is_active"]:
                # End the deactivated user's live sessions immediately.
                employee.tokens_valid_from = datetime.now(UTC)

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
