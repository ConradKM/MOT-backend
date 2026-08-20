from flask_jwt_extended import get_jwt_identity

from app.extensions import db
from app.models.employee import Employee


def get_current_employee() -> Employee | None:
    employee_id = get_jwt_identity()

    if employee_id is None:
        return None

    return db.session.get(Employee, int(employee_id))
