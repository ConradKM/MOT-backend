from flask import Blueprint, jsonify

customers_bp = Blueprint("customers", __name__)


@customers_bp.get("")
def list_customers():
    # TODO: require authenticated garage user and query only that garage's records.
    return jsonify({"customers": []})
