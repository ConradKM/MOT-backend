"""The development-only gate for :mod:`app.dev`.

A single place that decides whether the local-dev helpers are allowed to run.
They print seeded credentials and internal ids, so the bar is deliberately
simple and fail-closed: run only when ``APP_ENV`` is exactly ``development``.
Anything else - ``production``, ``staging``, an unset/typo'd value that
somehow isn't the default - is refused.
"""

from __future__ import annotations

from flask import current_app

DEVELOPMENT = "development"


class NotDevelopmentError(RuntimeError):
    """Raised when a development-only helper is invoked outside development."""


def current_env(app=None) -> str:
    app = app or current_app
    return str(app.config.get("APP_ENV", DEVELOPMENT)).lower()


def is_development(app=None) -> bool:
    """True only when the app is explicitly configured as ``development``."""
    return current_env(app) == DEVELOPMENT


def require_development(app=None) -> None:
    """Raise :class:`NotDevelopmentError` unless this is a development app."""
    if not is_development(app):
        raise NotDevelopmentError(
            "This is a development-only command. It exposes seeded logins and "
            "internal ids and is disabled because APP_ENV is "
            f"{current_env(app)!r}, not {DEVELOPMENT!r}. It must never be run "
            "against a production database."
        )
