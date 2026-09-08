from flask.views import MethodView
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    get_jwt,
    get_jwt_identity,
    jwt_required,
)
from flask_smorest import Blueprint, abort
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models.booking_request import BookingRequest
from app.models.customer import Customer

from .decorators import customer_required
from .schemas import (
    CustomerPasswordLoginSchema,
    CustomerReferenceLoginSchema,
    CustomerRefreshTokenSchema,
    CustomerSetPasswordSchema,
    CustomerTokenSchema,
)
from .utils import CUSTOMER_ACCOUNT_TYPE, get_current_customer

customer_auth_blp = Blueprint(
    "customer_auth",
    "customer_auth",
    url_prefix="/api/customer/auth",
    description="Customer-facing authentication: a booking reference, or a "
    "password once one has been set",
)

_CUSTOMER_CLAIMS = {"account_type": CUSTOMER_ACCOUNT_TYPE}


def _issue_tokens(customer_id) -> dict:
    return {
        "access_token": create_access_token(
            identity=str(customer_id), additional_claims=_CUSTOMER_CLAIMS
        ),
        "refresh_token": create_refresh_token(
            identity=str(customer_id), additional_claims=_CUSTOMER_CLAIMS
        ),
    }


@customer_auth_blp.route("/login/reference")
class CustomerReferenceLogin(MethodView):
    @customer_auth_blp.arguments(CustomerReferenceLoginSchema)
    @customer_auth_blp.response(200, CustomerTokenSchema)
    def post(self, data):
        reference = data["booking_reference"].strip().upper()

        # A booking reference is only ever issued alongside an eagerly-
        # resolved customer (see app/public_booking/routes.py and
        # app/conversation/actions.py::create_booking_request) - the
        # customer_id.isnot(None) guard just protects against a pre-this-
        # feature row that somehow has a reference but no linked customer.
        booking_request = BookingRequest.query.filter(
            BookingRequest.booking_reference == reference,
            BookingRequest.customer_email.ilike(data["email"]),
            BookingRequest.customer_id.isnot(None),
        ).first()

        # Never reveal which half (reference vs email) was wrong.
        if booking_request is None:
            abort(401, message="Invalid email or booking reference.")

        return _issue_tokens(booking_request.customer_id)


@customer_auth_blp.route("/login/password")
class CustomerPasswordLogin(MethodView):
    @customer_auth_blp.arguments(CustomerPasswordLoginSchema)
    @customer_auth_blp.response(200, CustomerTokenSchema)
    def post(self, data):
        candidates = Customer.query.filter(
            Customer.email.ilike(data["email"]),
            Customer.password_hash.isnot(None),
        ).all()
        matches = [c for c in candidates if check_password_hash(c.password_hash, data["password"])]

        # Same "must resolve to exactly one" rule as reference login: if this
        # email has a password set on more than one customer row (e.g. the
        # same person booked at two different garages and set a password at
        # both), password login can't disambiguate - sign in with a booking
        # reference instead, which always resolves to one specific booking.
        if len(matches) != 1:
            abort(401, message="Invalid email or password.")

        return _issue_tokens(matches[0].id)


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


@customer_auth_blp.route("/set-password")
class CustomerSetPassword(MethodView):
    """Lets an already-signed-in customer start using email + password -
    the "create an account" action on the customer account page."""

    @jwt_required()
    @customer_required
    @customer_auth_blp.arguments(CustomerSetPasswordSchema)
    @customer_auth_blp.response(204)
    def post(self, data):
        customer = get_current_customer()
        customer.password_hash = generate_password_hash(data["password"])
        db.session.commit()
