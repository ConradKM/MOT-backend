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

## 9. Purchase/assign a dedicated number
**COMAZ ADMIN DOES, WITH EXPLICIT CONFIRMATION** - search available UK
numbers ("Search numbers"), then "Buy number" for the chosen one
(`POST .../communications/voice/number`). This is a real Twilio purchase
with a real recurring cost - nothing in this system buys a number
automatically, and it is now provably safe to retry: a business that
already has a number returns that number unchanged rather than buying a
second one (fixed in this pass - previously a double-click or retried
request could have purchased and silently orphaned an extra number).
The purchased number's voice webhook is pointed at CoMaz automatically in
the same call - never live-but-unconfigured.
Do not reuse another business's number. Existing reference numbers
(`+447402220792`, `+443330382135`) belong to other tenants/testing and must
never be reassigned.

## 10. OpenAI voice activation
**AUTOMATED BY COMAZ, once the number is attached to the SIP trunk (below)**:
the OpenAI webhook infrastructure is shared safely across every tenant -
tenant resolution is per-number
(`app/ai_voice/tenant.py::resolve_business_for_sip_call`, reading the SIP
`Diversion`/`To` header, matched against this business's own
`voice_phone_number`). No per-business OpenAI project or API key is needed.
An unrecognised number fails closed - it is never routed to a default
tenant.

**COMAZ ADMIN MUST DO MANUALLY (not automated, no Platform Admin action for
this today)**: the number bought in step 9 defaults to CoMaz's own voice
webhook (Twilio ConversationRelay), *not* OpenAI. To route this specific
number through OpenAI instead, associate it with the existing Elastic SIP
Trunk in the **Twilio Console** (Super Network -> Elastic SIP Trunking ->
the CoMaz trunk -> Numbers -> add this number). A number belongs to exactly
one trunk/voice-URL configuration at a time, so this is a one-time,
one-number action - see `docs/OPENAI_VOICE_SETUP.md`. Skip this step
entirely if TOD should use CoMaz's existing ConversationRelay voice
assistant instead of the OpenAI Realtime one; both work, and moving between
them later is the same one-line Twilio Console change.

Also set an escalation/fallback number in Platform Admin
(`PUT .../communications/voice/routing`) if the owner wants human handoff.

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
**COMAZ ADMIN DOES**: `GET /api/platform-admin/tenants/<id>/readiness` -
`business_ready` and `ready_to_take_deposits` should both be `true` before
telling the owner they are live. `communications_ready` is optional - a
business can go live on public booking alone and add phone/WhatsApp later.
