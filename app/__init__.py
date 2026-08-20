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

    from .auth.routes import auth_blp
    from .customers.routes import customers_blp
    from .garages.routes import garages_blp
    from .health.routes import health_blp
    from .mot_records.routes import mot_records_blp
    from .vehicles.routes import vehicles_blp

    api.register_blueprint(health_blp)
    api.register_blueprint(auth_blp)
    api.register_blueprint(garages_blp)
    api.register_blueprint(customers_blp)
    api.register_blueprint(vehicles_blp)
    api.register_blueprint(mot_records_blp)

    from .models import appointment, customer, garage, mot_record, reminder, user, vehicle

    return app