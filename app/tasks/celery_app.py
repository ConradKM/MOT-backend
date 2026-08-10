import os

from celery import Celery


celery = Celery(
    "mot_garage",
    broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/2"),
)


@celery.task
def send_due_reminders():
    # TODO: query pending reminders, dispatch notifications, and record outcomes.
    return {"status": "not_implemented"}
