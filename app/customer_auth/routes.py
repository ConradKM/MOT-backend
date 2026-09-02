from flask.views import MethodView
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    get_jwt,
    get_jwt_identity,
    jwt_required,
)
from flask_smorest import Blueprint, abort

from app.models.customer import Customer
from app.models.vehicle import Vehicle

from .schemas import CustomerLoginSchema, CustomerRefreshTokenSchema, CustomerTokenSchema
from .utils import CUSTOMER_ACCOUNT_TYPE

customer_auth_blp = Blueprint(
    "customer_auth",
    "customer_auth",
    url_prefix="/api/customer/auth",
    description="Customer-facing authentication (email + a vehicle registration number)",
)

_CUSTOMER_CLAIMS = {"account_type": CUSTOMER_ACCOUNT_TYPE}


def _normalize_registration(value: str) -> str:
    # Same rule as app/models/vehicle.py::Vehicle.normalize_registration_number,
    # so a customer can type their plate with any spacing/case.
    return value.strip().upper().replace(" ", "")


@customer_auth_blp.route("/login")
class CustomerLogin(MethodView):

    @customer_auth_blp.arguments(CustomerLoginSchema)
    @customer_auth_blp.response(200, CustomerTokenSchema)
    def post(self, data):
        registration = _normalize_registration(data["registration_number"])

        customers = (
            Customer.query.join(Customer.vehicles)
            .filter(
                Customer.email.ilike(data["email"]),
                Vehicle.registration_number == registration,
            )
            .distinct()
            .all()
        )

        # Knowledge-factor auth - there's no customer password. The email plus a
        # registration on that account must resolve to exactly one customer.
        # Deliberately minimal; rate limiting / a stronger factor is tracked in
        # MOT-backend#17. Never reveal which half was wrong.
        if len(customers) != 1:
            abort(401, message="Invalid email or registration number.")

        customer = customers[0]

        return {
            "access_token": create_access_token(
                identity=str(customer.id), additional_claims=_CUSTOMER_CLAIMS
            ),
            "refresh_token": create_refresh_token(
                identity=str(customer.id), additional_claims=_CUSTOMER_CLAIMS
            ),
        }


@customer_auth_blp.route("/refresh")
class CustomerRefresh(MethodView):

    @jwt_required(refresh=True)
    @customer_auth_blp.response(200, CustomerRefreshTokenSchema)
    def post(self):
        # Only a customer refresh token may mint a customer access token here -
        # a valid *employee* refresh token would otherwise get one issued in its
        # name (harmless, since it resolves to no customer, but pointless).
        if get_jwt().get("account_type") != CUSTOMER_ACCOUNT_TYPE:
            abort(401, message="Customer refresh token required.")

        return {
            "access_token": create_access_token(
                identity=get_jwt_identity(), additional_claims=_CUSTOMER_CLAIMS
            )
        }
