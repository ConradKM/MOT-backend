# Onboarding: known readiness/visibility limitations and recommended fixes

Found during a production-readiness audit. Neither of these is fixed here -
both need either a product decision or a migration, and the overnight pass
this was found in deliberately avoided both. Recorded so tomorrow's
decision is quick to make rather than re-discovered from scratch.

## 1. Owner-invite "sent" means "token issued", not "email delivered"

`owner_invite_status()` (`app/platform_admin/provisioning.py`) derives its
`sent`/`expired`/`accepted` state entirely from whether a live
`PasswordResetToken` row exists - and that row is created and committed
**before** `_send_owner_invite()` is even called. If the email provider
fails (the send is wrapped in a bare `try/except Exception` that returns
`False`, never raises), the token row still exists, so the Onboarding tab's
invite badge reads **"sent"** even though the owner's inbox has nothing.

The one moment this is visible is transient: `provision_tenant()`'s
response includes `invite_sent: false`, and comaz-admin does show that on
the onboarding success screen and `TenantOnboarding.tsx` at creation time.
But once that response is gone (page navigated away, tab closed), there is
no durable record distinguishing "token issued, email confirmed sent" from
"token issued, email silently failed" - both read as the identical "sent"
state on every later visit to the Onboarding tab.

**Recommended fix** (needs a migration - one nullable column, e.g.
`PasswordResetToken.delivery_failed: bool`, set `True` in
`_send_owner_invite`'s except branch): let `owner_invite_status()` return a
distinct `state: "delivery_failed"` when the latest token's send is known
to have failed, so the badge and its "next admin action" copy can say
"resend - the last email didn't send" instead of a plain "sent" that turns
out to be wrong. Low risk, no behavior change to the invite flow itself,
additive column only.

**Workaround until fixed**: if a business's owner reports never receiving
their invite, don't trust the "sent" badge - just resend unconditionally.
Resending is always safe (see `docs/ONBOARDING_RETRY_IDEMPOTENCY_MATRIX.md`).

## 2. Opening-hours readiness is a "differs from default" heuristic, not "exists"

`_hours_customised()` (`app/platform_admin/onboarding.py`) - and by
extension `business_readiness()`'s `opening_hours_configured` check -
reports a business's hours as "not configured" until at least one day
differs from the seeded Mon-Fri 09:00-17:00 default, even if the business's
real hours genuinely are exactly that. A standard 9-5 trade business (a
plausible real case) can never clear this check by entering its own
correct hours, only by entering *different* ones.

This is a deliberate design choice, not a bug: the alternative (a plain
"row exists" check) would be fooled by hours nobody ever actually looked
at, which is worse - a genuinely un-set business would read as configured.
The current heuristic is a legitimate but imperfect proxy for "an admin
actually looked at this," and it fails safe (false negative: flags a
correctly-configured business as needing attention) rather than fails
dangerous (false positive: never happens from this check alone).

**Recommended fix** (needs a migration - one nullable
`opening_hours_reviewed_at` timestamp on `Garage` or a settings table, plus
a "confirm hours as reviewed" button in the UI): let an operator explicitly
mark hours as reviewed regardless of whether they match the seed, and check
that timestamp instead of (or alongside) the differs-from-default
heuristic. This requires a product decision on exactly what "reviewed"
should mean going forward (does editing hours again clear the flag?
does a public booking against those hours count as implicit review?) -
deliberately left for a human to decide, not invented here.

**Workaround until fixed**: if a business's real hours are the exact
default and this check won't clear, that's a known cosmetic false
negative - verify go-live readiness from the other checks instead of
trying to force this one to pass.
