from flask_smorest import Blueprint

customers_blp = Blueprint(
    "customers",
    "customers",
    url_prefix="/api/customers",
    description="Customer management",
)


@customers_blp.route("/")
def list_customers():
    # TODO: require authenticated garage user
    # and query only that garage's customers.
    return {"customers": []}