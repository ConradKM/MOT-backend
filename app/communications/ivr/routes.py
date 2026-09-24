"""Settings > Phone menu - a business's own incoming-call menu.

Any staff member can read it; only an owner can change it, the same boundary
as the AI voice FAQs (app/ai_voice/faqs/routes.py). Scoped to the signed-in
employee's own business - no endpoint takes a business id.
"""

from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort
from marshmallow import Schema, fields

from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee

from . import service
from .actions import IvrValidationError

voice_menu_blp = Blueprint(
    "voice-menu",
    "voice-menu",
    url_prefix="/api/communications/voice-menu",
    description="A business's own incoming-call phone menu (IVR)",
)


class VoiceMenuOptionSchema(Schema):
    digit = fields.String(required=True)
    label = fields.String(required=True)
    prompt = fields.String(allow_none=True, load_default=None)
    action = fields.String(required=True)
    target = fields.String(allow_none=True, load_default=None)


class VoiceMenuUpdateSchema(Schema):
    enabled = fields.Boolean(required=True)
    greeting = fields.String(allow_none=True, load_default=None)
    options = fields.List(fields.Nested(VoiceMenuOptionSchema), required=True)
    fallback_action = fields.String(load_default="HUMAN_TRANSFER")
    fallback_target = fields.String(allow_none=True, load_default=None)
    max_attempts = fields.Integer(load_default=service.DEFAULT_MAX_ATTEMPTS)


@voice_menu_blp.route("")
class VoiceMenu(MethodView):
    @jwt_required()
    @voice_menu_blp.response(200)
    def get(self):
        garage = get_current_employee().garage
        return service.serialise(garage, service.get_settings(garage.id))

    @jwt_required()
    @owner_required
    @voice_menu_blp.arguments(VoiceMenuUpdateSchema)
    @voice_menu_blp.response(200)
    def put(self, data):
        garage = get_current_employee().garage
        try:
            settings = service.update_settings(garage, data)
        except IvrValidationError as exc:
            abort(
                422,
                message=str(exc),
                errors={"json": {exc.field or "_schema": [str(exc)]}},
            )
        return service.serialise(garage, settings)
