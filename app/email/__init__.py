"""Outbound email abstraction.

One entry point - ``send_email`` - dispatches to the backend named by
``EMAIL_PROVIDER``. ``console`` (the dev default) logs the message so a
developer can copy a password-reset link out of the server log; ``resend`` is
wired to the real API; the remaining hosted providers (Postmark / SendGrid /
SES) are stubs that raise until wired, so switching provider later needs no
change to any caller.

This function can raise (a stub provider, an unconfigured/misconfigured
Resend account, or a genuine Resend API failure) - by design, so a programmer
error surfaces immediately rather than silently dropping mail. The two
existing callers (app/auth/reset.py, app/mot_reminders/service.py) accept
that. app/email/service.py, which drives the newer appointment/account
notifications, wraps this call itself so a Resend hiccup there can never
break the booking/account action that triggered the email - see its
docstring.
"""

from flask import current_app

_STUB_PROVIDERS = {"postmark", "sendgrid", "ses"}


def send_email(*, to: str, subject: str, body: str, html_body: str | None = None) -> None:
    provider = (current_app.config.get("EMAIL_PROVIDER") or "console").lower()
    sender = current_app.config.get("EMAIL_FROM", "no-reply@localhost")

    if provider == "console":
        # Dev only - logged at WARNING so it's visible in the dev server output.
        # "console" is never selected in production, so reset links are not
        # written to logs there.
        current_app.logger.warning(
            "[email:console] Would send email\n  from:    %s\n  to:      %s\n  subject: %s\n\n%s\n",
            sender,
            to,
            subject,
            body,
        )
        return

    if provider == "resend":
        _send_via_resend(to=to, subject=subject, body=body, html_body=html_body)
        return

    if provider in _STUB_PROVIDERS:
        raise RuntimeError(
            f"EMAIL_PROVIDER={provider!r} is not wired yet - add its client in "
            "app/email/ and an EMAIL_API_KEY, or use 'console' for local dev."
        )

    raise RuntimeError(
        f"Unknown EMAIL_PROVIDER {provider!r} - expected console, resend, "
        f"{', '.join(sorted(_STUB_PROVIDERS))}."
    )


def _send_via_resend(*, to: str, subject: str, body: str, html_body: str | None) -> None:
    """Send through the Resend API. Never reads/logs the API key itself -
    only whether it's present - and never includes it (or any request
    payload) in the exception it lets propagate on failure."""
    api_key = current_app.config.get("RESEND_API_KEY")
    from_address = current_app.config.get("RESEND_FROM_EMAIL") or current_app.config.get(
        "EMAIL_FROM", "no-reply@localhost"
    )

    if not api_key:
        raise RuntimeError(
            "EMAIL_PROVIDER=resend but RESEND_API_KEY is not set - see "
            "app/config.py and docs for the required environment variables."
        )

    import resend

    resend.api_key = api_key

    payload: resend.Emails.SendParams = {
        "from": from_address,
        "to": [to],
        "subject": subject,
        "text": body,
    }
    if html_body:
        payload["html"] = html_body

    resend.Emails.send(payload)
