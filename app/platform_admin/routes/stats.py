"""Platform-wide statistics.

Read-only, open to any platform admin. Everything is aggregated from the
tenants' own tables at request time - see ``app/platform_admin/stats.py`` for
why there is no rollup table and how rates handle empty denominators.
"""

from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint

from app.platform_admin.schemas import (
    GrowthQuerySchema,
    GrowthSchema,
    PeriodQuerySchema,
    PlatformOverviewSchema,
)
from app.platform_admin.security import platform_admin_required
from app.platform_admin.stats import platform_overview, signup_growth

platform_stats_blp = Blueprint(
    "platform_admin_stats",
    "platform_admin_stats",
    url_prefix="/api/platform-admin/stats",
    description="Platform Admin: platform-wide statistics",
)


@platform_stats_blp.route("/overview")
class PlatformStatsOverview(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_stats_blp.arguments(PeriodQuerySchema, location="query")
    @platform_stats_blp.response(200, PlatformOverviewSchema)
    def get(self, args):
        """Tenant counts, signups and aggregate usage across every business."""
        return platform_overview(days=args.get("days") or 30)


@platform_stats_blp.route("/growth")
class PlatformStatsGrowth(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_stats_blp.arguments(GrowthQuerySchema, location="query")
    @platform_stats_blp.response(200, GrowthSchema)
    def get(self, args):
        """Signups per day plus a running total, with empty days filled in."""
        return signup_growth(days=args.get("days") or 90)
