from flask_jwt_extended import get_jwt_identity

from app.extensions import db
from app.models.user import User


def get_current_user() -> User | None:
    user_id = get_jwt_identity()

    if user_id is None:
        return None

    return db.session.get(User, int(user_id))
