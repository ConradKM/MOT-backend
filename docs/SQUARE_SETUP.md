# Square setup: taking deposits through a business's own Square account

CoMaz registers **one Square application**. Each business connects its own
Square seller account to it with OAuth from **Settings -> Payments** (the
equivalent of Stripe Connect). Deposits are charged with that business's own
token, so the money settles straight into the business's Square account.
CoMaz never holds it and takes no application fee.

The architecture and the two-phase charge are covered in
[PAYMENTS_PROVIDERS.md](PAYMENTS_PROVIDERS.md). This document covers
activation only.

## 1. Create the Square application

1. Sign in at <https://developer.squareup.com/apps> and click **Create an
   application** ("CoMaz", or one per environment if you prefer).
2. On **Credentials**, note the **Application ID** for Sandbox and for
   Production. They are different values.
3. On **OAuth**, note the **Application secret** for each environment.

## 2. Set the OAuth redirect URL

On the application's **OAuth** page, for each environment, set **Redirect
URL** to the backend callback:

```
https://<api-host>/api/payments/square/oauth/callback
```

Use exactly the same value for `SQUARE_OAUTH_REDIRECT_URL`. Square sends the
seller back here after they approve or deny. The backend validates the signed
`state`, exchanges the code, then redirects the browser to
`/<garageId>/settings/payments?square=return`.

Scopes requested: `PAYMENTS_WRITE`, `PAYMENTS_READ`, `MERCHANT_PROFILE_READ`
(see `app/payments/square_oauth.py::SQUARE_OAUTH_SCOPES`).

## 3. Register the webhook subscription

On **Webhooks -> Subscriptions**, add a subscription for each environment:

- **Notification URL**: `https://<api-host>/api/webhooks/payments/square`
- **API version**: the same as `SQUARE_API_VERSION` (default `2026-08-19`)
- **Events**: `payment.created`, `payment.updated`, `refund.created`,
  `refund.updated`, `oauth.authorization.revoked`

Copy the subscription's **Signature key** into `SQUARE_WEBHOOK_SIGNATURE_KEY`.
Set `SQUARE_WEBHOOK_NOTIFICATION_URL` to the notification URL **exactly** as
registered (scheme, host, path, no trailing slash difference). Square's
signature is an HMAC over that string plus the raw body, so any mismatch
rejects every delivery with a 400.

The application's webhooks fire for every seller who has authorised it. The
backend resolves the business from the event's `merchant_id`. An event for an
unknown merchant is recorded and acknowledged with a 200.

## 4. Environment variables

| Variable | Value |
|---|---|
| `SQUARE_APPLICATION_ID` | Application ID for the chosen environment |
| `SQUARE_APPLICATION_SECRET` | Application secret for the chosen environment |
| `SQUARE_ENVIRONMENT` | `sandbox` or `production` (selects the API and OAuth host) |
| `SQUARE_WEBHOOK_SIGNATURE_KEY` | The webhook subscription's signature key |
| `SQUARE_WEBHOOK_NOTIFICATION_URL` | The exact registered notification URL |
| `SQUARE_OAUTH_REDIRECT_URL` | The exact registered OAuth redirect URL |
| `SQUARE_API_VERSION` | Optional; pinned default `2026-08-19`. Bump deliberately after reading Square's release notes |
| `PAYMENTS_SECRET_KEY` | Fernet key that encrypts each business's Square tokens at rest. **Separate from `COMMS_SECRET_KEY`** |

Generate `PAYMENTS_SECRET_KEY` with:

```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

If it is unset, Settings -> Payments refuses to start a Square connection
(nothing is ever stored in plaintext). Changing it later makes every stored
Square token unreadable: those businesses show **Needs attention** and must
reconnect.

**Never commit any of these values.** CI runs gitleaks over the full history.

The former placeholder `SQUARE_ACCESS_TOKEN` is gone. A single access token
belongs to one merchant, which can't work for a multi-tenant platform. Remove
it from any deployment that still sets it; nothing reads it.

Platform Admin's platform readiness lists any missing Square setting under
**payments**, and a business's own readiness shows its Square
connection, authorisation state and location.

## 5. Sandbox vs production

- `SQUARE_ENVIRONMENT=sandbox` uses `connect.squareupsandbox.com` and the
  frontend loads `https://sandbox.web.squarecdn.com/v1/square.js`.
  `production` uses `connect.squareup.com` and `https://web.squarecdn.com/v1/square.js`.
  The backend returns `environment` to the browser, so there is no separate
  frontend setting.
- Sandbox OAuth: in the Developer Console, open the **Sandbox test account**'s
  Seller Dashboard in the same browser first, then click **Connect Square** in
  CoMaz.
- Square's [sandbox test values](https://developer.squareup.com/docs/devtools/sandbox/payments)
  document card numbers for success, decline and SCA challenge.

## 6. Connecting a business (owner)

1. Settings -> Payments -> choose **Square** -> **Connect Square**.
2. Approve on Square's page.
3. Back in CoMaz, if the seller has several card-capable GBP locations, pick
   the one that takes deposits.
4. The card shows **Ready to take online payments**.

Switching between Stripe and Square is refused while a customer is
mid-checkout. Earlier payments always refund through the provider and
account that took them.

## 7. Token lifecycle

Access tokens last about 30 days. They are refreshed lazily when used, under a
row lock, and retried once on a 401. No scheduler is required. To refresh
businesses that haven't taken a deposit recently (optional, e.g. from an
external weekly cron):

```
flask --app app:create_app refresh-square-tokens
```

If a seller revokes CoMaz in their Square Dashboard, the
`oauth.authorization.revoked` webhook marks them **REVOKED**: deposits stop
(same effect as an unready Stripe account) and the owner sees **Reconnect**.

## Not in v1

- **Apple Pay / Google Pay through Square.** These need domain verification in
  the Square Developer Console plus the SDK's wallet buttons. v1 is card
  payments only.
- Application fees on Square payments (CoMaz is fee-free, as with Stripe).
