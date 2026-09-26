"""Reminder-scheduling readiness information.

Production schedules the Celery-free ``flask send-due-reminders`` command
with a Render Cron Job.  Celery remains a supported local/legacy wrapper, but
is not a production prerequisite.  Render service configuration is outside
the web process, so runtime health is inferred from reminder records in
``app.platform_admin.operations`` rather than pretending an environment
variable proves that a scheduler is running.
"""

def background_jobs_prerequisites() -> list[dict]:
    # This endpoint is rendered in the communications setup UI.  It is a
    # deployment-design statement, not a liveness probe: the command is
    # idempotent and Render invokes it outside this process.  Its observable
    # outcome belongs in Operations > Job health.
    return [
        {
            "key": "render_cron_reminders",
            "label": "Reminder scheduler",
            "satisfied": True,
            "how_to_fix": None,
        }
    ]
