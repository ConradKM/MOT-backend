# Tints on Demand - onboarding checklist

A fillable, ordered runbook for the real onboarding. Every step names who
does it. Nothing here performs an action by itself - this is the sequence
to follow in Platform Admin, MOT-frontend, the Twilio Console, the Stripe
dashboard, and by phone.

Legend: **[COMAZ]** automated by the platform · **[OPERATOR]** manual
Platform Admin action · **[OWNER]** the business owner does this ·
**[EXTERNAL]** an external provider's own hosted flow.

---

## Pre-flight

- [ ] **[OPERATOR]** Confirm `main` is green on both MOT-backend and
  comaz-admin (`gh run list --branch main --limit 1`).
- [ ] **[OPERATOR]** Confirm exactly one Alembic head
  (`flask db heads`).
- [ ] **[OPERATOR]** Have the following ready from TOD before starting:
  - Trading name
  - Contact email, phone, address, website (if any)
  - Services offered: name, duration, price, deposit amount (if any) per
    service
  - Opening hours
  - Whether they want phone bookings, WhatsApp, or public-link-only to
    start
  - Whether they want a brand-new CoMaz number or already have a number to
    bring across

## 1. Business
- [ ] **[OPERATOR]** Platform Admin -> Businesses -> Onboard business.
- [ ] **[OPERATOR]** Enter trading name, contact details, address.
- [ ] **[COMAZ]** Slug generated automatically, immutable.

## 2. Owner
- [ ] **[OPERATOR]** Enter the owner's name and email in the same wizard.
- [ ] **[COMAZ]** OWNER role created, set-password invite email sent after
  the business is created (never blocks tenant creation if the mail
  provider is briefly down).
- [ ] **[OWNER]** Owner completes the set-password link.
- [ ] **[OPERATOR]** If the owner reports no email arrived: Platform Admin
  -> the business -> Onboarding tab -> check the invite badge -> "Send a
  new set-password link" to resend. **Do not create a second owner/tenant
  as a workaround.**

## 3. Plan
- [ ] **[OPERATOR]** Select plan in the wizard (or Configuration tab
  afterwards). Feature entitlements follow the plan's defaults
  automatically; only override in the Features tab if TOD needs something
  non-standard.

## 4. Services
- [ ] **[OPERATOR]** Add each service: name, duration, price, deposit
  amount if TOD wants deposits on that service.
- [ ] Confirm at least one service is **ACTIVE** (not archived) - the
  readiness checklist requires this.

## 5. Opening hours
- [ ] **[OPERATOR]** Enter TOD's real hours.
- [ ] **Known quirk**: if TOD's hours genuinely are the seeded default
  (Mon-Fri 09:00-17:00), the "opening hours configured" readiness check
  will still read as **not configured**, by design (it checks "differs
  from the seed," not "exists") - see
  `app/platform_admin/onboarding.py`'s `opening_hours` step definition. If
  this happens and the hours are correct, that's a known
  cosmetic false-negative, not a blocker - use the Readiness tab's other
  checks to confirm real go-live status, and don't waste time re-entering
  identical hours to try to clear it.

## 6. Booking settings
- [ ] **[OPERATOR]** Review lead time, cancellation window, and any other
  booking rules in the Booking Settings tab. No readiness signal exists
  for this today - review manually.
- [ ] **[OPERATOR]** Confirm an active staff employee is available to be
  assigned to appointments. Booking requests are currently approved
  manually and approval requires an explicit active employee assignment;
  tenant auto-accept is not enabled.

## 6a. Customer records
- [ ] **[OWNER/OPERATOR]** Show TOD staff the Customers area before go-live:
  vehicle registrations are stored against each vehicle (not duplicated on
  the customer), and customer search accepts a registration with or without
  its display spaces.
- [ ] **[OWNER/OPERATOR]** Confirm staff understand that Customer Notes are
  internal, persistent plain-text notes for authorised business staff only.
  They are not included in public booking or customer-portal responses;
  do not use them for information a customer should receive.

## 7. Payments (Stripe Connect) - only if TOD wants deposits
- [ ] **[OPERATOR]** Start Stripe Connect onboarding from the business's
  Payments settings, send the owner the link.
- [ ] **[OWNER] [EXTERNAL]** Owner completes Stripe's own hosted onboarding
  (bank details, business verification, ID). This can take minutes to days
  depending on Stripe's own verification - **not something CoMaz controls
  or can accelerate.**
- [ ] **[COMAZ]** Account status (`charges_enabled`, `payouts_enabled`)
  refreshed automatically via webhook; deposits are only ever offered once
  both are genuinely `true`.
- [ ] **[OPERATOR]** Check the Readiness tab's Payments section before
  telling TOD deposits are live.

## 8. Communications - Twilio subaccount
- [ ] **[OPERATOR]** Platform Admin -> the business -> Communications ->
  Create Twilio subaccount.

## 9. Voice number
- [ ] **[OPERATOR]** Choose one:
  - **Buy new** - search, then buy. **Real recurring cost, requires your
    explicit confirmation.**
  - **Use existing number** - if TOD is bringing a number CoMaz's Twilio
    account already has access to.
  - Confirm the number is genuinely TOD's own, never a reused reference
    number (`+447402220792`, `+443330382135` are reserved/other-tenant and
    must never be selected).
- [ ] **[COMAZ]** Webhook pointed at CoMaz automatically in the same call.
- [ ] **[OPERATOR]** Set escalation/fallback number if TOD wants human
  handoff.
- [ ] **[OPERATOR]** Test voice - rings *your own* mobile, never the
  customer's.
- [ ] **[OPERATOR]** Mark voice online - a deliberate decision, not
  inferred.

## 10. OpenAI Voice (optional)
- [ ] **[OPERATOR]** Only if TOD wants the AI phone assistant: Platform
  Admin -> Communications -> Voice -> **Enable OpenAI Voice**. Fully
  automated - no Twilio Console step. **This creates a real per-business
  SIP trunk and requires your explicit confirmation.**
- [ ] Skip entirely if TOD should use the existing ConversationRelay
  assistant instead, or no AI voice at all.

## 11. WhatsApp (optional)
- [ ] **[OPERATOR]** Only if TOD wants WhatsApp: follow
  `docs/COMMUNICATIONS_ONBOARDING.md`'s WhatsApp section. Requires the
  owner's own Facebook/Meta sign-in for Embedded Signup - **cannot be done
  without the owner present or on a call.**

## 12. Reminders
- [ ] **[COMAZ]** No per-business setup - the reminder cron
  (`flask send-due-reminders`, Render Cron) already covers every active
  tenant. Nothing to configure here.

## 13. Readiness check
- [ ] **[OPERATOR]** Platform Admin -> the business -> **Readiness** tab.
- [ ] "Ready for public booking" must read **Ready**.
- [ ] "Ready to take deposits" must read **Ready** if TOD wants deposits
  (ignore if TOD isn't taking deposits - that rollup will correctly stay
  "Not yet" with no Stripe connected, which is expected, not broken).
- [ ] Communications is optional - TOD can go live on public booking alone.

## 14. Public booking smoke test
- [ ] **[OPERATOR]** Open the business's public booking link (shown on the
  Onboarding tab). Place one real booking request end to end as a test
  customer.

## 15. Owner smoke test
- [ ] **[OWNER]** Owner logs in, confirms they see their own business
  (branding, services, hours) and the test booking request from step 14.

## 16. Payment smoke test (if deposits enabled)
- [ ] **[OPERATOR/OWNER]** Take one real (or Stripe test-mode, if still
  verifying) deposit through the public flow. Confirm it appears in TOD's
  own Stripe dashboard, not the platform account's.

## 17. Phone smoke test (if voice enabled)
- [ ] **[OPERATOR]** Call the new number. Confirm the assistant (AI or
  static) identifies TOD correctly and quotes TOD's own services/hours,
  not another tenant's.

## 18. Approval/booking lifecycle smoke test
- [ ] **[OPERATOR/OWNER]** Approve the test booking request through to a
  confirmed appointment. Confirm status transitions correctly.

## 19. Go live
- [ ] **[OPERATOR]** Final Readiness tab check.
- [ ] **[OPERATOR]** Tell TOD they're live.

---

## If something goes wrong mid-onboarding

- **A step appears to hang or the page errors**: refresh and check the
  business's current state in Platform Admin before retrying - every
  provider-mutating action in steps 8-11 is safe to retry (see
  `docs/ONBOARDING_RETRY_IDEMPOTENCY_MATRIX.md`). The one exception is
  tenant creation itself (step 1) - don't resubmit with different details
  as a workaround; check `/tenants` for a partially-created business first.
- **A provider error appears** (Twilio/Stripe/OpenAI): the error code and
  message are shown verbatim in the Communications tab's "Recent provider
  errors" - look it up there before escalating.
