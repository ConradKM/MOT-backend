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

from app.auth.utils import get_current_employee
from app.employees.schemas import EmployeeSchema
from app.employees.service import validate_password
from app.extensions import db, limiter
from app.garages.onboarding import (
    GarageSpec,
    OnboardingEmailInUse,
    OnboardingError,
    OwnerSpec,
    onboard_garage,
)
from app.models.employee import Employee

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
    """Garage onboarding over HTTP.

    A thin wrapper over ``app.garages.onboarding`` - the same transactional
    service the ``flask onboard-garage`` CLI uses. There is no public path to
    create a bare employee here; onboarding always makes the Garage + its
    first OWNER together. Set ``ONBOARDING_HTTP_ENABLED=false`` to disable this
    endpoint and onboard via the CLI only.
    """

    @limiter.limit(lambda: current_app.config["AUTH_RESET_RATELIMIT"])
    @auth_blp.arguments(RegisterSchema)
    @auth_blp.response(201, TokenSchema)
    def post(self, data):
        if not current_app.config.get("ONBOARDING_HTTP_ENABLED", True):
            abort(
                404,
                message="Garage onboarding is handled by the platform team.",
            )

        try:
            result = onboard_garage(
                garage=GarageSpec(name=data["garage_name"]),
                owner=OwnerSpec(
                    email=data["email"],
                    password=data["password"],
                    first_name=data.get("first_name"),
                    last_name=data.get("last_name"),
                ),
            )
        except OnboardingEmailInUse as exc:
            abort(409, message=str(exc))
        except OnboardingError as exc:
            abort(422, message=str(exc))

        return {
            "access_token": create_access_token(identity=str(result.owner.id)),
            "refresh_token": create_refresh_token(identity=str(result.owner.id)),
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
