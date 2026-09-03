from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort
from sqlalchemy.exc import IntegrityError

from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_status import GarageAppointmentStatus

from .schemas import AppointmentStatusSchema, AppointmentStatusUpdateSchema

appointment_statuses_blp = Blueprint(
    "appointment-statuses",
    "appointment-statuses",
    url_prefix="/api/appointment-statuses",
    description="Per-garage labels / colours for appointment statuses. "
    "Appointment.status stays a string; these just customise its display and "
    "let a garage add its own.",
)


def _owned_status(status_id, garage_id):
    status = GarageAppointmentStatus.query.filter_by(
        id=status_id, garage_id=garage_id
    ).first()
    if status is None:
        abort(404, message="Appointment status not found")
    return status


@appointment_statuses_blp.route("/")
class AppointmentStatusList(MethodView):

    @jwt_required()
    @appointment_statuses_blp.response(200, AppointmentStatusSchema(many=True))
    def get(self):
        garage_id = get_current_employee().garage_id
        return (
            GarageAppointmentStatus.query.filter_by(garage_id=garage_id)
            .order_by(GarageAppointmentStatus.sort_order, GarageAppointmentStatus.label)
            .all()
        )

    @jwt_required()
    @owner_required
    @appointment_statuses_blp.arguments(AppointmentStatusSchema)
    @appointment_statuses_blp.response(201, AppointmentStatusSchema)
    def post(self, data):
        garage_id = get_current_employee().garage_id

        status = GarageAppointmentStatus(
            garage_id=garage_id,
            key=data["key_in"],
            label=data["label"],
            color=data["color"],
            sort_order=data.get("sort_order", 0),
            is_terminal=data.get("is_terminal", False),
            is_system=False,
        )
        db.session.add(status)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            abort(409, message="A status with this key already exists for your garage.")

        return status


@appointment_statuses_blp.route("/<uuid:status_id>")
class AppointmentStatusResource(MethodView):

    @jwt_required()
    @owner_required
    @appointment_statuses_blp.arguments(AppointmentStatusUpdateSchema)
    @appointment_statuses_blp.response(200, AppointmentStatusSchema)
    def patch(self, data, status_id):
        garage_id = get_current_employee().garage_id
        status = _owned_status(status_id, garage_id)

        for field, value in data.items():
            setattr(status, field, value)

        db.session.commit()
        return status

    @jwt_required()
    @owner_required
    @appointment_statuses_blp.response(204)
    def delete(self, status_id):
        garage_id = get_current_employee().garage_id
        status = _owned_status(status_id, garage_id)

        if status.is_system:
            abort(403, message="Built-in statuses can't be deleted.")

        in_use = Appointment.query.filter_by(
            garage_id=garage_id, status=status.key
        ).first()
        if in_use is not None:
            abort(409, message="This status is in use by one or more appointments.")

        db.session.delete(status)
        db.session.commit()
        return ""
