"""``flask create-platform-admin`` / ``flask list-platform-admins``.

The only way a Platform Admin account comes into existence. There is
deliberately no HTTP registration endpoint and no "invite" flow: creating an
account that can read every tenant on the platform requires shell access to
the deployment, which is the same bar as reading the database directly.

Mirrors the conventions of ``flask onboard-garage`` (app/garages/cli.py): a
click command, human-readable output, non-zero exit on failure, and never
echoing a password back.
"""

from __future__ import annotations

import secrets
import sys

import click
from flask.cli import with_appcontext
from werkzeug.security import generate_password_hash

from app.employees.service import email_format_error, password_policy_error
from app.extensions import db
from app.models.platform.admin import (
    PLATFORM_ADMIN_ROLES,
    ROLE_SUPERADMIN,
    PlatformAdmin,
)


def _fail(message: str) -> None:
    click.echo(f"Error: {message}", err=True)
    sys.exit(1)


@click.command("create-platform-admin")
@click.option("--email", required=True, help="Sign-in address for the administrator.")
@click.option(
    "--password",
    default=None,
    help="Password. Omit to have a strong one generated and printed once.",
)
@click.option("--first-name", default=None)
@click.option("--last-name", default=None)
@click.option(
    "--role",
    default=ROLE_SUPERADMIN,
    type=click.Choice(PLATFORM_ADMIN_ROLES, case_sensitive=False),
    help="SUPERADMIN changes tenants; SUPPORT reads and impersonates only.",
)
@with_appcontext
def create_platform_admin_command(email, password, first_name, last_name, role):
    """Create a CoMaz OS Platform Admin account.

    The password is printed exactly once, at creation, and is never stored or
    logged in plaintext - hand it over out of band and have the administrator
    change it.
    """
    email = (email or "").strip().lower()

    email_error = email_format_error(email)
    if email_error:
        _fail(f"--email: {email_error}")

    if PlatformAdmin.query.filter_by(email=email).first() is not None:
        _fail(f"A platform administrator already exists for {email!r}.")

    generated = password is None
    if generated:
        password = secrets.token_urlsafe(18)

    password_error = password_policy_error(password)
    if password_error:
        _fail(f"--password: {password_error}")

    admin = PlatformAdmin(
        email=email,
        password_hash=generate_password_hash(password),
        first_name=first_name,
        last_name=last_name,
        role=role.upper(),
    )
    db.session.add(admin)
    db.session.commit()

    click.echo(f"Created platform administrator {admin.email} ({admin.role}).")
    if generated:
        click.echo("")
        click.echo(f"  Temporary password: {password}")
        click.echo("")
        click.echo("  Shown once. Share it out of band and change it after first sign-in.")


@click.command("list-platform-admins")
@with_appcontext
def list_platform_admins_command():
    """List Platform Admin accounts (never their password hashes)."""
    admins = PlatformAdmin.query.order_by(PlatformAdmin.created_at).all()

    if not admins:
        click.echo(
            "No platform administrators yet - create one with `flask create-platform-admin`."
        )
        return

    for admin in admins:
        state = "active" if admin.is_active else "deactivated"
        last_login = admin.last_login_at.isoformat() if admin.last_login_at else "never"
        click.echo(f"{admin.email:40} {admin.role:11} {state:12} last login: {last_login}")
