"""Pluggable object storage for checklist evidence (photos / videos).

The app never streams file bytes itself - it hands out short-lived presigned
URLs and clients upload/download straight to the bucket. Backend is chosen by
config (STORAGE_BACKEND):

* "s3"   - any S3-compatible bucket (AWS S3, Cloudflare R2, MinIO). Prod is
           designed around R2; MinIO is fine for local dev.
* "none" - no real storage (default for local dev / tests). Presigned URLs
           are stand-ins and object_exists() is always True.
"""

from flask import current_app

from .base import ObjectStorage
from .memory import MemoryStorage
from .s3 import S3Storage

_EXT_KEY = "_object_storage"


def get_storage() -> ObjectStorage:
    """The configured storage backend, cached on the app extensions dict."""
    store = current_app.extensions.get(_EXT_KEY)
    if store is not None:
        return store

    backend = (current_app.config.get("STORAGE_BACKEND") or "none").lower()
    if backend == "s3":
        store = S3Storage(
            bucket=current_app.config["STORAGE_BUCKET"],
            endpoint_url=current_app.config.get("STORAGE_ENDPOINT_URL") or None,
            region=current_app.config.get("STORAGE_REGION") or None,
            access_key_id=current_app.config.get("STORAGE_ACCESS_KEY_ID") or None,
            secret_access_key=current_app.config.get("STORAGE_SECRET_ACCESS_KEY") or None,
        )
    elif backend == "none":
        store = MemoryStorage()
    else:
        raise RuntimeError(f"Unknown STORAGE_BACKEND {backend!r} - expected 's3' or 'none'")

    current_app.extensions[_EXT_KEY] = store
    return store


__all__ = ["ObjectStorage", "get_storage"]
