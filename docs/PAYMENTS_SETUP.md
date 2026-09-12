# Payments setup: connecting the real Stripe account

The deposit infrastructure (deposit config, payment records, capacity holds,
refunds, webhooks) is fully built and merged **dormant** - it ships disabled
by default and cannot break the existing booking flow. This document is the
only thing left to do: connect a real Stripe account.

Everything runs against an in-process fake provider in dev/CI today
(`PAYMENTS_PROVIDER=fake`), so nothing below is required to keep developing
or running tests.

## 1. Create the Stripe account

1. Go to <https://dashboard.stripe.com/register> and create (or use an
   existing) Stripe account for the business/platform that will hold funds.
2. Decide whether deposits are collected into one platform Stripe account
   (simplest - what this integration assumes) or per-business Connected
   Accounts (bigger change, out of scope for what's built - see "Not built"
   below).
3. Complete Stripe's business verification (bank details, business info) -
   required before you can go live, but not required to test in Test mode.

## 2. Get the keys

In the Stripe Dashboard, toggle **Test mode** first and use test keys until
you're ready to go live.

| Dashboard location | Env var |
|---|---|
| Developers -> API keys -> Secret key | `STRIPE_SECRET_KEY` |
| Developers -> API keys -> Publishable key | `STRIPE_PUBLISHABLE_KEY` |
| Developers -> Webhooks -> (your endpoint) -> Signing secret | `STRIPE_WEBHOOK_SECRET` |

Set these on the backend deployment (Render, or wherever it runs) alongside:

```
PAYMENTS_PROVIDER=stripe
```

`STRIPE_PUBLISHABLE_KEY` also needs to reach the frontend build - it's
returned to the browser by the deposit-intent endpoint
(`app/public_booking/routes.py::DepositIntentCreate`), so no separate
frontend env var is required; the backend hands it over per-request.

**Never commit any of these values.** Set them as deployment secrets only.

## 3. Configure the webhook endpoint

In the Stripe Dashboard: **Developers -> Webhooks -> Add endpoint**.

- **URL**: `https://<your-api-domain>/api/webhooks/payments/stripe`
  (e.g. `https://api.comaz.co.uk/api/webhooks/payments/stripe` - use
  `PUBLIC_API_BASE_URL` from your deployment's config to get the exact host).
- **Events to subscribe to**:
  - `payment_intent.succeeded`
  - `payment_intent.payment_failed`
  - `payment_intent.canceled`
  - `charge.refunded`
  - `refund.updated`

After creating it, copy the endpoint's **Signing secret** (starts `whsec_`)
into `STRIPE_WEBHOOK_SECRET`.

## 4. Test mode first

With test keys set:

1. Turn on a deposit for one Appointment Type (Settings -> Appointment
   Types -> edit -> "Require a deposit").
2. Go through the public booking wizard for that service and pay with a
   [Stripe test card](https://docs.stripe.com/testing#cards) (e.g.
   `4242 4242 4242 4242`, any future expiry, any CVC).
3. Confirm: the booking appears as a normal PENDING request in the business's
   Booking Requests list, showing "Deposit: paid".
4. Reject that request and confirm the payment's status flips to Refunded
   (Stripe Dashboard -> Payments -> the PaymentIntent -> Refunds).
5. Try a declining test card (e.g. `4000 0000 0000 0002`) and confirm the
   wizard shows a clear failure with a retry option, and the slot is
   eventually released if abandoned (default hold: 15 minutes -
   `PAYMENT_HOLD_MINUTES`).

## 5. Go live

1. In Stripe, complete account activation (if not already done) and switch
   the dashboard to **Live mode**.
2. Repeat step 2 and 3 above for the **live** keys/webhook (live and test
   webhooks are separate endpoints in Stripe - you need a second one with
   live keys).
3. Update the deployment's env vars to the live `STRIPE_SECRET_KEY` /
   `STRIPE_PUBLISHABLE_KEY` / `STRIPE_WEBHOOK_SECRET`. Redeploy.
4. Turn deposits on for whichever Appointment Types should actually require
   one (off is still the default for every type - nothing changes until you
   explicitly enable it per type).

## Production checklist

- [ ] Stripe account verified and activated for live payments
- [ ] Live `STRIPE_SECRET_KEY` / `STRIPE_PUBLISHABLE_KEY` set on the backend
      deployment (never committed to the repo)
- [ ] Live webhook endpoint added in Stripe pointing at
      `/api/webhooks/payments/stripe`, with `STRIPE_WEBHOOK_SECRET` set from
      its signing secret
- [ ] `PAYMENTS_PROVIDER=stripe` set
- [ ] A test deposit booking completed successfully in test mode before
      flipping to live
- [ ] Confirm a business owner actually wants deposits on before enabling
      `deposit_required` on any of their Appointment Types - it's off by
      default everywhere

## What happens if this isn't done yet

Nothing breaks. `is_payments_configured()`
(`app/payments/config.py`) reports "not configured" whenever
`STRIPE_SECRET_KEY`/`STRIPE_WEBHOOK_SECRET` are unset, and:

- Owners can still turn `deposit_required` on for an Appointment Type (the
  setting itself has no dependency on Stripe).
- A customer trying to book that service publicly gets a clear
  `503 Deposit payments aren't available for this business right now` instead
  of a crash - see `app/public_booking/routes.py::DepositIntentCreate`.
- Every other booking flow (no deposit required) is completely unaffected.

## Not built (deliberately out of scope)

- **Per-business Stripe Connect accounts / marketplace payouts.** This
  integration collects deposits into one platform Stripe account. Splitting
  funds out to individual businesses (Stripe Connect) is a materially
  different integration and was explicitly out of scope.
- **Partial refunds / a cancellation refund policy.** The infrastructure
  supports it (`BookingPayment.status` includes `PARTIALLY_REFUNDED`, and a
  manual staff-triggered refund endpoint exists -
  `POST /api/booking-requests/<id>/refund`), but no automatic policy decides
  *when* a cancellation should be refunded. Only a business rejecting an
  already-paid request auto-refunds (in full) today.
- **Non-card payment methods, subscriptions, split payments, tax/accounting.**

## Where things live in the codebase

- `app/payments/config.py` - the configuration gate (`is_payments_configured`)
- `app/payments/providers/` - the provider abstraction (`base.py`) and the
  Stripe (`stripe_provider.py`) / fake-for-tests (`fake.py`) adapters
- `app/payments/service.py` - deposit creation, hold expiry, refunds, webhook
  dispatch
- `app/payments/webhooks.py` - the Stripe webhook endpoint
- `app/models/payments/` - `BookingPayment`, `PaymentWebhookEvent`,
  `PaymentAuditLog`
- `app/appointments/types/` - deposit configuration on Appointment Types
- `app/public_booking/routes.py` - the public deposit-intent + status-poll
  endpoints
