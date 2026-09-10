"""Plans and per-tenant feature flags.

Two layers, resolved in this order:

1. the tenant's **plan** (``Garage.plan``) supplies a default for every known
   flag - :data:`PLANS` is the whole matrix, in one place; then
2. an explicit **override** row
   (:class:`~app.models.platform.feature_flag.GarageFeatureFlag`) wins for that
   one flag, for that one tenant.

A tenant with no override rows is exactly its plan. That is why an override is
stored only when it differs from nothing at all - the absence of a row means
"follow the plan", so changing a plan's defaults later moves every tenant that
never had a deliberate exception.

**Scope, stated plainly:** this module is the source of truth for what a
tenant's feature set *is*, and Platform Admin manages it end to end (read,
override, clear, audited). No existing product behaviour is gated on it yet -
adopting :func:`feature_enabled` at each feature's entry point is a separate,
deliberate change per feature, so that turning a flag off can be reviewed
against what that feature already does for live tenants.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.extensions import db
from app.models.platform.feature_flag import GarageFeatureFlag


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    description: str


#: Every flag Platform Admin knows about. Adding one here is all it takes -
#: no migration, since overrides are keyed by string.
FEATURES: tuple[Feature, ...] = (
    Feature(
        key="public_booking",
        label="Public booking",
        description="The unauthenticated booking wizard at /book/<slug> and its API.",
    ),
    Feature(
        key="communications",
        label="Communications",
        description="Twilio-backed voice and WhatsApp for this tenant.",
    ),
    Feature(
        key="whatsapp_automation",
        label="WhatsApp automation",
        description="The conversation engine answering WhatsApp messages automatically.",
    ),
    Feature(
        key="voice_assistant",
        label="Voice assistant",
        description="ConversationRelay answering inbound calls.",
    ),
    Feature(
        key="mot_reminders",
        label="MOT reminders",
        description="Automatic multi-stage MOT expiry reminders.",
    ),
    Feature(
        key="checklists",
        label="Digital checklists",
        description="Appointment checklist templates and photo evidence.",
    ),
    Feature(
        key="customer_portal",
        label="Customer portal",
        description="Customer accounts, login and self-service appointment history.",
    ),
)

FEATURES_BY_KEY = {feature.key: feature for feature in FEATURES}

#: Plan -> the flags it turns on. A key missing from a plan's set is off for
#: that plan. `Garage.plan` defaults to STANDARD (see app/models/garage.py).
PLANS: dict[str, frozenset[str]] = {
    "TRIAL": frozenset({"public_booking", "mot_reminders", "checklists", "customer_portal"}),
    "STANDARD": frozenset(
        {"public_booking", "mot_reminders", "checklists", "customer_portal", "communications"}
    ),
    "PRO": frozenset(FEATURES_BY_KEY),
}

DEFAULT_PLAN = "STANDARD"
PLAN_KEYS = tuple(PLANS)


class UnknownFeatureError(ValueError):
    """A flag key that isn't in :data:`FEATURES`."""


class UnknownPlanError(ValueError):
    """A plan key that isn't in :data:`PLANS`."""


def validate_plan(plan: str) -> str:
    if plan not in PLANS:
        raise UnknownPlanError(f"Unknown plan {plan!r}. Expected one of {list(PLANS)}.")
    return plan


def plan_default(plan: str | None, key: str) -> bool:
    """Whether ``key`` is on for ``plan``, ignoring overrides."""
    if key not in FEATURES_BY_KEY:
        raise UnknownFeatureError(f"Unknown feature flag {key!r}.")
    return key in PLANS.get(plan or DEFAULT_PLAN, PLANS[DEFAULT_PLAN])


def _overrides_for(garage) -> dict[str, bool]:
    return {
        row.key: row.enabled for row in GarageFeatureFlag.query.filter_by(garage_id=garage.id).all()
    }


def feature_enabled(garage, key: str) -> bool:
    """The effective value of one flag for one tenant: its override if it has
    one, otherwise its plan's default."""
    if key not in FEATURES_BY_KEY:
        raise UnknownFeatureError(f"Unknown feature flag {key!r}.")

    override = GarageFeatureFlag.query.filter_by(garage_id=garage.id, key=key).first()
    if override is not None:
        return bool(override.enabled)
    return plan_default(garage.plan, key)


def feature_summary(garage) -> list[dict]:
    """Every known flag for one tenant, with where its value came from.

    One query for the overrides, then pure computation - no per-flag lookups.
    """
    overrides = _overrides_for(garage)
    summary = []
    for feature in FEATURES:
        default = plan_default(garage.plan, feature.key)
        override = overrides.get(feature.key)
        summary.append(
            {
                "key": feature.key,
                "label": feature.label,
                "description": feature.description,
                "plan_default": default,
                "override": override,
                "enabled": default if override is None else override,
                "source": "plan" if override is None else "override",
            }
        )
    return summary


def set_feature_override(garage, key: str, enabled: bool | None, session=None) -> None:
    """Override one flag for one tenant, or clear the override.

    ``enabled=None`` deletes the override row, returning the tenant to its
    plan's default for that flag. Leaves the transaction open for the caller,
    so the change and its audit row commit together.
    """
    if key not in FEATURES_BY_KEY:
        raise UnknownFeatureError(f"Unknown feature flag {key!r}.")

    session = session or db.session
    row = GarageFeatureFlag.query.filter_by(garage_id=garage.id, key=key).first()

    if enabled is None:
        if row is not None:
            session.delete(row)
        return

    if row is None:
        session.add(GarageFeatureFlag(garage_id=garage.id, key=key, enabled=enabled))
    else:
        row.enabled = enabled
