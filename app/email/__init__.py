"""Outbound email abstraction.

One entry point - ``send_email`` - dispatches to the backend named by
``EMAIL_PROVIDER``. ``console`` (the dev default) logs the message so a
developer can copy a password-reset link out of the server log; the hosted
providers (Resend / Postmark / SendGrid / SES) are stubs that raise until
wired, so switching provider later needs no change to the reset flow.
"""

from flask import current_app

_STUB_PROVIDERS = {"resend", "postmark", "sendgrid", "ses"}


def send_email(*, to: str, subject: str, body: str) -> None:
    provider = (current_app.config.get("EMAIL_PROVIDER") or "console").lower()
    sender = current_app.config.get("EMAIL_FROM", "no-reply@localhost")

    if provider == "console":
        # Dev only - logged at WARNING so it's visible in the dev server output.
        # "console" is never selected in production, so reset links are not
        # written to logs there.
        current_app.logger.warning(
            "[email:console] Would send email\n"
            "  from:    %s\n  to:      %s\n  subject: %s\n\n%s\n",
            sender,
            to,
            subject,
            body,
        )
        return

    if provider in _STUB_PROVIDERS:
        raise RuntimeError(
            f"EMAIL_PROVIDER={provider!r} is not wired yet - add its client in "
            "app/email/ and an EMAIL_API_KEY, or use 'console' for local dev."
        )

    raise RuntimeError(
        f"Unknown EMAIL_PROVIDER {provider!r} - expected console, "
        f"{', '.join(sorted(_STUB_PROVIDERS))}."
    )
