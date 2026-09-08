import os

from app.branding import PLATFORM_NAME


def _normalize_db_url(url: str) -> str:
    """Force the psycopg3 driver, regardless of the scheme a host's managed
    Postgres add-on hands back (e.g. Render's plain postgres:// / postgresql://,
    which SQLAlchemy would otherwise resolve to the uninstalled psycopg2)."""
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


class Config:
    # Deployment environment marker. "development" (the default) unlocks the
    # local-only helpers - `flask seed-dev`, `flask dev-info` - which print
    # seeded logins and internal ids. Any other value (notably "production")
    # makes them refuse to run. Deliberately separate from Flask's own
    # debug flag: those helpers must be off in production even if someone
    # left debug on.
    APP_ENV = os.getenv("APP_ENV", os.getenv("FLASK_ENV", "development")).lower()

    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
    SQLALCHEMY_DATABASE_URI = _normalize_db_url(
        os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg://mot:mot@localhost:5432/mot_garage",
        )
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JWT_SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")

    # --- CORS (see app/__init__.py) -------------------------------------
    # Browser origins allowed to call /api/* cross-origin. The dev frontend
    # uses Vite's same-origin proxy so needs no entry here; these cover
    # deployed static builds that call the API directly (app.comaz.co.uk) and
    # the local Vite dev/preview servers. Comma-separated; override with the
    # CORS_ORIGINS env var. Never "*" - the API is authenticated with an
    # Authorization bearer header.
    CORS_ORIGINS: tuple[str, ...] = tuple(
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,"
            "https://app.comaz.co.uk,https://comaz.co.uk",
        ).split(",")
        if origin.strip()
    )

    API_TITLE = f"{PLATFORM_NAME} API"
    API_VERSION = "v1"
    OPENAPI_VERSION = "3.0.3"

    OPENAPI_URL_PREFIX = "/api"
    OPENAPI_SWAGGER_UI_PATH = "/docs"
    OPENAPI_SWAGGER_UI_URL = "https://cdn.jsdelivr.net/npm/swagger-ui-dist/"

    # --- Public booking (see app/public_booking) --------------------------
    # CAPTCHA verification for POST /api/public/<slug>/booking-requests.
    # "none" (default) skips it - set a real provider in production.
    CAPTCHA_PROVIDER = os.getenv("CAPTCHA_PROVIDER", "none")
    CAPTCHA_SECRET = os.getenv("CAPTCHA_SECRET", "")
    # Optional override; otherwise a per-provider default URL is used.
    CAPTCHA_VERIFY_URL = os.getenv("CAPTCHA_VERIFY_URL", "")

    # Rate limiting (Flask-Limiter). Falls back to Redis, then in-memory.
    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", os.getenv("REDIS_URL", "memory://"))
    RATELIMIT_ENABLED = os.getenv("RATELIMIT_ENABLED", "true").lower() != "false"
    PUBLIC_BOOKING_RATELIMIT = os.getenv("PUBLIC_BOOKING_RATELIMIT", "5 per hour;20 per day")
    # Garage auth: login attempts and password-reset requests.
    AUTH_LOGIN_RATELIMIT = os.getenv("AUTH_LOGIN_RATELIMIT", "10 per minute;100 per hour")
    AUTH_RESET_RATELIMIT = os.getenv("AUTH_RESET_RATELIMIT", "5 per hour;20 per day")
    # The availability calendar is a read endpoint the wizard polls as the
    # customer clicks around - a much looser limit than the write path.
    PUBLIC_AVAILABILITY_RATELIMIT = os.getenv("PUBLIC_AVAILABILITY_RATELIMIT", "60 per minute")

    # --- Garage onboarding (see app/garages/onboarding.py) --------------
    # The supported way to create a tenant is the `flask onboard-garage` CLI.
    # POST /api/auth/register calls the same onboarding service; set this to
    # "false" to make onboarding CLI-only (the HTTP endpoint then 404s).
    ONBOARDING_HTTP_ENABLED = os.getenv("ONBOARDING_HTTP_ENABLED", "true").lower() != "false"

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
    PASSWORD_RESET_TOKEN_MINUTES = int(os.getenv("PASSWORD_RESET_TOKEN_MINUTES", "30"))

    # --- Twilio communications (see app/communications) ------------------
    # CoMaz OS's own (master) Twilio account. Both unset (the default) is a
    # fully supported, permanent state for a deployment that hasn't turned on
    # communications yet - see app/communications/config.py::is_twilio_configured.
    # Never hard-code these; never commit real values.
    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
    # Optional: a Twilio Standard API Key (SID starts "SK") and its secret.
    # When BOTH are set, the outbound REST client (app/communications/client.py)
    # authenticates with the API key + Account SID instead of the Auth Token -
    # Twilio's recommendation for production. The Auth Token is still required:
    # webhook signature validation (security.py) has no API-key equivalent.
    TWILIO_API_KEY_SID = os.getenv("TWILIO_API_KEY_SID", "")
    TWILIO_API_KEY_SECRET = os.getenv("TWILIO_API_KEY_SECRET", "")
    # Twilio signs every webhook request; the app recomputes and compares that
    # signature before trusting the payload (app/communications/security.py).
    # Only ever set to "false" for local/manual testing with a client that
    # can't produce a real Twilio signature - production must leave this true.
    TWILIO_WEBHOOK_VALIDATE = os.getenv("TWILIO_WEBHOOK_VALIDATE", "true").lower() != "false"
    # If true, an inbound WhatsApp message that resolves to a garage gets an
    # immediate generic acknowledgement reply. Off by default - no CoMaz OS
    # deployment should send an automated WhatsApp reply until someone
    # deliberately turns it on for that environment.
    TWILIO_WHATSAPP_AUTO_ACK = os.getenv("TWILIO_WHATSAPP_AUTO_ACK", "false").lower() == "true"
    # This deployment's own public HTTPS origin (no trailing slash) - used only
    # to print the exact webhook URLs to give Twilio when configuring a number
    # (`flask twilio-webhook-urls`). Twilio itself is never told this by the
    # app; it's configured by hand in the Twilio console / API against
    # whichever number or WhatsApp sender you provision.
    PUBLIC_API_BASE_URL = os.getenv("PUBLIC_API_BASE_URL", "http://localhost:5001")

    # --- ConversationRelay voice assistant (app/communications/voice_relay.py,
    #     app/ws/twilio_voice.py) ---------------------------------------------
    # Off by default: an inbound call gets the existing static <Say> greeting
    # until this is switched on for the deployment. On => a resolved inbound
    # call returns <Connect><ConversationRelay> pointing at the WebSocket
    # bridge, which needs Twilio configured AND a WebSocket-capable server
    # (gunicorn's gevent worker - gunicorn.conf.py). If the TwiML build fails
    # for any reason the call still falls back to the static greeting.
    TWILIO_CONVERSATIONRELAY_ENABLED = (
        os.getenv("TWILIO_CONVERSATIONRELAY_ENABLED", "false").lower() == "true"
    )
    # STT + TTS language for the assistant - en-GB, never US English for a UK
    # business. Optional explicit provider/voice; unset => the provider's
    # default voice for the language.
    CONVERSATIONRELAY_LANGUAGE = os.getenv("CONVERSATIONRELAY_LANGUAGE", "en-GB")
    CONVERSATIONRELAY_TTS_PROVIDER = os.getenv("CONVERSATIONRELAY_TTS_PROVIDER", "")
    CONVERSATIONRELAY_VOICE = os.getenv("CONVERSATIONRELAY_VOICE", "")

    # --- Conversation engine (see app/conversation) -----------------------
    # The development conversation simulator (POST /api/conversation/simulate)
    # runs real customer messages through the real booking engine without
    # Twilio - essential before live credentials exist, but it must NEVER be
    # reachable in production (Part 29). Defaults on so local dev/CI need no
    # setup; a production deployment MUST set this to "false" explicitly.
    CONVERSATION_SIMULATOR_ENABLED = (
        os.getenv("CONVERSATION_SIMULATOR_ENABLED", "true").lower() != "false"
    )


class TestConfig(Config):
    """Config for the automated test suite. Always targets a dedicated
    test database, independent of DATABASE_URL, so the developer's dev
    database is never touched by a test run."""

    TESTING = True
    SQLALCHEMY_DATABASE_URI = _normalize_db_url(
        os.getenv(
            "TEST_DATABASE_URL",
            "postgresql+psycopg://mot:mot@localhost:5432/mot_garage_test",
        )
    )
    PROPAGATE_EXCEPTIONS = True

    # Deterministic CORS allowlist for the test suite, independent of any
    # CORS_ORIGINS env var the developer's shell might carry.
    CORS_ORIGINS: tuple[str, ...] = (
        "http://localhost:5173",
        "https://app.comaz.co.uk",
        "https://comaz.co.uk",
    )

    # Deterministic regardless of the developer's shell - the dev-only helper
    # tests flip this to "production" per-test to prove the refusal path.
    APP_ENV = "development"

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
