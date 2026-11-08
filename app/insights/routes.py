"""HTTP surface of Insights: ``/api/insights``.

Garage-scoped from the JWT like every staff route. Who sees what is decided
here, server-side, by :func:`_scope` - never by the client choosing what to
render:

* owners see the whole business (optionally narrowed with ``employee_id``)
  and money;
* any other employee is scoped to their own appointments with no money, and
  naming another employee is a 403.

Basic insights work on every plan. Advanced sections need the
``insights_advanced`` feature (PRO by default, overridable per tenant from
Platform Admin): on the main payload they come back as
``{"locked": true, "feature": "insights_advanced"}``; the advanced-only
endpoints (due-back list, CSV export) answer 403.
"""

from datetime import UTC, datetime, timedelta

from flask import Response
from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.auth.utils import get_current_employee
from app.garages.timezones import local_day_for
from app.models.employee import Employee
from app.platform_admin.features import feature_enabled

from .due_back import due_back_page
from .export import export_csv, export_filename
from .periods import Period
from .schemas import (
    DueBackPageSchema,
    DueBackQueryArgsSchema,
    InsightsQueryArgsSchema,
    InsightsSchema,
)
from .service import Scope, insights

insights_blp = Blueprint(
    "insights",
    "insights",
    url_prefix="/api/insights",
    description="A business's own analytics: bookings, revenue, customers, due back.",
)

DEFAULT_RANGE_DAYS = 30
ADVANCED_FEATURE = "insights_advanced"


def _scope(requested_employee_id) -> Scope:
    employee = get_current_employee()
    if employee is None:
        abort(401, message="Employee account not found.")
    assert employee is not None  # abort() raises; this narrows the type
    garage = employee.garage
    is_owner = employee.has_role("OWNER")
    advanced = feature_enabled(garage, ADVANCED_FEATURE)

    if not is_owner:
        if requested_employee_id is not None and requested_employee_id != employee.id:
            abort(403, message="Staff can only view their own insights.")
        return Scope(garage=garage, employee_id=employee.id, money=False, advanced=advanced)

    if requested_employee_id is not None:
        exists = Employee.query.filter_by(id=requested_employee_id, garage_id=garage.id).first()
        if exists is None:
            abort(404, message="Employee not found.")
    return Scope(garage=garage, employee_id=requested_employee_id, money=True, advanced=advanced)


def _period(scope: Scope, args) -> Period:
    if args.get("start") is not None:
        return Period.for_dates(scope.garage, args["start"], args["end"])
    today = local_day_for(scope.garage, datetime.now(UTC))
    return Period.for_dates(scope.garage, today - timedelta(days=DEFAULT_RANGE_DAYS - 1), today)


def _require_advanced(scope: Scope) -> None:
    if not scope.advanced:
        abort(403, message="Advanced insights are not on this business's plan.")


@insights_blp.route("")
class InsightsResource(MethodView):
    @jwt_required()
    @insights_blp.arguments(InsightsQueryArgsSchema, location="query")
    @insights_blp.response(200, InsightsSchema)
    def get(self, args):
        """Insights for ``start``..``end`` (business-local dates, inclusive;
        default the last 30 days) plus the previous period of equal length.
        At most 730 days."""
        scope = _scope(args.get("employee_id"))
        now = datetime.now(UTC)
        return insights(
            scope, _period(scope, args), today=local_day_for(scope.garage, now), now=now
        )


@insights_blp.route("/due-back")
class DueBackResource(MethodView):
    @jwt_required()
    @insights_blp.arguments(DueBackQueryArgsSchema, location="query")
    @insights_blp.response(200, DueBackPageSchema)
    def get(self, args):
        """Customers due back, as of today, one page at a time. Advanced."""
        scope = _scope(args.get("employee_id"))
        _require_advanced(scope)
        now = datetime.now(UTC)
        return due_back_page(
            scope,
            today=local_day_for(scope.garage, now),
            page=args["page"],
            per_page=args["per_page"],
            now=now,
        )


@insights_blp.route("/export.csv")
class InsightsExport(MethodView):
    @jwt_required()
    @insights_blp.arguments(InsightsQueryArgsSchema, location="query")
    @insights_blp.doc(
        responses={
            "200": {
                "description": "The period's appointments as CSV.",
                "content": {"text/csv": {"schema": {"type": "string"}}},
            }
        }
    )
    def get(self, args):
        """The period's appointments as CSV. Advanced. Staff get only their
        own rows and no money; customer contact details are owners only."""
        scope = _scope(args.get("employee_id"))
        _require_advanced(scope)
        period = _period(scope, args)
        return Response(
            export_csv(scope, period),
            mimetype="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{export_filename(scope, period)}"',
                "Cache-Control": "no-store",
            },
        )
