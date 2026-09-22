from datetime import UTC, datetime

from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.ai_voice_faq import GarageVoiceFAQ

from .schemas import VoiceFAQReorderSchema, VoiceFAQSchema, VoiceFAQUpdateSchema

voice_faqs_blp = Blueprint(
    "voice-faqs",
    "voice-faqs",
    url_prefix="/api/ai-voice/faqs",
    description="Owner-managed business FAQ knowledge for the AI phone agent",
)


def _owned_faq(faq_id, garage_id):
    faq = GarageVoiceFAQ.query.filter_by(id=faq_id, garage_id=garage_id, archived_at=None).first()
    if faq is None:
        abort(404, message="Voice FAQ not found")
    return faq


@voice_faqs_blp.route("/")
class VoiceFAQList(MethodView):
    @jwt_required()
    @voice_faqs_blp.response(200, VoiceFAQSchema(many=True))
    def get(self):
        garage_id = get_current_employee().garage_id
        return (
            GarageVoiceFAQ.query.filter_by(garage_id=garage_id, archived_at=None)
            .order_by(GarageVoiceFAQ.order, GarageVoiceFAQ.question)
            .all()
        )

    @jwt_required()
    @owner_required
    @voice_faqs_blp.arguments(VoiceFAQSchema)
    @voice_faqs_blp.response(201, VoiceFAQSchema)
    def post(self, data):
        faq = GarageVoiceFAQ(garage_id=get_current_employee().garage_id, **data)
        db.session.add(faq)
        db.session.commit()
        return faq


@voice_faqs_blp.route("/order")
class VoiceFAQReorder(MethodView):
    @jwt_required()
    @owner_required
    @voice_faqs_blp.arguments(VoiceFAQReorderSchema)
    @voice_faqs_blp.response(200, VoiceFAQSchema(many=True))
    def put(self, data):
        garage_id = get_current_employee().garage_id
        faqs = GarageVoiceFAQ.query.filter_by(garage_id=garage_id, archived_at=None).all()
        by_id = {faq.id: faq for faq in faqs}
        ids = data["ids"]
        if len(set(ids)) != len(ids) or set(ids) != set(by_id):
            abort(422, message="ids must list every active Voice FAQ exactly once.")
        for position, faq_id in enumerate(ids):
            by_id[faq_id].order = position
        db.session.commit()
        return sorted(faqs, key=lambda faq: (faq.order, faq.question))


@voice_faqs_blp.route("/<uuid:faq_id>")
class VoiceFAQResource(MethodView):
    @jwt_required()
    @voice_faqs_blp.response(200, VoiceFAQSchema)
    def get(self, faq_id):
        return _owned_faq(faq_id, get_current_employee().garage_id)

    @jwt_required()
    @owner_required
    @voice_faqs_blp.arguments(VoiceFAQUpdateSchema)
    @voice_faqs_blp.response(200, VoiceFAQSchema)
    def patch(self, data, faq_id):
        faq = _owned_faq(faq_id, get_current_employee().garage_id)
        for field, value in data.items():
            setattr(faq, field, value)
        db.session.commit()
        return faq

    @jwt_required()
    @owner_required
    @voice_faqs_blp.response(204)
    def delete(self, faq_id):
        faq = _owned_faq(faq_id, get_current_employee().garage_id)
        faq.is_enabled = False
        faq.archived_at = datetime.now(UTC)
        db.session.commit()
        return ""
