# Payment providers: architecture and how to add one

CoMaz is not dependent on Stripe. Every business picks its own payment
provider independently, and the booking/deposit/refund/capacity-hold logic
never knows or cares which one it's talking to. This document explains that
architecture and exactly what's involved in adding a new provider for real.

For Stripe Connect activation and payment-method-domain registration, see
[STRIPE_CONNECT_SETUP.md](STRIPE_CONNECT_SETUP.md); for Square, see
[SQUARE_SETUP.md](SQUARE_SETUP.md). This document is about the architecture,
not activation steps.

## Current state

| Provider | Status |
|---|---|
| Stripe | Implemented (`app/payments/providers/stripe_provider.py`) |
| PayPal | Architecture skeleton only (`app/payments/providers/paypal.py`) - not connected, never fakes success |
| Square | Implemented (`app/payments/providers/square.py`) - per-business OAuth (`app/payments/square_oauth.py`), card payments via the Web Payments SDK, two-phase charge (see below). Apple Pay / Google Pay via Square not in v1 |
| Fake | In-process test double (`app/payments/providers/fake.py`) - what CI/dev run against by default |

## Wallets (Apple Pay / Google Pay)

`Capabilities.supported_wallets` (a tuple of wallet names, e.g.
`("apple_pay", "google_pay")`) is purely descriptive - it never itself
enables a wallet. PayPal's skeleton reports `()`, and so does Square, because
v1 is cards only: Square wallets need their own domain verification in the
Square Developer Console and are a follow-up. Stripe's Express Checkout Element and Payment Element auto-detect and render a
wallet button when the browser/device supports it and the account is set
up for it; the backend's job is only to tell the frontend which wallets are
plausible via `available_wallets` inside `provider_data` (see
`stripe_provider.py::_client_data`), never to build a button itself.

**Manual, account-level step required for Apple Pay**: verify your domain
in the Stripe Dashboard (Settings -> Payment methods -> Apple Pay) before
Apple Pay will actually appear - this is not something any amount of code
here can do for you. Google Pay needs no separate verification.

## The call chain

```
Business (Garage)
  -> GaragePaymentSettings           (which provider? enabled at all?)
  -> app/payments/service.py         (deposit calc, capacity holds, refunds - provider-neutral)
  -> app/payments/providers/get_provider(name)
  -> a PaymentProvider adapter       (Stripe / PayPal / Square / fake)
```

Every business either has a `GaragePaymentSettings` row naming its provider,
or (the default, and what every business had before that table existed)
falls back to the deployment-wide `PAYMENTS_PROVIDER` config value. See
`app/payments/settings.py::resolve_provider_name`.

**Nothing above the adapter layer ever imports a provider SDK, matches on a
provider's own status strings, or reads a provider's raw webhook payload.**
That boundary is what makes swapping or adding a provider safe.

## The provider interface (`app/payments/providers/base.py`)

Every adapter implements `PaymentProvider`:

```python
class PaymentProvider(ABC):
    name: str
    capabilities: Capabilities

    def is_configured(self) -> bool: ...
    def create_payment(
        self, *, amount_minor, currency, idempotency_key, metadata
    ) -> PaymentSessionResult: ...
    def get_payment_status(self, provider_payment_id) -> PaymentSessionResult: ...
    def cancel_payment(self, provider_payment_id) -> None: ...
    def refund_payment(
        self, provider_payment_id, *, amount_minor, idempotency_key
    ) -> RefundResult: ...
    def verify_webhook(self, payload: bytes, headers: dict) -> ProviderWebhookEvent: ...
```

Every return value is a CoMaz-owned dataclass, never a provider SDK object:

- **`PaymentSessionResult`** - `provider_payment_id`, `status` (always one of
  `PAYMENT_STATUSES` below - never `"requires_payment_method"` or any other
  provider-native string), `checkout_mode`, `provider_data` (a dict of
  whatever client-safe fields the frontend needs - a Stripe `client_secret`,
  a future PayPal `approval_url`, ...), `metadata`.
- **`RefundResult`** - `provider_refund_id`, `status`, `amount_minor`.
- **`ProviderWebhookEvent`** - the *normalised* form of a webhook delivery:
  `kind` (one of the `WEBHOOK_*` constants below, never a raw Stripe event
  type or PayPal/Square event name), `provider_payment_id`,
  `provider_refund_id`, `status`, `failure_message`, and `raw` (the original
  payload, kept only for audit storage - domain code never reads it).

### Normalised payment statuses

```
REQUIRES_PAYMENT, PENDING, SUCCEEDED, FAILED, CANCELLED,
REFUND_PENDING, REFUNDED, PARTIALLY_REFUNDED, REFUND_FAILED
```

A provider's own status vocabulary (Stripe's `requires_payment_method`,
`processing`, `succeeded`, ...; PayPal's `COMPLETED`, `DENIED`, ...; Square's
`COMPLETED`, `FAILED`, ...) is mapped to these **inside that provider's own
adapter file** and never leaves it. `app/payments/service.py` only ever
reads/writes these nine strings.

### Normalised webhook event kinds

```
payment.succeeded, payment.failed, payment.cancelled, refund.updated, unhandled
```

Same rule: each adapter maps its own event names to these in
`verify_webhook`. `app/payments/service.py::_dispatch` switches on `kind`,
never on a provider's own event-type string.

### Capabilities

```python
@dataclass(frozen=True)
class Capabilities:
    supports_embedded_checkout: bool = False
    supports_redirect_checkout: bool = False
    supports_refunds: bool = False
    supports_partial_refunds: bool = False
    supports_payment_cancellation: bool = False
    supports_webhooks: bool = False
    supports_idempotency: bool = False
    supports_saved_payment_methods: bool = False
    supported_wallets: tuple[str, ...] = ()
    requires_client_token_submission: bool = False
```

Purely descriptive - nothing enforces these automatically. Call
`provider.capabilities.require("supports_partial_refunds")` before relying
on a capability a provider might not have; it raises `UnsupportedCapability`
(a subclass of `PaymentProviderError`) with a clear message instead of the
booking flow silently doing the wrong thing.

### Two-phase providers (`requires_client_token_submission`)

Stripe creates a PaymentIntent up front and the browser confirms it with its
`client_secret`. **Square has no such object.** The browser tokenises the card
with the Web Payments SDK into a single-use `source_id`, and the *server*
charges it with CreatePayment. An adapter like that sets
`Capabilities.requires_client_token_submission=True` and implements the
optional `charge_with_source(...)`:

1. `POST /api/public/<slug>/booking-requests/deposit-intent` behaves exactly
   as before (booking request, capacity hold, local `BookingPayment` in
   `REQUIRES_PAYMENT`). `create_payment()` charges nothing and returns
   `provider_payment_id=None` plus client-safe `provider_data` (Square:
   `application_id`, `location_id`, `environment`, `amount_minor`, `amount`,
   `currency`).
2. The browser tokenises with `card.tokenize(verificationDetails)`, which
   runs Strong Customer Authentication (3DS) as part of tokenising. That is
   Square's current replacement for the deprecated `verifyBuyer()` and its
   separate verification token. It then posts `{source_id}` (plus an optional
   legacy `verification_token`) to
   `POST /api/public/<slug>/booking-requests/<reference>/payments/token`.
   `app/payments/service.py::submit_client_payment_token` locks the request
   and its payments, calls `charge_with_source` with the attempt's own
   `deposit-<payment uuid>` idempotency key (44 characters, within Square's
   45-character limit), and returns `PENDING`. A double-submit returns the
   same attempt without charging again. A declined attempt stays `FAILED`
   and the next submission gets a fresh attempt row (a new idempotency key),
   so the customer can retry while the hold lasts.
3. The verified webhook remains the only thing that marks the deposit
   `SUCCEEDED`. Because the server created the provider payment, its webhook
   can arrive before the payment id is committed. The public status poll
   therefore re-checks a submitted attempt's live status
   (`reconcile_submitted_payment`), for two-phase providers only.

Domain code branches on the capability, never on a provider name. Resume and
recovery of a not-yet-charged attempt rebuild its checkout data from the
attempt itself (`resume_payment_session`). A payment that completes after its
hold expired goes through the same belated-success path as any provider
(`_reinstate_or_flag_expired_booking`).

### Checkout modes

- **`EMBEDDED`** - a provider-hosted widget rendered inline in the wizard
  (Stripe's Payment Element, Square's Web Payments SDK). The browser never
  leaves the page.
- **`REDIRECT`** - the customer is sent to the provider's own hosted page and
  comes back afterwards (classic PayPal Checkout).
- **`HOSTED`** - reserved for a provider that fits neither cleanly (e.g. an
  in-page modal that isn't quite "embedded" in the Stripe Element sense).

The frontend picks its checkout component from `provider` + `checkout_mode`
in the deposit-intent response - see "Frontend" below.

## The payment-session API contract

`POST /api/public/<slug>/booking-requests/deposit-intent` returns:

```json
{
  "provider": "stripe",
  "checkout_mode": "EMBEDDED",
  "provider_data": { "client_secret": "...", "publishable_key": "..." },
  "booking_request_id": "...",
  "booking_reference": "...",
  "status": "AWAITING_PAYMENT",
  "payment_status": "REQUIRES_PAYMENT",
  "currency": "GBP",
  "service_total": "100.00",
  "deposit_amount": "20.00",
  "remaining_balance": "80.00",
  "hold_expires_at": "..."
}
```

`provider_data` is the *only* place any provider-specific field lives, and
it's always client-safe (never a secret key, never anything that could
authorise a charge on our backend's behalf). The frontend branches on
`provider`/`checkout_mode`, not on which fields happen to be present.

## Business-specific provider selection

`GaragePaymentSettings` (`app/models/payments/garage_payment_settings.py`) -
one optional row per garage. Stripe rows additionally store the non-secret
connected-account ID and Stripe-synchronised onboarding/readiness fields.
Square rows store the merchant id, chosen location, connection status and the
merchant's OAuth tokens, **Fernet-encrypted** with `PAYMENTS_SECRET_KEY`
(`app/payments/secrets.py`):

| Column | Meaning |
|---|---|
| `provider` | Adapter name (`stripe`/`paypal`/`square`/...) |
| `enabled` | Garage-level kill switch, independent of any one Appointment Type's `deposit_required` |
| `configuration_status` | `NOT_CONFIGURED` / `TEST` / `LIVE` / `ERROR` - informational, for a future Platform Admin view |
| `live_mode` | Whether this garage's provider is in real/live mode |
| `merchant_account_reference` | An **opaque** reference (e.g. a secret-manager key name, or a future Stripe Connect account id) for a later per-tenant-credential model - never a secret itself, and nothing reads/writes real credentials through it today |

No row = the deployment's `PAYMENTS_PROVIDER` default, exactly as every
garage behaved before this table existed - fully backwards compatible.

The business owner picks the provider in **Settings -> Payments**
(`PUT /api/payments/provider`, owner-only; choices are
`OWNER_SELECTABLE_PROVIDERS` validated against the registered adapters).
Switching is refused with a 409 while any deposit is mid-checkout. Each
`BookingPayment` snapshots its own `provider` and `provider_account_id`
(Stripe account / Square merchant), and refunds and cancels always use those,
never the garage's current choice.

Per-business account readiness is provider-neutral:
`app/payments/settings.py::provider_account_ready(garage, provider)` (Stripe:
charges enabled; Square: connected, location chosen, usable token), called by
`is_payments_configured`. An unready provider gives the same graceful 503
"not available online right now" at deposit-intent.

### Credentials

Stripe is configured per deployment with CoMaz's platform keys, while each
garage's Direct Charges run in its own Stripe Connect account. The platform
key creates/manages those accounts; no garage secret is stored by CoMaz.

Square is configured per deployment with CoMaz's **application** credentials
(`SQUARE_APPLICATION_ID`/`_SECRET`). Each garage's own merchant access and
refresh tokens come from OAuth and are stored only encrypted
(`square_*_token_enc`, key `PAYMENTS_SECRET_KEY`, deliberately separate from
`COMMS_SECRET_KEY`). They are refreshed lazily at point of use and never
returned by an API, logged, audited or sent to the browser. The browser only
ever receives the application id, location id and environment.

`GaragePaymentSettings.merchant_account_reference` remains an unused, opaque
seam. Business users never see or edit a raw provider secret.

## Adding a new provider for real

1. **Adapter** (`app/payments/providers/<name>.py`) - implement
   `PaymentProvider`. Copy `stripe_provider.py`'s structure: a private
   status-map dict, a private event-kind map, and the five interface
   methods. Set `capabilities` honestly (only claim what you've actually
   tested).
2. **Credentials/config** (`app/config.py`) - the env vars it needs (see the
   `PAYPAL_*`/`SQUARE_*` placeholders already reserved there). Never commit
   real values; never log them.
3. **Status mapping** - inside the adapter only, provider-native status ->
   `PAYMENT_STATUSES`.
4. **Webhook verification** - inside the adapter's `verify_webhook`: verify
   the signature (raising `WebhookVerificationError` on failure), parse the
   payload, map the provider's event name to a `WEBHOOK_*` kind.
   `/api/webhooks/payments/<provider>` already routes to it - no new route
   needed (see `app/payments/webhooks.py`).
5. **Refund implementation** - `refund_payment`, returning a normalised
   `RefundResult`.
6. **Register it** in `app/payments/providers/__init__.py::get_provider`.
7. **Frontend checkout component** - add
   `src/components/customer/payments/<Name>Checkout.tsx` and register it in
   `PaymentCheckout.tsx`'s provider/checkout-mode switch (see "Frontend"
   below). The common Deposit UI (summary, branding, retry/error state)
   needs no changes.
8. **Tests** - mirror `tests/test_payment_providers.py` and
   `tests/api/test_payments_webhooks.py`; no real account needed for most of
   it (contract/mapping tests), but budget for one deliberately
   manually-verified pass against the provider's real sandbox before calling
   it done.
9. Deployment readiness in `is_payments_configured` is automatic once
   `is_configured()` is implemented honestly. If the provider charges per
   business, add its readiness check to `_ACCOUNT_READINESS` and its account
   id to `resolve_provider_account_id` in `app/payments/settings.py`.
10. If the browser tokenises and the server charges (like Square), set
    `requires_client_token_submission` and implement `charge_with_source` -
    see "Two-phase providers" above. No new route is needed.

## Frontend

`src/components/customer/DepositStep.tsx` owns the common UI: service total,
deposit amount, remaining balance, business branding, payment state,
retry/error state. It renders one child, `PaymentCheckout`
(`src/components/customer/payments/PaymentCheckout.tsx`), which switches on
`provider` + `checkout_mode` from the deposit-intent response and renders
the matching component:

- `src/components/customer/payments/StripeCheckout.tsx` - Stripe Elements.
- `src/components/customer/payments/SquareCheckout.tsx` - Square Web Payments
  SDK card form (script loaded only when a Square deposit is due), buyer
  verification, token submission, then polling for the webhook-confirmed
  outcome.
- A future `PayPalCheckout.tsx` would live alongside them, registered in
  `PaymentCheckout.tsx`.

A provider/checkout-mode combination with no matching component renders a
clear "this business's payment method isn't available right now" message -
never a blank screen, never a silent failure.

## What stays completely provider-independent

None of the following ever changes to add a provider:

- Fixed/percentage deposit calculation and validation
  (`app/payments/money.py`)
- Capacity holds, the 15-minute expiry sweep, booking-request transitions
  (`app/payments/service.py`, `app/public_booking/routes.py`)
- Rejection -> full refund, the manual staff refund endpoint
  (`app/booking_requests/routes.py`)
- Idempotency at the booking/webhook level (a provider's own idempotency key
  format is its own adapter's concern - see `Capabilities.supports_idempotency`)
- Business-side booking review UI, customer review UI
