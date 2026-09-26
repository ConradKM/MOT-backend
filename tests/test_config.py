"""Configuration safeguards that must remain true in deployed environments."""

import pytest

from app import create_app
from app.config import TestConfig


def _unsafe_production_config(signing_key):
    class UnsafeProductionConfig(TestConfig):
        """A deployment accidentally started without a real signing key."""

        APP_ENV = "production"
        SECRET_KEY = signing_key
        JWT_SECRET_KEY = signing_key

    return UnsafeProductionConfig


class SafeProductionConfig(TestConfig):
    APP_ENV = "production"
    SECRET_KEY = "test-production-signing-key"
    JWT_SECRET_KEY = "test-production-signing-key"


@pytest.mark.parametrize("signing_key", ["", "change-me", "dev-only-change-me"])
def test_production_startup_rejects_missing_or_placeholder_signing_key(signing_key):
    with pytest.raises(RuntimeError, match="default signing key"):
        create_app(_unsafe_production_config(signing_key))


def test_production_startup_accepts_configured_signing_key():
    app = create_app(SafeProductionConfig)

    assert app.config["SECRET_KEY"] == "test-production-signing-key"
