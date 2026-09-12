import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.appointments.appointment import Appointment
    from app.models.appointments.appointment_type import GarageAppointmentType
    from app.models.customer import Customer
    from app.models.employee import Employee
    from app.models.garage import Garage
    from app.models.payments.payment import BookingPayment
    from app.models.vehicle import Vehicle

# AWAITING_PAYMENT: the appointment type requires a deposit and the customer
# hasn't paid it yet - a short-lived hold (see app/payments/service.py),
# reserving capacity exactly like PENDING (see
# app/public_booking/availability.py) but not yet visible to staff for
# review. It becomes PENDING automatically once the provider webhook
# confirms the deposit succeeded, or EXPIRED if the hold times out
# unpaid (app/payments/service.py::expire_stale_payment_holds).
# PENDING: awaiting staff review, and still reserving capacity for its
# preferred slot (see app/public_booking/availability.py). APPROVED: staff
# accepted it and the linked customer/vehicle/appointment rows were created.
# REJECTED: staff declined it - if a deposit was paid, it is refunded in
# full (see app/booking_requests/routes.py). EXPIRED: nobody reviewed it
# before its preferred date/time passed, or its payment hold timed out -
# set automatically, never by staff action. CANCELLED: withdrawn (customer
# or staff) after being PENDING/APPROVED; reserved for the refund
# infrastructure in app/payments/service.py - no automatic transition into
# it exists yet. Every terminal status (everything except PENDING/
# AWAITING_PAYMENT) releases the capacity the request was holding, simply by
# no longer being counted in availability.py.
BOOKING_REQUEST_STATUSES = (
    "AWAITING_PAYMENT",
    "PENDING",
    "APPROVED",
    "REJECTED",
    "EXPIRED",
    "CANCELLED",
)


class BookingRequest(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    """An unauthenticated public booking submission, held for staff review.

    Deliberately a flat snapshot of what the public form collected - it never
    writes into customers/vehicles/appointments directly. Approval (see
    app/booking_requests) is what turns it into real records.
    """

    __tablename__ = "booking_requests"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    # A short customer-facing code (see app/booking_requests/reference.py),
    # e.g. "BK7F3K9Q2" - shown on the confirmation screen and usable to log
    # in (see app/customer_auth/routes.py::CustomerReferenceLogin) without a
    # password. Always set by application code at creation; nullable at the
    # DB level only for rows that predate this column.
    booking_reference: Mapped[str | None] = mapped_column(String(16), unique=True, index=True)

    # --- what the public form submitted -------------------------------------
    customer_first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    customer_last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Required by the public web form's own schema (see
    # app/public_booking/schemas.py::BookingRequestCreateSchema) - nullable
    # at the DB level because the conversational booking flow (WhatsApp/voice,
    # see app/conversation/actions.py::create_booking_request) never asks a
    # customer for an email; that channel already *is* the confirmation
    # channel. Falls back to the matched customer's own email when one exists.
    customer_email: Mapped[str | None] = mapped_column(String(320))
    # Nullable at the DB level only for pre-existing rows submitted before the
    # mobile number became required; the public form's schema (see
    # app/public_booking/schemas.py::UKMobileField) requires and E.164-
    # normalises it for every new submission.
    customer_phone: Mapped[str | None] = mapped_column(String(40))

    vehicle_registration: Mapped[str] = mapped_column(String(20), nullable=False)
    vehicle_make: Mapped[str | None] = mapped_column(String(100))
    vehicle_model: Mapped[str | None] = mapped_column(String(100))
    vehicle_year: Mapped[int | None] = mapped_column(Integer)
    vehicle_mileage: Mapped[int | None] = mapped_column(Integer)

    # Optional: the garage may not have configured appointment types, or the
    # type may be deleted later - keep the request either way.
    appointment_type_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("garage_appointment_types.id", ondelete="SET NULL")
    )
    # Snapshot of the appointment type's duration/price at submission time -
    # the type may be edited (or deleted) while this request is still
    # PENDING, but what staff review should reflect what the customer actually
    # saw and requested, not whatever the type looks like today. NULL on
    # requests submitted before this column existed, or with no type chosen
    # (see service.py for the display fallback).
    requested_duration_minutes: Mapped[int | None] = mapped_column(Integer)
    requested_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    preferred_date: Mapped[date] = mapped_column(Date, nullable=False)
    preferred_time: Mapped[time | None] = mapped_column(Time)
    preferred_employee_note: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)

    # --- staff review outcome --------------------------------------------
    reviewed_by_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("employees.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Staff-internal - never rendered to the customer (see
    # app/email/service.py::send_booking_request_rejected_email). May say
    # anything at all, which is exactly why it must stay separate from
    # customer_rejection_reason below.
    staff_notes: Mapped[str | None] = mapped_column(Text)
    # Set only on REJECTED, and only when the rejecting staff member chose to
    # give one. Explicitly customer-facing - included in the rejection email
    # verbatim when present. Distinct from staff_notes on purpose: existing
    # staff notes were written under the assumption they are private, and
    # must never become customer-visible by a later change to this column.
    customer_rejection_reason: Mapped[str | None] = mapped_column(Text)

    # Records created when the request was approved (nullable, SET NULL so
    # deleting one of them doesn't delete the request's history).
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customers.id", ondelete="SET NULL")
    )
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("vehicles.id", ondelete="SET NULL")
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("appointments.id", ondelete="SET NULL")
    )

    # Set only while status == AWAITING_PAYMENT: when the payment hold
    # expires (see app/payments/service.py::expire_stale_payment_holds).
    # Null the rest of this row's life - a request that never required a
    # deposit, or one whose deposit has already resolved, has nothing to
    # expire.
    payment_hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    garage: Mapped["Garage"] = relationship("Garage")
    appointment_type: Mapped["GarageAppointmentType | None"] = relationship("GarageAppointmentType")
    reviewed_by: Mapped["Employee | None"] = relationship("Employee")
    customer: Mapped["Customer | None"] = relationship("Customer")
    vehicle: Mapped["Vehicle | None"] = relationship("Vehicle")
    appointment: Mapped["Appointment | None"] = relationship("Appointment")
    payments: Mapped[list["BookingPayment"]] = relationship(
        "BookingPayment", back_populates="booking_request", order_by="BookingPayment.created_at"
    )

    @property
    def active_payment(self) -> "BookingPayment | None":
        """The most recent payment attempt that isn't a dead end (CANCELLED/
        FAILED) - what the review screen and the customer's own status poll
        should show. Falls back to the most recent attempt of any kind if
        every attempt failed/was cancelled, so a failure is still visible
        rather than silently showing no payment at all."""
        if not self.payments:
            return None
        live = [p for p in self.payments if p.status not in ("CANCELLED", "FAILED")]
        return live[-1] if live else self.payments[-1]
