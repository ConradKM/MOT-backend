from flask import abort
from flask.views import MethodView
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    get_jwt_identity,
    jwt_required,
)
from flask_smorest import Blueprint
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models.employee import Employee
from app.models.garage import Garage

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
                description="A user with this email already exists.",
            )

        garage = Garage(
            name=data["garage_name"]
        )

        db.session.add(garage)
        db.session.flush()

        employee = Employee(
            garage_id=garage.id,
            email=data["email"],
            password_hash=generate_password_hash(
                data["password"]
            ),
            role="OWNER",
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
                description="Invalid email or password.",
            )

        if not check_password_hash(
            employee.password_hash,
            data["password"],
        ):
            abort(
                401,
                description="Invalid email or password.",
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
