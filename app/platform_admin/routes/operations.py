"""Operations: email delivery, communication failures, background job health.

Reading is open to any platform admin. The one action here - resending a
failed email - is ``@superadmin_required`` and audited, because it sends real
mail to a real customer.
"""

from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.extensions import db
from app.models.communications.communication_log import CommunicationLog
from app.platform_admin.operations import (
    OperationsError,
    communication_failures,
    email_failure_summary,
    job_health,
    list_emails,
    resend_email,
)
from app.platform_admin.schemas import (
    CommunicationFailuresSchema,
    EmailFailureSummarySchema,
    EmailLogListSchema,
    EmailLogQuerySchema,
    EmailLogSchema,
    FailuresQuerySchema,
    JobHealthSchema,
    PeriodQuerySchema,
)
from app.platform_admin.security import (
    get_current_platform_admin,
    platform_admin_required,
    superadmin_required,
)

platform_operations_blp = Blueprint(
    "platform_admin_operations",
    "platform_admin_operations",
    url_prefix="/api/platform-admin/operations",
    description="Platform Admin: delivery, failures and job health",
)


@platform_operations_blp.route("/emails")
class EmailLog(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_operations_blp.arguments(EmailLogQuerySchema, location="query")
    @platform_operations_blp.response(200, EmailLogListSchema)
    def get(self, args):
        """The email delivery log across every tenant, newest first.

        ``status=failed`` / ``status=sent`` are convenience buckets over the
        provider's own free-text statuses.
        """
        return list_emails(
            status=args.get("status"),
            garage_id=args.get("garage_id"),
            search=args.get("search"),
            days=args.get("days"),
            page=args.get("page") or 1,
            per_page=args.get("per_page") or 50,
        )


@platform_operations_blp.route("/emails/summary")
class EmailSummary(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_operations_blp.arguments(PeriodQuerySchema, location="query")
    @platform_operations_blp.response(200, EmailFailureSummarySchema)
    def get(self, args):
        """Sent / failed counts and the failure rate for the dashboard tile."""
        return email_failure_summary(days=args.get("days") or 7)


@platform_operations_blp.route("/emails/<uuid:log_id>/resend")
class EmailResend(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_operations_blp.response(201, EmailLogSchema)
    def post(self, log_id):
        """Resend one failed email.

        The recipient comes from the stored row, never from the request, so
        this cannot be pointed at another address. The original failure is
        left untouched; the retry is a new row linked back to it.
        """
        log = db.session.get(CommunicationLog, log_id)
        if log is None:
            abort(404, message="Message not found.")
        try:
            return resend_email(admin=get_current_platform_admin(), log=log)
        except OperationsError as exc:
            abort(422, message=str(exc))


@platform_operations_blp.route("/communication-failures")
class CommunicationFailures(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_operations_blp.arguments(FailuresQuerySchema, location="query")
    @platform_operations_blp.response(200, CommunicationFailuresSchema)
    def get(self, args):
        """Failed communications across every channel and tenant, with the
        per-tenant and per-error-code breakdown behind them."""
        return communication_failures(days=args.get("days") or 7, limit=args.get("limit") or 50)


@platform_operations_blp.route("/jobs")
class JobHealth(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_operations_blp.response(200, JobHealthSchema)
    def get(self):
        """Health of the scheduled work this deployment depends on.

        Each check reports what it measured in the data; ``unknown`` means
        there is no evidence either way, which is more useful than a green
        light nothing verified.
        """
        return job_health()
