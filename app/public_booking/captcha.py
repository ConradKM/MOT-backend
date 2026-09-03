"""Provider-agnostic CAPTCHA verification for the public booking endpoint.

reCAPTCHA, hCaptcha and Cloudflare Turnstile all expose the same server-side
contract: form-POST ``secret`` + ``response`` to a verify URL, read
``success`` from the JSON reply. So one generic verifier covers all three;
the only per-provider bit is the URL.

Driven by config (see app/config.py):

* ``CAPTCHA_PROVIDER``  - "none" (default, skips verification), "recaptcha",
  "hcaptcha" or "turnstile". Anything else raises at request time so a
  typo can't silently disable protection.
* ``CAPTCHA_SECRET``    - the provider's secret key.
* ``CAPTCHA_VERIFY_URL`` - optional override of the per-provider default.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from flask import current_app

_VERIFY_URLS = {
    "recaptcha": "https://www.google.com/recaptcha/api/siteverify",
    "hcaptcha": "https://api.hcaptcha.com/siteverify",
    "turnstile": "https://challenges.cloudflare.com/turnstile/v0/siteverify",
}

_TIMEOUT_SECONDS = 5


def verify_captcha(token: str | None) -> bool:
    """True if `token` is a valid CAPTCHA response for the configured provider.

    Provider "none" always returns True (local dev / tests). A network or
    parse failure against a real provider returns False - fail closed.
    """
    provider = (current_app.config.get("CAPTCHA_PROVIDER") or "none").lower()

    if provider == "none":
        return True

    if provider not in _VERIFY_URLS:
        raise RuntimeError(
            f"Unknown CAPTCHA_PROVIDER {provider!r} - expected one of "
            f"none, {', '.join(_VERIFY_URLS)}"
        )

    if not token:
        return False

    secret = current_app.config.get("CAPTCHA_SECRET") or ""
    verify_url = current_app.config.get("CAPTCHA_VERIFY_URL") or _VERIFY_URLS[provider]
    body = urllib.parse.urlencode({"secret": secret, "response": token}).encode()

    try:
        with urllib.request.urlopen(  # noqa: S310 - fixed https provider URLs
            verify_url, data=body, timeout=_TIMEOUT_SECONDS
        ) as resp:
            payload = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False

    return bool(payload.get("success"))
