# Stripe Connect setup

CoMaz is the Stripe Connect platform. Each garage gets an Express connected
account and customer deposits are **Direct Charges** on that account. CoMaz
does not receive or store a garage's Stripe credentials.

## Accounts v2 migration (new accounts only)

Stripe deprecated `/v1/accounts` for *creating* new connected accounts in
favour of `/v2/core/accounts` - `app/payments/connect.py::
create_connected_account` uses the v2 API for every new account from here
on. Nothing else changed: v1's `Account.retrieve`/`AccountLink`/webhooks all
keep working unmodified against a v2-created account, because Stripe's v1
endpoints accept a v2 account id and respond in v1 shape (Stripe's own
"Accounts v2" docs: "you can still pass the ID of a v2 Account to an
Accounts v1 API endpoint... the response is structured as a v1 Account").
Every account created before this change remains an ordinary v1 Express
account, untouched.

The new account is configured to reproduce a v1 `type="express"` account's
default behaviour exactly:

| v1 (old) | v2 (new) | Meaning |
| --- | --- | --- |
| `type="express"` | `dashboard="express"` | Same limited, CoMaz-branded Express Dashboard. |
| (default) | `defaults.responsibilities.fees_collector="stripe"` | Stripe deducts its processing fee directly from the connected account's charge - CoMaz takes no platform cut (no `application_fee_amount` is set anywhere in this codebase). |
| (default) | `defaults.responsibilities.losses_collector="stripe"` | Stripe is liable for the connected account's negative balances, same as a v1 Express account. |
| `capabilities.card_payments` | `configuration.merchant.capabilities.card_payments` | The only capability CoMaz has ever requested. |

**Before ever using this in Live mode**, verify in Stripe *Test* mode that a
freshly-created v2 account behaves identically end to end: connect a test
garage, complete the Stripe-hosted onboarding form, confirm the return
redirect and `GET /api/payments/stripe/status` correctly report
`stripe_charges_enabled`/`stripe_payouts_enabled`, and that a test deposit on
that account still works as a Direct Charge.

## Deployment variables

Set these only in the backend deployment. Use values all from Test mode or all
from Live mode; switching modes is an environment change, not a code change.

| Variable | Purpose |
| --- | --- |
| `PAYMENTS_PROVIDER=stripe` | Enables Stripe as the deployment default. |
| `STRIPE_SECRET_KEY` | CoMaz platform secret key; creates/manages accounts and Direct Charges. |
| `STRIPE_PUBLISHABLE_KEY` | Returned only alongside a customer PaymentIntent client secret. |
| `STRIPE_WEBHOOK_SECRET` | Signing secret for `/api/webhooks/payments/stripe` (keep configured for the provider gate). |
| `STRIPE_CONNECT_WEBHOOK_SECRET` | Signing secret for `/api/webhooks/payments/stripe/connect`. |
| `APP_BASE_URL` | Public frontend origin used for Stripe onboarding return and refresh URLs. |

Never put an `sk_` key or either `whsec_` secret in the frontend. The API gives
Stripe.js only the publishable key, the PaymentIntent client secret, and the
server-selected connected account ID.

## Dashboard setup (test first)

1. In Stripe **Test mode**, enable Connect and use CoMaz's platform test API
   keys above.
2. Create a **Connect webhook endpoint** at
   `https://<api-host>/api/webhooks/payments/stripe/connect`, choosing
   **Listen to events on connected accounts**. Subscribe to:
   `account.updated`, `payment_intent.succeeded`,
   `payment_intent.payment_failed`, `payment_intent.canceled`,
   `charge.refunded`, and `refund.updated`. Put its secret in
   `STRIPE_CONNECT_WEBHOOK_SECRET`.
3. Create the ordinary endpoint
   `https://<api-host>/api/webhooks/payments/stripe` with the payment/refund
   events above and put its secret in `STRIPE_WEBHOOK_SECRET`. Direct Charge
   payment events are processed at the Connect endpoint; this endpoint keeps
   provider configuration explicit and supports any platform events.
4. Deploy, sign in as the garage owner, go to **Settings → Payments**, and
   choose **Connect Stripe**. Complete Stripe's hosted Express onboarding.
   Returning to CoMaz refreshes the account from Stripe; only
   `charges_enabled` makes deposits available.
5. Enable a deposit on an appointment type, make a public booking, and use
   Stripe's `4242 4242 4242 4242` test card. Wait for the webhook: only then
   does the booking move from `AWAITING_PAYMENT` to `PENDING`. Reject it from
   Booking Requests to verify the refund targets the original connected
   account. Test a decline with `4000 0000 0000 0002` too.

## Live controlled smoke test

1. Complete Connect platform activation and repeat the two webhook endpoints
   in **Live mode**; test and live endpoints have distinct signing secrets.
2. Set the live `sk_live_`, `pk_live_`, ordinary webhook secret, and Connect
   webhook secret together; redeploy. Do not alter existing live credentials
   automatically.
3. Onboard one real garage through Settings → Payments and confirm its status
   says *Ready to take online payments*.
4. Enable a £1.00 (or another deliberately low permitted) deposit on a
   non-production test service, book it from a real supported device, confirm
   the Connect event and PENDING booking, then reject/refund it. Verify the
   PaymentIntent and refund are in that garage's connected-account context.
5. Disable that test service/deposit if it should not remain customer-facing.

## Apple Pay, Google Pay, and Link

The booking payment step uses Stripe's Express Checkout Element above the
Payment Element. It renders Apple Pay, Google Pay, and Link only when Stripe,
the connected account, the browser, and the device say they are eligible; an
empty wallet region and its separator are not shown.

For **Direct Charges**, register every public booking domain with **each
connected account that will accept Direct Charges** in Stripe's payment-method
domain workflow (or use Stripe's supported platform/domain-registration flow
for your Connect configuration). Register the exact configurable public
booking host, not a hard-coded CoMaz production hostname, in both test and
live as applicable. Upload/serve Stripe's domain-association asset when
Stripe requests it, then verify the domain in the Dashboard. Apple Pay needs
this domain verification. Google Pay and Link still depend on Stripe payment
method settings and browser/device eligibility; they are not guaranteed to
appear on every machine.

The browser result is never authoritative: `payment_intent.*` webhooks are
signature-verified and idempotently update the booking/payment records.
