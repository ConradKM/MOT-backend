"""Business Dashboard Help Centre: feedback submission.

Feedback is stored in its own table (app/models/feedback.py) as the source of
truth; the support-team email (app/feedback/service.py) is a best-effort
notification sent after the DB commit, so a Resend hiccup never loses
feedback. `garage_id`/`employee_id`/`business_name` are always derived from
the authenticated employee, never trusted from the request body - a business
user can only ever submit feedback under their own tenant.
"""

from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.feedback import Feedback

from .schemas import FeedbackCreateSchema, FeedbackSchema
from .service import send_feedback_notification

feedback_blp = Blueprint(
    "feedback",
    "feedback",
    url_prefix="/api/feedback",
    description="Business Dashboard feedback submission (Help Centre).",
)

_AUTH_DOC: dict[str, list[dict[str, list[str]]]] = {"security": [{"bearerAuth": []}]}


@feedback_blp.route("/")
class FeedbackResource(MethodView):
    @jwt_required()
    @feedback_blp.doc(**_AUTH_DOC)
    @feedback_blp.arguments(FeedbackCreateSchema)
    @feedback_blp.response(201, FeedbackSchema)
    def post(self, data):
        employee = get_current_employee()
        if employee is None:
            abort(401, message="Not authenticated as a business user.")

        feedback = Feedback(
            garage_id=employee.garage_id,
            employee_id=employee.id,
            business_name=employee.garage.name,
            user_email=data.get("email") or employee.email,
            type=data["type"],
            subject=data.get("subject"),
            message=data["message"],
            priority=data["priority"],
        )
        db.session.add(feedback)
        db.session.commit()

        send_feedback_notification(feedback)

        return feedback
