"""``flask sweep-walkin-queue`` - run the queue's no-show timeout for every
business, once, then exit.

Not required for correctness: every staff and public queue read already runs
the same sweep (see app/queueing/service.py). This exists for the case where
nobody is looking - a called customer who never came forward should still be
skipped and the next person texted. Idempotent; schedule it every minute or
two alongside ``flask send-due-reminders``.
"""

from __future__ import annotations

import click
from flask.cli import with_appcontext


@click.command("sweep-walkin-queue")
@with_appcontext
def sweep_walkin_queue_command():
    from app.queueing.service import sweep_all_garages

    called = sweep_all_garages()
    click.echo(f"Walk-ins auto-called after a no-show timeout: {called}")
