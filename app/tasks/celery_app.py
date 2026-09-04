import os

from celery import Celery

celery = Celery(
    "mot_garage",
    broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/2"),
)


@celery.task
def send_due_reminders():
    """Send every MOT reminder stage that is due and not already sent.

    Idempotent - designed to run frequently (e.g. hourly). The actual logic
    lives in ``app.mot_reminders.service`` so it can be unit-tested and called
    directly; this task just wraps it in an app context.
    """
    from app import create_app
    from app.mot_reminders.service import send_due_automatic_reminders

    app = create_app()
    with app.app_context():
        created = send_due_automatic_reminders()
        return {"sent": len(created)}
