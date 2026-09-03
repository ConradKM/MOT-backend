# MOT Garage Backend

Backend API for a multi-tenant MOT/garage management SaaS.

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

## Project structure

```text
app/
├── __init__.py
├── config.py
├── extensions.py
├── models/
├── auth/
├── customer_auth/      # email + registration login for the customer portal
├── customer_portal/    # read-only customer account API
├── garages/
├── customers/
├── vehicles/
├── mot_records/
├── appointments/
├── public_booking/     # unauthenticated garage lookup + booking-request submit
├── booking_requests/   # staff review / approve / reject of booking requests
├── reminders/          # model only, not yet exposed via API
├── notifications/      # not yet implemented
├── tasks/
└── health/
```

This is intentionally a modular monolith. Keep domain logic inside modules and move asynchronous work into Celery tasks.
