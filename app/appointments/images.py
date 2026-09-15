"""Images on services and service groups.

Both models carry the same three columns (``image_storage_key`` /
``image_content_type`` / ``image_uploaded_at``) and want identical behaviour,
so the row-side half of the presigned flow lives here once rather than twice
in the two route modules. The validation half - and the magic-byte sniffing
that is the only trustworthy check on what actually landed - is
``app/storage/images.py``, shared with the business logo.

See ``app/models/appointments/appointment_type_group.py::DISPLAY_MODES`` for
why images matter at all: in GRID mode the picture is what the customer is
choosing between, so a business selling a *look* cannot sell it from a name.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from flask import current_app

from app.extensions import db
from app.storage.images import (
    delete_image,
    finalize_image_upload,
    image_url,
    request_image_upload,
)

#: Tenant-scoped key namespace. Distinct from the logo's "branding" so a
#: business's service photos and its brand mark never collide, and so either
#: could be lifecycled independently later.
IMAGE_NAMESPACE = "service-images"


def _max_bytes() -> int:
    return int(current_app.config["SERVICE_IMAGE_MAX_BYTES"])


def request_upload(garage_id: uuid.UUID, *, content_type: str, size_bytes: int | None) -> dict:
    """Issue a presigned PUT ticket. Writes nothing to any row - a client that
    takes a ticket and never uploads must not leave a service claiming an
    image it doesn't have."""
    return request_image_upload(
        garage_id,
        IMAGE_NAMESPACE,
        content_type=content_type,
        size_bytes=size_bytes,
        max_bytes=_max_bytes(),
    )


def finalize_upload(owner, *, storage_key: str):
    """Point ``owner`` (a service or a group) at a confirmed upload.

    The previous object is deleted only once the new reference is committed -
    a failed finalize must never leave a business with no image where it had
    one a moment ago.
    """
    content_type = finalize_image_upload(
        owner.garage_id, IMAGE_NAMESPACE, storage_key, max_bytes=_max_bytes()
    )

    previous_key = owner.image_storage_key

    owner.image_storage_key = storage_key
    owner.image_content_type = content_type
    owner.image_uploaded_at = datetime.now(UTC)
    db.session.commit()

    if previous_key and previous_key != storage_key:
        delete_image(previous_key)

    return owner


def clear_image(owner) -> None:
    """Drop the reference and remove the object. A no-op when there is none."""
    key = owner.image_storage_key
    if not key:
        return

    owner.image_storage_key = None
    owner.image_content_type = None
    owner.image_uploaded_at = None
    db.session.commit()

    delete_image(key)


def owner_image_url(owner) -> str | None:
    """A fresh short-lived presigned GET url, or None - the client renders its
    own fallback rather than a broken-image icon."""
    return image_url(owner.image_storage_key)
