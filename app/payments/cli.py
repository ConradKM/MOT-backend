"""``flask backfill-payment-method-domains`` - one-off catch-up for Apple Pay
domain registration on garages that finished Stripe Connect onboarding
before app/payments/connect.py::refresh_connect_status started registering
the public booking domain automatically.

Safe to run any number of times: registration is per (domain, connected
account) and idempotent on Stripe's side, and this skips any garage whose
account can't yet take charges.

    flask --app app:create_app backfill-payment-method-domains
"""

from __future__ import annotations

import click
from flask.cli import with_appcontext

from app.models.payments.garage_payment_settings import GaragePaymentSettings
from app.payments.connect import ConnectError, refresh_connect_status


@click.command("backfill-payment-method-domains")
@with_appcontext
def backfill_payment_method_domains_command():
    """Register the Apple Pay payment-method domain for every garage with an
    existing Stripe Connect account, without waiting for its staff to next
    open the Payments settings page (the other path that already triggers
    this - see app/payments/connect.py::refresh_connect_status)."""
    settings_rows = GaragePaymentSettings.query.filter(
        GaragePaymentSettings.stripe_account_id.isnot(None)
    ).all()

    if not settings_rows:
        click.echo("No garages with a Stripe Connect account yet.")
        return

    for settings in settings_rows:
        try:
            refresh_connect_status(settings.garage)
        except ConnectError as exc:
            click.echo(f"  {settings.garage_id}: skipped ({exc})")
            continue
        click.echo(
            f"  {settings.garage_id}: refreshed (charges_enabled={settings.stripe_charges_enabled})"
        )

    click.echo(f"Done - checked {len(settings_rows)} garage(s).")
