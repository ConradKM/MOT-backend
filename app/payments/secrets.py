"""Encryption at rest for per-business payment-provider tokens (today: each
connected Square merchant's OAuth access and refresh token).

The same Fernet scheme as app/communications/secrets.py, deliberately keyed by
its own ``PAYMENTS_SECRET_KEY`` rather than ``COMMS_SECRET_KEY``: a leaked or
rotated key for one class of secret must never expose (or invalidate) the
other.

If ``PAYMENTS_SECRET_KEY`` is unset or malformed, :func:`encrypt_token` raises
:class:`PaymentsSecretsNotConfiguredError` - connecting Square then fails with
a clear blocker instead of quietly storing a live merchant credential in the
clear.

Generate a key with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

from flask import current_app


class PaymentsSecretsNotConfiguredError(RuntimeError):
    """``PAYMENTS_SECRET_KEY`` is missing or unusable."""


class PaymentsSecretDecryptionError(RuntimeError):
    """A stored token could not be decrypted with the current key - almost
    always means ``PAYMENTS_SECRET_KEY`` changed. Treated as "this merchant's
    credential is unavailable" (reconnect), never as an empty token."""


def payments_secrets_configured() -> bool:
    """Whether this deployment can store provider tokens at all - validates
    the key itself, not just that the variable is set."""
    try:
        _fernet()
    except PaymentsSecretsNotConfiguredError:
        return False
    return True


def _fernet():
    key = current_app.config.get("PAYMENTS_SECRET_KEY")
    if not key:
        raise PaymentsSecretsNotConfiguredError(
            "PAYMENTS_SECRET_KEY is not set for this deployment, so payment-provider "
            "tokens cannot be stored. Set it before connecting Square."
        )
    from cryptography.fernet import Fernet

    try:
        return Fernet(key if isinstance(key, bytes) else key.encode())
    except (ValueError, TypeError) as exc:
        raise PaymentsSecretsNotConfiguredError(
            "PAYMENTS_SECRET_KEY is not a valid Fernet key. Generate one with "
            "Fernet.generate_key()."
        ) from exc


def encrypt_token(value: str) -> str:
    """Ciphertext for ``value``, safe to store. Never returns the input."""
    ciphertext: bytes = _fernet().encrypt(value.encode())
    return ciphertext.decode()


def decrypt_token(ciphertext: str) -> str:
    """The plaintext behind a stored ciphertext. Use immediately (an
    Authorization header); never log it, return it, or persist it elsewhere."""
    from cryptography.fernet import InvalidToken

    try:
        plaintext: bytes = _fernet().decrypt(ciphertext.encode())
    except InvalidToken as exc:
        raise PaymentsSecretDecryptionError(
            "A stored payment-provider token could not be decrypted - "
            "PAYMENTS_SECRET_KEY has changed since it was written."
        ) from exc
    return plaintext.decode()
