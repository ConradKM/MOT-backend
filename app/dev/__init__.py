"""Local-development helpers.

Nothing in this package is imported by request-handling code. It backs two
CLI commands - ``flask seed-dev`` and ``flask dev-info`` - that only make
sense against a developer's local database and that print seeded logins and
internal ids. Both call :func:`app.dev.guard.require_development` first, so
they refuse to run when ``APP_ENV`` is anything other than ``development``.
"""
