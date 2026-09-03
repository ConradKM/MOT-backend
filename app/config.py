import os


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://mot:mot@localhost:5432/mot_garage",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JWT_SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")

    API_TITLE = "MOT Garage API"
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
    # The availability calendar is a read endpoint the wizard polls as the
    # customer clicks around - a much looser limit than the write path.
    PUBLIC_AVAILABILITY_RATELIMIT = os.getenv(
        "PUBLIC_AVAILABILITY_RATELIMIT", "60 per minute"
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
