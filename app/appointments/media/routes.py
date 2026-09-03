import uuid
from datetime import UTC, datetime

from flask import current_app
from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.appointments.appointment_checklist_item import AppointmentChecklistItem
from app.models.appointments.checklist_item_media import ChecklistItemMedia
from app.storage import get_storage

from .schemas import (
    ALLOWED_CONTENT_TYPES,
    MediaCreateSchema,
    MediaFinalizeSchema,
    MediaSchema,
    MediaUploadTicketSchema,
    MediaWithDownloadSchema,
)

checklist_item_media_blp = Blueprint(
    "checklist-item-media",
    "checklist-item-media",
    url_prefix="/api",
    description="Photo / video evidence for checklist items. The API only issues "
    "short-lived presigned URLs - bytes go straight to object storage.",
)

_EXT_FOR_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
}


def _owned_item(item_id):
    garage_id = get_current_employee().garage_id
    item = AppointmentChecklistItem.query.filter_by(
        id=item_id, garage_id=garage_id
    ).first()
    if item is None:
        abort(404, message="Checklist item not found")
    return item


def _owned_media(media_id):
    garage_id = get_current_employee().garage_id
    media = ChecklistItemMedia.query.filter_by(id=media_id, garage_id=garage_id).first()
    if media is None:
        abort(404, message="Media not found")
    return media


@checklist_item_media_blp.route(
    "/appointment-checklist-items/<uuid:item_id>/media"
)
class ChecklistItemMediaList(MethodView):

    @jwt_required()
    @checklist_item_media_blp.response(200, MediaSchema(many=True))
    def get(self, item_id):
        item = _owned_item(item_id)
        return [m for m in item.media if m.is_uploaded]

    @jwt_required()
    @checklist_item_media_blp.arguments(MediaCreateSchema)
    @checklist_item_media_blp.response(201, MediaUploadTicketSchema)
    def post(self, data, item_id):
        item = _owned_item(item_id)

        # The item's template media_type gates what can be attached.
        allowed = {"PHOTO", "VIDEO"} if item.media_type == "EITHER" else {item.media_type}
        if data["media_type"] not in allowed:
            abort(
                422,
                message=f"This checklist item only accepts {item.media_type} media.",
            )

        if data["content_type"] not in ALLOWED_CONTENT_TYPES[data["media_type"]]:
            abort(422, message="Unsupported file type.")

        max_bytes = current_app.config["MEDIA_MAX_BYTES"]
        if data.get("size_bytes") and data["size_bytes"] > max_bytes:
            abort(422, message=f"File exceeds the {max_bytes}-byte limit.")

        ext = _EXT_FOR_CONTENT_TYPE.get(data["content_type"], "")
        storage_key = (
            f"garages/{item.garage_id}/checklist-items/{item.id}/{uuid.uuid4()}{ext}"
        )

        media = ChecklistItemMedia(
            garage_id=item.garage_id,
            appointment_checklist_item_id=item.id,
            media_type=data["media_type"],
            content_type=data["content_type"],
            original_filename=data.get("original_filename"),
            size_bytes=data.get("size_bytes"),
            storage_key=storage_key,
        )
        db.session.add(media)
        db.session.commit()

        expires_in = current_app.config["STORAGE_PRESIGN_EXPIRY"]
        upload_url = get_storage().presigned_put_url(
            storage_key, data["content_type"], expires_in
        )

        return {
            "id": media.id,
            "storage_key": storage_key,
            "upload_url": upload_url,
            "expires_in": expires_in,
        }


@checklist_item_media_blp.route("/checklist-item-media/<uuid:media_id>/finalize")
class ChecklistItemMediaFinalize(MethodView):

    @jwt_required()
    @checklist_item_media_blp.arguments(MediaFinalizeSchema)
    @checklist_item_media_blp.response(200, MediaSchema)
    def post(self, data, media_id):
        media = _owned_media(media_id)

        if media.is_uploaded:
            abort(409, message="This upload has already been finalized.")

        if not get_storage().object_exists(media.storage_key):
            abort(409, message="No uploaded object found for this media yet.")

        media.uploaded_at = datetime.now(UTC)
        if data.get("size_bytes"):
            media.size_bytes = data["size_bytes"]
        db.session.commit()

        return media


@checklist_item_media_blp.route("/checklist-item-media/<uuid:media_id>")
class ChecklistItemMediaResource(MethodView):

    @jwt_required()
    @checklist_item_media_blp.response(200, MediaWithDownloadSchema)
    def get(self, media_id):
        media = _owned_media(media_id)

        if not media.is_uploaded:
            abort(409, message="This media hasn't finished uploading.")

        expires_in = current_app.config["STORAGE_PRESIGN_EXPIRY"]
        download_url = get_storage().presigned_get_url(media.storage_key, expires_in)

        return {
            "id": media.id,
            "appointment_checklist_item_id": media.appointment_checklist_item_id,
            "media_type": media.media_type,
            "content_type": media.content_type,
            "size_bytes": media.size_bytes,
            "original_filename": media.original_filename,
            "uploaded_at": media.uploaded_at,
            "created_at": media.created_at,
            "download_url": download_url,
            "expires_in": expires_in,
        }

    @jwt_required()
    @checklist_item_media_blp.response(204)
    def delete(self, media_id):
        media = _owned_media(media_id)

        if media.storage_key:
            get_storage().delete(media.storage_key)
        db.session.delete(media)
        db.session.commit()

        return ""
