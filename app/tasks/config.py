"""Background-job (Celery) readiness gate.

Deliberately does not treat "``CELERY_BROKER_URL`` is set" as "background
jobs work". A broker URL only says a process *could* connect to a queue -
it says nothing about whether a worker is actually consuming that queue, or
whether anything ever puts ``send_due_reminders`` (app/tasks/celery_app.py)
onto it in the first place. Both of those live entirely in Render's service
configuration (a worker service, and a Render Cron Job or Celery Beat
schedule), outside this repository and unreachable from inside the running
web process - so this can only ever report "confirm manually", never a
false green.
"""

from __future__ import annotations

from flask import current_app


def background_jobs_prerequisites() -> list[dict]:
    cfg = current_app.config
    checks = [
        (
            "celery_broker_configured",
            "Celery broker URL",
            bool(cfg.get("CELERY_BROKER_URL")),
            (
                "Set CELERY_BROKER_URL (and CELERY_RESULT_BACKEND) to a real Redis "
                "instance - both default to localhost, which does not exist on a "
                "Render web service."
            ),
        ),
        (
            "worker_service_confirmed",
            "Worker service running send_due_reminders",
            False,
            (
                "Not detectable from inside the web process. Confirm in Render that "
                "a separate worker service is running "
                "`celery -A app.tasks.celery_app.celery worker`, pointed at the same "
                "broker."
            ),
        ),
        (
            "schedule_confirmed",
            "Scheduled trigger for send_due_reminders",
            False,
            (
                "There is no Celery Beat schedule or periodic trigger in this "
                "repository. Confirm a Render Cron Job (or an external scheduler) "
                "calls send_due_reminders on a recurring basis - without one, "
                "reminders are never sent automatically."
            ),
        ),
    ]
    return [
        {"key": key, "label": label, "satisfied": ok, "how_to_fix": None if ok else fix}
        for key, label, ok, fix in checks
    ]
