"""``flask loyalty-reconcile`` - issue any loyalty reward a completion earned
but didn't get.

Issuing normally happens in the same request that completes the appointment
(app/loyalty/issuing.py::issue_reward_safely), guarded so a loyalty failure
can never block the completion. That guard logs and moves on, so this catches
up: it finds customers whose recent completion landed on a milestone with no
code, issues the code and sends the email. Only completions made in the app
after the programme was switched on are considered, so it never mass-issues
history. Safe to run any number of times, by hand or from an external cron:

    flask --app app:create_app loyalty-reconcile [--days 30]
"""

from __future__ import annotations

import click
from flask.cli import with_appcontext


@click.command("loyalty-reconcile")
@click.option(
    "--days",
    default=30,
    show_default=True,
    type=click.IntRange(1, 365),
    help="How far back to look for completions that missed their reward.",
)
@with_appcontext
def loyalty_reconcile_command(days: int):
    """Issue missed loyalty reward codes (and their emails)."""
    from app.loyalty.issuing import reconcile

    issued = reconcile(days=days)
    if not issued:
        click.echo("No missed loyalty rewards.")
        return
    for code in issued:
        # Ids only - never the code itself, which is a bearer discount.
        click.echo(
            f"  {code.garage_id}: issued for customer {code.customer_id} "
            f"(visit {code.issued_for_visit_count})"
        )
    click.echo(f"Done - issued {len(issued)} code(s).")
