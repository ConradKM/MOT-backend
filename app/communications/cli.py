"""Platform-side CLI for garage communications configuration.

Twilio resource identifiers (subaccount SID, phone number, WhatsApp sender,
messaging service SID) are platform-controlled, never owner-editable - the
same boundary app/garages/details.py already draws around business identity
fields. This is the one supported way to set them today, ahead of any admin
UI, and mirrors ``flask update-garage-details`` in shape and intent.
"""

from __future__ import annotations

import click
from flask import current_app
from flask.cli import with_appcontext

from app.extensions import db
from app.garages.details import GarageNotFoundError, resolve_garage
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)


def _get_or_create_settings(garage) -> GarageCommunicationSettings:
    settings = garage.communication_settings
    if settings is None:
        settings = GarageCommunicationSettings(garage_id=garage.id)
        db.session.add(settings)
    return settings


@click.command("configure-garage-communications")
@click.option(
    "--garage", "identifier", required=True, help="Target garage - its slug or its UUID."
)
@click.option(
    "--enable/--disable", "enabled", default=None,
    help="Turn communications on/off for this garage.",
)
@click.option("--twilio-subaccount-sid", default=None)
@click.option(
    "--voice-number", "voice_phone_number", default=None,
    help="E.164, e.g. +441234567890.",
)
@click.option(
    "--whatsapp-sender", default=None, help='e.g. "whatsapp:+14155238886".'
)
@click.option("--messaging-service-sid", default=None)
@with_appcontext
def configure_garage_communications_command(
    identifier,
    enabled,
    twilio_subaccount_sid,
    voice_phone_number,
    whatsapp_sender,
    messaging_service_sid,
):
    """Set one garage's Twilio resource identifiers.

    Never touches an Auth Token - the platform (master account) token stays
    in TWILIO_AUTH_TOKEN; a future subaccount's own token belongs in a
    secrets manager, not this table (see docs/TWILIO_SETUP.md).
    """
    try:
        garage = resolve_garage(identifier)
    except GarageNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    settings = _get_or_create_settings(garage)

    if enabled is not None:
        settings.communications_enabled = enabled
    if twilio_subaccount_sid is not None:
        settings.twilio_subaccount_sid = twilio_subaccount_sid or None
    if voice_phone_number is not None:
        settings.voice_phone_number = voice_phone_number or None
    if whatsapp_sender is not None:
        settings.whatsapp_sender = whatsapp_sender or None
    if messaging_service_sid is not None:
        settings.messaging_service_sid = messaging_service_sid or None

    db.session.commit()

    click.echo(f"Communications settings for '{garage.name}' ({garage.slug}):")
    click.echo(f"  enabled:               {settings.communications_enabled}")
    click.echo(f"  twilio_subaccount_sid: {settings.twilio_subaccount_sid or '—'}")
    click.echo(f"  voice_phone_number:    {settings.voice_phone_number or '—'}")
    click.echo(f"  whatsapp_sender:       {settings.whatsapp_sender or '—'}")
    click.echo(f"  messaging_service_sid: {settings.messaging_service_sid or '—'}")


@click.command("twilio-webhook-urls")
@with_appcontext
def twilio_webhook_urls_command():
    """Print the exact webhook URLs to give Twilio when configuring a number
    or WhatsApp sender in the console, built from PUBLIC_API_BASE_URL."""
    base = (current_app.config.get("PUBLIC_API_BASE_URL") or "").rstrip("/")
    if not base or "localhost" in base or "127.0.0.1" in base:
        click.echo(
            "Warning: PUBLIC_API_BASE_URL is unset or local "
            f"({base or '(empty)'}) - Twilio cannot reach localhost. Set it to "
            "your deployment's public HTTPS origin (or a tunnel URL for local "
            "testing) before configuring these in the Twilio console.\n"
        )
    click.echo(f"Voice incoming:      {base}/api/webhooks/twilio/voice/incoming")
    click.echo(f"Voice status:        {base}/api/webhooks/twilio/voice/status")
    click.echo(f"WhatsApp incoming:   {base}/api/webhooks/twilio/whatsapp/incoming")
    click.echo(f"WhatsApp status:     {base}/api/webhooks/twilio/whatsapp/status")
