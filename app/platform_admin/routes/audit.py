"""The audit trail, read-only.

Every sensitive action in Platform Admin writes here (see
``app/platform_admin/audit.py``). Nothing in the API edits or deletes an
entry, and this blueprint exposes no write verb at all - so what an admin
reads is what was recorded at the time.
"""

from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint

from app.platform_admin.audit import list_audit_logs
from app.platform_admin.schemas import AuditLogListSchema, AuditLogQuerySchema
from app.platform_admin.security import platform_admin_required

platform_audit_blp = Blueprint(
    "platform_admin_audit",
    "platform_admin_audit",
    url_prefix="/api/platform-admin/audit-logs",
    description="Platform Admin: audit trail",
)


@platform_audit_blp.route("")
class AuditLogList(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_audit_blp.arguments(AuditLogQuerySchema, location="query")
    @platform_audit_blp.response(200, AuditLogListSchema)
    def get(self, args):
        """Who changed what, when, and for which tenant.

        Filterable by admin, tenant, action and free text - the last of which
        also matches the rendered summary, so "suspend" finds suspensions
        without knowing the action constant.
        """
        return list_audit_logs(
            admin_id=args.get("admin_id"),
            garage_id=args.get("garage_id"),
            action=args.get("action"),
            search=args.get("search"),
            page=args.get("page") or 1,
            per_page=args.get("per_page") or 50,
        )
