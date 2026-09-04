"""``flask onboard-garage`` - the developer-controlled way to create a tenant.

    # From a structured spec (slug is NOT part of the spec - it's generated):
    flask --app app:create_app onboard-garage --file new_garage.json

    # Or inline:
    flask --app app:create_app onboard-garage \
        --name "Kingsway MOT & Service Centre" \
        --owner-email owner@kingswaymot.co.uk --owner-password 's3cret-pass'

    # Validate without writing anything:
    flask --app app:create_app onboard-garage --file new_garage.json --dry-run

See ``docs/GARAGE_ONBOARDING.md`` for the spec format and the full runbook.
The equivalent standalone script is ``scripts/onboard_garage.py``.
"""

from __future__ import annotations

import click
from flask.cli import with_appcontext

from app.garages.onboarding import (
    GarageSpec,
    OnboardingError,
    OwnerSpec,
    load_spec_file,
    onboard_garage,
    validate,
)


def _specs_from_options(
    file, name, owner_email, owner_password, owner_first_name,
    owner_last_name, layout_variant,
) -> tuple[GarageSpec, OwnerSpec]:
    if file:
        return load_spec_file(file)

    missing = [
        flag
        for flag, value in (
            ("--name", name),
            ("--owner-email", owner_email),
            ("--owner-password", owner_password),
        )
        if not value
    ]
    if missing:
        raise OnboardingError(
            f"Provide --file, or all of: {', '.join(missing)}."
        )

    return (
        GarageSpec(name=name, layout_variant=layout_variant),
        OwnerSpec(
            email=owner_email,
            password=owner_password,
            first_name=owner_first_name,
            last_name=owner_last_name,
        ),
    )


@click.command("onboard-garage")
@click.option(
    "--file",
    "file",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a JSON (or YAML) onboarding spec. The spec must not contain a slug.",
)
@click.option("--name", help="Garage display name (if not using --file).")
@click.option("--owner-email", help="First owner's login email.")
@click.option("--owner-password", help="First owner's initial password.")
@click.option("--owner-first-name", default=None)
@click.option("--owner-last-name", default=None)
@click.option(
    "--layout-variant",
    default=None,
    help="Optional registered layout variant key (default: the shared layout).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Validate the spec and exit without creating anything.",
)
@with_appcontext
def onboard_garage_command(
    file, name, owner_email, owner_password, owner_first_name,
    owner_last_name, layout_variant, dry_run,
):
    """Create a garage tenant and its first OWNER account."""
    try:
        garage_spec, owner_spec = _specs_from_options(
            file, name, owner_email, owner_password,
            owner_first_name, owner_last_name, layout_variant,
        )

        if dry_run:
            validate(garage_spec, owner_spec)
            click.echo(
                f"OK (dry run): '{garage_spec.name}' with owner "
                f"{owner_spec.email} would be onboarded. Nothing was written."
            )
            return

        result = onboard_garage(garage=garage_spec, owner=owner_spec)
    except OnboardingError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo("Garage onboarded.")
    click.echo(f"  id:            {result.garage.id}")
    click.echo(f"  name:          {result.garage.name}")
    click.echo(f"  slug:          {result.garage.slug}")
    click.echo(f"  layout:        {result.garage.layout_variant or 'default'}")
    click.echo(f"  owner:         {result.owner.email} (OWNER)")
    click.echo(
        "\nThe owner can now sign in at the garage login with the password "
        "you set."
    )
