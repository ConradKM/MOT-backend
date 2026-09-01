from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort
from sqlalchemy.exc import IntegrityError

from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.role import Role

from .schemas import RoleSchema, RoleUpdateSchema

roles_blp = Blueprint(
    "roles",
    "roles",
    url_prefix="/api/roles",
    description="Per-garage employee roles",
)


@roles_blp.route("/")
class RoleList(MethodView):

    @jwt_required()
    @roles_blp.response(200, RoleSchema(many=True))
    def get(self):
        garage_id = get_current_employee().garage_id

        return Role.query.filter_by(garage_id=garage_id).order_by(Role.name).all()

    @jwt_required()
    @owner_required
    @roles_blp.arguments(RoleSchema)
    @roles_blp.response(201, RoleSchema)
    def post(self, data):
        garage_id = get_current_employee().garage_id

        role = Role(garage_id=garage_id, name=data["name"])
        db.session.add(role)

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            abort(409, message="A role with this name already exists.")

        return role


@roles_blp.route("/<uuid:role_id>")
class RoleResource(MethodView):

    @jwt_required()
    @owner_required
    @roles_blp.arguments(RoleUpdateSchema)
    @roles_blp.response(200, RoleSchema)
    def patch(self, data, role_id):
        garage_id = get_current_employee().garage_id

        role = Role.query.filter_by(id=role_id, garage_id=garage_id).first()

        if not role:
            abort(404, message="Role not found")

        if role.is_protected:
            abort(403, message=f'The "{role.name}" role cannot be renamed.')

        role.name = data["name"]

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            abort(409, message="A role with this name already exists.")

        return role

    @jwt_required()
    @owner_required
    @roles_blp.response(204)
    def delete(self, role_id):
        garage_id = get_current_employee().garage_id

        role = Role.query.filter_by(id=role_id, garage_id=garage_id).first()

        if not role:
            abort(404, message="Role not found")

        if role.is_protected:
            abort(403, message=f'The "{role.name}" role cannot be deleted.')

        db.session.delete(role)
        db.session.commit()
