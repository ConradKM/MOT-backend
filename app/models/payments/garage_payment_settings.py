"""Per-garage payment provider selection.

Optional and additive: a garage with no row here uses the deployment's
default provider (``PAYMENTS_PROVIDER`` - see app/payments/config.py),
exactly as every business behaved before this table existed. A row only
starts to matter once a business needs to choose a *different* provider (or
be disabled outright) from the platform default - see
app/payments/settings.py::resolve_provider_name.

Holds no plaintext secrets. The only credentials here are a connected
Square merchant's OAuth tokens, stored Fernet-encrypted (``*_enc`` columns,
see app/payments/secrets.py). ``merchant_account_reference`` is an opaque
pointer kept as a future seam; nothing reads or writes credentials through it.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.garage import Garage

# NOT_CONFIGURED: no working credentials yet (the default - safe, dormant).
# TEST: provider credentials present but in the provider's own test/sandbox
# mode. LIVE: real, live credentials - deposits can actually charge a card.
# ERROR: was configured but the platform has detected a problem (e.g. a
# webhook signature stopped verifying) - surfaced to Platform Admin so it
# doesn't fail silently as "just not configured yet".
PAYMENT_CONFIGURATION_STATUSES = ("NOT_CONFIGURED", "TEST", "LIVE", "ERROR")

# Square OAuth connection state. NOT_CONNECTED: never authorised (or
# disconnected). CONNECTED: tokens stored and usable. REVOKED: the merchant
# revoked CoMaz's access in Square (oauth.authorization.revoked webhook, or a
# refresh rejected as revoked) - needs a reconnect. ERROR: a token refresh or
# decryption failed for another reason - needs attention/reconnect.
SQUARE_STATUS_NOT_CONNECTED = "NOT_CONNECTED"
SQUARE_STATUS_CONNECTED = "CONNECTED"
SQUARE_STATUS_REVOKED = "REVOKED"
SQUARE_STATUS_ERROR = "ERROR"
SQUARE_STATUSES = (
    SQUARE_STATUS_NOT_CONNECTED,
    SQUARE_STATUS_CONNECTED,
    SQUARE_STATUS_REVOKED,
    SQUARE_STATUS_ERROR,
)


class GaragePaymentSettings(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    """One row per garage - which payment provider adapter it uses, and
    whether payments are allowed for it at all. See module docstring."""

    __tablename__ = "garage_payment_settings"
    __table_args__ = (UniqueConstraint("garage_id", name="uq_garage_payment_settings_garage_id"),)

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Adapter name - see app/payments/providers/__init__.py::get_provider.
    # Not validated against a fixed enum at the DB level (like
    # APPOINTMENT_TYPE_STATUSES elsewhere) so a new provider can be adopted
    # without a migration; the application layer validates it against the
    # actual registered adapters.
    provider: Mapped[str] = mapped_column(String(30), nullable=False, default="stripe")
    # A garage-level kill switch independent of deposit_required on any one
    # appointment type - flipping this off refuses every deposit attempt for
    # the business regardless of what its appointment types say.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    configuration_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="NOT_CONFIGURED"
    )
    live_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Opaque reference only - see module docstring. Superseded for Stripe by
    # the structured columns below, but left in place (unused) rather than
    # dropped - still a valid future seam for a non-Stripe per-tenant
    # credential model.
    merchant_account_reference: Mapped[str | None] = mapped_column(String(255))

    # --- Stripe Connect ------------------------------------------------
    # One tenant-bound connected account per business (see
    # app/payments/connect.py).
    # Direct Charges: the connected account is the merchant of record for its
    # own customers' deposits - CoMaz's platform Stripe account never holds
    # that money. Populated once onboarding starts; never a secret (Stripe
    # account ids are not credentials).
    stripe_account_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    # True once Stripe reports details_submitted on the account - onboarding
    # is done, even if a capability is still pending review.
    stripe_onboarding_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Mirrors Stripe Account.charges_enabled/payouts_enabled exactly - synced
    # from account.updated webhooks (app/payments/webhooks.py) and from an
    # explicit status refresh after the onboarding return redirect. A deposit
    # can only be taken through this account once charges_enabled is True -
    # see app/payments/config.py::is_payments_configured.
    stripe_charges_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stripe_payouts_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stripe_details_submitted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- Square (OAuth) -------------------------------------------------
    # One authorised Square merchant per business (see
    # app/payments/square_oauth.py). Funds settle straight to that merchant;
    # CoMaz never holds them. The merchant/location ids are not secrets; the
    # two tokens are, and are only ever stored Fernet-encrypted with
    # PAYMENTS_SECRET_KEY (app/payments/secrets.py) - never returned by any
    # API, logged, or audited.
    square_merchant_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    square_location_id: Mapped[str | None] = mapped_column(String(255))
    square_access_token_enc: Mapped[str | None] = mapped_column(Text)
    square_refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    square_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    square_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # One of SQUARE_STATUSES. REVOKED/ERROR stop deposits exactly like an
    # unready Stripe account (app/payments/settings.py::provider_account_ready).
    square_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="NOT_CONNECTED", server_default="NOT_CONNECTED"
    )

    garage: Mapped["Garage"] = relationship("Garage", back_populates="payment_settings")
