from flask import Flask

from .config import Config
from .extensions import api, db, jwt, limiter, migrate


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
    from .appointments.types.routes import appointment_types_blp
    from .auth.routes import auth_blp
    from .booking_requests.routes import booking_requests_blp
    from .customer_auth.routes import customer_auth_blp
    from .customer_portal.routes import customer_portal_blp
    from .customers.routes import customers_blp
    from .employees.routes import employees_blp
    from .garages.routes import garages_blp, public_garages_blp
    from .health.routes import health_blp
    from .mot_records.routes import mot_records_blp
    from .public_booking.routes import public_booking_blp
    from .roles.routes import roles_blp
    from .vehicles.routes import vehicles_blp

    api.register_blueprint(health_blp)
    api.register_blueprint(auth_blp)
    api.register_blueprint(customer_auth_blp)
    api.register_blueprint(customer_portal_blp)
    api.register_blueprint(garages_blp)
    api.register_blueprint(public_garages_blp)
    api.register_blueprint(public_booking_blp)
    api.register_blueprint(booking_requests_blp)
    api.register_blueprint(customers_blp)
    api.register_blueprint(employees_blp)
    api.register_blueprint(roles_blp)
    api.register_blueprint(vehicles_blp)
    api.register_blueprint(mot_records_blp)
    api.register_blueprint(appointment_types_blp)
    api.register_blueprint(checklist_templates_blp)
    api.register_blueprint(appointments_blp)
    api.register_blueprint(appointment_checklists_blp)
    api.register_blueprint(checklist_item_media_blp)

    from .models import (  # noqa: F401
        booking_request,
        customer,
        employee,
        garage,
        mot_record,
        reminder,
        role,
        vehicle,
    )
    from .models.appointments import (  # noqa: F401
        appointment,
        appointment_checklist,
        appointment_checklist_item,
        appointment_type,
        checklist_item_media,
        checklist_template,
        checklist_template_item,
    )

    return app