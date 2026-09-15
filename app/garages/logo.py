"""Business logo persistence.

Deliberately separate from ``app/garages/business_onboarding.py``: the logo
file must never be part of the tenant-provisioning transaction (see the
schemas' history - ``TenantBusinessInputSchema`` had no `logo` field and
briefly 422'd every onboarding submission when the console tried to send one
inline). This module is the *only* place a logo touches a Garage row, and it
never runs inside ``provision_tenant``.

The presigned three-step flow (ticket -> client PUTs -> finalize) and the
magic-byte sniffing that backs it now live in ``app/storage/images.py``,
shared with service-group and service images. This module is what maps that
flow onto the Garage row: which columns to write, and when the *previous*
object is safe to delete.
"""

from __future__ import annotations

from datetime import UTC, datetime

from flask import current_app

from app.extensions import db
from app.models.garage import Garage
from app.storage.images import (
    IMAGE_CONTENT_TYPES,
    ImageError,
    ImageNotUploadedError,
    delete_image,
    finalize_image_upload,
    image_url,
    request_image_upload,
)

#: Kept as a module-level alias for readers coming from the Garage model's
#: comments; the canonical list is app/storage/images.py.
LOGO_CONTENT_TYPES = IMAGE_CONTENT_TYPES

#: The tenant-scoped key namespace logos are issued under. Changing it would
#: orphan every existing logo object, so it is fixed.
LOGO_NAMESPACE = "branding"


# Named aliases rather than subclasses: app/platform_admin/routes/tenants.py
# catches these two by name to map them onto 422 / 409, and the storage layer
# raises the ImageError pair. Subclassing would mean the storage layer's own
# raises no longer matched, which is exactly the bug this avoids.
LogoError = ImageError
LogoNotUploadedError = ImageNotUploadedError


def request_logo_upload(
    garage: Garage,
    *,
    content_type: str,
    size_bytes: int | None = None,
) -> dict:
    """Issue a presigned PUT ticket for a new logo. Writes nothing to
    `garage` - see the module docstring for why."""
    return request_image_upload(
        garage.id,
        LOGO_NAMESPACE,
        content_type=content_type,
        size_bytes=size_bytes,
        max_bytes=current_app.config["LOGO_MAX_BYTES"],
    )


def finalize_logo_upload(
    garage: Garage,
    *,
    storage_key: str,
    original_filename: str | None = None,
) -> Garage:
    """Confirm the object at `storage_key` landed and is really an image,
    then make it this garage's live logo, deleting the previous one (if any)
    only once the new one is confirmed live."""
    content_type = finalize_image_upload(
        garage.id,
        LOGO_NAMESPACE,
        storage_key,
        max_bytes=current_app.config["LOGO_MAX_BYTES"],
    )

    previous_key = garage.logo_storage_key

    garage.logo_storage_key = storage_key
    garage.logo_content_type = content_type
    garage.logo_original_filename = original_filename
    garage.logo_uploaded_at = datetime.now(UTC)
    db.session.commit()

    # Only now - a failed finalize must never leave a business with no logo
    # at all when it had one a moment ago.
    if previous_key and previous_key != storage_key:
        delete_image(previous_key)

    return garage


def logo_metadata(garage: Garage) -> dict | None:
    """Current logo metadata + a fresh presigned GET url, or None if this
    business has no logo."""
    if not garage.logo_storage_key:
        return None

    return {
        "content_type": garage.logo_content_type,
        "original_filename": garage.logo_original_filename,
        "uploaded_at": garage.logo_uploaded_at,
        "url": image_url(garage.logo_storage_key),
        "expires_in": current_app.config["STORAGE_PRESIGN_EXPIRY"],
    }


def logo_public_url(garage: Garage) -> str | None:
    """The same presigned GET url, for the unauthenticated public-booking
    response - None when the business has no logo, so the frontend renders
    its own fallback rather than a broken-image icon."""
    return image_url(garage.logo_storage_key)


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

    delete_image(key)
