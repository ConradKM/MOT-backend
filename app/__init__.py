from flask import Flask

from .config import Config
from .extensions import db, migrate, jwt


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)

    from .health.routes import health_bp
    from .customers.routes import customers_bp

    app.register_blueprint(health_bp, url_prefix="/api/health")
    app.register_blueprint(customers_bp, url_prefix="/api/customers")

    # Import models so Alembic can discover them.
    from .models import appointment, customer, garage, reminder, user, vehicle  # noqa: F401

    return app
