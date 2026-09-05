import os

from app.branding import PLATFORM_NAME


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://mot:mot@localhost:5432/mot_garage",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JWT_SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")

    API_TITLE = f"{PLATFORM_NAME} API"
    API_VERSION = "v1"
    OPENAPI_VERSION = "3.0.3"

    OPENAPI_URL_PREFIX = "/api"
    OPENAPI_SWAGGER_UI_PATH = "/docs"
    OPENAPI_SWAGGER_UI_URL = (
        "https://cdn.jsdelivr.net/npm/swagger-ui-dist/"
    )

    # --- Public booking (see app/public_booking) --------------------------
    # CAPTCHA verification for POST /api/public/<slug>/booking-requests.
    # "none" (default) skips it - set a real provider in production.
    CAPTCHA_PROVIDER = os.getenv("CAPTCHA_PROVIDER", "none")
    CAPTCHA_SECRET = os.getenv("CAPTCHA_SECRET", "")
    # Optional override; otherwise a per-provider default URL is used.
    CAPTCHA_VERIFY_URL = os.getenv("CAPTCHA_VERIFY_URL", "")

    # Rate limiting (Flask-Limiter). Falls back to Redis, then in-memory.
    RATELIMIT_STORAGE_URI = os.getenv(
        "RATELIMIT_STORAGE_URI", os.getenv("REDIS_URL", "memory://")
    )
    RATELIMIT_ENABLED = os.getenv("RATELIMIT_ENABLED", "true").lower() != "false"
    PUBLIC_BOOKING_RATELIMIT = os.getenv(
        "PUBLIC_BOOKING_RATELIMIT", "5 per hour;20 per day"
    )
    # Garage auth: login attempts and password-reset requests.
    AUTH_LOGIN_RATELIMIT = os.getenv(
        "AUTH_LOGIN_RATELIMIT", "10 per minute;100 per hour"
    )
    AUTH_RESET_RATELIMIT = os.getenv(
        "AUTH_RESET_RATELIMIT", "5 per hour;20 per day"
    )
    # The availability calendar is a read endpoint the wizard polls as the
    # customer clicks around - a much looser limit than the write path.
    PUBLIC_AVAILABILITY_RATELIMIT = os.getenv(
        "PUBLIC_AVAILABILITY_RATELIMIT", "60 per minute"
    )

    # --- Garage onboarding (see app/garages/onboarding.py) --------------
    # The supported way to create a tenant is the `flask onboard-garage` CLI.
    # POST /api/auth/register calls the same onboarding service; set this to
    # "false" to make onboarding CLI-only (the HTTP endpoint then 404s).
    ONBOARDING_HTTP_ENABLED = (
        os.getenv("ONBOARDING_HTTP_ENABLED", "true").lower() != "false"
    )

    # --- Checklist evidence storage (see app/storage) --------------------
    # "s3" for any S3-compatible bucket (AWS / Cloudflare R2 / MinIO), "none"
    # (default) for local dev - no real storage, stand-in presigned URLs.
    STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "none")
    STORAGE_BUCKET = os.getenv("STORAGE_BUCKET", "")
    STORAGE_ENDPOINT_URL = os.getenv("STORAGE_ENDPOINT_URL", "")
    STORAGE_REGION = os.getenv("STORAGE_REGION", "auto")
    STORAGE_ACCESS_KEY_ID = os.getenv("STORAGE_ACCESS_KEY_ID", "")
    STORAGE_SECRET_ACCESS_KEY = os.getenv("STORAGE_SECRET_ACCESS_KEY", "")
    STORAGE_PRESIGN_EXPIRY = int(os.getenv("STORAGE_PRESIGN_EXPIRY", "900"))
    MEDIA_MAX_BYTES = int(os.getenv("MEDIA_MAX_BYTES", str(100 * 1024 * 1024)))

    # --- Outbound email (see app/email) --------------------------------
    # Password-reset links are built from APP_BASE_URL. EMAIL_PROVIDER "console"
    # (default) just logs the message in dev; "resend" / "postmark" / "sendgrid"
    # / "ses" are pluggable later without touching the reset flow.
    APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:5173")
    EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "console")
    EMAIL_FROM = os.getenv("EMAIL_FROM", "no-reply@localhost")
    EMAIL_API_KEY = os.getenv("EMAIL_API_KEY", "")
    # Reset tokens live ~30 minutes.
    PASSWORD_RESET_TOKEN_MINUTES = int(
        os.getenv("PASSWORD_RESET_TOKEN_MINUTES", "30")
    )

    # --- Twilio communications (see app/communications) ------------------
    # CoMaz OS's own (master) Twilio account. Both unset (the default) is a
    # fully supported, permanent state for a deployment that hasn't turned on
    # communications yet - see app/communications/config.py::is_twilio_configured.
    # Never hard-code these; never commit real values.
    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
    # Twilio signs every webhook request; the app recomputes and compares that
    # signature before trusting the payload (app/communications/security.py).
    # Only ever set to "false" for local/manual testing with a client that
    # can't produce a real Twilio signature - production must leave this true.
    TWILIO_WEBHOOK_VALIDATE = (
        os.getenv("TWILIO_WEBHOOK_VALIDATE", "true").lower() != "false"
    )
    # If true, an inbound WhatsApp message that resolves to a garage gets an
    # immediate generic acknowledgement reply. Off by default - no CoMaz OS
    # deployment should send an automated WhatsApp reply until someone
    # deliberately turns it on for that environment.
    TWILIO_WHATSAPP_AUTO_ACK = (
        os.getenv("TWILIO_WHATSAPP_AUTO_ACK", "false").lower() == "true"
    )
    # This deployment's own public HTTPS origin (no trailing slash) - used only
    # to print the exact webhook URLs to give Twilio when configuring a number
    # (`flask twilio-webhook-urls`). Twilio itself is never told this by the
    # app; it's configured by hand in the Twilio console / API against
    # whichever number or WhatsApp sender you provision.
    PUBLIC_API_BASE_URL = os.getenv("PUBLIC_API_BASE_URL", "http://localhost:5001")


class TestConfig(Config):
    """Config for the automated test suite. Always targets a dedicated
    test database, independent of DATABASE_URL, so the developer's dev
    database is never touched by a test run."""

    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://mot:mot@localhost:5432/mot_garage_test",
    )
    PROPAGATE_EXCEPTIONS = True

    # Never call out to a real CAPTCHA provider, a shared rate-limit store, or
    # object storage from the test suite, regardless of the developer's shell.
    CAPTCHA_PROVIDER = "none"
    RATELIMIT_ENABLED = False
    RATELIMIT_STORAGE_URI = "memory://"
    STORAGE_BACKEND = "none"

    # No real Twilio account in CI/local test runs; signature validation would
    # otherwise reject every request the test suite sends itself. Tests that
    # specifically exercise validation flip this back on for the duration of
    # the test (see tests/api/test_twilio_webhooks.py).
    TWILIO_WEBHOOK_VALIDATE = False
