"""Encryption for the one class of secret CoMaz has to keep: a customer's
Twilio subaccount Auth Token.

Why it has to be kept at all: Twilio's Messaging **Senders v2** API
authenticates as whichever account's credentials sign the request - unlike the
2010-04-01 REST API there is no ``/Accounts/{subaccount}/…`` path a parent
account can act through. Registering a WhatsApp sender for a business
therefore requires that business's own subaccount credentials, which means
storing them.

How: Fernet (AES-128-CBC + HMAC-SHA256, authenticated) keyed by
``COMMS_SECRET_KEY``. The key lives only in the environment; the ciphertext
lives only in ``twilio_subaccount_credentials``. Neither on its own can act as
a customer's Twilio account.

If ``COMMS_SECRET_KEY`` is unset, :func:`encrypt_secret` raises rather than
falling back to plaintext - subaccount provisioning then reports a clear,
actionable blocker in Platform Admin instead of quietly storing a live
credential in the clear. Every other part of CoMaz keeps working: this is the
same "not configured is a supported state" rule
``app/communications/config.py`` already applies to Twilio itself.

Generate a key with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

from flask import current_app

#: Bumped only if the encryption scheme itself changes. Rotating
#: COMMS_SECRET_KEY does not change this - re-encrypted rows are identified by
#: whether they decrypt, not by their version.
CURRENT_KEY_VERSION = 1


class SecretsNotConfiguredError(RuntimeError):
    """``COMMS_SECRET_KEY`` is missing or unusable."""


class SecretDecryptionError(RuntimeError):
    """A stored ciphertext could not be decrypted with the current key.

    Almost always means ``COMMS_SECRET_KEY`` was changed or lost. Treated as
    "this subaccount's credential is unavailable", never as "the token is
    empty" - the difference decides whether an operator re-provisions or goes
    looking for the old key.
    """


def secrets_configured() -> bool:
    """Whether this deployment can store subaccount credentials at all."""
    return bool(current_app.config.get("COMMS_SECRET_KEY"))


def _fernet():
    key = current_app.config.get("COMMS_SECRET_KEY")
    if not key:
        raise SecretsNotConfiguredError(
            "COMMS_SECRET_KEY is not set for this deployment, so Twilio subaccount "
            "credentials cannot be stored. Set it before provisioning subaccounts."
        )
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
        raise SecretsNotConfiguredError(
            "The 'cryptography' package is not installed, so Twilio subaccount "
            "credentials cannot be encrypted."
        ) from exc

    try:
        return Fernet(key if isinstance(key, bytes) else key.encode())
    except (ValueError, TypeError) as exc:
        raise SecretsNotConfiguredError(
            "COMMS_SECRET_KEY is not a valid Fernet key. Generate one with Fernet.generate_key()."
        ) from exc


def encrypt_secret(value: str) -> str:
    """Ciphertext for ``value``, safe to store. Never returns the input."""
    ciphertext: bytes = _fernet().encrypt(value.encode())
    return ciphertext.decode()


def decrypt_secret(ciphertext: str) -> str:
    """The plaintext behind a stored ciphertext.

    Callers must use the result immediately (to build a Twilio client) and
    must never log it, return it from a view, or write it to another column.
    """
    from cryptography.fernet import InvalidToken

    try:
        plaintext: bytes = _fernet().decrypt(ciphertext.encode())
    except InvalidToken as exc:
        raise SecretDecryptionError(
            "A stored Twilio subaccount credential could not be decrypted - "
            "COMMS_SECRET_KEY has changed since it was written."
        ) from exc
    return plaintext.decode()
