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
flask --app app:create_app db upgrade
flask --app app:create_app run --debug
```

The API will be available at `http://localhost:5000`.

## Docker

```bash
docker compose up --build
```

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
├── appointments/
├── reminders/
├── notifications/
├── tasks/
└── health/
```

This is intentionally a modular monolith. Keep domain logic inside modules and move asynchronous work into Celery tasks.
