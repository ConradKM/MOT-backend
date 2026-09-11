"""Business logo persistence.

Deliberately separate from ``app/garages/business_onboarding.py``: the logo
file must never be part of the tenant-provisioning transaction (see the
schemas' history - ``TenantBusinessInputSchema`` had no `logo` field and
briefly 422'd every onboarding submission when the console tried to send one
inline). This module is the *only* place a logo touches a Garage row, and it
never runs inside ``provision_tenant``.

Same presigned-URL shape as ``app/appointments/media/`` (see
``app/storage/``): the app never streams file bytes itself. The three-step
flow is:

1. :func:`request_logo_upload` - validate the *declared* type/size, return a
   presigned PUT ticket for a fresh, garage-scoped key. Nothing is written to
   the Garage row yet - a client that requests a ticket and never uploads
   must not make the business appear to have a logo.
2. The client PUTs the bytes straight to object storage.
3. :func:`finalize_logo_upload` - confirm the object actually landed, sniff
   its *real* content type from its first bytes (never trust the declared
   one, and never trust the filename extension), and only then persist the
   reference on the Garage row. The previous logo's object (if any) is
   deleted once the new one is live, never before - a failed finalize must
   never leave a business with no logo at all when it had one a moment ago.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from flask import current_app

from app.extensions import db
from app.models.garage import Garage
from app.storage import get_storage

#: Declared content-type -> file extension for the generated storage key.
#: Deliberately smaller than app/appointments/media's ALLOWED_CONTENT_TYPES -
#: a business logo is PNG/JPEG/WebP, never HEIC/video.
LOGO_CONTENT_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}

#: Magic-byte signatures for the same three types, used at finalize time to
#: check what was *actually* uploaded rather than trusting the client's
#: declared Content-Type or the filename extension.
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_HEAD_SNIFF_BYTES = 16


class LogoError(ValueError):
    """A rejected logo operation (maps to 422)."""


class LogoNotUploadedError(LogoError):
    """finalize was called before the object actually landed (maps to 409)."""


def _sniff_image_content_type(head: bytes) -> str | None:
    """The real content type of `head` (the object's first bytes), or None if
    it isn't a recognised image at all - a renamed executable, an empty
    object, a truncated upload, garbage."""
    if head.startswith(_PNG_SIGNATURE):
        return "image/png"
    if head.startswith(_JPEG_SIGNATURE):
        return "image/jpeg"
    if len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _logo_key(garage_id: uuid.UUID, content_type: str) -> str:
    ext = LOGO_CONTENT_TYPES[content_type]
    return f"garages/{garage_id}/branding/{uuid.uuid4()}{ext}"


def request_logo_upload(
    garage: Garage,
    *,
    content_type: str,
    size_bytes: int | None = None,
) -> dict:
    """Issue a presigned PUT ticket for a new logo. Writes nothing to
    `garage` - see the module docstring for why."""
    if content_type not in LOGO_CONTENT_TYPES:
        raise LogoError("Use a PNG, JPEG or WebP image.")

    max_bytes = current_app.config["LOGO_MAX_BYTES"]
    if size_bytes is not None and size_bytes > max_bytes:
        raise LogoError(f"Logo exceeds the {max_bytes}-byte limit.")

    key = _logo_key(garage.id, content_type)
    expires_in = current_app.config["STORAGE_PRESIGN_EXPIRY"]
    upload_url = get_storage().presigned_put_url(key, content_type, expires_in)

    return {"storage_key": key, "upload_url": upload_url, "expires_in": expires_in}


def finalize_logo_upload(
    garage: Garage,
    *,
    storage_key: str,
    original_filename: str | None = None,
) -> Garage:
    """Confirm the object at `storage_key` landed and is really an image,
    then make it this garage's live logo, deleting the previous one (if any)
    only once the new one is confirmed live."""
    expected_prefix = f"garages/{garage.id}/branding/"
    if not storage_key.startswith(expected_prefix):
        # Defence in depth: request_logo_upload only ever hands out keys
        # under this prefix, so this rejects a client passing back a key that
        # was never issued to it (another garage's, or made up).
        raise LogoError("storage_key does not belong to this business.")

    storage = get_storage()
    if not storage.object_exists(storage_key):
        raise LogoNotUploadedError("No uploaded object found for this logo yet.")

    # The declared size_bytes at ticket time (request_logo_upload) is only
    # ever a client's claim - a presigned PUT goes straight to the bucket, so
    # nothing stops it uploading more than it declared. This is the real
    # check, against what actually landed.
    max_bytes = current_app.config["LOGO_MAX_BYTES"]
    if storage.content_length(storage_key) > max_bytes:
        storage.delete(storage_key)
        raise LogoError(f"Logo exceeds the {max_bytes}-byte limit.")

    head = storage.read_head(storage_key, _HEAD_SNIFF_BYTES)
    sniffed_content_type = _sniff_image_content_type(head)
    if sniffed_content_type is None:
        # Whatever landed there isn't a real PNG/JPEG/WebP - don't leave it
        # sitting in the bucket under this business's prefix.
        storage.delete(storage_key)
        raise LogoError("The uploaded file is not a valid PNG, JPEG or WebP image.")

    previous_key = garage.logo_storage_key

    garage.logo_storage_key = storage_key
    garage.logo_content_type = sniffed_content_type
    garage.logo_original_filename = original_filename
    garage.logo_uploaded_at = datetime.now(UTC)
    db.session.commit()

    if previous_key and previous_key != storage_key:
        storage.delete(previous_key)

    return garage


def logo_metadata(garage: Garage) -> dict | None:
    """Current logo metadata + a fresh presigned GET url, or None if this
    business has no logo. The url is generated per call (short-lived,
    STORAGE_PRESIGN_EXPIRY) rather than stored - the same choice
    app/appointments/media/routes.py makes for download_url."""
    if not garage.logo_storage_key:
        return None

    expires_in = current_app.config["STORAGE_PRESIGN_EXPIRY"]
    return {
        "content_type": garage.logo_content_type,
        "original_filename": garage.logo_original_filename,
        "uploaded_at": garage.logo_uploaded_at,
        "url": get_storage().presigned_get_url(garage.logo_storage_key, expires_in),
        "expires_in": expires_in,
    }


def logo_public_url(garage: Garage) -> str | None:
    """The same presigned GET url, for the unauthenticated public-booking
    response - None when the business has no logo, so the frontend renders
    its own fallback rather than a broken-image icon."""
    if not garage.logo_storage_key:
        return None
    expires_in = current_app.config["STORAGE_PRESIGN_EXPIRY"]
    return get_storage().presigned_get_url(garage.logo_storage_key, expires_in)


def delete_logo(garage: Garage) -> None:
    """Clear the logo reference and remove the object. A no-op if the
    business has none."""
    key = garage.logo_storage_key
    if not key:
        return

    garage.logo_storage_key = None
    garage.logo_content_type = None
    garage.logo_original_filename = None
    garage.logo_uploaded_at = None
    db.session.commit()

    get_storage().delete(key)
