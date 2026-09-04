"""Read-only MOT reminder visibility for garage staff.

Reminders are meant to be automated; this endpoint just surfaces each vehicle's
MOT expiry alongside any existing rows in the ``reminders`` table (type
``MOT*``) so staff can see what is scheduled / sent. It never creates or sends
anything and it is strictly scoped to the authenticated garage.
"""

from collections import defaultdict

from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint

from app.auth.utils import get_current_employee
from app.models.customer import Customer
from app.models.reminder import Reminder
from app.models.vehicle import Vehicle

from .schemas import MOTReminderRowSchema

mot_reminders_blp = Blueprint(
    "mot-reminders",
    "mot-reminders",
    url_prefix="/api/mot-reminders",
    description="Staff visibility of vehicle MOT expiries and reminder status.",
)

_AUTH_DOC = {"security": [{"bearerAuth": []}]}


@mot_reminders_blp.route("/")
class MOTReminderList(MethodView):

    @jwt_required()
    @mot_reminders_blp.doc(**_AUTH_DOC)
    @mot_reminders_blp.response(200, MOTReminderRowSchema(many=True))
    def get(self):
        garage_id = get_current_employee().garage_id

        vehicles = (
            Vehicle.query.filter(
                Vehicle.garage_id == garage_id,
                Vehicle.mot_expiry_date.isnot(None),
            )
            .order_by(Vehicle.mot_expiry_date)
            .all()
        )
        customers = {
            c.id: c
            for c in Customer.query.filter_by(garage_id=garage_id).all()
        }

        reminders_by_vehicle = defaultdict(list)
        for r in Reminder.query.filter(
            Reminder.garage_id == garage_id, Reminder.type.ilike("MOT%")
        ).all():
            reminders_by_vehicle[r.vehicle_id].append(r)

        rows = []
        for v in vehicles:
            customer = customers.get(v.customer_id)
            rems = reminders_by_vehicle.get(v.id, [])
            sent_times = [r.sent_at for r in rems if r.sent_at is not None]
            pending_times = sorted(
                r.scheduled_at
                for r in rems
                if r.sent_at is None and r.status == "PENDING"
            )
            last_sent = max(sent_times) if sent_times else None
            next_scheduled = pending_times[0] if pending_times else None

            if next_scheduled is not None:
                status = "scheduled"
            elif last_sent is not None:
                status = "sent"
            else:
                status = "not_scheduled"

            rows.append(
                {
                    "vehicle_id": v.id,
                    "customer_id": v.customer_id,
                    "customer_name": (
                        f"{customer.first_name} {customer.last_name}"
                        if customer
                        else "—"
                    ),
                    "registration_number": v.registration_number,
                    "make": v.make,
                    "model": v.model,
                    "mot_expiry_date": v.mot_expiry_date,
                    "reminder_status": status,
                    "last_reminder_sent": last_sent,
                    "next_reminder_scheduled": next_scheduled,
                }
            )
        return rows
