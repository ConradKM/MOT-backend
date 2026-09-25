# CLAUDE.md — MOT-backend

Read this before making changes. It exists to keep every prompt/session working
against the same facts instead of re-deriving them from scratch each time.

## What this is

Flask + SQLAlchemy + PostgreSQL REST API for a multi-tenant MOT/garage SaaS
("CoMaz OS"). Feature code lives one directory per domain under `app/`
(`app/payments`, `app/booking_flow`, `app/customer_portal`, `app/platform_admin`,
etc.) — each is effectively its own blueprint/service/model slice. Full stack
and architecture notes: [README.md](README.md); feature-specific setup docs live
under [docs/](docs/) (payments, Twilio, onboarding, platform admin, ...) — check
there before assuming how a subsystem is configured.

Sibling repo `MOT-frontend` is the only consumer of this API; a contract change
here (new/changed route, request/response shape) needs a matching frontend
change — see that repo's own `CLAUDE.md`.

## Before opening a PR — mirror `.github/workflows/ci.yml` locally

CI runs six independent jobs on every PR into `main`: `pr-title`, `secrets`,
`lint`, `migrations`, `test`, `openapi`, `docker`. Run the local equivalents
before pushing:

```bash
# Lint, format, types, sanity — pip install -r requirements-dev.txt first
python -c "from app import create_app; create_app()"   # app boots cleanly
python -c "import wsgi; assert wsgi.app is not None"    # gunicorn entrypoint imports
ruff check .
ruff format --check .          # `ruff format .` to fix
mypy app tests scripts migrations

# Tests — needs local Postgres; runs against mot_garage_test, never dev's mot_garage
pytest

# Migrations — always against a scratch DB, never your dev one
createdb mot_garage_ci_check
DATABASE_URL=postgresql+psycopg://mot:mot@localhost:5432/mot_garage_ci_check \
  flask --app app:create_app db upgrade        # clean apply from empty
flask --app app:create_app db heads            # must print exactly ONE head
DATABASE_URL=postgresql+psycopg://mot:mot@localhost:5432/mot_garage_ci_check \
  flask --app app:create_app db migrate -m check   # must say "No changes in
                                                    # schema detected" — if it
                                                    # generates a file, your
                                                    # models and migration
                                                    # history drifted; commit
                                                    # that generated migration
dropdb mot_garage_ci_check

# OpenAPI spec must generate and be valid
python -c "
from app import create_app
with create_app().test_client() as c:
    assert c.get('/api/openapi.json').status_code == 200
"

# Docker image must still build
docker build -t mot-garage-backend:ci .
```

- **PR title must reference an issue number** (e.g. contains `#38`) — checked on
  open, edit, reopen, and every push.
- **Secret scan** (gitleaks) runs over full history — never commit real
  credentials/keys, including in test fixtures or onboarding JSON examples.
- **Exactly one migration head at all times.** If your branch and another
  merged branch both added a migration against the same parent revision, you'll
  need to merge/rebase the heads before `db heads` will pass.
- Model changes always need a matching Alembic migration in `migrations/` —
  the drift check above fails the build otherwise.

## Conventions worth knowing before writing code

- **Multi-tenant by construction**: nearly everything is scoped to a `garage_id`
  (aka business). New endpoints/queries must filter by the caller's garage —
  check an existing sibling module in `app/` for the pattern before adding one.
- **Platform admin is a separate trust boundary**: `app/platform_admin` accounts
  have their own table and JWT claim; a garage/customer credential must never
  reach an endpoint there.
- **Public booking** (`app/public_booking` / `app/booking_requests`) never
  writes `customers`/`vehicles`/`appointments` directly — it only creates
  `PENDING` `booking_requests` that staff approve/reject. Don't shortcut that.
- **Object storage** (photos/videos) is presigned-URL only — the API never
  streams bytes. `STORAGE_BACKEND=none` (default) stubs this for local/tests.
- **Communications (Twilio)** and **AI voice** run with zero credentials by
  default (sends recorded as `SKIPPED_NOT_CONFIGURED`, webhooks 503) — don't
  assume Twilio/OpenAI config is present; check `docs/TWILIO_SETUP.md` /
  `docs/OPENAI_VOICE_SETUP.md`.
- **Payments**: provider abstraction documented in `docs/PAYMENTS_PROVIDERS.md`
  and `docs/PAYMENTS_SETUP.md`, Stripe Connect specifics in
  `docs/STRIPE_CONNECT_SETUP.md` — read before touching `app/payments`.
- Dev-only seed/onboarding commands (`flask seed-dev`, `onboard_business.py`)
  refuse to run outside `APP_ENV=development` — don't rely on them meaning
  anything in CI or production.
