"""CoMaz OS Platform Admin - the internal, cross-tenant operator console.

A separate product surface from the garage app and the customer portal, for
CoMaz/MazTrad staff only. It is deployed on its own origin
(``admin.comaz.co.uk``, the ``comaz-admin`` frontend), authenticates against
its own account table, and lives entirely under ``/api/platform-admin/*``.

The boundary, in one place:

* **Separate identity.** A platform login is a
  :class:`~app.models.platform.admin.PlatformAdmin` row, never an ``Employee``
  or ``Customer``. Its access token carries ``account_type="platform_admin"``;
  every route here rejects any token without that claim *and* re-resolves the
  claim against ``platform_admins`` on each request
  (``app/platform_admin/security.py``). A garage owner's credentials cannot
  reach a single endpoint in this namespace.
* **Separate privileges.** ``SUPERADMIN`` may change tenant configuration,
  suspend, and flip feature flags; ``SUPPORT`` is read-only apart from
  impersonation. Both are enforced by decorator, not by convention.
* **Tenant isolation is untouched.** Nothing here relaxes a tenant-scoped
  query. Platform Admin is a *new actor above* the tenants: it reads across
  them through its own explicitly cross-tenant queries
  (``app/platform_admin/stats.py``), and every tenant-facing route in the rest
  of the app still derives its garage from the caller's own JWT.
* **Everything sensitive is audited.** Tenant edits, suspension, feature-flag
  changes, email resends, admin sign-ins, and both ends of an impersonation
  session write a
  :class:`~app.models.platform.audit_log.PlatformAuditLog` row.
* **Business logic is reused, not re-implemented.** Tenant detail edits go
  through ``app.garages.details.update_garage_details``; email resends go
  through ``app.email.send_email``; statistics aggregate the same tables the
  garage app writes.

There is no HTTP path that creates a platform admin: use
``flask create-platform-admin`` (``app/platform_admin/cli.py``).
"""
