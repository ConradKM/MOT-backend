from datetime import UTC, datetime

from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.communications.events import (
    BOOKING_REQUEST_REJECTED,
    emit_event,
)
from app.email.service import STATUS_SENT
from app.extensions import db
from app.models.booking_request import BookingRequest
from app.models.communications.communication_log import CHANNEL_EMAIL, CommunicationLog
from app.payments.service import expire_stale_payment_holds, refund_deposit

from .schemas import (
    BookingRequestApproveSchema,
    BookingRequestQueryArgsSchema,
    BookingRequestRefundSchema,
    BookingRequestRejectSchema,
    BookingRequestSchema,
)
from .service import (
    approve_booking_request,
    attach_review_context,
    expire_stale_booking_requests,
    is_request_stale,
)

booking_requests_blp = Blueprint(
    "booking_requests",
    "booking_requests",
    url_prefix="/api/booking-requests",
    description="Staff review of public booking requests. Approving one creates "
    "the customer / vehicle / appointment; rejecting one just records the decision.",
)


def _get_owned_request(request_id, *, lock: bool = False):
    employee = get_current_employee()
    assert employee is not None
    garage_id = employee.garage_id
    query = BookingRequest.query.filter_by(id=request_id, garage_id=garage_id)
    if lock:
        query = query.with_for_update()
    booking_request = query.first()

    if booking_request is None:
        abort(404, message="Booking request not found")

    return booking_request


#: What the operator is told happened to the customer notification - not
#: stored, computed fresh after every reject from whether the request even
#: has an email on file and, if so, what app/email/service.py's own
#: CommunicationLog row for the send says. See BookingRequestSchema
#: .notification_result.
NOTIFICATION_SENT = "SENT"
NOTIFICATION_FAILED = "FAILED"
NOTIFICATION_NO_EMAIL = "NO_EMAIL"


def _reject_notification_result(booking_request: BookingRequest) -> str:
    if not booking_request.customer_email:
        return NOTIFICATION_NO_EMAIL

    # channel=EMAIL matters: the WhatsApp automation handler
    # (app/conversation/automation.py) is registered for the same
    # BOOKING_REQUEST_REJECTED event and logs its own CommunicationLog row for
    # this same booking_request_id - without this filter the "most recent"
    # row could just as easily be its WhatsApp attempt, not the email's.
    log = (
        CommunicationLog.query.filter_by(
            booking_request_id=booking_request.id,
            trigger_event=BOOKING_REQUEST_REJECTED,
            channel=CHANNEL_EMAIL,
        )
        .order_by(CommunicationLog.created_at.desc())
        .first()
    )
    if log is None:
        # A customer_email is on file but nothing was logged - only reachable
        # if a duplicate send was skipped (app/email/service.py::_send's own
        # dedupe), which can't happen on a fresh PENDING -> REJECTED
        # transition (the 409 above already refuses a second rejection).
        # Reported as sent rather than implying a failure nothing recorded.
        return NOTIFICATION_SENT
    return NOTIFICATION_SENT if log.status == STATUS_SENT else NOTIFICATION_FAILED


@booking_requests_blp.route("/")
class BookingRequestList(MethodView):
    @jwt_required()
    @booking_requests_blp.arguments(BookingRequestQueryArgsSchema, location="query")
    @booking_requests_blp.response(200, BookingRequestSchema(many=True))
    def get(self, args):
        garage_id = get_current_employee().garage_id

        # A PENDING request whose preferred time has passed shouldn't stay
        # actionable - sweep before every read rather than relying on staff
        # to notice and reject it manually (see service.py).
        expire_stale_booking_requests(garage_id=garage_id)
        expire_stale_payment_holds(garage_id=garage_id)

        query = BookingRequest.query.filter_by(garage_id=garage_id)
        if args.get("status") is not None:
            query = query.filter(BookingRequest.status == args["status"])

        results = query.order_by(BookingRequest.created_at.desc()).all()
        attach_review_context(results)
        return results


@booking_requests_blp.route("/<uuid:request_id>")
class BookingRequestResource(MethodView):
    @jwt_required()
    @booking_requests_blp.response(200, BookingRequestSchema)
    def get(self, request_id):
        expire_stale_booking_requests(garage_id=get_current_employee().garage_id)
        expire_stale_payment_holds(garage_id=get_current_employee().garage_id)
        booking_request = _get_owned_request(request_id)
        attach_review_context([booking_request])
        return booking_request


@booking_requests_blp.route("/<uuid:request_id>/approve")
class BookingRequestApprove(MethodView):
    @jwt_required()
    @booking_requests_blp.arguments(BookingRequestApproveSchema)
    @booking_requests_blp.response(200, BookingRequestSchema)
    def post(self, data, request_id):
        employee = get_current_employee()
        booking_request = approve_booking_request(
            reviewer=employee,
            request_id=request_id,
            data=data,
        )

        attach_review_context([booking_request])
        return booking_request


@booking_requests_blp.route("/<uuid:request_id>/reject")
class BookingRequestReject(MethodView):
    @jwt_required()
    @booking_requests_blp.arguments(BookingRequestRejectSchema)
    @booking_requests_blp.response(200, BookingRequestSchema)
    def post(self, data, request_id):
        employee = get_current_employee()
        booking_request = _get_owned_request(request_id, lock=True)

        if booking_request.status == "PENDING" and is_request_stale(booking_request):
            booking_request.status = "EXPIRED"
            db.session.commit()

        if booking_request.status != "PENDING":
            abort(
                409,
                message=f"This booking request has already been {booking_request.status.lower()}.",
            )

        booking_request.status = "REJECTED"
        booking_request.reviewed_by_employee_id = employee.id
        booking_request.reviewed_at = datetime.now(UTC)
        booking_request.staff_notes = data.get("staff_notes")
        booking_request.customer_rejection_reason = data.get("customer_rejection_reason")

        db.session.commit()

        # If a deposit was actually paid, refund it in full now that the
        # business has declined the booking - the customer shouldn't have to
        # ask (see the deposit spec's refund policy: full refund on
        # rejection). A no-op when there's no charged payment to refund.
        refund_deposit(booking_request, reason="booking_rejected")

        # Never rolled back on a failed send - app/email/service.py's own
        # _send never raises (a failure becomes a FAILED CommunicationLog
        # row), so this always runs after the REJECTED status is already
        # committed. The result below is read back from that same row.
        emit_event(
            BOOKING_REQUEST_REJECTED, garage=booking_request.garage, booking_request=booking_request
        )

        # attach_review_context resets _notification_result to None on every
        # call (see service.py) - set the real value *after* it, or this
        # call's own reset would immediately overwrite it.
        attach_review_context([booking_request])
        booking_request._notification_result = _reject_notification_result(booking_request)
        return booking_request


@booking_requests_blp.route("/<uuid:request_id>/refund")
class BookingRequestRefund(MethodView):
    """Manual refund trigger - infrastructure for the cancellation case the
    deposit spec deliberately leaves without an automatic policy (unlike
    rejection, which always refunds in full - see BookingRequestReject
    above). A staff member decides per request whether a cancelled, paid
    booking gets refunded; this just makes that decision executable rather
    than "email the customer and refund by hand outside CoMaz OS"."""

    @jwt_required()
    @owner_required
    @booking_requests_blp.arguments(BookingRequestRefundSchema)
    @booking_requests_blp.response(200, BookingRequestSchema)
    def post(self, data, request_id):
        booking_request = _get_owned_request(request_id, lock=True)

        payment = booking_request.active_payment
        if payment is None or payment.status != "SUCCEEDED":
            abort(409, message="This booking has no successful payment to refund.")

        refund_deposit(booking_request, reason=data.get("reason") or "manual_staff_refund")

        attach_review_context([booking_request])
        return booking_request
