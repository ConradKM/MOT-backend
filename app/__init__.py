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

    from .health.routes import health_blp
    from .customers.routes import customers_blp

    api.register_blueprint(health_blp)
    api.register_blueprint(customers_blp)

    from .models import appointment, customer, garage, reminder, user, vehicle

    return app