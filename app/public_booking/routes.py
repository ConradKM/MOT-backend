from datetime import UTC, date, datetime

from flask import current_app
from flask.views import MethodView
from flask_smorest import Blueprint, abort

from app.extensions import db, limiter
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.garage import Garage

from .availability import availability_range, check_slot_available, single_day
from .captcha import verify_captcha
from .schemas import (
    AvailabilityQueryArgsSchema,
    AvailabilityRangeSchema,
    BookingRequestCreatedSchema,
    BookingRequestCreateSchema,
    DaySlotsSchema,
    PublicGarageDetailSchema,
)

public_booking_blp = Blueprint(
    "public_booking",
    "public_booking",
    url_prefix="/api/public",
    description="Unauthenticated customer booking - garage lookup by slug, "
    "server-computed availability, and booking-request submission",
)


def _get_garage_by_slug(slug):
    garage = Garage.query.filter_by(slug=slug).first()
    if garage is None:
        abort(404, message="Garage not found")
    return garage


@public_booking_blp.route("/<slug>")
class PublicGarageBySlug(MethodView):

    @public_booking_blp.response(200, PublicGarageDetailSchema)
    def get(self, slug):
        garage = _get_garage_by_slug(slug)

        return {
            "id": garage.id,
            "name": garage.name,
            "slug": garage.slug,
            "appointment_types": [
                t for t in garage.appointment_types if t.status == "ACTIVE"
            ],
        }


@public_booking_blp.route("/<slug>/availability")
class PublicGarageAvailability(MethodView):

    @limiter.limit(lambda: current_app.config["PUBLIC_AVAILABILITY_RATELIMIT"])
    @public_booking_blp.arguments(AvailabilityQueryArgsSchema, location="query")
    @public_booking_blp.response(200, AvailabilityRangeSchema)
    def get(self, args, slug):
        garage = _get_garage_by_slug(slug)
        return availability_range(
            garage, args.get("from_"), args.get("to"), datetime.now(UTC)
        )


@public_booking_blp.route("/<slug>/availability/<day>")
class PublicGarageDayAvailability(MethodView):

    @limiter.limit(lambda: current_app.config["PUBLIC_AVAILABILITY_RATELIMIT"])
    @public_booking_blp.response(200, DaySlotsSchema)
    def get(self, slug, day):
        garage = _get_garage_by_slug(slug)
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            abort(422, message="day must be an ISO date (YYYY-MM-DD).")
        return single_day(garage, parsed, datetime.now(UTC))


@public_booking_blp.route("/<slug>/booking-requests")
class BookingRequestSubmit(MethodView):

    # Rate-limit before parsing anything. Storage / on-off / the limit string
    # itself are all config-driven (see app/config.py + app/extensions.py).
    @limiter.limit(lambda: current_app.config["PUBLIC_BOOKING_RATELIMIT"])
    @public_booking_blp.arguments(BookingRequestCreateSchema)
    @public_booking_blp.response(201, BookingRequestCreatedSchema)
    def post(self, data, slug):
        garage = _get_garage_by_slug(slug)

        if not verify_captcha(data.get("captcha_token")):
            abort(400, message="CAPTCHA verification failed.")

        appointment_type_id = data.get("appointment_type_id")
        if appointment_type_id is not None:
            appt_type = GarageAppointmentType.query.filter_by(
                id=appointment_type_id, garage_id=garage.id
            ).first()
            if appt_type is None or appt_type.status != "ACTIVE":
                abort(
                    422,
                    message="appointment_type_id is not an active type for this garage.",
                )

        # Double-booking guard: the slot the customer picked from the calendar
        # may have filled up since. Only meaningful when a specific time was
        # chosen - date-only requests carry no slot to re-check. Take a
        # per-garage row lock first so two concurrent submits can't both pass.
        preferred_time = data.get("preferred_time")
        if preferred_time is not None:
            db.session.query(Garage).filter_by(id=garage.id).with_for_update().one()
            if not check_slot_available(
                garage, data["preferred_date"], preferred_time, datetime.now(UTC)
            ):
                abort(
                    409,
                    message="This time is no longer available. "
                    "Please select another time.",
                )

        booking_request = BookingRequest(
            garage_id=garage.id,
            status="PENDING",
            customer_first_name=data["customer_first_name"],
            customer_last_name=data["customer_last_name"],
            customer_email=data["customer_email"],
            customer_phone=data.get("customer_phone"),
            vehicle_registration=data["vehicle_registration"],
            vehicle_make=data.get("vehicle_make"),
            vehicle_model=data.get("vehicle_model"),
            vehicle_year=data.get("vehicle_year"),
            vehicle_mileage=data.get("vehicle_mileage"),
            appointment_type_id=appointment_type_id,
            preferred_date=data["preferred_date"],
            preferred_time=preferred_time,
            preferred_employee_note=data.get("preferred_employee_note"),
            notes=data.get("notes"),
        )

        db.session.add(booking_request)
        db.session.commit()

        return booking_request
