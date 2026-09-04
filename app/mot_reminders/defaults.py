"""Built-in MOT reminder schedule.

A garage with no ``mot_reminder_settings`` row resolves to exactly these
values, so the feature is safe for existing tenants with no backfill - the
same belt-and-braces pattern as ``app/garages/schedule/defaults.py``.
"""

from types import SimpleNamespace

# stage attr name -> (enabled, days_before). Stage 1 is furthest from expiry.
DEFAULT_MOT_REMINDER_SETTINGS = {
    "stage1_enabled": True,
    "stage1_days_before": 30,
    "stage2_enabled": True,
    "stage2_days_before": 7,
    "stage3_enabled": True,
    "stage3_days_before": 1,
}

# Bounds for a single stage interval.
MIN_DAYS_BEFORE = 1
MAX_DAYS_BEFORE = 365


def resolve_mot_reminder_settings(garage_id, session):
    """The garage's ``MOTReminderSettings`` row, or an in-code stand-in with
    the same attributes if it has none yet."""
    from app.models.mot_reminder_settings import MOTReminderSettings

    row = MOTReminderSettings.query.filter_by(garage_id=garage_id).first()
    if row is not None:
        return row
    return SimpleNamespace(
        garage_id=garage_id,
        id=None,
        created_at=None,
        updated_at=None,
        **DEFAULT_MOT_REMINDER_SETTINGS,
    )


def seed_mot_reminder_settings(garage_id, session) -> None:
    """Create the default settings row for ``garage_id`` (no-op if one exists)."""
    from app.models.mot_reminder_settings import MOTReminderSettings

    existing = (
        session.query(MOTReminderSettings.id)
        .filter_by(garage_id=garage_id)
        .first()
    )
    if existing is not None:
        return
    session.add(
        MOTReminderSettings(garage_id=garage_id, **DEFAULT_MOT_REMINDER_SETTINGS)
    )


def stages_from(settings) -> list[tuple[str, int]]:
    """``[(stage_key, days_before), ...]`` for the *enabled* stages of a
    settings row / stand-in, ordered furthest-out first."""
    from app.models.reminder import STAGE_1, STAGE_2, STAGE_3

    raw = [
        (STAGE_1, settings.stage1_enabled, settings.stage1_days_before),
        (STAGE_2, settings.stage2_enabled, settings.stage2_days_before),
        (STAGE_3, settings.stage3_enabled, settings.stage3_days_before),
    ]
    enabled = [(key, days) for key, enabled_, days in raw if enabled_]
    return sorted(enabled, key=lambda pair: pair[1], reverse=True)
