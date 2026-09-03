from flask.views import MethodView
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    get_jwt_identity,
    jwt_required,
)
from flask_smorest import Blueprint, abort
from werkzeug.security import check_password_hash, generate_password_hash

from app.appointments.statuses.defaults import seed_default_statuses
from app.extensions import db
from app.garages.slug import slugify_unique
from app.models.employee import Employee
from app.models.garage import Garage
from app.models.role import Role

from .schemas import LoginSchema, RefreshTokenSchema, RegisterSchema, TokenSchema

auth_blp = Blueprint(
    "auth",
    "auth",
    url_prefix="/api/auth",
    description="Authentication",
)

@auth_blp.route("/register")
class Register(MethodView):

    @auth_blp.arguments(RegisterSchema)
    @auth_blp.response(201, TokenSchema)
    def post(self, data):

        existing_employee = Employee.query.filter_by(
            email=data["email"]
        ).first()

        if existing_employee:
            abort(
                409,
                message="A user with this email already exists.",
            )

        garage = Garage(
            name=data["garage_name"],
            slug=slugify_unique(data["garage_name"], db.session),
        )

        db.session.add(garage)
        db.session.flush()

        # A new garage starts with no appointment types - the owner defines
        # its own from Settings > Appointment types (see #9). Until they add
        # one, the public booking wizard shows "no services yet" and staff
        # can't create an appointment.

        # Appointment statuses, on the other hand, ship with the built-in set
        # (the owner can then rename / recolour / extend them).
        seed_default_statuses(garage.id, db.session)

        owner_role = Role(garage_id=garage.id, name="OWNER")
        db.session.add(owner_role)
        db.session.add(Role(garage_id=garage.id, name="STAFF"))

        employee = Employee(
            garage_id=garage.id,
            email=data["email"],
            password_hash=generate_password_hash(
                data["password"]
            ),
            roles=[owner_role],
        )

        db.session.add(employee)
        db.session.commit()

        access_token = create_access_token(
            identity=str(employee.id),
        )

        refresh_token = create_refresh_token(
            identity=str(employee.id),
        )

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
        }


@auth_blp.route("/login")
class Login(MethodView):

    @auth_blp.arguments(LoginSchema)
    @auth_blp.response(200, TokenSchema)
    def post(self, data):

        employee = Employee.query.filter_by(
            email=data["email"]
        ).first()

        if not employee:
            abort(
                401,
                message="Invalid email or password.",
            )

        if not check_password_hash(
            employee.password_hash,
            data["password"],
        ):
            abort(
                401,
                message="Invalid email or password.",
            )

        access_token = create_access_token(
            identity=str(employee.id),
        )

        refresh_token = create_refresh_token(
            identity=str(employee.id),
        )

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
        }

@auth_blp.route("/refresh")
class Refresh(MethodView):

    @jwt_required(refresh=True)
    @auth_blp.response(200, RefreshTokenSchema)
    def post(self):

        employee_id = get_jwt_identity()

        access_token = create_access_token(
            identity=employee_id,
        )

        return {
            "access_token": access_token,
        }
