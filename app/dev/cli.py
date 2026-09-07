"""``flask seed-dev`` and ``flask dev-info`` - local-development CLI helpers.

    flask --app app:create_app seed-dev            # seed / re-seed the example garage
    flask --app app:create_app seed-dev --fresh    # TRUNCATE everything first
    flask --app app:create_app dev-info            # show seeded garages + logins
    flask --app app:create_app dev-info --all      # include onboarded garages

Both refuse to run unless ``APP_ENV`` is ``development`` (see app/dev/guard.py):
their output includes seeded logins and internal ids.
"""

from __future__ import annotations

import click
from flask.cli import with_appcontext

from app.dev.guard import NotDevelopmentError, require_development
from app.dev.info import (
    collect_local_garages,
    render_dev_info,
    render_seed_result,
)
from app.dev.seed import seed_dev_garage


@click.command("seed-dev")
@click.option(
    "--fresh",
    is_flag=True,
    help="TRUNCATE every table before seeding (full clean-slate reset; mints a new random slug).",
)
@with_appcontext
def seed_dev_command(fresh):
    """Seed (or idempotently re-seed) the example garage for manual testing."""
    try:
        require_development()
        result = seed_dev_garage(fresh=fresh)
    except NotDevelopmentError as exc:
        raise click.ClickException(str(exc)) from exc

    info = next(
        (i for i in collect_local_garages() if i.garage_id == result.garage_id),
        None,
    )
    if info is None:  # pragma: no cover - the row was just committed
        raise click.ClickException("Seed committed but the garage vanished?")

    click.echo(render_seed_result(info, created=result.created, counts=result.counts))


@click.command("dev-info")
@click.option(
    "--all",
    "include_all",
    is_flag=True,
    help="Also list garages created via `flask onboard-garage` (no passwords).",
)
@with_appcontext
def dev_info_command(include_all):
    """Print local seeded garages: booking URL, slug, API URL and logins."""
    try:
        require_development()
    except NotDevelopmentError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(render_dev_info(collect_local_garages(include_all=include_all)))
