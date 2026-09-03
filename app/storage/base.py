from typing import Protocol


class ObjectStorage(Protocol):
    """Minimal object-store surface the app needs.

    Implementations must namespace nothing themselves - callers pass a full
    `key` that already includes the garage-scoped prefix.
    """

    def presigned_put_url(self, key: str, content_type: str, expires_in: int) -> str:
        """A URL the client can PUT the object to, valid for `expires_in` seconds."""
        ...

    def presigned_get_url(self, key: str, expires_in: int) -> str:
        """A URL the client can GET the object from, valid for `expires_in` seconds."""
        ...

    def object_exists(self, key: str) -> bool:
        """Whether an object has actually landed at `key`."""
        ...

    def delete(self, key: str) -> None:
        """Remove the object at `key` (no error if it's already gone)."""
        ...
