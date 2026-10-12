"""Subscription-feed tokens.

Same scheme as password-reset tokens (app/auth/reset.py): 32 random bytes from
``secrets.token_urlsafe``, only the SHA-256 stored. The raw token exists in
exactly two places - the response that creates or regenerates a feed, and the
calendar app it is pasted into. Nothing here logs it; gunicorn's access log
redacts it (gunicorn.conf.py).
"""

import hashlib
import hmac
import re
import secrets

from flask import current_app

from app.models.calendar_feed import CalendarFeed

# token_urlsafe(32) is always 43 characters of the URL-safe base64 alphabet.
# Anything else is rejected before touching the database, with the same 404 as
# an unknown token.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")

FEED_PATH_PREFIX = "/api/calendar-feeds/"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_token(feed: CalendarFeed) -> str:
    """Give ``feed`` a fresh token (replacing any old one, which stops working
    at once) and return the raw value - the only time it is ever available."""
    raw = secrets.token_urlsafe(32)
    feed.token_hash = hash_token(raw)
    return raw


def find_active_feed(raw: str) -> CalendarFeed | None:
    """The live feed a token belongs to; None for malformed, unknown and
    revoked tokens alike, so callers can't tell them apart."""
    if not _TOKEN_RE.match(raw or ""):
        return None
    token_hash = hash_token(raw)
    feed: CalendarFeed | None = CalendarFeed.query.filter_by(token_hash=token_hash).first()
    if feed is None or not hmac.compare_digest(feed.token_hash, token_hash):
        return None
    if feed.revoked_at is not None:
        return None
    return feed


def feed_urls(raw: str) -> dict[str, str]:
    """The https:// URL (Google's "From URL", or any client) and the webcal://
    form that makes Apple devices offer to subscribe in one tap."""
    base = str(current_app.config["PUBLIC_API_BASE_URL"]).rstrip("/")
    subscription_url = f"{base}{FEED_PATH_PREFIX}{raw}.ics"
    webcal_url = re.sub(r"^https?://", "webcal://", subscription_url)
    return {"subscription_url": subscription_url, "webcal_url": webcal_url}
