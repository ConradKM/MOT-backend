"""Thin shim around ``flask seed-dev`` (app/dev/seed.py).

Kept so ``python scripts/seed_example_garage.py`` still works from a plain
shell without the ``flask`` CLI. The seeding logic, the DEV-ONLY account
definition and the summary output all live in :mod:`app.dev`.

    python scripts/seed_example_garage.py
    python scripts/seed_example_garage.py --fresh

Prefer ``flask --app app:create_app seed-dev`` when you have the CLI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running this file directly - put the repo root on the import path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

# The `flask` CLI loads .env for us; do it by hand for the standalone script.
load_dotenv()

from app import create_app
from app.dev.guard import NotDevelopmentError
from app.dev.info import collect_local_garages, render_seed_result
from app.dev.seed import seed_dev_garage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="TRUNCATE every table before seeding (full clean-slate reset).",
    )
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        print(f"Database: {app.config['SQLALCHEMY_DATABASE_URI']}")
        try:
            result = seed_dev_garage(fresh=args.fresh)
        except NotDevelopmentError as exc:
            print(f"Refused: {exc}", file=sys.stderr)
            return 1

        info = next(i for i in collect_local_garages() if i.garage_id == result.garage_id)
        print(render_seed_result(info, created=result.created, counts=result.counts))

    return 0


if __name__ == "__main__":
    sys.exit(main())
