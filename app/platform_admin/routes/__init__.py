"""Platform Admin HTTP surface - every blueprint under ``/api/platform-admin``.

Split by concern (auth / tenants / stats / operations / audit) rather than
kept in one module, so each file's guard decorators are easy to read at a
glance: *every* view outside ``auth`` carries ``@platform_admin_required``,
and the ones that change something carry ``@superadmin_required``.
"""

from .audit import platform_audit_blp
from .auth import platform_auth_blp
from .operations import platform_operations_blp
from .stats import platform_stats_blp
from .tenants import platform_tenants_blp

PLATFORM_ADMIN_BLUEPRINTS = (
    platform_auth_blp,
    platform_tenants_blp,
    platform_stats_blp,
    platform_operations_blp,
    platform_audit_blp,
)

__all__ = [
    "PLATFORM_ADMIN_BLUEPRINTS",
    "platform_audit_blp",
    "platform_auth_blp",
    "platform_operations_blp",
    "platform_stats_blp",
    "platform_tenants_blp",
]
