from flask_jwt_extended import JWTManager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
from flask_smorest import Api
from flask_sock import Sock
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
migrate = Migrate()
jwt = JWTManager()
api = Api()

# WebSocket transport (app/ws/*). Only usable behind a WebSocket-capable
# server - gunicorn's gevent worker in production (see gunicorn.conf.py), the
# Werkzeug dev server locally. Registered routes are inert under a plain sync
# WSGI server, so this is safe to always initialise.
sock = Sock()

# Storage URI, enabled/disabled and the actual limits are all driven from
# app config (RATELIMIT_STORAGE_URI / RATELIMIT_ENABLED / per-view limits).
limiter = Limiter(key_func=get_remote_address)
