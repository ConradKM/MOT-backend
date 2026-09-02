from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.customer_auth.decorators import customer_required
from app.customer_auth.utils import get_current_customer
from app.models.appointments.appointment import Appointment

from .schemas import CustomerAccountSchema, CustomerAppointmentDetailSchema

customer_portal_blp = Blueprint(
    "customer_portal",
    "customer_portal",
    url_prefix="/api/customer",
    description="Read-only view of the signed-in customer's own records",
)


def _appointment_summary(appointment):
    return {
        "id": appointment.id,
        "start_time": appointment.start_time,
        "end_time": appointment.end_time,
        "status": appointment.status,
        "notes": appointment.notes,
        "appointment_type_name": appointment.appointment_type.name,
        "vehicle_registration": (
            appointment.vehicle.registration_number if appointment.vehicle else None
        ),
    }


@customer_portal_blp.route("/account")
class CustomerAccountResource(MethodView):

    @jwt_required()
    @customer_required
    @customer_portal_blp.response(200, CustomerAccountSchema)
    def get(self):
        customer = get_current_customer()

        return {
            "customer": {
                "id": customer.id,
                "first_name": customer.first_name,
                "last_name": customer.last_name,
                "email": customer.email,
                "phone": customer.phone,
                "garage_name": customer.garage.name,
            },
            "vehicles": [
                {
                    "id": v.id,
                    "registration_number": v.registration_number,
                    "make": v.make,
                    "model": v.model,
                    "year": v.year,
                    "current_mileage": v.current_mileage,
                    "mot_expiry_date": v.mot_expiry_date,
                    "mot_records": v.mot_records,
                }
                for v in customer.vehicles
            ],
            "appointments": [
                _appointment_summary(a)
                for a in sorted(customer.appointments, key=lambda a: a.start_time)
            ],
        }


@customer_portal_blp.route("/appointments/<uuid:appointment_id>")
class CustomerAppointmentResource(MethodView):

    @jwt_required()
    @customer_required
    @customer_portal_blp.response(200, CustomerAppointmentDetailSchema)
    def get(self, appointment_id):
        customer = get_current_customer()

        appointment = Appointment.query.filter_by(
            id=appointment_id, customer_id=customer.id
        ).first()

        # 404 (not 403) for an appointment that exists but isn't this customer's -
        # don't confirm its existence to someone who can't see it.
        if appointment is None:
            abort(404, message="Appointment not found")

        return {
            "id": appointment.id,
            "start_time": appointment.start_time,
            "end_time": appointment.end_time,
            "status": appointment.status,
            "notes": appointment.notes,
            "appointment_type_name": appointment.appointment_type.name,
            "appointment_type_description": appointment.appointment_type.description,
            "vehicle": (
                {
                    "registration_number": appointment.vehicle.registration_number,
                    "make": appointment.vehicle.make,
                    "model": appointment.vehicle.model,
                    "year": appointment.vehicle.year,
                }
                if appointment.vehicle
                else None
            ),
            "garage_name": appointment.garage.name,
        }
