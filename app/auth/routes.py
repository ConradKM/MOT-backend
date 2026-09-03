from flask.views import MethodView
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    get_jwt_identity,
    jwt_required,
)
from flask_smorest import Blueprint, abort
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.garages.slug import slugify_unique
from app.models.appointments.appointment_type import GarageAppointmentType
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

# Temporary stopgap so a brand-new garage isn't left with zero appointment
# types (and therefore unable to book anything) before owners have a real
# way to build their own list from scratch. Tracked for removal in a
# follow-up issue once that exists - see migration 46c9ee69459d's successor
# for the equivalent one-time backfill for garages that already existed.
# (name, default_duration_minutes) - Repair/Other are too variable to guess
# a duration for, so they're left unset (still bookable, just requires an
# explicit end_time).
_DEFAULT_APPOINTMENT_TYPES = (
    ("MOT", 45),
    ("Service", 60),
    ("MOT + Service", 105),
    ("Repair", None),
    ("Other", None),
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

        for name, duration_minutes in _DEFAULT_APPOINTMENT_TYPES:
            db.session.add(
                GarageAppointmentType(
                    garage_id=garage.id, name=name, default_duration_minutes=duration_minutes
                )
            )

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
