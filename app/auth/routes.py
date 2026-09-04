from flask import current_app, request
from flask.views import MethodView
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    get_jwt_identity,
    jwt_required,
)
from flask_smorest import Blueprint, abort
from werkzeug.security import check_password_hash

from app.appointments.statuses.defaults import seed_default_statuses
from app.auth.utils import get_current_employee
from app.employees.schemas import EmployeeSchema
from app.employees.service import create_employee_account, validate_password
from app.extensions import db, limiter
from app.garages.schedule.defaults import seed_default_schedule
from app.garages.slug import slugify_unique
from app.models.employee import Employee
from app.models.garage import Garage
from app.models.role import Role

from .reset import (
    consume_token_and_set_password,
    find_valid_token,
    issue_reset_token,
    send_reset_link,
)
from .schemas import (
    ForgotPasswordSchema,
    LoginSchema,
    MessageSchema,
    RefreshTokenSchema,
    RegisterSchema,
    ResetPasswordSchema,
    ResetTokenStatusSchema,
    TokenSchema,
)

auth_blp = Blueprint(
    "auth",
    "auth",
    url_prefix="/api/auth",
    description="Authentication",
)

# Shown for every forgot-password call so the response can't be used to tell
# whether an email is registered.
_GENERIC_RESET_MESSAGE = (
    "If an account exists for this email address, a password reset link "
    "has been sent."
)


@auth_blp.route("/register")
class Register(MethodView):
    """Garage onboarding - the ONLY way a garage + its first OWNER are made.
    There is no public path to create a bare employee here."""

    @limiter.limit(lambda: current_app.config["AUTH_RESET_RATELIMIT"])
    @auth_blp.arguments(RegisterSchema)
    @auth_blp.response(201, TokenSchema)
    def post(self, data):
        validate_password(data["password"])

        garage = Garage(
            name=data["garage_name"],
            slug=slugify_unique(data["garage_name"], db.session),
        )
        db.session.add(garage)
        db.session.flush()

        # New garages ship with the built-in appointment statuses + default
        # scheduling; they define their own appointment types later.
        seed_default_statuses(garage.id, db.session)
        seed_default_schedule(garage.id, db.session)

        owner_role = Role(garage_id=garage.id, name="OWNER")
        db.session.add(owner_role)
        db.session.add(Role(garage_id=garage.id, name="STAFF"))

        employee = create_employee_account(
            garage_id=garage.id,
            email=data["email"],
            password=data["password"],
            first_name=data.get("first_name"),
            last_name=data.get("last_name"),
            roles=[owner_role],
        )
        db.session.commit()

        return {
            "access_token": create_access_token(identity=str(employee.id)),
            "refresh_token": create_refresh_token(identity=str(employee.id)),
        }


@auth_blp.route("/login")
class Login(MethodView):

    @limiter.limit(lambda: current_app.config["AUTH_LOGIN_RATELIMIT"])
    @auth_blp.arguments(LoginSchema)
    @auth_blp.response(200, TokenSchema)
    def post(self, data):
        employee = Employee.query.filter_by(email=data["email"]).first()

        # One message for "no such account", "wrong password" and "deactivated"
        # so login can't be used to probe which emails exist / are active.
        if (
            employee is None
            or not employee.is_active
            or not check_password_hash(employee.password_hash, data["password"])
        ):
            abort(401, message="Invalid email or password.")

        return {
            "access_token": create_access_token(identity=str(employee.id)),
            "refresh_token": create_refresh_token(identity=str(employee.id)),
        }


@auth_blp.route("/refresh")
class Refresh(MethodView):

    @jwt_required(refresh=True)
    @auth_blp.response(200, RefreshTokenSchema)
    def post(self):
        return {"access_token": create_access_token(identity=get_jwt_identity())}


@auth_blp.route("/me")
class Me(MethodView):

    @jwt_required()
    @auth_blp.response(200, EmployeeSchema)
    def get(self):
        """The signed-in employee's own record (id, garage, email, roles).

        Lets the frontend gate owner-only UI up front instead of discovering
        the restriction from a 403 on the action itself.
        """
        employee = get_current_employee()
        if employee is None:
            abort(401, message="Not authenticated as an employee.")
        return employee


@auth_blp.route("/forgot-password")
class ForgotPassword(MethodView):

    @limiter.limit(lambda: current_app.config["AUTH_RESET_RATELIMIT"])
    @auth_blp.arguments(ForgotPasswordSchema)
    @auth_blp.response(200, MessageSchema)
    def post(self, data):
        employee = Employee.query.filter_by(email=data["email"]).first()
        if employee is not None and employee.is_active:
            raw_token = issue_reset_token(employee)
            db.session.commit()
            send_reset_link(employee, raw_token)

        return {"message": _GENERIC_RESET_MESSAGE}


@auth_blp.route("/reset-password")
class ResetPassword(MethodView):

    @auth_blp.arguments(ResetTokenStatusSchema(only=()), location="query")
    @auth_blp.response(200, ResetTokenStatusSchema)
    def get(self, _args):
        """Lets the reset page show the invalid/expired state before the user
        types a new password."""
        token = request.args.get("token", "")
        if find_valid_token(token) is None:
            abort(
                400,
                message="This password reset link is invalid or has expired. "
                "Please request a new one.",
            )
        return {"valid": True}

    @limiter.limit(lambda: current_app.config["AUTH_RESET_RATELIMIT"])
    @auth_blp.arguments(ResetPasswordSchema)
    @auth_blp.response(200, MessageSchema)
    def post(self, data):
        validate_password(data["password"])

        row = find_valid_token(data["token"])
        if row is None:
            abort(
                400,
                message="This password reset link is invalid or has expired. "
                "Please request a new one.",
            )

        consume_token_and_set_password(row, data["password"])
        db.session.commit()

        return {
            "message": "Your password has been reset successfully. "
            "You can now log in."
        }
