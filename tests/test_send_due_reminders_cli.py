"""``flask send-due-reminders`` - the Celery-free entry point a Render Cron
Job can invoke directly. Both underlying services are already covered by
their own test suites (tests/test_mot_reminders_service.py and
tests/test_appointment_reminders_service.py); this only proves the CLI
command wires them up and exits cleanly."""

from __future__ import annotations


def test_send_due_reminders_cli_runs_both_services_and_exits_cleanly(app):
    result = app.test_cli_runner().invoke(args=["send-due-reminders"])

    assert result.exit_code == 0
    assert "MOT reminders sent:" in result.output
    assert "Appointment reminders sent:" in result.output


def test_send_due_reminders_cli_is_safe_to_run_with_nothing_due(app):
    """No vehicles, no appointments - the common case between cron ticks."""
    result = app.test_cli_runner().invoke(args=["send-due-reminders"])

    assert result.exit_code == 0
    assert "MOT reminders sent: 0" in result.output
    assert "Appointment reminders sent: 0" in result.output
