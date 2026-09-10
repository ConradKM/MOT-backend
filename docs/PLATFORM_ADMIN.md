# Platform Admin

The internal CoMaz/MazTrad operator console: one place to see every business on
the platform, support them, and watch the plumbing. It is a **separate product
surface** from the garage app and the customer portal - its own frontend
(`comaz-admin`, deployed at `admin.comaz.co.uk`), its own account table, and its
own API namespace (`/api/platform-admin/*`).

Backend code lives in [`app/platform_admin/`](../app/platform_admin/); its
package docstring is the short version of this document.

---

## The permission boundary

This is the part to get right, so it is stated explicitly.

| | Garage employee | Customer | Platform admin |
|---|---|---|---|
| Table | `employees` | `customers` | `platform_admins` |
| JWT `account_type` | *(absent)* | `"customer"` | `"platform_admin"` |
| Can reach `/api/platform-admin/*` | **No** (403) | **No** (403) | Yes |
| Scope | its own garage | its own records | every tenant |

Four properties hold, and each has a test in
[`tests/api/test_platform_admin_auth.py`](../tests/api/test_platform_admin_auth.py):

1. **The claim is never sufficient.** Every guarded request re-loads the
   `PlatformAdmin` row and re-checks that it exists and is active. Forging
   `account_type: "platform_admin"` onto an employee's id fails, because no such
   row exists.
2. **Deactivation is immediate.** A live token dies on its next request, via the
   JWT blocklist loader in [`app/__init__.py`](../app/__init__.py) - not at
   expiry.
3. **Identity is resolved per request, never cached.** Flask reuses one app
   context (and one `flask.g`) across requests whenever one is already pushed,
   so memoising the resolved admin there would leak identity between requests.
4. **Roles are enforced by decorator, not convention.** `SUPERADMIN` changes
   tenants; `SUPPORT` reads and impersonates only.

Tenant isolation is unchanged. Platform Admin is a *new actor above* the
tenants: it reads across them through its own explicitly cross-tenant queries in
`app/platform_admin/`, and every tenant-facing route in the rest of the app
still derives its garage from the caller's own JWT, exactly as before.

---

## Creating an administrator

There is no HTTP registration path and no invite flow. Creating an account that
can read every tenant requires shell access to the deployment - the same bar as
reading the database directly.

```bash
flask create-platform-admin --email you@comaz.co.uk --role SUPERADMIN
```

Omit `--password` to have a strong one generated and printed **once**. Hand it
over out of band. `flask list-platform-admins` shows who exists (never a hash).

| Role | Read everything | Impersonate | Edit / suspend / feature flags / resend |
|---|---|---|---|
| `SUPERADMIN` | Yes | Yes | Yes |
| `SUPPORT` | Yes | Yes | No |

---

## Support impersonation

"Log in as this business", without ever touching the owner's password. No
password is read, compared, reset or displayed; impersonation mints a *new*
short-lived token for an existing, active employee account.

```
1. START     POST /api/platform-admin/tenants/<id>/impersonate   (audited)
             -> a single-use handoff code. NOT a garage token: the console
                itself can never act as the tenant.

2. HANDOFF   console opens the garage app at /impersonate#code=...
             A URL fragment is never sent to a server, and the code is
             single-use, SHA-256 at rest, and valid for ~90 seconds.

3. EXCHANGE  POST /api/auth/impersonation/exchange   (unauthenticated:
             the code is the credential)
             -> burns the code, returns an employee access token with
                `impersonation_id` + `impersonated_by` claims, a hard
                15-minute expiry, and NO refresh token.
```

Consequently:

- **Short-lived.** It expires with the token and cannot be refreshed. A longer
  session means starting - and re-auditing - a new one.
- **Revocable.** `POST /api/platform-admin/impersonation-sessions/<id>/revoke`
  kills the token on its *next request*, not at expiry.
- **Clearly displayed.** The claims are readable by the garage frontend, which
  shows a persistent banner naming the administrator for as long as the session
  lasts.
- **Fully audited.** Both the start (with the reason the admin typed, which is
  required) and the revocation write a `PlatformAuditLog` row.
- **Non-escalating.** The token is an *employee* token with no
  `account_type` claim, so it cannot reach a single `/api/platform-admin` route,
  and it is still scoped to that one tenant.

Impersonation deliberately still works on a **suspended** tenant: getting in to
fix whatever caused the suspension is the point. Ordinary staff logins stay
blocked.

---

## Tenant lifecycle

`Garage.status` is platform-owned - no garage-facing route reads or writes it,
and it is absent from `GarageSchema`.

- `ACTIVE` - live tenant.
- `TRIAL` - same access, with `trial_ends_at` attached. Nothing enforces the
  date yet; the column is what a future billing job would read.
- `SUSPENDED` - staff lose access immediately (login is refused, and live tokens
  die on their next request). **Nothing is deleted**, other tenants are
  unaffected, and support impersonation still works.

"Dormant" is deliberately *not* a status. It is derived at read time from the
newest activity across a tenant's appointments, booking requests, communications
and customers, so a tenant that goes quiet and comes back is simply active again
- there is no flag to remember to clear.

Onboarding progress is derived the same way: each step is a question answered
from the tenant's own data, so it can never drift, and a business that did the
work before Platform Admin existed already shows as complete. Two steps (a team
beyond the owner, live communications) are reported but excluded from the
percentage - plenty of healthy single-operator garages will never do them, and a
score that cannot reach 100% is worse than useless for spotting who is stuck.

---

## Statistics

Aggregated from the tables the product already writes. No rollup table, no
counter to keep in sync: a figure shown to the platform team can never disagree
with what the tenant sees.

Two rules, both tested:

- **Aggregate in SQL.** Counts are `GROUP BY` queries, so the tenant list stays
  a fixed number of queries however many businesses are onboarded.
- **A rate over an empty denominator is `null`, never `0`.** "No booking
  requests yet" and "every request was rejected" must not render as the same
  number.

Rates are also scoped to what they can honestly describe: approval/rejection
rates cover only requests staff actually decided (expired ones are excluded -
nobody saw them), and the no-show rate covers only appointments whose outcome is
known, so an upcoming booking never drags it down.

**Revenue is absent rather than faked.** The hooks a subscription metric needs -
`Garage.plan`, `Garage.trial_ends_at`, and audited `tenant.plan_change` events -
are in place, and the overview already reports tenants per plan. Turning that
into MRR needs prices, which live outside this system today.

---

## Operations

- **Email delivery log** - `CommunicationLog` rows with `channel="EMAIL"`, which
  `app/email/service.py` already writes for every attempt.
- **Resending a failure** goes through the same `app.email.send_email` the
  product uses, and writes a **new** row linked to the failure by `retry_of_id`.
  The original is never mutated: a failure that happened stays in the history,
  with its retry attached. The recipient is read from the stored row, never from
  the request, so the endpoint cannot be pointed at another address.
- **Communication failures** across every channel and tenant, grouped by
  channel, tenant and provider error code. `SKIPPED_NOT_CONFIGURED` is counted
  separately - it means "this tenant has not turned the channel on", not
  "delivery broke".
- **Job health** is inferred from evidence, not from a scheduler we don't
  control. Nothing records "the reminder job ran at 10:00"; what *is* recorded is
  the reminders it produced, the requests the expiry sweep should have retired,
  and the sends that failed. Each check states what it measured, and reports
  `unknown` where there is no evidence either way - more useful than a green
  light nothing verified.

---

## Feature flags

Resolved in two layers: the tenant's **plan** supplies a default for every known
flag, and an explicit **override** row wins for that one flag on that one
tenant. No override row means "follow the plan", so changing a plan's defaults
later moves every tenant that never had a deliberate exception.

**Scope, stated plainly:** `app/platform_admin/features.py` is the source of
truth for what a tenant's feature set *is*, and Platform Admin manages it end to
end (read, override, clear, audited). **No existing product behaviour is gated
on it yet.** Adopting `feature_enabled()` at each feature's entry point is a
separate, deliberate change per feature, so that turning a flag off can be
reviewed against what that feature already does for live tenants.

---

## Audit trail

Every sensitive action writes an append-only `PlatformAuditLog` row: tenant
edits (with before/after per field), suspension and reactivation (with the
reason), feature-flag changes, email resends, admin sign-ins **and failed
sign-in attempts**, and both ends of an impersonation session.

- The audit row commits **in the same transaction as the action**, so an action
  can never succeed unaudited, and a rejected action leaves no trail claiming it
  happened.
- Admin email and tenant name are **snapshotted** alongside the foreign keys, so
  deleting a tenant or an admin never erases the history of what was done.
- Secrets never reach the table: `record_audit` drops any key that looks like a
  password, token, secret or hash.
- Nothing in the API updates or deletes an entry - the blueprint exposes no
  write verb at all.

---

## API summary

Everything below requires a platform token. **Bold** entries require
`SUPERADMIN`.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/platform-admin/auth/login` | Sign in (rate limited, audited) |
| POST | `/api/platform-admin/auth/refresh` | New access token |
| GET | `/api/platform-admin/auth/me` | The signed-in administrator |
| GET | `/api/platform-admin/tenants` | Search / filter / sort every business |
| GET | `/api/platform-admin/tenants/<id>` | One business |
| **PATCH** | `/api/platform-admin/tenants/<id>` | Edit configuration |
| **POST** | `/api/platform-admin/tenants/<id>/suspend` | Suspend (reason required) |
| **POST** | `/api/platform-admin/tenants/<id>/reactivate` | Lift a suspension |
| GET | `/api/platform-admin/tenants/<id>/onboarding` | Setup progress |
| GET | `/api/platform-admin/tenants/<id>/stats` | Per-business statistics |
| GET | `/api/platform-admin/tenants/<id>/feature-flags` | Effective feature set |
| **PUT** | `/api/platform-admin/tenants/<id>/feature-flags` | Override / clear one flag |
| POST | `/api/platform-admin/tenants/<id>/impersonate` | Start support impersonation |
| GET | `/api/platform-admin/impersonation-sessions` | Live + historic sessions |
| POST | `/api/platform-admin/impersonation-sessions/<id>/revoke` | End one now |
| GET | `/api/platform-admin/stats/overview` | Platform totals and growth |
| GET | `/api/platform-admin/stats/growth` | Signups per day |
| GET | `/api/platform-admin/operations/emails` | Email delivery log |
| GET | `/api/platform-admin/operations/emails/summary` | Failure rate |
| **POST** | `/api/platform-admin/operations/emails/<id>/resend` | Resend a failure |
| GET | `/api/platform-admin/operations/communication-failures` | Failures, every channel |
| GET | `/api/platform-admin/operations/jobs` | Background job health |
| GET | `/api/platform-admin/audit-logs` | Who changed what, when |

Plus one unauthenticated garage-app endpoint, used only by the impersonation
handoff: `POST /api/auth/impersonation/exchange`.

---

## Tests

| File | Covers |
|---|---|
| `tests/api/test_platform_admin_auth.py` | The permission boundary, from every direction |
| `tests/api/test_platform_admin_tenants.py` | Listing, configuration, suspension, flags, onboarding |
| `tests/api/test_platform_admin_impersonation.py` | The full impersonation lifecycle |
| `tests/api/test_platform_admin_stats.py` | Statistics, and empty-denominator rates |
| `tests/api/test_platform_admin_operations.py` | Delivery log, resends, job health, audit trail |
