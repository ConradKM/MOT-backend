class MemoryStorage:
    """No-op storage for local dev / tests.

    Presigned URLs are deterministic stand-ins pointing at a fake host.
    Issuing a PUT URL does *not* make the object exist - a client (or, in
    tests, `mark_uploaded`) has to "upload" it first - so the finalize step's
    existence check is still meaningful. Nothing is actually stored.
    """

    _HOST = "https://storage.local.test"

    def __init__(self) -> None:
        self._uploaded: set[str] = set()

    def presigned_put_url(self, key: str, content_type: str, expires_in: int) -> str:
        return f"{self._HOST}/{key}?method=PUT&content_type={content_type}&expires_in={expires_in}"

    def presigned_get_url(self, key: str, expires_in: int) -> str:
        return f"{self._HOST}/{key}?method=GET&expires_in={expires_in}"

    def object_exists(self, key: str) -> bool:
        return key in self._uploaded

    def delete(self, key: str) -> None:
        self._uploaded.discard(key)

    # --- test helper -------------------------------------------------------
    def mark_uploaded(self, key: str) -> None:
        """Simulate a client having PUT the object to `key`."""
        self._uploaded.add(key)
