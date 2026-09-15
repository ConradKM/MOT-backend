"""Shared presigned-upload plumbing for small business-owned images.

Extracted from ``app/garages/logo.py``, which was the first caller and is now
a thin wrapper over this module. A second caller arrived with service groups
and services (see ``app/appointments/type_groups/``), and the part that must
never be reimplemented per-caller is :func:`sniff_image_content_type`: the
declared Content-Type and the filename extension are both just claims from a
client that is about to PUT straight into the bucket, so the only trustworthy
answer comes from the bytes that actually landed.

The three-step flow every caller follows:

1. :func:`request_image_upload` - validate the *declared* type/size and hand
   back a presigned PUT ticket for a fresh, tenant-scoped key. Nothing is
   persisted yet: a client that takes a ticket and never uploads must not
   leave a row claiming to have an image it doesn't have.
2. The client PUTs the bytes straight to object storage.
3. :func:`finalize_image_upload` - confirm the object landed, check its real
   size and sniff its real type, and hand the caller back the content type to
   persist. Deleting whatever the row pointed at *before* is the caller's job,
   and must happen only once the new reference is live.

This module deliberately knows nothing about models. Callers own their own
columns; all they share is the key layout and the validation.
"""

from __future__ import annotations

import uuid

from flask import current_app

from app.storage import get_storage

#: Declared content-type -> extension for the generated storage key. A brand
#: asset is PNG/JPEG/WebP; this is deliberately much narrower than
#: app/appointments/media's ALLOWED_CONTENT_TYPES, which also covers the
#: HEIC/video a mechanic's phone produces on a checklist.
IMAGE_CONTENT_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
HEAD_SNIFF_BYTES = 16


class ImageError(ValueError):
    """A rejected image operation (maps to 422)."""


class ImageNotUploadedError(ImageError):
    """finalize was called before the object actually landed (maps to 409)."""


def sniff_image_content_type(head: bytes) -> str | None:
    """The real content type of ``head`` (an object's first bytes), or None if
    it isn't a recognised image at all - a renamed executable, an empty
    object, a truncated upload, garbage."""
    if head.startswith(_PNG_SIGNATURE):
        return "image/png"
    if head.startswith(_JPEG_SIGNATURE):
        return "image/jpeg"
    if len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def key_prefix(garage_id: uuid.UUID, namespace: str) -> str:
    """The only prefix a given tenant + namespace ever issues keys under.

    Used both to build new keys and, at finalize time, to reject a key that
    was never handed out - see :func:`finalize_image_upload`.
    """
    return f"garages/{garage_id}/{namespace}/"


def request_image_upload(
    garage_id: uuid.UUID,
    namespace: str,
    *,
    content_type: str,
    size_bytes: int | None = None,
    max_bytes: int,
) -> dict:
    """Issue a presigned PUT ticket. Persists nothing - see the module docstring."""
    if content_type not in IMAGE_CONTENT_TYPES:
        raise ImageError("Use a PNG, JPEG or WebP image.")

    if size_bytes is not None and size_bytes > max_bytes:
        raise ImageError(f"Image exceeds the {max_bytes}-byte limit.")

    key = f"{key_prefix(garage_id, namespace)}{uuid.uuid4()}{IMAGE_CONTENT_TYPES[content_type]}"
    expires_in = current_app.config["STORAGE_PRESIGN_EXPIRY"]

    return {
        "storage_key": key,
        "upload_url": get_storage().presigned_put_url(key, content_type, expires_in),
        "expires_in": expires_in,
    }


def finalize_image_upload(
    garage_id: uuid.UUID,
    namespace: str,
    storage_key: str,
    *,
    max_bytes: int,
) -> str:
    """Validate what actually landed at ``storage_key`` and return its real
    content type, for the caller to persist.

    Raises rather than returning on every failure path, and deletes the
    offending object as it goes - a rejected upload must not be left sitting
    in the bucket under a tenant's prefix.
    """
    if not storage_key.startswith(key_prefix(garage_id, namespace)):
        # Defence in depth: request_image_upload only ever hands out keys
        # under this prefix, so this rejects a client passing back a key it
        # was never issued - another tenant's, or invented.
        raise ImageError("storage_key does not belong to this business.")

    storage = get_storage()
    if not storage.object_exists(storage_key):
        raise ImageNotUploadedError("No uploaded object found yet.")

    # The declared size at ticket time is only ever a claim - a presigned PUT
    # goes straight to the bucket, so nothing stopped the client uploading
    # more than it said it would. This is the real check.
    if storage.content_length(storage_key) > max_bytes:
        storage.delete(storage_key)
        raise ImageError(f"Image exceeds the {max_bytes}-byte limit.")

    content_type = sniff_image_content_type(storage.read_head(storage_key, HEAD_SNIFF_BYTES))
    if content_type is None:
        storage.delete(storage_key)
        raise ImageError("The uploaded file is not a valid PNG, JPEG or WebP image.")

    return content_type


def image_url(storage_key: str | None) -> str | None:
    """A fresh short-lived presigned GET url, or None when there's no image.

    Generated per call rather than stored, matching
    app/appointments/media/routes.py's download_url - a stored url would
    outlive its own signature.
    """
    if not storage_key:
        return None
    return get_storage().presigned_get_url(
        storage_key, current_app.config["STORAGE_PRESIGN_EXPIRY"]
    )


def delete_image(storage_key: str | None) -> None:
    """Remove the object, if there is one. Never touches any row."""
    if storage_key:
        get_storage().delete(storage_key)
