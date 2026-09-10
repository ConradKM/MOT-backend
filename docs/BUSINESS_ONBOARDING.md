# Business onboarding

> **Start in Platform Admin.** `+ Onboard business` at
> [admin.comaz.co.uk](https://admin.comaz.co.uk) does everything this document
> describes, from a browser, with no shell and no JSON — see
> [`PLATFORM_ADMIN.md` → Onboarding a business](PLATFORM_ADMIN.md#onboarding-a-business).
> It runs the same code as the CLI below, and it emails the owner a
> set-password invite instead of handing you a temporary password to pass on.
>
> This document remains the reference for the **CLI**, which is still the right
> tool for a bulk or scripted import, for preparing a spec **offline** at a
> customer's premises, and for reading what a spec actually contains.

How you (no developer needed) add a real business to CoMaz OS — including
preparing one **offline** at their premises and creating it later against
production from **Render Shell**.

The atomic core is still `onboard_garage()` (see
[`GARAGE_ONBOARDING.md`](GARAGE_ONBOARDING.md) for the identifiers, the
randomised immutable slug, and what a tenant is made of). This document is
about the wrapper — `scripts/onboard_business.py` — which is **idempotent** and
also seeds the business's **services** and **opening hours** from one JSON file.

---

## What a spec file contains

`onboarding/<name>.json` (safe to commit — **never** put a password in one):

```json
{
  "business": {
    "name": "Tints on Demand",
    "phone": "+44 20 3971 1619",
    "address": "London, E16 2ES",
    "postcode": "E16 2ES",
    "email": null,
    "website": null
  },
  "owner": { "email": "owner@example.com", "first_name": "Mubz", "last_name": null },
  "services": [
    { "name": "Window tints", "base_price": null, "default_duration_minutes": 60 }
  ],
  "opening_hours": {
    "mon": ["08:00", "17:00"], "tue": ["08:00", "17:00"],
    "wed": ["08:00", "17:00"], "thu": ["08:00", "17:00"],
    "fri": ["08:00", "17:00"], "sat": null, "sun": null
  },
  "booking_settings": { "min_lead_time_hours": 4, "max_advance_days": 45 },
  "notes": "internal platform-team note — saved to the tenant's internal notes"
}
```

`business` also accepts `layout_variant`, and the platform-owned lifecycle
fields `plan` (a key of `PLANS`), `status` (`ACTIVE` or `TRIAL` — never
`SUSPENDED`, which is its own audited operation) and `trial_ends_at`. A `TRIAL`
requires a future `trial_ends_at`; any other status must not carry one.

- `opening_hours` **omitted or `null`** → the seeded default **Mon–Fri
  09:00–17:00** is kept. A day set to `null` means closed. Only the days you
  list are changed; the rest keep the default.
- `services` may be `[]`. Each needs a `name`; `base_price` (a decimal string
  like `"54.85"`) and `default_duration_minutes` are optional. Give a service a
  duration if you want the booking calendar to offer sensibly-sized slots.
- `booking_settings` **omitted** → the seeded defaults are kept. It overrides
  the same `garage_schedule_settings` row Settings > Availability edits:
  `slot_interval_minutes`, `default_appointment_minutes`, `min_lead_time_hours`,
  `max_advance_days`, `capacity_per_slot` (`null` = fall back to the active
  employee count) and `limited_threshold_ratio`.
- A spec that contains `slug`, `id`, `garage_id` or `password` is **rejected** —
  the slug is generated, ids are the platform's, passwords are never stored.

### The minimum for a usable booking page

`business.name` + `owner.email` + at least the default opening hours (kept
automatically) + **one ACTIVE service**. Everything else the owner can fill in
later from Settings.

---

## Offline: what you can prepare with no internet

At the customer's premises, with a laptop that has the repo checked out:

1. Write / edit `onboarding/<their-name>.json` from the template above.
2. Validate it — **no database, no network**:

   ```bash
   python scripts/onboard_business.py onboarding/their-name.json --validate
   ```

   This parses the file, checks the email format, the service prices/durations,
   the opening-hours times, and rejects duplicates or forbidden keys. It prints
   `OK: '<name>' spec is valid.` or a specific error. Nothing is created.

3. Commit the spec (it has no secrets) or just keep the file. You now have
   everything needed; the slug and the temporary password are generated later,
   online.

**What needs the internet:** actually creating the business (the slug's
uniqueness check and the write happen against the production database), and
therefore also the one-time temporary password.

---

## Online: create it against production (Render Shell)

Open the backend service in the Render dashboard → **Shell**. The repo and the
production `DATABASE_URL` are already there.

```bash
# 0. make sure the code is current and migrations are applied
git pull
flask --app app:create_app db upgrade

# 1. dry run — confirms it isn't already onboarded, writes nothing
python scripts/onboard_business.py onboarding/their-name.json --dry-run

# 2. create it — prints the business id, slug, booking URL and a
#    ONE-TIME temporary password (never saved anywhere)
python scripts/onboard_business.py onboarding/their-name.json
```

Copy the whole output block. The `TEMP PASSWORD` line appears **once** — put it
straight into your password manager.

### Idempotency / duplicate prevention

Re-running the same spec (same `owner.email`) prints
`Business already onboarded - nothing was written (idempotent).` and the
existing id/slug/URL. It never makes a second business, a second owner, or a
new id. Safe to run twice if you lost connection mid-way or aren't sure it
went through.

If you need a fresh password for an already-onboarded owner (e.g. the first one
was never handed over):

```bash
python scripts/onboard_business.py onboarding/their-name.json --reset-password
```

---

## Handing over the login (credentials & security)

- The temporary password is a strong random token generated on the server and
  shown once. It is **not** in the spec, the repo, or any log.
- Give it to the owner over a secure channel (in person, or your password
  manager's share feature).
- **The owner must change it on first login** — this is a mandatory onboarding
  step. Today there is no forced-change screen, so: tell them to sign in at
  `https://app.comaz.co.uk/login` and immediately use **Forgot password** (or
  change it from their account once a real email is on file). Treat the temp
  password as burned the moment it's been used once.
- A placeholder email (e.g. `something@admin.com`) **cannot receive a reset
  link**. For those businesses the temp password is the only way in until a
  real email is set — collect the real address and update it (see below)
  before go-live, and before relying on password recovery.
- Production email delivery must be configured (`EMAIL_PROVIDER` + `EMAIL_API_KEY`
  on Render) for **Forgot password** to actually send. Until then, hand the
  password over directly and have the owner change it in-app.

---

## Verification checklist (run after step 2)

From Render Shell / any machine — replace `<slug>` and `<id>` with the printed
values:

```bash
# a) the business resolves publicly, by id and by slug
curl -s https://mot-backend.onrender.com/api/public/garages/<id> | head -c 400
curl -s https://mot-backend.onrender.com/api/public/<slug> | head -c 400
```

- [ ] both return `200` with the business name and the ACTIVE services.
- [ ] Open `https://app.comaz.co.uk/book/<id>` in a browser — the booking
      wizard loads with the business name and the service list.
- [ ] Sign in at `https://app.comaz.co.uk/login` with the owner email + temp
      password → lands on the dashboard.
- [ ] Settings → **Business Details** shows the contact details and the
      **booking link + QR code**.
- [ ] Settings → **Appointment Types** lists the services; Settings →
      **Availability** shows the opening hours you set (or the default, flagged).
- [ ] Re-running the onboarding command prints "already onboarded — nothing was
      written".

---

## Updating a business later

- **The owner** (once logged in) edits contact details at Settings → Business
  Details, services at Settings → Appointment Types, and hours at Settings →
  Availability. This is the normal path.
- **You**, before the owner has logged in, or for a business that isn't yours:

  ```bash
  flask --app app:create_app update-garage-details \
    --garage <slug-or-id> --email "real@theircompany.com" --phone "+44 …"
  ```

  Edits only that business; never touches the slug or id.
- **The slug never changes.** It is the immutable public identifier. If one
  ever genuinely had to change it would be a deliberate, announced data
  migration — and every printed QR code / shared `/book/<slug>` link would stop
  working. (The dashboard QR encodes the **id**, not the slug, so it would be
  unaffected — but any link a business printed by hand using the slug would
  break.)

---

## Deactivating / removing a test or mistaken business

There is no "delete business" API — deliberately. Options, safest first:

1. **Deactivate the owner login** so nobody can sign in, leaving the data
   intact for inspection:

   ```bash
   flask --app app:create_app shell
   >>> from app.models.employee import Employee
   >>> from app.extensions import db
   >>> for e in Employee.query.filter_by(email="owner@example.com"):
   ...     e.is_active = False
   >>> db.session.commit()
   ```

2. **Rename it** out of the way (keeps it, clearly marks it):

   ```bash
   flask --app app:create_app update-garage-details \
     --garage <slug-or-id> --name "DISABLED — created in error 2026-09-08"
   ```

3. **Hard-delete** (only for a genuine test business you are certain about —
   this cascades to its employees, customers, vehicles, appointments, etc.):

   ```bash
   flask --app app:create_app shell
   >>> from app.models.garage import Garage
   >>> from app.extensions import db
   >>> g = Garage.query.filter_by(slug="<slug>").one()
   >>> db.session.delete(g); db.session.commit()
   ```

   Take the id/slug from the onboarding output first; never guess.

---

## Fixing a mistake in a spec

Caught **before** you ran step 2: edit the JSON, re-run `--validate`, then run
it.

Caught **after**: the business exists. Fix it the "Updating a business later"
way above (contact details via CLI or the owner; services/hours via the
owner in Settings). Re-running the onboarding command will **not** apply spec
changes to an existing business — it's a no-op by design. For a test business,
hard-delete and re-onboard from the corrected spec.
