"""Platform-level models: the CoMaz OS staff side of the system.

Everything in this package is **outside** every tenant. A row here belongs to
the platform operator (CoMaz/MazTrad), not to a garage:

* :class:`~app.models.platform.admin.PlatformAdmin` - an internal staff login.
  A completely separate table from ``employees``: a garage/customer credential
  can never become a platform login, and a platform login has no ``garage_id``.
* :class:`~app.models.platform.audit_log.PlatformAuditLog` - the append-only
  record of what an admin did, to which tenant, and when.
* :class:`~app.models.platform.impersonation.ImpersonationSession` - one
  short-lived, revocable "log in as this business" grant.
* :class:`~app.models.platform.feature_flag.GarageFeatureFlag` - a per-tenant
  override of a plan's default feature set.

Tenant isolation is unchanged by any of it: these tables add a *new* actor
above the tenants, they never widen what a tenant-scoped query can see.
"""
