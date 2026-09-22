"""Direct, Celery-free entry point for sending due reminders.

Both reminder services (``app.mot_reminders.service.send_due_automatic_reminders``
and ``app.appointment_reminders.service.send_due_appointment_reminders``) are
plain, idempotent, synchronous functions - safe to call as often as you like,
with no Celery-specific behaviour (no retries, chaining, or fan-out) in
either. The existing Celery task in ``app/tasks/celery_app.py`` only wraps
the MOT one in an app context; it adds no capability neither function
already has on its own.

This command is that same wrapping, invocable directly by a scheduler that
doesn't require a broker or a worker process - e.g. a Render Cron Job
running ``flask send-due-reminders`` on a fixed interval.
"""

from __future__ import annotations

import click
from flask.cli import with_appcontext


@click.command("send-due-reminders")
@with_appcontext
def send_due_reminders_command():
    """Send every due MOT and appointment reminder, once, then exit.

    Idempotent - each underlying service only sends a reminder it hasn't
    already sent for the current due instant, so running this on a schedule
    (e.g. every 5-15 minutes) is the entire mechanism, with no separate
    "has this run yet" state to track here.
    """
    from app.appointment_reminders.service import send_due_appointment_reminders
    from app.mot_reminders.service import send_due_automatic_reminders

    mot_created = send_due_automatic_reminders()
    appointment_created = send_due_appointment_reminders()

    click.echo(f"MOT reminders sent: {len(mot_created)}")
    click.echo(f"Appointment reminders sent: {len(appointment_created)}")
