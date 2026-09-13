# Payment providers: architecture and how to add one

CoMaz is not dependent on Stripe. Every business picks its own payment
provider independently, and the booking/deposit/refund/capacity-hold logic
never knows or cares which one it's talking to. This document explains that
architecture and exactly what's involved in adding a new provider for real.

For connecting the currently-implemented Stripe adapter to a real merchant
account, see [PAYMENTS_SETUP.md](PAYMENTS_SETUP.md) instead - this document
is about the architecture, not activation steps.

## Current state

| Provider | Status |
|---|---|
| Stripe | Implemented (`app/payments/providers/stripe_provider.py`) |
| PayPal | Architecture skeleton only (`app/payments/providers/paypal.py`) - not connected, never fakes success |
| Square | Architecture skeleton only (`app/payments/providers/square.py`) - not connected, never fakes success |
| Fake | In-process test double (`app/payments/providers/fake.py`) - what CI/dev run against by default |

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
```

Purely descriptive - nothing enforces these automatically. Call
`provider.capabilities.require("supports_partial_refunds")` before relying
on a capability a provider might not have; it raises `UnsupportedCapability`
(a subclass of `PaymentProviderError`) with a clear message instead of the
booking flow silently doing the wrong thing.

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
one optional row per garage:

| Column | Meaning |
|---|---|
| `provider` | Adapter name (`stripe`/`paypal`/`square`/...) |
| `enabled` | Garage-level kill switch, independent of any one Appointment Type's `deposit_required` |
| `configuration_status` | `NOT_CONFIGURED` / `TEST` / `LIVE` / `ERROR` - informational, for a future Platform Admin view |
| `live_mode` | Whether this garage's provider is in real/live mode |
| `merchant_account_reference` | An **opaque** reference (e.g. a secret-manager key name, or a future Stripe Connect account id) for a later per-tenant-credential model - never a secret itself, and nothing reads/writes real credentials through it today |

No row = the deployment's `PAYMENTS_PROVIDER` default, exactly as every
garage behaved before this table existed - fully backwards compatible.

There is deliberately no Platform Admin UI for this yet (see Part 19/23 of
the deposit spec this was built against) - set it directly via the ORM/a
migration/data fix until that's built. Building that UI is a natural,
separate follow-up once a second provider is actually implemented.

### Credentials: today vs. later

Today, every implemented provider (just Stripe) is configured **per
deployment**, via plain env vars (`STRIPE_SECRET_KEY`, etc.) - one CoMaz-
managed merchant account serves every business using that provider. This is
"Model A" (CoMaz-managed account).

`GaragePaymentSettings.merchant_account_reference` exists so a later "Model
B" (each business has its own merchant account/credentials) doesn't need a
new column - just a real secret-backed lookup behind that reference (a
secrets manager, or Stripe Connect's own account-id model). **Nothing
implements that today** - this is intentionally just the seam for it.
Business users must never see or edit a raw provider secret; if that
capability is built, it belongs in Platform Admin, reading from a proper
secret store, never a plaintext column on a normal business record.

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
9. Update `is_payments_configured`'s behaviour is automatic once
   `is_configured()` is implemented honestly - no separate change needed
   there.

## Frontend

`src/components/customer/DepositStep.tsx` owns the common UI: service total,
deposit amount, remaining balance, business branding, payment state,
retry/error state. It renders one child, `PaymentCheckout`
(`src/components/customer/payments/PaymentCheckout.tsx`), which switches on
`provider` + `checkout_mode` from the deposit-intent response and renders
the matching component:

- `src/components/customer/payments/StripeCheckout.tsx` - Stripe Elements,
  today's only real implementation.
- A future `PayPalCheckout.tsx` / `SquareCheckout.tsx` would live alongside
  it, registered in `PaymentCheckout.tsx`.

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
