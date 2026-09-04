import uuid

from flask import Flask

from .config import Config
from .extensions import api, db, jwt, limiter, migrate


@jwt.token_in_blocklist_loader
def _employee_token_revoked(_jwt_header, jwt_payload) -> bool:
    """Reject an employee JWT whose account is deactivated or was issued
    before the user's last password reset. Customer-portal tokens
    (account_type == "customer") are left to app/customer_auth."""
    if jwt_payload.get("account_type") == "customer":
        return False

    from .models.employee import Employee

    identity = jwt_payload.get("sub")
    try:
        employee = db.session.get(Employee, uuid.UUID(identity))
    except (TypeError, ValueError):
        return True

    if employee is None or not employee.is_active:
        return True

    valid_from = employee.tokens_valid_from
    return (
        valid_from is not None
        and jwt_payload.get("iat", 0) < valid_from.timestamp()
    )


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    limiter.init_app(app)
    api.init_app(app)

    api.spec.components.security_scheme(
        "bearerAuth", {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    )

    from .appointments.checklist_templates.routes import checklist_templates_blp
    from .appointments.checklists.routes import appointment_checklists_blp
    from .appointments.media.routes import checklist_item_media_blp
    from .appointments.routes import appointments_blp
    from .appointments.statuses.routes import appointment_statuses_blp
    from .appointments.types.routes import appointment_types_blp
    from .auth.routes import auth_blp
    from .booking_requests.routes import booking_requests_blp
    from .customer_auth.routes import customer_auth_blp
    from .customer_portal.routes import customer_portal_blp
    from .customers.routes import customers_blp
    from .employees.routes import employees_blp
    from .garages.routes import garages_blp, public_garages_blp
    from .garages.schedule.routes import garage_schedule_blp
    from .health.routes import health_blp
    from .mot_records.routes import mot_records_blp
    from .mot_reminders.routes import mot_reminders_blp
    from .public_booking.routes import public_booking_blp
    from .roles.routes import roles_blp
    from .vehicles.routes import vehicles_blp

    api.register_blueprint(health_blp)
    api.register_blueprint(auth_blp)
    api.register_blueprint(customer_auth_blp)
    api.register_blueprint(customer_portal_blp)
    api.register_blueprint(garages_blp)
    api.register_blueprint(garage_schedule_blp)
    api.register_blueprint(public_garages_blp)
    api.register_blueprint(public_booking_blp)
    api.register_blueprint(booking_requests_blp)
    api.register_blueprint(customers_blp)
    api.register_blueprint(employees_blp)
    api.register_blueprint(roles_blp)
    api.register_blueprint(vehicles_blp)
    api.register_blueprint(mot_records_blp)
    api.register_blueprint(mot_reminders_blp)
    api.register_blueprint(appointment_types_blp)
    api.register_blueprint(appointment_statuses_blp)
    api.register_blueprint(checklist_templates_blp)
    api.register_blueprint(appointments_blp)
    api.register_blueprint(appointment_checklists_blp)
    api.register_blueprint(checklist_item_media_blp)

    from .models import (  # noqa: F401
        booking_request,
        customer,
        employee,
        garage,
        garage_schedule,
        mot_record,
        password_reset_token,
        reminder,
        role,
        vehicle,
    )
    from .models.appointments import (  # noqa: F401
        appointment,
        appointment_checklist,
        appointment_checklist_item,
        appointment_status,
        appointment_type,
        checklist_item_media,
        checklist_template,
        checklist_template_item,
    )

    return app