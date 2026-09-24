# Onboarding a real business - runbook

A concise, repeatable checklist for onboarding any real business (not
garage-specific - CoMaz's onboarding schemas already capture a generic
service business: name, contact details, services with durations/prices,
opening hours, booking/deposit rules).

Check go-live status at any point via `GET
/api/platform-admin/tenants/<id>/readiness` (Platform Admin) - it reads
authoritative state (services/hours rows, Stripe's own `charges_enabled` /
`payouts_enabled`, Communications Setup's own status), never fabricated from
whether an ID merely exists.

## 1. Information to collect from the owner
**OWNER MUST DO** (before or during the call)
- Business name, contact email, phone, address
- Services offered: name, duration, price, deposit (if any)
- Opening hours
- Whether they want phone/WhatsApp bookings in addition to the public link
- If they want a dedicated CoMaz phone number, or already have one to bring

## 2-6. Create business, owner, services, hours, booking rules
**COMAZ ADMIN DOES** - Platform Admin -> Onboard business (the existing
wizard: Business -> Owner -> Plan -> Services -> Opening Hours -> Booking
Settings -> Review -> Create). See `docs/BUSINESS_ONBOARDING.md` for the
full field reference and a worked example.
**AUTOMATED BY COMAZ**: owner invite email, seeded booking settings,
onboarding-progress tracking.

## 7. Stripe Connect onboarding
**OWNER MUST DO**: complete Stripe's own hosted onboarding form (bank
details, business verification) after Platform Admin starts it.
**COMAZ ADMIN DOES**: start onboarding from the garage's Payments settings
(`POST /api/payments/stripe/connect`), send the owner the link.
**AUTOMATED BY COMAZ**: account status refresh (`GET
/api/payments/stripe/status`), Apple Pay domain registration once
chargeable. Deposits are only ever offered once `stripe_charges_enabled`
is genuinely `true` - never from account-id presence alone
(`app/payments/settings.py::stripe_connect_ready`).

## 8. Communications provisioning (Twilio subaccount)
**COMAZ ADMIN DOES** - Platform Admin -> business -> Communications Setup ->
"Create Twilio subaccount" (or "Attach existing subaccount" if one was made
by hand beforehand). Isolates this business's credentials and Twilio usage
from every other tenant (`app/communications/provisioning/subaccounts.py`).
Encrypted at rest via `COMMS_SECRET_KEY` (already configured). Safe to
retry - clicking it again on a business that already has a subaccount just
returns the existing one, never creates a second.

## 9. Acquire a dedicated number
**COMAZ ADMIN DOES, WITH EXPLICIT CONFIRMATION** - Platform Admin ->
business -> Communications -> Voice. Three ways to get a number, all
idempotent (a business that already has a number is never bought/moved a
second time):
- **Buy new**: search available UK numbers, then *Buy* the chosen one
  (`POST .../communications/voice/number`). A real Twilio purchase with a
  real recurring cost.
- **Use existing number**: discovers numbers CoMaz's Twilio account already
  owns (in this business's own subaccount, in the shared parent account, or
  flags a number as external if it's genuinely not CoMaz's) and either
  adopts or transfers it in - no purchase.
- **Return number to CoMaz**: moves a business's number back to the shared
  parent account (never released/deleted) so it becomes discoverable again
  for a different business.

Whichever path is used, the number's voice webhook is pointed at CoMaz
automatically in the same call - never live-but-unconfigured.
Do not reuse another business's number. Existing reference numbers
(`+447402220792`, `+443330382135`) and the reference SIP trunk
(`TKf170893068fae837c390ccbd06b06fb9`) belong to other tenants/testing and
must never be reassigned, modified, or deleted through any of these flows.

## 10. OpenAI voice activation
**COMAZ ADMIN DOES, WITH EXPLICIT CONFIRMATION** - Platform Admin ->
business -> Communications -> Voice -> **Enable OpenAI Voice**. Deliberately
a separate, explicit action from acquiring the number itself: a business
can have a fully working Twilio number with OpenAI Voice never turned on.

**Fully automated, no Twilio Console step required** (this replaced an
earlier manual step): this creates (or reuses) the business's own Elastic
SIP Trunk in its own Twilio subaccount, points it at OpenAI's shared
Realtime SIP destination, and associates the number - all idempotent, safe
to retry after a failure. The existing platform reference trunk
(`TKf170893068fae837c390ccbd06b06fb9`) is never read or touched by this
action; each business gets its own trunk (Twilio's Trunking API has no
parent-acts-as-subaccount path, unlike number purchase/management).

Skip this step entirely if the business should use CoMaz's own
ConversationRelay voice assistant instead of the OpenAI Realtime one - both
work, and this action is reversible only by leaving it disabled (there is
no "disable" button; a business simply never enables it, or a fresh
Platform Admin decision can be made before this step).

Also set an escalation/fallback number in Platform Admin
(`PUT .../communications/voice/routing`) if the owner wants human handoff.

**Usage/cost visibility**: once calls flow through OpenAI Voice, per-call
usage (token counts, duration, tool-call/booking/escalation outcomes) and
an *estimated* OpenAI + Twilio infrastructure cost are captured
automatically and readable via `GET
/api/platform-admin/tenants/<id>/voice-telemetry` and
`GET /api/platform-admin/stats/voice-telemetry` (never a provider-billed
actual - see `app/ai_voice/pricing.py` for exactly why). **No Platform
Admin screen renders this yet** - it is API-only today.

## 11. Public booking verification
**COMAZ ADMIN DOES**: open the business's public booking link, place a real
booking request end to end.

## 12. Payment test
**COMAZ ADMIN OR OWNER**: take one real (or Stripe test-mode, if still
verifying) deposit through the public flow; confirm it appears in the
business's own Stripe dashboard, not the platform account's.

## 13. Phone call test
**COMAZ ADMIN DOES**: call the new number, confirm the AI assistant
identifies the correct business and can quote services/availability that
belong to it, not another tenant's.

## 14. Approval/booking lifecycle test
**COMAZ ADMIN OR OWNER**: approve a booking request through to a confirmed
appointment; confirm status transitions correctly.

## 15. Reminder readiness
**COMAZ ADMIN DOES**: confirm the business appears in the reminder run
(`flask send-due-reminders`, Render Cron, already operational platform-wide -
no per-business setup needed here).

## 16. Final GO LIVE check
**COMAZ ADMIN DOES**: Platform Admin -> business -> **Readiness** tab (or
`GET /api/platform-admin/tenants/<id>/readiness` directly) - "Ready for
public booking" and "Ready to take deposits" should both read Ready before
telling the owner they are live. Communications is explicitly optional for
go-live - a business can go live on public booking alone and add
phone/WhatsApp/OpenAI Voice later.
