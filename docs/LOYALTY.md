# Loyalty rewards and discount codes

Code: `app/loyalty/` (service, issuing, routes, CLI), models in `app/models/loyalty/`.
Issue: #288. Frontend: Settings → Discounts, the booking review step, the staff
appointment form.

## The rule

Per business (`LoyaltySettings`): on/off, every **X** completed visits, a
percentage or fixed-GBP discount, and how many days the code is valid. A *visit*
is a `COMPLETED` appointment for that customer at that business. Every Xth visit
issues one personal, single-use `LOYAL-XXXXXXXX` code and emails it; the count
keeps going toward the next one.

- **History counts, but never triggers.** Completions from before the programme
  (and before `appointments.completed_at` existed) and bulk-imported history
  count toward a customer's total, but only a new completion in the app can
  issue a code. Switching the programme on never mass-issues or mass-emails.
- **Exactly once.** A completion's visit number is its position among the
  customer's completed appointments (customer row locked while it is worked
  out); `(customer_id, issued_for_visit_count)` is unique.
- **Never blocks a completion.** Issuing is an explicit, guarded call after the
  completion commits (`issue_reward_safely`), not an `emit_event` handler (which
  swallows failures). A failure is logged as `LOYALTY_ISSUE_FAILED`.

## Catching up: `flask loyalty-reconcile`

```bash
flask --app app:create_app loyalty-reconcile [--days 30]
```

Issues (and emails) any reward a completion in the last `--days` earned but did
not get. Only completions made after the programme was last switched on
(`LoyaltySettings.enabled_at`) are considered. Safe to run any number of times;
run it from cron alongside `send-due-reminders` if you want belt and braces.

## Codes and redemption

One model (`DiscountCode`) for both loyalty (`LOYALTY`, bound to one customer,
`max_uses=1`) and promo codes (`MANUAL`, optional dates and total/per-customer
limits). Codes compare case-, space- and dash-insensitively, and only ever
resolve within the business in the URL / the caller's JWT.

`app/loyalty/service.py::evaluate_code` is the single authority, used by:

| Entry point | Notes |
| --- | --- |
| `POST /api/public/<slug>/discount-codes/validate` | Advisory. Rate-limited per IP (`PUBLIC_DISCOUNT_VALIDATE_RATELIMIT`) and per business (`PUBLIC_DISCOUNT_VALIDATE_GARAGE_RATELIMIT`). One generic message for unknown/expired/used/disabled/wrong-customer. |
| Public booking submit and deposit intent | Re-run `evaluate_code` and own the price; clients send only `discount_code`. The code is part of an attempt's idempotency identity. |
| `POST /api/loyalty/evaluate`, staff appointment create/update | Specific reasons for staff. |

The discounted total becomes `requested_price` / `price_at_booking`; the deposit
is taken on it. The discount is frozen onto the booking (`discount_*`,
`price_before_discount`). A booking a code makes free skips the deposit and is
submitted as an ordinary booking (deposit-intent answers 422
`deposit_not_required`). Editing a discounted appointment re-applies the frozen
discount to the new total rather than undoing or compounding it.

### Redemption lifecycle (`DiscountRedemption`)

| Status | When |
| --- | --- |
| `RESERVED` | Deposit booking waiting for payment |
| `CONSUMED` | Plain booking submitted; deposit succeeded; staff-created appointment |
| `RELEASED` | Request expired/rejected/cancelled; appointment cancelled/no-show; hold abandoned |

Reserved and consumed both count against limits. Limits are checked under a row
lock on the code; a partial unique index backs single-use codes. Before checking
limits, redemptions whose booking has since fallen through are released, so a
missed transition can never use up a customer's code for good.

`POST /api/public/<slug>/booking-requests/deposit-attempt/abandon` (recovery
token in the body) lets the browser give up its own unpaid hold when the code
changes after the payment step opened. It never abandons a paid or
in-confirmation payment.

## Email and opt-out

`send_loyalty_reward_email` (template `loyalty_reward`) is promotional: it carries
a signed, non-expiring unsubscribe link (`/unsubscribe?token=…` on the frontend,
`POST /api/public/marketing/unsubscribe[/preview]`) and is never sent to a
customer with `marketing_email_opt_out`. The code is still created; its
`email_status` (`SENT`/`FAILED`/`NO_EMAIL`/`OPTED_OUT`) tells staff when to pass
it on by hand. The owner's "Resend email" deliberately bypasses the already-sent
guard. Transactional email is unaffected by the opt-out.

Whether the business may send this email under UK PECR (soft opt-in for existing
customers, or explicit consent) is the business's decision; this code provides
the opt-out mechanics either way.
