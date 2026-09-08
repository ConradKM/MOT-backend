# CoMaz OS Backend

Backend API for CoMaz OS™, a multi-tenant MOT/garage management SaaS platform.

## Stack

- Python 3.12
- Flask
- SQLAlchemy
- PostgreSQL
- Alembic / Flask-Migrate
- Celery + Redis
- Pytest
- Docker

## Local setup
Only have to do the first 2 lines on your first time coding

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
flask --app app:create_app db init
flask --app app:create_app db upgrade
flask --app app:create_app run --debug
```

The API will be available at `http://localhost:5000`.

## Docker

```bash
docker compose up --build
```

## Onboarding a business

Tenants are created by an operator, not by public signup.

```bash
# From a JSON spec (onboarding/<name>.json) - idempotent, seeds services +
# opening hours, prints a one-time temp password. Safe to run in Render Shell.
python scripts/onboard_business.py onboarding/their-name.json --validate   # offline check
python scripts/onboard_business.py onboarding/their-name.json --dry-run    # online, writes nothing
python scripts/onboard_business.py onboarding/their-name.json              # create

# Lower-level, business row + first owner only:
flask --app app:create_app onboard-garage --file new_garage.json          # or --dry-run
```

Both create the business (generated, immutable public slug), its default
statuses/schedule/roles and the first OWNER login in one transaction.
Full runbook — offline→online, Render Shell commands, verification checklist,
editing and deactivation — in
[`docs/BUSINESS_ONBOARDING.md`](docs/BUSINESS_ONBOARDING.md); the identifier
model and slug rationale in
[`docs/GARAGE_ONBOARDING.md`](docs/GARAGE_ONBOARDING.md).

## Testing

Tests run against a dedicated `mot_garage_test` database on the same Postgres
server (never against the dev `mot_garage` database). It's created
automatically on first run; override with `TEST_DATABASE_URL` if needed.

```bash
pytest
```

This runs the full suite (model/relationship tests, API tests, multi-tenant
isolation tests) and writes a plain-text pass/fail summary to
`tests/test-results/test.log`. No manual cleanup is required between runs.

## Continuous Integration

Every pull request into `main` (see `.github/workflows/ci.yml`) runs the same
checks below — run them locally before pushing so nothing surprises you in CI.
`pip install -r requirements-dev.txt` gets you ruff and mypy on top of the
runtime dependencies.

```bash
# Lint, format, types, sanity
python -c "from app import create_app; create_app()"  # app boots cleanly
ruff check .
ruff format --check .          # `ruff format .` to fix
mypy app tests scripts migrations

# Tests (needs the same local Postgres as `pytest` above)
pytest

# Migrations - run against a scratch database, never your dev one
createdb mot_garage_ci_check
DATABASE_URL=postgresql+psycopg://mot:mot@localhost:5432/mot_garage_ci_check \
  flask --app app:create_app db upgrade      # clean apply from empty
flask --app app:create_app db heads          # must print exactly one head
DATABASE_URL=postgresql+psycopg://mot:mot@localhost:5432/mot_garage_ci_check \
  flask --app app:create_app db migrate -m check   # must say "No changes in
                                                    # schema detected" - if it
                                                    # generates a file instead,
                                                    # your models and migration
                                                    # history have drifted;
                                                    # commit that file
dropdb mot_garage_ci_check

# OpenAPI spec generates and is valid JSON
python -c "
from app import create_app
with create_app().test_client() as c:
    assert c.get('/api/openapi.json').status_code == 200
"

# Docker image still builds
docker build -t mot-garage-backend:ci .
```

CI also runs a secret scan ([gitleaks](https://github.com/gitleaks/gitleaks))
over the full commit history and checks that the PR title references an issue
(e.g. contains `#38`) — nothing to run locally for either of those.

## Public booking

Logged-out customers book through `POST /api/public/<slug>/booking-requests`,
where `<slug>` is a `Garage.slug` (auto-generated from the garage name at
registration; look it up via `GET /api/public/<slug>`, which also returns the
garage's active appointment types). Submissions are held in `booking_requests`
as `PENDING` — they never create `customers` / `vehicles` / `appointments`
directly. Staff review them (`GET /api/booking-requests/`) and
`POST /api/booking-requests/<id>/approve` (creates + links the real records) or
`.../reject`.

The public endpoint is protected by:

- **CAPTCHA** — set `CAPTCHA_PROVIDER` (`recaptcha` | `hcaptcha` | `turnstile`)
  and `CAPTCHA_SECRET`. Defaults to `none` (no check) for local dev.
- **Rate limiting** — Flask-Limiter, `PUBLIC_BOOKING_RATELIMIT`
  (default `5 per hour;20 per day`), counters in `RATELIMIT_STORAGE_URI`
  (defaults to `REDIS_URL`, then in-memory).

## Checklist evidence storage

Photos / videos attached to checklist items are held in S3-compatible object
storage (`app/storage`); the API never streams bytes, it only issues
short-lived presigned URLs:

1. `POST /api/appointment-checklist-items/<id>/media` → a presigned **PUT**
   URL + a `PENDING` `checklist_item_media` row.
2. client uploads straight to storage, then
   `POST /api/checklist-item-media/<id>/finalize` confirms it landed.
3. `GET /api/checklist-item-media/<id>` → a presigned **GET** URL for display;
   `DELETE` removes the object + row.

`STORAGE_BACKEND=none` (default) uses stand-in URLs for local dev / tests.
`STORAGE_BACKEND=s3` targets a real bucket — production is designed around
Cloudflare R2, MinIO works locally. Objects are keyed
`garages/<garage_id>/checklist-items/<item_id>/<uuid>` and every endpoint is
scoped to the caller's garage.

## Communications (Twilio)

Foundation for phone-call and WhatsApp features (`app/communications`) - no
AI phone/chat agent yet, just tenant-aware config, webhooks, and a service
layer future work builds on. Runs with zero Twilio credentials: the app
starts normally, booking is unaffected, sends are recorded as
`SKIPPED_NOT_CONFIGURED`, and the webhook endpoints 503 instead of trusting
unverifiable requests. Full architecture, setup checklist and required
environment variables: [`docs/TWILIO_SETUP.md`](docs/TWILIO_SETUP.md).

## Project structure

```text
app/
├── __init__.py
├── config.py
├── extensions.py
├── storage/            # pluggable object storage (S3 / R2 / MinIO / none)
├── models/
├── auth/
├── customer_auth/      # email + registration login for the customer portal
├── customer_portal/    # read-only customer account API
├── garages/
├── customers/
├── vehicles/
├── mot_records/
├── appointments/
│   └── media/          # presigned upload/download for checklist evidence
├── public_booking/     # unauthenticated garage lookup + booking-request submit
├── booking_requests/   # staff review / approve / reject of booking requests
├── mot_reminders/      # MOT reminder scheduling + delivery
├── communications/     # Twilio foundation: config, webhooks, service layer
├── tasks/
└── health/
```

This is intentionally a modular monolith. Keep domain logic inside modules and move asynchronous work into Celery tasks.
