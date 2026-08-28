from flask import Flask

from .config import Config
from .extensions import api, db, jwt, migrate


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    api.init_app(app)

    api.spec.components.security_scheme(
        "bearerAuth", {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    )

    from .appointments.routes import appointments_blp
    from .auth.routes import auth_blp
    from .customers.routes import customers_blp
    from .employees.routes import employees_blp
    from .garages.routes import garages_blp
    from .health.routes import health_blp
    from .mot_records.routes import mot_records_blp
    from .vehicles.routes import vehicles_blp

    api.register_blueprint(health_blp)
    api.register_blueprint(auth_blp)
    api.register_blueprint(garages_blp)
    api.register_blueprint(customers_blp)
    api.register_blueprint(employees_blp)
    api.register_blueprint(vehicles_blp)
    api.register_blueprint(mot_records_blp)
    api.register_blueprint(appointments_blp)

    from .models import (  # noqa: F401
        appointment,
        customer,
        employee,
        garage,
        mot_record,
        reminder,
        vehicle,
    )

    return app