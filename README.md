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

## Project structure

```text
app/
├── __init__.py
├── config.py
├── extensions.py
├── models/
├── auth/
├── garages/
├── customers/
├── vehicles/
├── mot_records/
├── appointments/       # model only, not yet exposed via API
├── reminders/          # model only, not yet exposed via API
├── notifications/      # not yet implemented
├── tasks/
└── health/
```

This is intentionally a modular monolith. Keep domain logic inside modules and move asynchronous work into Celery tasks.
