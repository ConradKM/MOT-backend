# Business Terms & Conditions

A business writes its own Terms & Conditions (plain text) in Settings. While
they exist, every customer-facing way of booking requires the customer to
accept them first. Code lives in `app/terms/`.

## Data

- `garages.terms_and_conditions` / `garages.terms_version` — the current text
  and its version. `NULL` text (never empty/whitespace) means "no terms", and
  then nothing about booking changes.
- `garage_terms_versions` — every wording ever current, immutable (an ORM
  `before_update` hook refuses edits). A new row is only written when the
  trimmed text actually changes.
- `booking_requests.terms_accepted_at` / `terms_version`, and the same on
  `queue_entries` — when and which wording was accepted, always stamped
  server-side. Where it was accepted is the request's existing `source`
  (`WEB` checkbox vs the `CONVERSATION` texted link). Approval leaves these on
  the request.

## API

| Route | Who | Notes |
| --- | --- | --- |
| `GET/PUT /api/garage/terms` | read: any staff; write: OWNER | Same precedent as `PATCH /api/garage`. 50,000-char cap (422). |
| `GET /api/public/<slug>`, `/api/public/garages/<id>` | public | Carry `terms_and_conditions` + `terms_version` (both null without terms). |
| `GET /api/public/garages/<id>/terms[/<version>]` | public | Standalone/confirmation pages. 404 when none/unknown. Versioned responses are `immutable`-cacheable. |
| `POST /api/public/<slug>/terms-acceptance/lookup` | token | Booking summary, current terms, `state`. |
| `POST /api/public/<slug>/terms-acceptance/accept` | token | Single use. |

## Enforcement

Web paths (`booking-requests`, `booking-requests/deposit-intent`,
`queue/join`) send `terms_accepted` + `terms_version`. Missing/false is a 422 on
`terms_accepted`; a version that isn't current is a 409 with
`errors.reason = "terms_version_mismatch"` so the page re-prompts. The check
runs before every idempotent-retry lookup, so no retry path skips it, and a
retry that returns an existing request never re-stamps it.

## WhatsApp / SMS / voice: `AWAITING_TERMS`

`create_booking_request` / `create_deposit_booking_request` create the request
as `AWAITING_TERMS` with a single-purpose acceptance token (hash stored only)
and send the customer `BOOKING_BASE_URL/accept-terms/<garage id>#<token>` (the
token rides in the fragment so it never reaches access logs). The hold:

- reserves capacity exactly like `PENDING`/`AWAITING_PAYMENT`;
- expires after `TERMS_ACCEPTANCE_HOLD_MINUTES` (default **60**) via
  `expire_stale_payment_holds`;
- can't be approved by staff (409, `reason: awaiting_terms`);
- emits no `BOOKING_REQUEST_CREATED`, so no "received"/"confirmed" message.

Accepting moves it to `PENDING` (then the normal "received" messages and
auto-accept run), or for a deposit service to `AWAITING_PAYMENT` with a fresh
payment hold and a deposit recovery token — the page continues straight into
the existing checkout. If the wording changed since the booking was made, the
customer simply sees and accepts the current version.

## Confirmations

The "request received" and "appointment confirmed" email and SMS append
`Terms you accepted: <BOOKING_BASE_URL>/terms/<garage id>/v/<version>` when the
booking recorded acceptance.
