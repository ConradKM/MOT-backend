# Garage onboarding

How a new garage (tenant) is added to the platform.

---

## Overview

Onboarding is a **developer-controlled** operation. There is no public "Register
your garage" page and no anonymous signup API. A developer runs one command;
that command creates the whole tenant in a single database transaction.

Every garage has **three identifiers**, and it matters which is which:

| Concept | Field | Who sets it | Changes later? | Where it appears |
| --- | --- | --- | --- | --- |
| **Internal id** | `garages.id` (UUID) | the database, at creation | never | never in a URL, never accepted from a client; every tenant-scoped query filters on it |
| **Public slug** | `garages.slug` | onboarding, generated | **never — immutable** | unauthenticated booking URLs: `/api/public/<slug>/…` |
| **Display name** | `garages.name` | the garage owner | yes, freely (`PATCH /api/garage`) | staff dashboard, customer-facing copy, emails |

Renaming the business changes `garages.name` **only**. The slug and the internal
id never move, so existing booking links and integrations keep working.

One optional, platform-controlled field rides along:

| **Layout variant** | `garages.layout_variant` (nullable) | onboarding | by a developer only | selects a registered presentation bundle; `NULL` = the shared default |

What onboarding creates, atomically:

1. the `Garage` row (with the generated slug and optional `layout_variant`),
2. the built-in appointment **statuses** (7 rows) and the default **schedule**
   (a settings row + 7 opening-hours rows),
3. the `OWNER` and `STAFF` **roles**, and
4. the first **OWNER** `Employee` login (email + securely hashed password).

If any step fails, the whole thing rolls back — there is never a half-created
garage.

---

## Required business information checklist

Collect this before running onboarding:

**Garage**

- [ ] **Display name** — exactly as it should read on the dashboard and to
      customers (e.g. `Kingsway MOT & Service Centre`). *Required.*
- [ ] Contact email (public-facing). *Optional.*
- [ ] Contact phone. *Optional.*
- [ ] Address (single line). *Optional.*
- [ ] Layout variant — leave blank unless this client has a bespoke layout
      that is already registered (see [Layout variants](#layout-variants)).
      *Optional; developer decision, not the client's.*

**First owner account**

- [ ] Owner's email address — this is their login. Must not already be in use
      by any account on the platform. *Required.*
- [ ] Owner's first / last name. *Optional but recommended.*
- [ ] An **initial password** — at least **8 characters**
      (`PASSWORD_MIN_LENGTH` in `app/employees/service.py`). The owner changes
      it after first login, or via **Forgot password**. The plaintext is
      hashed immediately and never stored.

**Not collected — the platform assigns these:**

- ✗ Slug — generated from the name (see below). Do **not** ask the client for
  one, and do **not** put one in a spec file.
- ✗ Garage id / any UUID.
- ✗ Roles, statuses, schedule — seeded automatically; the owner tunes them
  later in Settings.

---

## The randomised slug

`app/garages/slug.py` builds the slug as:

```
slugify(<display name>)  +  "-"  +  <6 random chars>
```

e.g. `Kingsway MOT & Service Centre` → `kingsway-mot-service-centre-h4k2qp`.

- **`slugify(...)`** lowercases, replaces every run of non-alphanumerics with a
  hyphen, and trims. A name that reduces to nothing falls back to `garage`.
- **The random suffix** is 6 characters drawn with `secrets.choice` from a
  30-symbol unambiguous alphabet (`SLUG_RANDOM_SUFFIX_LENGTH` /
  `_SUFFIX_ALPHABET` — no `0/o/1/l/i`). `slugify_unique()` re-draws on the
  (astronomically unlikely) collision with an existing slug, so it is
  guaranteed unique without a `-2`, `-3` counter.

Why a random suffix:

- The public URL is **decoupled from the business name** — you can't guess a
  garage's booking URL just by knowing its name, and enumeration is harder.
- Two garages called "City Motors" get distinct slugs with no special-casing.
- Because the slug never has to encode "which City Motors is this", it can stay
  **immutable** — nothing about a rename forces a slug change.

**The slug is immutable.** It is `dump_only` on `GarageSchema`, absent from
every update schema, and no route or CLI accepts a `slug` input (the
`update-garage-details` CLI's `EDITABLE_FIELDS` deliberately excludes it).
Changing it would break every booking link already in the wild; if a slug ever
genuinely must change, that is a deliberate data migration, not a settings
toggle.

---

## Claude onboarding prompt template

Copy this, fill in the checklist values, and paste it into a Claude Code
session on the **MOT-backend** repo. It does not invent a slug and it does not
touch application code.

```
Onboard a new garage tenant on this repo. Do NOT change any application code,
migrations, or tests — this is a data operation only.

Use the developer onboarding path: `flask --app app:create_app onboard-garage`
(or `.venv/Scripts/python scripts/onboard_garage.py`), which is defined in
app/garages/cli.py and backed by app/garages/onboarding.py.

Steps:
1. Confirm the dev database is reachable and migrations are current
   (`flask --app app:create_app db upgrade`).
2. Write a spec file `scratch/onboarding/<slug-safe-name>.json` with EXACTLY
   these keys — no "slug", no "id":

   {
     "garage": {
       "name": "<DISPLAY NAME>",
       "email": "<GARAGE EMAIL or omit>",
       "phone": "<GARAGE PHONE or omit>",
       "address": "<ADDRESS or omit>",
       "postcode": "<POSTCODE or omit>",
       "website": "<WEBSITE or omit>",
       "layout_variant": null
     },
     "owner": {
       "email": "<OWNER LOGIN EMAIL>",
       "password": "<INITIAL PASSWORD, 8+ chars>",
       "first_name": "<OWNER FIRST NAME or omit>",
       "last_name": "<OWNER LAST NAME or omit>"
     }
   }

3. Dry-run first:
   flask --app app:create_app onboard-garage --file <path> --dry-run
   Expect "OK (dry run): ... Nothing was written."

4. If the dry run passes, run it for real (drop --dry-run). Report back:
   the new garage id, the generated slug, and the owner email.

5. Tell me the owner's booking URL: /book/<generated-slug>, and that the owner
   signs in at the garage login with the email + initial password.

Do NOT commit the spec file (it contains a plaintext initial password).
```

---

## Running an onboarding (exact steps)

### Prerequisites

```bash
# from the repo root, dev DB reachable via DATABASE_URL
flask --app app:create_app db upgrade
```

### Option A — structured spec file (recommended)

Create a JSON spec (YAML also works if `pyyaml` is installed). **The spec must
not contain `slug`, `id` or `garage_id`** — `parse_spec()` in
`app/garages/onboarding.py` rejects any spec that does.

`new_garage.json`:

```json
{
  "garage": {
    "name": "Kingsway MOT & Service Centre",
    "email": "hello@kingswaymot.co.uk",
    "phone": "+44 20 7946 1234",
    "address": "12 Kingsway, London",
    "postcode": "WC2B 6NH",
    "website": "https://kingswaymot.co.uk",
    "layout_variant": null
  },
  "owner": {
    "email": "owner@kingswaymot.co.uk",
    "password": "change-me-after-first-login",
    "first_name": "Jordan",
    "last_name": "Bell"
  }
}
```

A flat shape is also accepted:
`{"name": "...", "email": "...", "owner": {"email": "...", "password": "..."}}`.

```bash
# validate only — writes nothing
flask --app app:create_app onboard-garage --file new_garage.json --dry-run

# create it
flask --app app:create_app onboard-garage --file new_garage.json
```

Output:

```
Garage onboarded.
  id:            3f1c…-…-…
  name:          Kingsway MOT & Service Centre
  slug:          kingsway-mot-service-centre-h4k2qp
  layout:        default
  owner:         owner@kingswaymot.co.uk (OWNER)
```

Do not commit the spec file — it holds a plaintext initial password.

### Option B — inline flags

```bash
flask --app app:create_app onboard-garage \
  --name "Kingsway MOT & Service Centre" \
  --owner-email owner@kingswaymot.co.uk \
  --owner-password 'change-me-after-first-login' \
  --owner-first-name Jordan --owner-last-name Bell
```

### Option C — standalone script

Identical behaviour without the `flask` entrypoint (loads `.env` itself):

```bash
.venv/Scripts/python scripts/onboard_garage.py --file new_garage.json
.venv/Scripts/python scripts/onboard_garage.py --file new_garage.json --dry-run
```

### After onboarding

- Owner booking URL: **`/book/<generated-slug>`**.
- Owner signs in at the garage login with their email + initial password, then
  adds staff under **Settings → Employees** and tunes **Settings →
  Availability / Appointment types / Roles / Statuses / MOT Reminders**.

### Updating a tenant's business details later

Business details (name, email, phone, address, postcode, website) are
**read-only** to garage users — `Settings → Garage Details` just displays them,
and `PATCH /api/garage` is `403` for everyone. A developer changes one tenant's
details with:

```bash
flask --app app:create_app update-garage-details \
  --garage <slug-or-id> --phone "+44 20 7946 9999" --website "https://…"
```

It edits **only** the named garage (resolved to its immutable id) and never
touches the slug or `layout_variant`. Backed by `app/garages/details.py`.

### The HTTP endpoint

`POST /api/auth/register` (`app/auth/routes.py::Register`) calls the **same**
`onboard_garage()` service. It exists for the in-repo onboarding form and is
governed by `ONBOARDING_HTTP_ENABLED` (`app/config.py`, default `true`). Set
`ONBOARDING_HTTP_ENABLED=false` to make onboarding **CLI-only** — the endpoint
then returns `404`. There is no other public path to tenant creation.

---

## Layout variants

Every garage renders the **same shared UI**. Business logic is identical for
every tenant. A garage may optionally be pinned to a named **layout variant** —
a presentation-only bundle (theme tokens, which optional panels show, …).

- Backend registry: `app/garages/layouts.py` → `LAYOUT_VARIANTS` (a `dict`
  keyed by string), `resolve_layout(garage)`, `validate_layout_variant(...)`.
- Frontend registry: `src/lib/layoutVariant.ts` → `LAYOUT_VARIANTS`,
  `resolveLayoutVariant(garage)`. `src/components/Layout.tsx` puts the resolved
  key on `data-layout-variant` for CSS to hook.
- `Garage.layout_variant` is `dump_only` on `GarageSchema` and not editable by
  any garage user. It is set at onboarding (`--layout-variant`, or
  `garage.layout_variant` in a spec) and only a developer can change it later.
- An unknown or retired variant resolves back to `DEFAULT_LAYOUT` /
  `DEFAULT_LAYOUT_VARIANT` — it never errors at render time.

**To add a variant:**

1. Add an entry to `LAYOUT_VARIANTS` in `app/garages/layouts.py`.
2. Add the matching entry to `LAYOUT_VARIANTS` in `src/lib/layoutVariant.ts`,
   plus any CSS keyed on `[data-layout-variant="<key>"]`.
3. Onboard (or re-point) the garage with that key.

**Never** select layout with `if garage.name == …` or `if garage.id == …`.
Variation is data in the registry, resolved by key — nowhere else.

---

## What must not be changed

Treat these as invariants. Changing them breaks tenants already in production.

- **The slug of an existing garage.** It is immutable by design. No endpoint,
  schema, CLI flag or admin screen may edit `garages.slug`. If a slug truly
  must change, it is a planned data migration with redirects — not a feature.
- **Slug generation shape.** Keep it `slugify(name)` + random suffix + a
  uniqueness re-draw (`app/garages/slug.py`). Do not switch back to a
  name-only slug or a `-2`/`-3` counter, and do not shrink the random suffix
  such that guessing a garage's URL becomes practical.
- **`garages.id`** — never expose it in a URL, never accept it from a client,
  never let one request name another garage's id. Tenant scope is always
  `get_current_employee().garage_id` from the JWT.
- **The public-registration boundary.** No anonymous/public route may create a
  `Garage`. Tenant creation goes through `onboard_garage()` only (CLI, or the
  `ONBOARDING_HTTP_ENABLED` HTTP wrapper).
- **Garage business details are read-only to garage users.** `GET /api/garage`
  displays `name` / `email` / `phone` / `address` / `postcode` / `website`;
  `PATCH /api/garage` is `403` for everyone. Editing goes through
  `app/garages/details.py` (the `update-garage-details` CLI) only. Do not add a
  garage-user write path, and do not make the columns immutable — the platform
  still edits them.
- **Onboarding atomicity.** `onboard_garage()` must remain all-or-nothing —
  garage, statuses, schedule, roles, the default MOT reminder schedule and the
  first owner in one transaction, full rollback on any failure.
- **Layout selection by key, not identity.** No `if garage.name ==` /
  `if garage.id ==` branching anywhere — backend or frontend.

---

## Tenant isolation guarantees

- Every owner/employee endpoint derives the garage from the authenticated
  token (`get_current_employee().garage_id`), never from the request body.
- `owner_required` (`app/auth/decorators.py`) gates account creation and
  garage/settings edits to the `OWNER` role.
- A Garage A owner cannot read or modify Garage B — its users, its garage
  record, its `layout_variant` or its slug.
- Owner-account safety: the only active owner cannot be deactivated or stripped
  of the `OWNER` role; an employee cannot promote themselves.

---

## Tests

`tests/api/test_onboarding.py` covers slug generation and immutability, the
transactional onboarding service (including rollback on a late failure and on a
duplicate owner email), structured-spec parsing (and rejection of a spec that
names a slug), the `flask onboard-garage` CLI (`--file`, `--dry-run`, inline
flags, bad input), the `ONBOARDING_HTTP_ENABLED` switch, the
platform-vs-operational settings split, and the layout registry.

```bash
.venv/Scripts/python -m pytest tests/api/test_onboarding.py -q
```
