from flask_jwt_extended import JWTManager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
from flask_smorest import Api
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
migrate = Migrate()
jwt = JWTManager()
api = Api()

# Storage URI, enabled/disabled and the actual limits are all driven from
# app config (RATELIMIT_STORAGE_URI / RATELIMIT_ENABLED / per-view limits).
limiter = Limiter(key_func=get_remote_address)
